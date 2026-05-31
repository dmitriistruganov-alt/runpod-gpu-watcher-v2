#!/usr/bin/env python3
"""
RunPod GPU Watcher v4 — честная проверка, без фейков.

ПОЧЕМУ v4:
  Для RTX PRO 6000 (и A100) RunPod НЕ отдаёт сток через API — поля
  stockStatus / price / rentedCount всегда null. Поэтому проверка "по стоку"
  для этого GPU физически не работает — бот всегда думал, что GPU нет.
  Проверено вживую: 4090 даёт stockStatus="Low", а PRO 6000 — null.

ЧТО ДЕЛАЕТ v4 (только реальные данные):
  1. Каждую минуту берёт РЕАЛЬНЫЙ статус пода (desiredStatus + runtime.gpus).
  2. Если под RUNNING и у него есть GPU — пишет "✅ GPU активен" (это решает
     случай "я сам включил, а бот пишет нет" — теперь бот это видит).
  3. Если под EXITED и AUTO_RESUME=1 — реально пытается запустить под
     (podResume). Получилось → GPU был свободен → "🚀 GPU СВОБОДЕН, под запущен".
     Не получилось → GPU нет, тихо ждёт. Это единственный 100% честный способ
     узнать наличие этого GPU.
  4. Каждые 20 мин — отчёт: проверок, поймано GPU, время, аптайм.

Работает 24/7 на Railway, не зависит от компа. Зависимостей нет (stdlib).
"""
import json
import os
import sys
import ssl
import time
import http.client
import urllib.request

# ── Конфиг (значения из переменных окружения Railway) ────────────────────────
TG_TOKEN        = os.environ.get("TG_TOKEN", "")
TG_CHAT         = os.environ.get("TG_CHAT", "6356247638")
RUNPOD_KEY      = os.environ.get("RUNPOD_KEY", "")
POD_ID          = os.environ.get("POD_ID", "06187ayaswoyq2")
GPU_COUNT       = int(os.environ.get("GPU_COUNT", "1"))

# AUTO_RESUME=1 — бот сам пытается поймать GPU (запускает под). Это и есть
# единственная честная проверка наличия для RTX PRO 6000. По умолчанию ВКЛ.
AUTO_RESUME     = os.environ.get("AUTO_RESUME", "1") == "1"

POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL", "60"))      # проверка раз в минуту
REPORT_INTERVAL = int(os.environ.get("REPORT_INTERVAL", "1200"))  # отчёт каждые 20 мин
ON_WARN_INTERVAL = int(os.environ.get("ON_WARN_INTERVAL", "600")) # напоминать "под ВКЛЮЧЕН" каждые 10 мин
COST_PER_HR     = float(os.environ.get("COST_PER_HR", "0.945"))   # $/час когда под работает
LOW_BALANCE     = float(os.environ.get("LOW_BALANCE", "3.0"))     # предупреждать когда баланс ниже, $

# ── Окно авто-запуска (по PDT) ───────────────────────────────────────────────
# В часы [ACTIVE_START, ACTIVE_END) бот САМ ловит GPU (auto-resume).
# Вне окна (ночью) — только следит и шлёт отчёты, под НЕ запускает.
ACTIVE_START    = int(os.environ.get("ACTIVE_START", "7"))    # 07:00 PDT
ACTIVE_END      = int(os.environ.get("ACTIVE_END", "23"))     # 23:00 PDT


def in_active_window() -> bool:
    """Сейчас рабочее окно ловли GPU? (по PDT)."""
    hour = (time.gmtime().tm_hour - 7) % 24   # PDT = UTC-7
    return ACTIVE_START <= hour < ACTIVE_END

START_TS = time.time()


def now_pdt() -> str:
    u = time.gmtime()
    return f"{(u.tm_hour - 7) % 24:02d}:{u.tm_min:02d} PDT"


def fmt_dur(sec) -> str:
    sec = int(sec)
    return f"{sec // 3600}ч {(sec % 3600) // 60}м"


# ── Telegram ─────────────────────────────────────────────────────────────────
def send_tg(text: str):
    if not TG_TOKEN:
        print(f"[NO TG] {text[:80]}", flush=True)
        return
    try:
        data = json.dumps({
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read())
            print(f"[TG {'OK' if res.get('ok') else 'FAIL'}] {text[:50]}", flush=True)
    except Exception as e:
        print(f"[TG ERROR] {e}", flush=True)


# ── RunPod GraphQL (http.client — urllib иногда даёт 403 на этот эндпоинт) ────
def runpod_gql(query: str) -> dict:
    if not RUNPOD_KEY:
        print("[ERROR] RUNPOD_KEY не задан", flush=True)
        return {}
    body = json.dumps({"query": query}).encode("utf-8")
    conn = http.client.HTTPSConnection(
        "api.runpod.io", timeout=25, context=ssl.create_default_context()
    )
    try:
        conn.request(
            "POST",
            f"/graphql?api_key={RUNPOD_KEY}",
            body=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "runpod-watcher/4.0"},
        )
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        if resp.status != 200:
            print(f"[RunPod] HTTP {resp.status}: {raw[:200]}", flush=True)
            return {}
        parsed = json.loads(raw)
        if parsed.get("errors"):
            print(f"[RunPod] GQL errors: {parsed['errors'][:1]}", flush=True)
        return parsed.get("data") or {}
    except Exception as e:
        print(f"[RunPod] Error: {e}", flush=True)
        return {}
    finally:
        conn.close()


