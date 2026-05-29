#!/usr/bin/env python3
"""
RunPod GPU Watcher — постоянный воркер (Railway, 24/7).

Что делает:
  - каждые POLL_INTERVAL сек дёргает РЕАЛЬНЫЙ RunPod API (никаких фейков);
  - один GraphQL-запрос отдаёт статус пода + наличие GPU нужного типа;
  - как только GPU появляется в наличии для выключенного пода — мгновенно
    шлёт в Telegram «GPU доступен, заходи и жми Resume»;
  - повторно пингует пока GPU держится (раз в ALERT_COOLDOWN сек), чтобы не спамить;
  - каждые REPORT_INTERVAL (20 мин) шлёт отчёт: сколько проверок, сколько раз
    GPU был доступен, время последней доступности, аптайм воркера.

Работает независимо от компа — крутится на Railway.
Зависимостей нет (только стандартная библиотека Python).
"""
import json
import os
import sys
import ssl
import time
import http.client
import urllib.request

# ── Конфиг (значения берутся из переменных окружения Railway) ────────────────
TG_TOKEN        = os.environ.get("TG_TOKEN", "")
TG_CHAT         = os.environ.get("TG_CHAT", "6356247638")
RUNPOD_KEY      = os.environ.get("RUNPOD_KEY", "")
POD_ID          = os.environ.get("POD_ID", "06187ayaswoyq2")
GPU_TYPE        = os.environ.get("GPU_TYPE", "NVIDIA RTX PRO 6000 Blackwell Workstation Edition")
GPU_COUNT       = int(os.environ.get("GPU_COUNT", "1"))

POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL", "10"))      # как часто проверять, сек
REPORT_INTERVAL = int(os.environ.get("REPORT_INTERVAL", "1200"))  # отчёт каждые 20 мин
ALERT_COOLDOWN  = int(os.environ.get("ALERT_COOLDOWN", "300"))    # повтор алерта пока GPU есть, сек

START_TS = time.time()


# ── Время по PDT (Лос-Анджелес, где обычно дата-центры) ──────────────────────
def now_pdt() -> str:
    u = time.gmtime()
    return f"{(u.tm_hour - 7) % 24:02d}:{u.tm_min:02d} PDT"


def fmt_dur(sec: int) -> str:
    sec = int(sec)
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h}ч {m}м"


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
        "api.runpod.io", timeout=20, context=ssl.create_default_context()
    )
    try:
        conn.request(
            "POST",
            f"/graphql?api_key={RUNPOD_KEY}",
            body=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "runpod-watcher/3.0"},
        )
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        if resp.status != 200:
            print(f"[RunPod] HTTP {resp.status}: {raw[:200]}", flush=True)
            return {}
        return json.loads(raw).get("data") or {}
    except Exception as e:
        print(f"[RunPod] Error: {e}", flush=True)
        return {}
    finally:
        conn.close()


def poll() -> dict | None:
    """Один запрос: статус пода + наличие GPU нужного типа."""
    q = (
        'query { '
        f'pod(input: {{podId: "{POD_ID}"}}) {{ name desiredStatus '
        'runtime { uptimeInSeconds gpus { gpuUtilPercent } } } '
        f'gpuTypes(input: {{id: "{GPU_TYPE}"}}) {{ displayName '
        f'lowestPrice(input: {{gpuCount: {GPU_COUNT}}}) {{ stockStatus uninterruptablePrice }} }} '
        '}'
    )
    data = runpod_gql(q)
    if not data:
        return None

    pod = data.get("pod")
    if pod is None:
        print("[SKIP] под не найден в ответе API", flush=True)
        return None

    rt = pod.get("runtime")
    gpu_types = data.get("gpuTypes") or []
    price = (gpu_types[0].get("lowestPrice") or {}) if gpu_types else {}
    stock = price.get("stockStatus")
    rent_price = price.get("uninterruptablePrice")

    # GPU "появился", если RunPod показывает наличие на secure cloud:
    #   stockStatus != null  ИЛИ  есть цена аренды.
    available = bool(stock) or rent_price is not None

    return {
        "status": pod.get("desiredStatus", "UNKNOWN"),
        "running": rt is not None,
        "uptime": rt.get("uptimeInSeconds", 0) if rt else 0,
        "gpu_available": available,
        "stock": stock,
        "price": rent_price,
    }


