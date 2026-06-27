#!/usr/bin/env python3
"""
RunPod GPU Monitor v6 — GitHub Actions, каждые 5 мин (cron) при выключенном ПК.
State: state.json (коммитится в репо между запусками cron).

Поведение (точно как просил Дмитрий):
  • Под EXITED → PROBE: пробует podResume. Получилось (GPU был свободен) →
    шлёт в TG «🟢 GPU СВОБОДЕН — заходи» и СРАЗУ гасит под (podStop).
    Дмитрий заходит и включает САМ для работы. Бот под не держит.
  • Под RUNNING + GPU (Дмитрий сам включил и работает) → ЗАЩИТА: НЕ трогает,
    НЕ гасит. Раз в час пишет «✅ под активен, ты работаешь».
  • Каждый час — отчёт: сколько проверок, сколько раз GPU был свободен.

Для RTX PRO 6000 RunPod не отдаёт сток через API (stockStatus=null), поэтому
единственный честный способ узнать наличие GPU — пробный podResume.
Зависимостей нет (stdlib). Один запуск = одна проверка (cron повторяет каждые 5 мин).
"""
import json, os, sys, time
import http.client, ssl
from pathlib import Path

TG_TOKEN    = os.environ.get("TG_TOKEN", "")
TG_CHAT     = os.environ.get("TG_CHAT",  "6356247638")
RUNPOD_KEY  = os.environ.get("RUNPOD_KEY", "")
POD_ID      = os.environ.get("POD_ID",  "06187ayaswoyq2")
REPORT_SEC  = 3600                       # отчёт раз в час
FREE_COOLDOWN_SEC = 1800                  # анти-спам: повтор «GPU свободен» не чаще 30 мин
POST_OFF_GRACE  = 1800                    # после ТВОЕГО выключения пода — не дёргать probe 30 мин
GPU_CONFIRM_SEC = 25                      # анти-фейк: подтвердить gpus>0 после resume (под в очереди ≠ GPU)
OUR_PROBE_MAX_UP = 120                    # под с uptime>120с = НЕ наш probe, а рабочий под Дмитрия (анти-гонка)
STATE_FILE  = Path("state.json")


def pdt():
    u = time.gmtime()
    return f"{(u.tm_hour - 7) % 24:02d}:{u.tm_min:02d} PDT"


def fmt_up(sec):
    return f"{sec // 3600}h {(sec % 3600) // 60}m"