def get_status() -> dict | None:
    """РЕАЛЬНЫЙ статус пода + баланс аккаунта (один запрос)."""
    q = (f'query {{ pod(input: {{podId: "{POD_ID}"}}) {{ '
         f'name desiredStatus runtime {{ uptimeInSeconds gpus {{ id }} }} }} '
         f'myself {{ clientBalance }} }}')
    data = runpod_gql(q)
    pod = data.get("pod")
    if pod is None:
        return None
    rt = pod.get("runtime")
    gpus = (rt.get("gpus") or []) if rt else []
    balance = (data.get("myself") or {}).get("clientBalance")
    return {
        "status": pod.get("desiredStatus", "UNKNOWN"),
        "running": rt is not None,
        "gpus": len(gpus),
        "uptime": rt.get("uptimeInSeconds", 0) if rt else 0,
        "balance": balance,
    }


def try_resume() -> bool:
    """Реальная попытка запустить под — БЕЗ gpuCount (как веб-кнопка Resume).

    КЛЮЧЕВОЙ ФИКС (проверено вживую 2026-05-30):
      podResume С gpuCount:1  → "not enough free GPUs" (просит НОВЫЙ свободный GPU) ❌
      podResume БЕЗ gpuCount  → {"desiredStatus":"RUNNING"} ✅ (возвращает поду
                                его УЖЕ зарезервированный GPU — ровно как веб-кнопка)
    Под привязан к машине через network volume, поэтому gpuCount заставлял RunPod
    искать чужой свободный GPU и отказывать. Без него — берётся свой.
    """
    q = (f'mutation {{ podResume(input: {{podId: "{POD_ID}"}}) '
         f'{{ id desiredStatus }} }}')
    data = runpod_gql(q)
    res = data.get("podResume")
    # Успех = RunPod вернул объект пода (не null).
    return res is not None