# ── Главный цикл ─────────────────────────────────────────────────────────────
def main():
    print(f"[{now_pdt()}] Watcher v3 запущен | pod={POD_ID} | "
          f"poll={POLL_INTERVAL}s | report={REPORT_INTERVAL}s", flush=True)
    send_tg(
        f"🤖 <b>RunPod Watcher запущен</b>  {now_pdt()}\n"
        f"Под: <code>{POD_ID}</code>\n"
        f"GPU: RTX PRO 6000 Blackwell\n"
        f"Проверка каждые {POLL_INTERVAL} сек. Отчёт каждые {REPORT_INTERVAL // 60} мин.\n"
        f"Жду появления GPU…"
    )

    prev_status = None
    last_alert_ts = 0.0          # когда последний раз слали алерт о наличии GPU
    last_report_ts = time.time()

    # счётчики за окно отчёта
    checks = 0
    gpu_hits = 0                 # сколько раз за окно GPU был доступен
    last_available_str = "—"

    while True:
        loop_start = time.time()
        try:
            info = poll()
            if info is not None:
                checks += 1
                cur = info["status"]

                # ── под сам сменил статус (ты вручную запустил/остановил) ──
                if prev_status is not None and cur != prev_status:
                    if prev_status == "EXITED" and cur == "RUNNING":
                        send_tg(f"🟢 <b>Под запущен</b>  {now_pdt()}\n"
                                f"Аптайм: {fmt_dur(info['uptime'])}")
                    elif prev_status == "RUNNING" and cur == "EXITED":
                        # под выключился — снова в поиск GPU
                        send_tg(f"🔴 <b>Под остановлен</b>  {now_pdt()}\n"
                                f"Снова ищу GPU…")
                        last_alert_ts = 0
                prev_status = cur

                # ── главное: ищем GPU только пока под выключен ──
                if cur == "EXITED" and info["gpu_available"]:
                    gpu_hits += 1
                    last_available_str = now_pdt()
                    if time.time() - last_alert_ts >= ALERT_COOLDOWN:
                        stock = info["stock"] or "есть"
                        price = info["price"]
                        price_str = f" · ${price}/ч" if price is not None else ""
                        send_tg(
                            f"🚀 <b>GPU ДОСТУПЕН!</b>  {now_pdt()}\n"
                            f"RTX PRO 6000 Blackwell · наличие: {stock}{price_str}\n\n"
                            f"⚡ Заходи в RunPod и жми <b>Resume</b> на поде "
                            f"<code>{POD_ID}</code> — успей пока не разобрали!"
                        )
                        last_alert_ts = time.time()

                status_emoji = "🟢" if cur == "RUNNING" else "🔴"
                print(f"[{now_pdt()}] {status_emoji} {cur} | "
                      f"gpu_avail={info['gpu_available']} stock={info['stock']} | "
                      f"checks={checks} hits={gpu_hits}", flush=True)
            else:
                print(f"[{now_pdt()}] API не ответил — повтор через {POLL_INTERVAL}s", flush=True)

            # ── отчёт каждые 20 минут ──
            if time.time() - last_report_ts >= REPORT_INTERVAL:
                st = prev_status or "?"
                emoji = "🟢" if st == "RUNNING" else "🔴"
                send_tg(
                    f"📊 <b>Отчёт за {REPORT_INTERVAL // 60} мин</b>  {now_pdt()}\n"
                    f"Под: {emoji} {st}\n"
                    f"Проверок выполнено: <b>{checks}</b>\n"
                    f"GPU был доступен: <b>{gpu_hits}</b> раз\n"
                    f"Последний раз доступен: {last_available_str}\n"
                    f"Аптайм воркера: {fmt_dur(time.time() - START_TS)}"
                )
                last_report_ts = time.time()
                checks = 0
                gpu_hits = 0
                last_available_str = "—"

        except Exception as e:
            print(f"[LOOP ERROR] {type(e).__name__}: {e}", flush=True)

        # ── ровный интервал ──
        elapsed = time.time() - loop_start
        time.sleep(max(1, POLL_INTERVAL - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("stopped", flush=True)
        sys.exit(0)
