#!/usr/bin/env python3
"""
RunPod GPU Monitor v4
- Проверка каждую минуту (cron * * * * *)
- podResume при каждой проверке если pod EXITED (без TG на каждую попытку)
- Отчёт каждые 20 мин: сколько проверок, попыток, статус
- GPU подтверждён (runtime + gpus) -> немедленный TG
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

REPORT_EVERY = 20 * 60   # отчёт каждые 20 мин


def pdt():
    u = time.gmtime()
    return f"{(u.tm_hour - 7) % 24:02d}:{u.tm_min:02d} PDT"


def fmt_up(sec):
    h, m = sec // 3600, (sec % 3600) // 60
    return f"{h}h {m}m"


def send_tg(text: str):
    if not TG_TOKEN:
        print(f"[NO TG] {text[:80]}")
        return
    import urllib.request
    try:
        data = json.dumps({"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read())
            print(f"[TG {'OK' if res.get('ok') else 'FAIL'}] {text[:60]}")
    except Exception as e:
        print(f"[TG ERROR] {e}")


def runpod_gql(query: str) -> dict:
    if not RUNPOD_KEY:
        print("[ERROR] RUNPOD_KEY не задан")
        return {}
    body = json.dumps({"query": query}).encode("utf-8")
    ctx  = ssl.create_default_context()
    conn = http.client.HTTPSConnection("api.runpod.io", timeout=20, context=ctx)
    try:
        conn.request("POST", f"/graphql?api_key={RUNPOD_KEY}", body=body,
                     headers={"Content-Type": "application/json", "User-Agent": "runpod-watcher/4.0"})
        resp = conn.getresponse()
        raw  = resp.read().decode("utf-8")
        conn.close()
        if resp.status != 200:
            print(f"[RunPod] HTTP {resp.status}: {raw[:200]}")
            return {}
        return json.loads(raw).get("data") or {}
    except Exception as e:
        print(f"[RunPod] Error: {e}")
        try: conn.close()
        except: pass
        return {}


def get_pod() -> dict | None:
    """
    running=True ТОЛЬКО если runtime не None (GPU физически выделен).
    desiredStatus=RUNNING без runtime = под в очереди, GPU ещё не дали.
    """
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


def try_resume(silent=True) -> bool:
    """
    Запрос на запуск пода. silent=True — не шлём TG на каждую попытку.
    Возвращает True если запрос принят.
    НИКОГДА не считаем success = GPU выделен.
    """
    q = ('mutation { podResume(input: {podId: "%s", gpuCount: 1}) '
         '{ id desiredStatus } }') % POD_ID
    data = runpod_gql(q)
    if not data:
        return False
    result = data.get("podResume") or {}
    new_status = result.get("desiredStatus", "")
    print(f"[RESUME] podResume -> {new_status!r}")
    return bool(new_status)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, indent=2))


def main():
    print(f"[{pdt()}] RunPod Monitor v4 | AUTO_RESUME={AUTO_RESUME}")

    pod = get_pod()
    if pod is None:
        sys.exit(0)

    state = load_state()
    now   = int(time.time())

    prev_running  = state.get("running")
    gpu_confirmed = state.get("gpu_confirmed", False)
    checks        = state.get("checks", 0) + 1   # проверок с последнего отчёта
    attempts      = state.get("attempts", 0)       # попыток resume с последнего отчёта
    total_att     = state.get("total_att", 0)      # всего попыток

    cur_s       = pod["status"]
    cur_running = pod["running"]
    cur_g       = pod["gpus"]

    # GPU реально готов
    truly_ready = cur_s == "RUNNING" and cur_running and cur_g > 0

    print(f"desiredStatus={cur_s} | runtime={'YES' if cur_running else 'NO'} | GPUs={cur_g}")

    # ── GPU подтверждён впервые ─────────────────────────────────────────────
    if truly_ready and not gpu_confirmed:
        send_tg(
            f"<b>GPU ЗАПУЩЕН! Заходи и включай под!</b>\n"
            f"GPU: {cur_g} шт | Util: {pod['gpu_util']}%\n"
            f"Uptime: {fmt_up(pod['uptime'])}\n"
            f"Pod: <code>{POD_ID}</code>\n\n"
            f"Запусти:\n"
            f"<code>bash /root/pod_first_boot.sh</code>\n"
            f"<code>bash /root/xmode_stabilize.sh</code>"
        )
        state["gpu_confirmed"] = True
        state["total_att"] = 0

    # ── Под остановился ─────────────────────────────────────────────────────
    elif prev_running and not cur_running:
        send_tg(f"<b>Под остановился</b>  {pdt()}\nPod: {POD_ID}")
        state["gpu_confirmed"] = False
        state["total_att"] = 0

    elif cur_s == "RUNNING" and not cur_running:
        print(f"[WAIT] desiredStatus=RUNNING но runtime=None — в очереди...")

    # ── Авто-резюм при каждом EXITED ────────────────────────────────────────
    if AUTO_RESUME and cur_s == "EXITED":
        ok = try_resume(silent=True)   # без TG — отчёт каждые 20 мин
        if ok:
            attempts  += 1
            total_att = state.get("total_att", 0) + 1
            state["attempts"] = attempts
            state["total_att"] = total_att
            print(f"[RESUME] Попытка #{total_att}")

    # ── Отчёт каждые 20 мин ─────────────────────────────────────────────────
    state["checks"] = checks
    if now - state.get("report_ts", 0) >= REPORT_EVERY:
        if truly_ready:
            msg = (
                f"RunPod  {pdt()}\n"
                f"РАБОТАЕТ | GPU: {cur_g}\n"
                f"Util: {pod['gpu_util']}% | Uptime: {fmt_up(pod['uptime'])}"
            )
        elif cur_s == "RUNNING" and not cur_running:
            msg = (
                f"RunPod  {pdt()}\n"
                f"В очереди (ждём GPU)\n"
                f"Проверок за 20 мин: {checks} | Попыток запуска: {attempts}"
            )
        else:
            msg = (
                f"RunPod  {pdt()}\n"
                f"ВЫКЛЮЧЕН\n"
                f"Проверок за 20 мин: {checks} | Попыток запуска: {attempts}"
            )
        send_tg(msg)
        state["report_ts"] = now
        state["checks"]    = 0
        state["attempts"]  = 0

    state["status"]  = cur_s
    state["running"] = cur_running
    state["gpus"]    = cur_g
    save_state(state)
    print(f"[DONE] checks={checks} attempts={attempts}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        send_tg(f"GPU Watcher ошибка: {type(e).__name__}: {e}")
        print(f"[FATAL] {e}")
        sys.exit(0)
