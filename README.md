# RunPod GPU Watcher

Постоянный воркер: следит за наличием GPU для выключенного пода RunPod и
мгновенно шлёт уведомление в Telegram, чтобы можно было зайти и нажать **Resume**.
Работает 24/7 на Railway — **не зависит от того, включён ли компьютер**.

## Что делает `worker.py`
- Каждые `POLL_INTERVAL` сек (по умолчанию 10) дёргает реальный RunPod GraphQL API.
- Один запрос отдаёт статус пода + наличие GPU нужного типа на secure cloud.
- Как только GPU появился — мгновенный алерт в Telegram (повтор раз в 5 мин пока держится).
- Каждые 20 мин — отчёт: сколько проверок, сколько раз GPU был доступен, время, аптайм.
- Замечает, когда под сам стал RUNNING (ты нажал Resume) и когда снова EXITED.

## Деплой на Railway
1. railway.app → **New Project → Deploy from GitHub repo** → `Diamond8932/runpod-watcher`.
2. Railway сам подхватит `railway.json` и запустит `python worker.py` как воркер (auto-restart).
3. Вкладка **Variables** — добавить:

| Переменная | Значение |
|-----------|----------|
| `RUNPOD_KEY` | ключ RunPod API (`rpa_...`) |
| `TG_TOKEN` | токен Telegram-бота |
| `TG_CHAT` | `6356247638` |
| `POD_ID` | `06187ayaswoyq2` |

Необязательные (есть значения по умолчанию): `GPU_TYPE`, `GPU_COUNT`,
`POLL_INTERVAL` (сек, по умолчанию 10), `REPORT_INTERVAL` (сек, 1200),
`ALERT_COOLDOWN` (сек, 300).

После добавления переменных Railway передеплоит — в Telegram придёт
«🤖 RunPod Watcher запущен».

## Важно про детект наличия
Сигнал берётся из `gpuTypes.lowestPrice.stockStatus` — это наличие GPU-типа на
secure cloud RunPod. Это реальные данные. В редких случаях, когда сток есть
глобально, но конкретный дата-центр твоего пода занят, Resume может не сработать
с первого раза — просто повтори. Это ограничение RunPod, не бота.
