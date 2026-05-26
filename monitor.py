#!/usr/bin/env python3
"""
RunPod GPU Monitor v3 — строгая верификация GPU.
"Pod запущен" ТОЛЬКО когда: desiredStatus=RUNNING + runtime IS NOT NULL + gpus > 0
State: state.json (коммитится в репо)
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

RESUME_COOLDOWN = 20 * 60  # не чаще раза в 20 мин


def pdt():
    u = time.gmtime()
    return f"{(u.tm_hour - 7) % 24:02d}:{u.tm_min:02d} PDT"


def fmt_up(sec):
    return f"{sec // 3600}h {(sec % 3600) // 60}m"


def send_tg(text: str):
    if not TG_TOKEN:
        print(f"[NO TG] {text[:80]}")
        return
    import urllib.request
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
                print(f"[TG OK] {text[:80]}")
            else:
                print(f"[TG FAIL] {res}")
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
        conn.request(
            "POST",
            f"/graphql?api_key={RUNPOD_KEY}",
            body=body,
            headers={"Content-Type": "application/json", "User-Agent": "runpod-watcher/3.0"}
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
        try:
            conn.close()
        except Exception:
            pass
        return {}


def get_pod() -> dict | None:
    """
    Получить статус пода.
    running=True ТОЛЬКО если runtime существует (GPU выделен).
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


def try_resume_pod() -> str:
    """
    Отправить запрос на запуск пода.
    Возвращает 'queued' или 'error'.
    ВАЖНО: desiredStatus=RUNNING = запрос принят, НЕ = GPU выделен.
    Результат этой функции НИКОГДА не считается подтверждением GPU.
    """
    q = ('mutation { podResume(input: {podId: "%s", gpuCount: 1}) '
         '{ id desiredStatus } }') % POD_ID
    data = runpod_gql(q)
    if not data:
        return "error"
    result = data.get("podResume") or {}
    new_status = result.get("desiredStatus", "")
    print(f"[RESUME] podResume -> desiredStatus={new_status!r}")
    return "queued" if new_status else "error"


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, indent=2))


def main():
    print(f"[{pdt()}] RunPod Monitor v3 | AUTO_RESUME={AUTO_RESUME}")

    pod = get_pod()
    if pod is None:
        sys.exit(0)

    state = load_state()
    now   = int(time.time())

    prev_s        = state.get("status")
    prev_running  = state.get("running")
    gpu_confirmed = state.get("gpu_confirmed", False)
    last_resume   = state.get("last_resume_ts", 0)
    attempts      = state.get("resume_attempts", 0)
    n             = state.get("n", 0) + 1

    cur_s       = pod["status"]
    cur_running = pod["running"]
    cur_g       = pod["gpus"]

    # GPU реально готов: RUNNING + runtime существует + GPUs > 0
    truly_ready = cur_s == "RUNNING" and cur_running and cur_g > 0

    print(f"desiredStatus={cur_s} | runtime={'YES' if cur_running else 'NO'} | GPUs={cur_g}")
    print(f"prev: status={prev_s} running={prev_running} | gpu_confirmed={gpu_confirmed}")

    # ── GPU подтверждён впервые в этой сессии ────────────────────────────────
    if truly_ready and not gpu_confirmed:
        send_tg(
            f"<b>GPU ГОТОВ! Под работает!</b>  {pdt()}\n"
            f"GPU: {cur_g} | Util: {pod['gpu_util']}%\n"
            f"Uptime: {fmt_up(pod['uptime'])}\n"
            f"Pod: <code>{POD_ID}</code>\n\n"
            f"Запусти:\n"
            f"<code>bash /root/pod_first_boot.sh</code>\n"
            f"<code>bash /root/xmode_stabilize.sh</code>"
        )
        state["gpu_confirmed"] = True
        state["resume_attempts"] = 0

    # ── Под остановился (был running, теперь нет) ────────────────────────────
    elif prev_running and not cur_running:
        send_tg(f"<b>Под остановился</b>  {pdt()}\nPod: {POD_ID}")
        state["gpu_confirmed"] = False
        state["resume_attempts"] = 0

    # ── desiredStatus=RUNNING но runtime=None (в очереди) ────────────────────
    elif cur_s == "RUNNING" and not cur_running:
        print(f"[WAIT] desiredStatus=RUNNING но runtime=None — ждём GPU...")

    # ── Авто-резюм при EXITED ─────────────────────────────────────────────────
    if AUTO_RESUME and cur_s == "EXITED":
        if now - last_resume >= RESUME_COOLDOWN:
            print(f"[AUTO_RESUME] EXITED -> попытка #{attempts + 1}")
            result = try_resume_pod()
            state["last_resume_ts"] = now
            state["resume_attempts"] = attempts + 1

            if result == "queued":
                send_tg(
                    f"Запрос на запуск пода  {pdt()}\n"
                    f"Попытка #{attempts + 1} | Pod: <code>{POD_ID}</code>\n"
                    f"Ждём GPU... проверка через 10 мин"
                )
            else:
                print("[AUTO_RESUME] podResume вернул ошибку — GPU недоступен")
        else:
            wait_min = (RESUME_COOLDOWN - (now - last_resume)) // 60
            print(f"[AUTO_RESUME] Кулдаун — следующая попытка через {wait_min} мин")

    # ── Отчёт каждые 10 мин ──────────────────────────────────────────────────
    if now - state.get("report_ts", 0) >= 10 * 60:
        att = state.get("resume_attempts", 0)
        if truly_ready:
            msg = (
                f"RunPod Отчёт  {pdt()}\n"
                f"РАБОТАЕТ | GPU: {cur_g}\n"
                f"Util: {pod['gpu_util']}% | Mem: {pod['gpu_mem']}%\n"
                f"Uptime: {fmt_up(pod['uptime'])}"
            )
        elif cur_s == "RUNNING" and not cur_running:
            msg = (
                f"RunPod Отчёт  {pdt()}\n"
                f"В очереди (desiredStatus=RUNNING, runtime=None)\n"
                f"Попыток запуска: {att}"
            )
        else:
            msg = (
                f"RunPod Отчёт  {pdt()}\n"
                f"ВЫКЛЮЧЕН\n"
                f"Попыток запуска: {att}\n"
                f"Проверок: {n}"
            )
        send_tg(msg)
        state["report_ts"] = now
        state["n"] = 0

    state["status"]  = cur_s
    state["running"] = cur_running
    state["gpus"]    = cur_g
    state["n"]       = n
    save_state(state)
    print("[DONE] state saved")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = f"GPU Watcher ошибка: {type(e).__name__}: {e}"
        send_tg(msg)
        print(f"[FATAL] {e}")
        sys.exit(0)
