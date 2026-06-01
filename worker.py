#!/usr/bin/env python3
"""
RunPod GPU Watcher v7 — пассивный мониторинг.

Дима включает/выключает под сам.
Бот только смотрит и говорит:
  - Под включился + GPU работает → уведомление
  - Под выключился → уведомление
  - Каждые 5 мин отчёт (если нужен)
  - Каждые 10 мин напоминание пока под включён (идёт оплата)
  - Баланс < LOW_BALANCE → предупреждение
"""
import json
import os
import sys
import ssl
import time
import http.client
import urllib.request

TG_TOKEN         = os.environ.get("TG_TOKEN", "")
TG_CHAT          = os.environ.get("TG_CHAT", "6356247638")
RUNPOD_KEY       = os.environ.get("RUNPOD_KEY", "")
POD_ID           = os.environ.get("POD_ID", "06187ayaswoyq2")
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL", "300"))    # проверка раз в 5 мин
REPORT_INTERVAL  = int(os.environ.get("REPORT_INTERVAL", "1200")) # отчёт каждые 20 мин
ON_WARN_INTERVAL = int(os.environ.get("ON_WARN_INTERVAL", "600")) # напоминание об оплате каждые 10 мин
COST_PER_HR      = float(os.environ.get("COST_PER_HR", "0.945"))
LOW_BALANCE      = float(os.environ.get("LOW_BALANCE", "3.0"))

START_TS = time.time()


def now_pdt() -> str:
    u = time.gmtime()
    return f"{(u.tm_hour - 7) % 24:02d}:{u.tm_min:02d} PDT"


def fmt_dur(sec) -> str:
    sec = int(sec)
    return f"{sec // 3600}ч {(sec % 3600) // 60}м"


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
            print(f"[TG {'OK' if res.get('ok') else 'FAIL'}] {text[:60]}", flush=True)
    except Exception as e:
        print(f"[TG ERROR] {e}", flush=True)


def runpod_gql(query: str) -> dict:
    if not RUNPOD_KEY:
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
            headers={"Content-Type": "application/json", "User-Agent": "runpod-watcher/7.0"},
        )
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        if resp.status != 200:
            print(f"[RunPod] HTTP {resp.status}: {raw[:200]}", flush=True)
            return {}
        parsed = json.loads(raw)
        return parsed.get("data") or {}
    except Exception as e:
        print(f"[RunPod] Error: {e}", flush=True)
        return {}
    finally:
        conn.close()


def get_status() -> dict | None:
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
        "name":    pod.get("name", POD_ID),
        "status":  pod.get("desiredStatus", "UNKNOWN"),
        "running": rt is not None,
        "gpus":    len(gpus),
        "uptime":  rt.get("uptimeInSeconds", 0) if rt else 0,
        "balance": balance,
    }


def main():
    print(f"[{now_pdt()}] Watcher v7 (пассивный) | pod={POD_ID} | poll={POLL_INTERVAL}s", flush=True)
    send_tg(
        f"🤖 <b>RunPod Watcher v7 запущен</b>  {now_pdt()}\n"
        f"Под: <code>{POD_ID}</code>\n"
        f"Режим: пассивный — Дима включает сам.\n"
        f"Проверка раз в {POLL_INTERVAL // 60} мин · отчёт каждые {REPORT_INTERVAL // 60} мин"
    )

    prev_status   = None
    running_alerted = False
    last_warn_ts  = 0.0
    low_bal_alert = False
    last_report_ts = time.time()
    checks = 0

    while True:
        loop_start = time.time()
        try:
            info = get_status()
            if info is not None:
                checks += 1
                cur = info["status"]
                bal = info.get("balance")

                # ── под ВКЛЮЧЁН и GPU есть ──
                if cur == "RUNNING" and info["gpus"] > 0:
                    if not running_alerted:
                        bal_l = f"\n💰 Баланс: ${bal:.2f}" if bal is not None else ""
                        send_tg(
                            f"🟢 <b>Под ВКЛЮЧЁН, GPU работает</b>  {now_pdt()}\n"
                            f"GPU: {info['gpus']} шт · аптайм {fmt_dur(info['uptime'])}{bal_l}\n"
                            f"⚠️ Идёт оплата ${COST_PER_HR}/ч\n\n"
                            f"Jupyter: https://{POD_ID}-8888.proxy.runpod.net\n"
                            f"ComfyUI: https://{POD_ID}-8188.proxy.runpod.net"
                        )
                        running_alerted = True
                        last_warn_ts = time.time()

                    # напоминание об оплате каждые 10 мин
                    if time.time() - last_warn_ts >= ON_WARN_INTERVAL:
                        up_h = info["uptime"] / 3600
                        spent = up_h * COST_PER_HR
                        bal_line = f"\nБаланс: ${bal:.2f}" if bal is not None else ""
                        send_tg(
                            f"⚠️ <b>Под включён — идёт оплата</b>  {now_pdt()}\n"
                            f"Работает: {fmt_dur(info['uptime'])} · ~${spent:.2f} потрачено{bal_line}"
                        )
                        last_warn_ts = time.time()

                    # низкий баланс
                    if bal is not None and bal < LOW_BALANCE and not low_bal_alert:
                        send_tg(f"🔴 <b>Низкий баланс RunPod!</b>  ${bal:.2f} (~{bal/COST_PER_HR:.1f}ч)")
                        low_bal_alert = True
                    if bal is not None and bal >= LOW_BALANCE:
                        low_bal_alert = False

                # ── под ВЫКЛЮЧЕН ──
                elif cur == "EXITED":
                    if running_alerted:
                        send_tg(f"🔴 <b>Под выключен</b>  {now_pdt()}\nПод: <code>{POD_ID}</code>")
                    running_alerted = False

                # ── промежуточный статус (стартует/останавливается) ──
                else:
                    print(f"[{now_pdt()}] {cur} · gpus={info['gpus']}", flush=True)

                print(f"[{now_pdt()}] {cur} | GPU={info['gpus']} | checks={checks}", flush=True)
                prev_status = cur

            else:
                print(f"[{now_pdt()}] API нет ответа", flush=True)

            # отчёт каждые 20 мин
            if time.time() - last_report_ts >= REPORT_INTERVAL:
                st = prev_status or "?"
                emoji = "🟢" if st == "RUNNING" else "🔴"
                bal = (info or {}).get("balance") if info else None
                bal_line = f"\nБаланс: ${bal:.2f}" if bal is not None else ""
                send_tg(
                    f"📊 <b>Отчёт {REPORT_INTERVAL // 60} мин</b>  {now_pdt()}\n"
                    f"Под: {emoji} {st}\n"
                    f"Проверок: {checks}\n"
                    f"Аптайм воркера: {fmt_dur(time.time() - START_TS)}"
                    f"{bal_line}"
                )
                last_report_ts = time.time()
                checks = 0

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