def send_tg(text: str, retries: int = 3):
    import urllib.request
    if not TG_TOKEN:
        print(f"[NO TG] {text[:80]}")
        return
    data = json.dumps({"chat_id": TG_CHAT, "text": text,
                       "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                if json.loads(r.read()).get("ok"):
                    print(f"[TG OK] {text[:60]}")
                    return
        except Exception as e:
            print(f"[TG try{attempt}] {e}")
        if attempt < retries:
            time.sleep(2 * (attempt + 1))
    print(f"[TG FAIL] {text[:60]}")


def runpod_gql(query: str):
    """Возвращает {'data':..,'errors':..} ИЛИ None при сетевом/HTTP сбое
    (None отличаем от 'GPU нет', чтобы не врать в отчётах)."""
    if not RUNPOD_KEY:
        print("[ERROR] RUNPOD_KEY не задан")
        return None
    body = json.dumps({"query": query}).encode("utf-8")
    conn = http.client.HTTPSConnection("api.runpod.io", timeout=20,
                                       context=ssl.create_default_context())
    try:
        conn.request("POST", f"/graphql?api_key={RUNPOD_KEY}", body=body,
                     headers={"Content-Type": "application/json",
                              "User-Agent": "runpod-watcher/6.0"})
        resp = conn.getresponse()
        raw  = resp.read().decode("utf-8")
        if resp.status != 200:
            print(f"[RunPod HTTP {resp.status}] {raw[:200]}")
            return None
        return json.loads(raw)
    except Exception as e:
        print(f"[RunPod NETERR] {e}")
        return None
    finally:
        conn.close()


def get_pod():
    """('RUNNING'|'EXITED'|..., gpus:int, uptime:int) или (None,0,0) при сбое сети."""
    d = runpod_gql('{ pod(input: {podId: "%s"}) { desiredStatus runtime { '
                   'uptimeInSeconds gpus { id } } } }' % POD_ID)
    if d is None or d.get("data") is None:
        return None, 0, 0
    pod = d["data"].get("pod") or {}
    rt  = pod.get("runtime")
    gpus = len(rt.get("gpus") or []) if rt else 0
    up   = rt.get("uptimeInSeconds", 0) if rt else 0
    return pod.get("desiredStatus"), gpus, up


def resume_pod():
    """True если под реально запустился (GPU был свободен). False — GPU нет / ошибка."""
    d = runpod_gql('mutation { podResume(input: {podId: "%s"}) { id desiredStatus } }' % POD_ID)
    if d is None:
        return False
    errs = d.get("errors") or []
    if errs:
        msg = (errs[0].get("message") or "").lower()
        if "not enough free gpu" in msg or "no gpu" in msg:
            return False                     # GPU точно нет — под не стартовал
        print(f"[RESUME GQL err] {errs[0].get('message')}")
        return False
    res = (d.get("data") or {}).get("podResume") or {}
    return res.get("desiredStatus") == "RUNNING"


def stop_pod():
    """Гасит под — Дмитрий заходит и включает сам."""
    runpod_gql('mutation { podStop(input: {podId: "%s"}) { desiredStatus } }' % POD_ID)
    print("[STOP] podStop отправлен")


def confirm_gpu():
    """Анти-фейк: podResume даёт RUNNING даже когда под просто в очереди БЕЗ GPU.
    Возвращает (gpu_есть:bool, uptime:int). uptime нужен чтобы отличить НАШ свежий
    probe (uptime секунды) от РАБОЧЕГО пода Дмитрия (uptime минуты/часы) — анти-гонка."""
    deadline = time.time() + GPU_CONFIRM_SEC
    while time.time() < deadline:
        time.sleep(5)
        _, g, u = get_pod()
        if g > 0:
            return True, u
    return False, 0


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"report_ts": 0, "n": 0, "found": 0, "last_status": None}


def save_state(s):
    # атомарная запись: пишем во временный файл и переименовываем (без полу-записанного state.json)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    tmp.replace(STATE_FILE)


def main():
    print(f"[{pdt()}] RunPod Monitor v6 (probe+защита) | pod={POD_ID}")
    st  = load_state()
    now = int(time.time())
    st["n"] = st.get("n", 0) + 1
    if not st.get("report_ts"):           # первый запуск/сброс state → отсчёт часа от сейчас (без мгновенного отчёта)
        st["report_ts"] = now

    status, gpus, uptime = get_pod()
    if status is None:
        print("[SKIP] API/сеть недоступна — пропуск (не считаем за 'нет GPU')")
        save_state(st)
        return

    print(f"Pod: {status} | GPU: {gpus} | uptime: {uptime}s")

    # ── ЗАЩИТА: Дмитрий сам включил под и работает → НЕ трогаем, НЕ гасим ─────
    if status == "RUNNING" and gpus > 0:
        print("[GUARD] под RUNNING с GPU — ты работаешь, НЕ трогаю")
        st["free_notify_ts"] = 0             # ты зашёл — сбрасываем cooldown на будущее
        if now - st.get("report_ts", 0) >= REPORT_SEC:
            send_tg(f"✅ <b>Под RUNNING с GPU</b>  {pdt()}\n"
                    f"Ты работаешь — вотчер не вмешивается. Uptime {fmt_up(uptime)}.")
            st["report_ts"] = now; st["n"] = 0; st["found"] = 0
        st["last_status"] = status
        save_state(st)
        return

    # ── ДЕТЕКТ: ты сам выключил под (RUNNING → EXITED) → grace, не дёргать ────
    if st.get("last_status") == "RUNNING" and status == "EXITED":
        st["user_off_ts"] = now
        print("[GRACE] ты только что выключил под — probe на паузе 30 мин (не включаю сразу)")

    # ── PROBE: под выключен → пробуем поймать свободный GPU ──────────────────
    got = False
    if status == "EXITED":
        post_off  = (now - st.get("user_off_ts", 0)) < POST_OFF_GRACE
        cooldown  = (now - st.get("free_notify_ts", 0)) < FREE_COOLDOWN_SEC
        if post_off:
            left = int((POST_OFF_GRACE - (now - st["user_off_ts"])) // 60)
            print(f"[GRACE] ты выключил под ~недавно — probe пропущен ещё ~{left} мин (не включаю сразу)")
        elif cooldown:
            left = int((FREE_COOLDOWN_SEC - (now - st["free_notify_ts"])) // 60)
            print(f"[COOLDOWN] уже сообщил про свободный GPU — тишина ещё ~{left} мин")
        elif resume_pod():
            # АНТИ-ФЕЙК: resume=RUNNING ещё не значит GPU выделен (под мог встать в очередь).
            has_gpu, up_now = confirm_gpu()
            if has_gpu and up_now > OUR_PROBE_MAX_UP:
                # 🛡️ АНТИ-ГОНКА (нашёл Opus): под с большим uptime = это ТВОЙ рабочий под,
                # ты зашёл между get_pod и resume. НЕ гасим и НЕ уведомляем — это не наш probe.
                print(f"[RACE-GUARD] под занят тобой (uptime {up_now}s > {OUR_PROBE_MAX_UP}) — НЕ гашу, НЕ уведомляю")
            elif has_gpu:
                # НАШ свежий probe (uptime мал) поймал реально свободный GPU
                got = True
                st["found"] = st.get("found", 0) + 1
                st["free_notify_ts"] = now   # анти-спам
                send_tg(f"🟢🟢 <b>GPU СВОБОДЕН — ЗАХОДИ!</b> 🟢🟢  {pdt()}\n"
                        f"Под <code>{POD_ID}</code> · RTX PRO 6000.\n"
                        f"Включай вручную для работы, пока не перехватили.\n"
                        f"<i>(бот под не держит — гашу обратно; повтор не раньше 30 мин)</i>")
                stop_pod()                   # наш probe — гасим, ты включишь сам
            else:
                # gpus=0 — под в очереди без GPU (наш фейковый probe) → гасим обратно
                print("[ANTI-FAKE] resume=RUNNING, но gpus=0 (под в очереди без GPU) — НЕ уведомляю")
                stop_pod()
    print(f"[{pdt()}] проверка #{st['n']} | GPU={'ДА' if got else 'нет'}")

    # ── Отчёт раз в час ─────────────────────────────────────────────────────
    if now - st.get("report_ts", 0) >= REPORT_SEC:
        send_tg(f"📊 <b>Отчёт за час</b>  {pdt()}\n"
                f"{st['n']} проверок · GPU свободен <b>{st.get('found', 0)}</b> раз\n"
                f"Под сейчас: {status}")
        st["report_ts"] = now; st["n"] = 0; st["found"] = 0

    st["last_status"] = status
    save_state(st)
    print("[DONE] state saved")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        send_tg(f"⚠️ <b>GPU Watcher (GitHub) ошибка</b>\n<code>{type(e).__name__}: {e}</code>")
        print(f"[FATAL] {e}")
        sys.exit(0)
