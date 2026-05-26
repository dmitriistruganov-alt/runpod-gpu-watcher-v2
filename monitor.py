#!/usr/bin/env python3
"""
RunPod GPU Monitor v2 — GitHub Actions, каждую минуту.
State: state.json (коммитится в репо).
Новое: авто-запуск пода + авто-старт Jupyter через RunPod exec API.
"""
import json, os, sys, time
import http.client, ssl
from pathlib import Path

TG_TOKEN    = os.environ.get("TG_TOKEN", "")
TG_CHAT     = os.environ.get("TG_CHAT",  "6356247638")
RUNPOD_KEY  = os.environ.get("RUNPOD_KEY", "")
POD_ID      = os.environ.get("POD_ID",  "06187ayaswoyq2")
AUTO_RESUME = os.environ.get("AUTO_RESUME", "0") == "1"
STATE_FILE  = Path("state.json")


def pdt():
    import time as t
    u = t.gmtime()
    return f"{(u.tm_hour - 7) % 24:02d}:{u.tm_min:02d} PDT"


def fmt_up(sec):
    return f"{sec // 3600}h {(sec % 3600) // 60}m"


def send_tg(text: str):
    import urllib.request
    if not TG_TOKEN:
        print(f"[NO TG] {text[:80]}")
        return
    try:
        data = json.dumps({
            "chat_id": TG_CHAT,
            "text": text,
            "parse_mode": "HTML"
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read())
            if res.get("ok"):
                print(f"[TG OK] {text[:60]}")
            else:
                print(f"[TG FAIL] {res}")
    except Exception as e:
        print(f"[TG ERROR] {e}")


def runpod_gql(query: str) -> dict:
    """GraphQL запрос к RunPod API через http.client (urllib даёт 403)."""
    if not RUNPOD_KEY:
        print("[ERROR] RUNPOD_KEY не задан")
        return {}
    body = json.dumps({"query": query}).encode("utf-8")
    ctx  = ssl.create_default_context()
    conn = http.client.HTTPSConnection("api.runpod.io", timeout=20, context=ctx)
    try:
        conn.request(
            "POST",
            f"/graphql?api_key={RUNPOD_KEY}",
            body=body,
            headers={"Content-Type": "application/json", "User-Agent": "runpod-watcher/2.0"}
        )
        resp = conn.getresponse()
        raw  = resp.read().decode("utf-8")
        conn.close()
        if resp.status != 200:
            print(f"[RunPod] HTTP {resp.status}: {raw[:200]}")
            return {}
        return json.loads(raw).get("data") or {}
    except Exception as e:
        print(f"[RunPod] Error: {e}")
        conn.close()
        return {}


def get_pod() -> dict | None:
    """Получить статус пода."""
    q = ('{ pod(input: {podId: "%s"}) { desiredStatus runtime { '
         'uptimeInSeconds gpus { gpuUtilPercent memoryUtilPercent } } } }') % POD_ID
    data = runpod_gql(q)
    pod  = data.get("pod")
    if pod is None:
        print("[SKIP] Pod не найден или API ошибка")
        return None
    rt   = pod.get("runtime")
    gpus = rt.get("gpus", []) if rt else []
    up   = rt.get("uptimeInSeconds", 0) if rt else 0
    return {
        "status":   pod.get("desiredStatus", "UNKNOWN"),
        "running":  rt is not None,
        "gpus":     len(gpus),
        "gpu_util": gpus[0].get("gpuUtilPercent",   0) if gpus else 0,
        "gpu_mem":  gpus[0].get("memoryUtilPercent", 0) if gpus else 0,
        "uptime":   up,
    }


def resume_pod() -> bool:
    """Попытаться запустить под (авто-резюм)."""
    q = ('mutation { podResume(input: {podId: "%s", gpuCount: 1}) '
         '{ id desiredStatus } }') % POD_ID
    data = runpod_gql(q)
    result = data.get("podResume") or {}
    new_status = result.get("desiredStatus", "")
    print(f"[RESUME] podResume → {new_status}")
    return new_status == "RUNNING"



def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except:
        return {"status": None, "gpus": None, "report_ts": 0, "n": 0}


def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, indent=2))


def main():
    print(f"[{pdt()}] RunPod Monitor v2 запущен | AUTO_RESUME={AUTO_RESUME}")

    pod = get_pod()
    if pod is None:
        sys.exit(0)

    state   = load_state()
    now     = int(time.time())
    state["n"] = state.get("n", 0) + 1

    prev_s  = state.get("status")
    prev_g  = state.get("gpus")
    cur_s   = pod["status"]
    cur_g   = pod["gpus"]

    print(f"Pod: {cur_s} | GPU: {cur_g} шт | prev: {prev_s}/{prev_g}")

    # ── Алерты при изменениях статуса ──────────────────────────────────────
    if prev_s is not None:
        if prev_s == "EXITED" and cur_s == "RUNNING":
            send_tg(
                f"🟢 <b>Pod ЗАПУСТИЛСЯ!</b>  {pdt()}\n"
                f"GPU: {cur_g} шт\n"
                f"Uptime: {fmt_up(pod['uptime'])}\n\n"
                f"⚡ Запусти Jupyter:\n"
                f"<code>bash /root/jupyter-watchdog.sh &</code>"
            )

        if prev_s == "RUNNING" and cur_s == "EXITED":
            send_tg(f"🔴 <b>Pod ОСТАНОВИЛСЯ</b>  {pdt()}\nPod: {POD_ID}")

        if prev_g == 0 and cur_g > 0 and pod["running"]:
            send_tg(
                f"💪 <b>GPU АКТИВЕН!</b>  {pdt()}\n"
                f"Util: {pod['gpu_util']}% | Mem: {pod['gpu_mem']}%\n"
                f"Uptime: {fmt_up(pod['uptime'])}"
            )

        if prev_g is not None and prev_g > 0 and cur_g == 0 and pod["running"]:
            send_tg(f"⚠️ <b>GPU ПРОПАЛ</b> при работающем поде!  {pdt()}")

    # ── Авто-запуск пода ────────────────────────────────────────────────────
    if AUTO_RESUME and cur_s == "EXITED":
        print("[AUTO_RESUME] Pod EXITED → пробуем запустить...")
        ok = resume_pod()
        if ok:
            send_tg(
                f"🚀 <b>Авто-запуск пода!</b>  {pdt()}\n"
                f"Pod ID: {POD_ID}\n"
                f"Ждём GPU..."
            )
        else:
            print("[AUTO_RESUME] Запуск не удался (GPU недоступен?)")

    # ── Отчёт каждые 10 мин ────────────────────────────────────────────────
    if now - state.get("report_ts", 0) >= 10 * 60:
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
    print("[DONE] state saved")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = f"⚠️ <b>GPU Watcher ошибка</b>\n<code>{type(e).__name__}: {e}</code>"
        send_tg(msg)
        print(f"[FATAL] {e}")
        sys.exit(0)
