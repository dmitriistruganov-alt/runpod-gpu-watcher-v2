# RunPod GPU Watcher v4

Мониторинг пода RunPod через GitHub Actions. Каждые ~60 секунд проверяет статус GPU.

## Механизм

Self-trigger: после каждого запуска workflow триггерит следующий через 55s.
Backup cron `*/5 * * * *` восстанавливает цепь при обрыве.

GPU подтверждён ТОЛЬКО когда: `desiredStatus=RUNNING` + `runtime IS NOT NULL` + `gpus > 0`