# ── Главный цикл ─────────────────────────────────────────────────────────────
def main():
    print(f"[{now_pdt()}] Watcher v4 | pod={POD_ID} | poll={POLL_INTERVAL}s | "
          f"AUTO_RESUME={AUTO_RESUME}", flush=True)
    win_now = "🟢 АКТИВНО" if in_active_window() else "🌙 НОЧЬ (жду утра)"
    mode = (f"сам ловит GPU {ACTIVE_START}:00–{ACTIVE_END}:00 PDT, ночью только следит"
            if AUTO_RESUME else "только следит за статусом")
    send_tg(
        f"🤖 <b>RunPod Watcher v5 запущен</b>  {now_pdt()}\n"
        f"Под: <code>{POD_ID}</code> · RTX PRO 6000\n"
        f"Режим: {mode}\n"
        f"Окно ловли GPU: <b>{ACTIVE_START}:00–{ACTIVE_END}:00 PDT</b> · сейчас {win_now}\n"
        f"Проверка раз в {POLL_INTERVAL} сек · отчёт каждые {REPORT_INTERVAL // 60} мин"
    )

    prev_status = None
    running_alerted = False        # чтобы не спамить "GPU активен" каждую минуту
    last_on_warn_ts = 0.0          # когда последний раз напоминали "под ВКЛЮЧЕН"
    low_bal_alerted = False        # чтобы предупредить о низком балансе один раз
    last_report_ts = time.time()

    checks = 0
    gpu_caught = 0                 # сколько раз за окно реально поймали/увидели GPU
    last_caught_str = "—"

    while True:
        loop_start = time.time()
        try:
            info = get_status()
            if info is not None:
                checks += 1
                cur = info["status"]

                bal = info.get("balance")

                # ── под РАБОТАЕТ и GPU реально есть ──
                if cur == "RUNNING" and info["gpus"] > 0:
                    gpu_caught += 1
                    last_caught_str = now_pdt()
                    if not running_alerted:
                        bal_l = f"\n💰 Баланс: ${bal:.2f}" if bal is not None else ""
                        send_tg(
                            f"🟢🟢🟢 <b>ПОД ВКЛЮЧЁН!</b> 🟢🟢🟢  {now_pdt()}\n\n"
                            f"<b>GPU РАБОТАЕТ</b> · {info['gpus']} шт · "
                            f"аптайм {fmt_dur(info['uptime'])}{bal_l}\n"
                            f"⚠️ ИДЁТ ОПЛАТА ${COST_PER_HR}/ч\n\n"
                            f"Jupyter: https://06187ayaswoyq2-8888.proxy.runpod.net\n"
                            f"ComfyUI: https://06187ayaswoyq2-8188.proxy.runpod.net"
                        )
                        running_alerted = True
                        last_on_warn_ts = time.time()

                    # ── ПРЕДУПРЕЖДЕНИЕ: под включён, идёт оплата (каждые 10 мин) ──
                    if time.time() - last_on_warn_ts >= ON_WARN_INTERVAL:
                        up_h = info["uptime"] / 3600
                        spent = up_h * COST_PER_HR
                        bal_line = f"\nБаланс: ${bal:.2f}" if bal is not None else ""
                        hours_left = (f" (~{bal / COST_PER_HR:.1f}ч осталось)"
                                      if bal is not None else "")
                        send_tg(
                            f"⚠️ <b>ПОД ВКЛЮЧЁН — идёт оплата!</b>  {now_pdt()}\n"
                            f"Работает: {fmt_dur(info['uptime'])} · "
                            f"потрачено ~${spent:.2f}"
                            f"{bal_line}{hours_left}\n"
                            f"Не забудь выключить, когда закончишь."
                        )
                        last_on_warn_ts = time.time()

                    # ── предупреждение о низком балансе (один раз) ──
                    if bal is not None and bal < LOW_BALANCE and not low_bal_alerted:
                        send_tg(
                            f"🔴 <b>НИЗКИЙ БАЛАНС RunPod!</b>  {now_pdt()}\n"
                            f"Осталось ${bal:.2f} (~{bal / COST_PER_HR:.1f}ч). "
                            f"Пополни, иначе под скоро отключится."
                        )
                        low_bal_alerted = True
                    if bal is not None and bal >= LOW_BALANCE:
                        low_bal_alerted = False  # сброс после пополнения

                # ── под выключен → честно пробуем поймать GPU ──
                elif cur == "EXITED":
                    running_alerted = False
                    active = in_active_window()
                    if AUTO_RESUME and active:
                        # ── рабочее окно 07:00–23:00 PDT: ловим GPU ──
                        if try_resume():
                            gpu_caught += 1
                            last_caught_str = now_pdt()
                            send_tg(
                                f"🚀 <b>GPU СВОБОДЕН — под ПОЙМАН и запущен!</b>  {now_pdt()}\n"
                                f"Заходи скорее: "
                                f"https://06187ayaswoyq2-8888.proxy.runpod.net\n"
                                f"ComfyUI: https://06187ayaswoyq2-8188.proxy.runpod.net"
                            )
                            print(f"[{now_pdt()}] 🚀 RESUMED — GPU пойман!", flush=True)
                        else:
                            print(f"[{now_pdt()}] 🔴 EXITED · GPU занят на хосте, "
                                  f"ждём · checks={checks}", flush=True)
                    elif AUTO_RESUME and not active:
                        # ── ночь 23:00–07:00 PDT: только следим, GPU не ловим ──
                        print(f"[{now_pdt()}] 🌙 НОЧЬ ({ACTIVE_START}:00–{ACTIVE_END}:00 PDT "
                              f"окно выкл) · только слежу · checks={checks}", flush=True)
                    else:
                        print(f"[{now_pdt()}] 🔴 EXITED · auto-resume выключен", flush=True)

                # ── под включается/выключается, GPU ещё нет ──
                else:
                    running_alerted = False
                    print(f"[{now_pdt()}] {cur} · gpus={info['gpus']} · "
                          f"checks={checks}", flush=True)

                prev_status = cur
            else:
                print(f"[{now_pdt()}] API не ответил — повтор через {POLL_INTERVAL}s",
                      flush=True)

            # ── отчёт каждые 20 минут ──
            if time.time() - last_report_ts >= REPORT_INTERVAL:
                st = prev_status or "?"
                emoji = "🟢" if st == "RUNNING" else "🔴"
                bal = (info or {}).get("balance") if info else None
                bal_line = f"\nБаланс RunPod: ${bal:.2f}" if bal is not None else ""
                send_tg(
                    f"📊 <b>Отчёт за {REPORT_INTERVAL // 60} мин</b>  {now_pdt()}\n"
                    f"Под сейчас: {emoji} {st}\n"
                    f"Проверок выполнено: <b>{checks}</b>\n"
                    f"GPU был доступен: <b>{gpu_caught}</b> раз\n"
                    f"Последний раз GPU: {last_caught_str}\n"
                    f"Аптайм воркера: {fmt_dur(time.time() - START_TS)}"
                    f"{bal_line}"
                )
                last_report_ts = time.time()
                checks = 0
                gpu_caught = 0
                last_caught_str = "—"

        except Exception as e:
            print(f"[LOOP ERROR] {type(e).__name__}: {e}", flush=True)

        elapsed = time.time() - loop_start
        time.sleep(max(1, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("stopped", flush=True)
        sys.exit(0)
