#!/usr/bin/env python3
"""
RunPod GPU Monitor — запускается GitHub Actions каждые 5 минут.
State сохраняется в state.json (коммитится в репо).
"""
import json, os, sys, time, urllib.request
from pathlib import Path

TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT    = os.environ.get("TG_CHAT", "6356247638")
RUNPOD_KEY = os.environ.get("RUNPOD_KEY", "")
POD_ID     = os.environ.get("POD_ID", "06187ayaswoyq2")
STATE_FILE = Path("state.json")

def pdt():
    u = time.gmtime()
    return f"{(u.tm_hour-7)%24:02d}:{u.tm_min:02d} PDT"

def send_tg(text: str):
    if not TG_TOKEN:
        print(f"[NO TG] {text}")
        return
    try:
        data = json.dumps({"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read())
            if res.get("ok"):
                print(f"[TG OK] {text[:60]}")
            else:
                print(f"[TG FAIL] {res}")
    except Exception as e:
        print(f"[TG ERROR] {e}")

def get_pod():
    if not RUNPOD_KEY:
        print("[ERROR] RUNPOD_KEY не задан")
        return None
    query = '{"query":"{pod(input:{podId:\\"%s\\"}){desiredStatus runtime{uptimeInSeconds gpus{gpuUtilPercent memoryUtilPercent}}}}"}'
    data = (query % POD_ID).encode()
    req = urllib.request.Request(
        f"https://api.runpod.io/graphql?api_key={RUNPOD_KEY}",
        data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        resp = json.loads(r.read())
    pod = resp.get("data", {}).get("pod", {})
    rt = pod.get("runtime")
    gpus = rt.get("gpus", []) if rt else []
    uptime = rt.get("uptimeInSeconds", 0) if rt else 0
    return {
        "status": pod.get("desiredStatus", "UNKNOWN"),
        "running": rt is not None,
        "gpus": len(gpus),
        "gpu_util": gpus[0].get("gpuUtilPercent", 0) if gpus else 0,
        "gpu_mem": gpus[0].get("memoryUtilPercent", 0) if gpus else 0,
        "uptime": uptime
    }

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except:
        return {"status": None, "gpus": None, "report_ts": 0, "n": 0}

def save_state(s):
    STATE_FILE.write_text(json.dumps(s, indent=2))

def fmt_up(sec):
    return f"{sec//3600}h {(sec%3600)//60}m"

def main():
    print(f"[{pdt()}] RunPod Monitor запущен")
    pod = get_pod()
    if not pod:
        print("[SKIP] Не удалось получить статус пода")
        sys.exit(0)

    state = load_state()
    state["n"] = state.get("n", 0) + 1
    now = int(time.time())

    prev_s = state.get("status")
    prev_g = state.get("gpus")
    cur_s  = pod["status"]
    cur_g  = pod["gpus"]

    print(f"Pod: {cur_s} | GPU: {cur_g} | prev: {prev_s}/{prev_g}")

    # --- Алерты при изменениях ---
    if prev_s is not None:
        if prev_s == "EXITED" and cur_s == "RUNNING":
            send_tg(
                f"🟢 <b>Pod ЗАПУСТИЛСЯ!</b>  {pdt()}\n"
                f"GPU: {cur_g} шт\n"
                f"Uptime: {fmt_up(pod['uptime'])}"
            )

        if prev_s == "RUNNING" and cur_s == "EXITED":
            send_tg(f"🔴 <b>Pod ОСТАНОВИЛСЯ</b>  {pdt()}\nPod: {POD_ID}")

        if prev_g == 0 and cur_g > 0 and pod["running"]:
            send_tg(
                f"💪 <b>GPU АКТИВЕН!</b>  {pdt()}\n"
                f"Util: {pod['gpu_util']}% | Mem: {pod['gpu_mem']}%"
            )

        if prev_g is not None and prev_g > 0 and cur_g == 0 and pod["running"]:
            send_tg(f"⚠️ <b>GPU ПРОПАЛ</b> при работающем поде!  {pdt()}")

    # --- Отчёт каждые 45 мин ---
    if now - state.get("report_ts", 0) >= 45 * 60:
        if pod["running"] and cur_g > 0:
            msg = (
                f"📊 <b>Отчёт RunPod</b>  {pdt()}\n"
                f"🟢 RUNNING | GPU: {cur_g} шт\n"
                f"Util: <b>{pod['gpu_util']}%</b> | Mem: {pod['gpu_mem']}%\n"
                f"Uptime: {fmt_up(pod['uptime'])}\n"
                f"Проверок: {state['n']}"
            )
        elif pod["running"]:
            msg = (
                f"📊 <b>Отчёт RunPod</b>  {pdt()}\n"
                f"🟡 RUNNING (нет GPU)\n"
                f"Uptime: {fmt_up(pod['uptime'])}"
            )
        else:
            msg = (
                f"📊 <b>Отчёт RunPod</b>  {pdt()}\n"
                f"🔴 EXITED — Pod выключен\n"
                f"Проверок с запуска: {state['n']}"
            )
        send_tg(msg)
        state["report_ts"] = now
        state["n"] = 0

    state["status"] = cur_s
    state["gpus"]   = cur_g
    save_state(state)
    print(f"[DONE] state saved")


if __name__ == "__main__":
    main()
