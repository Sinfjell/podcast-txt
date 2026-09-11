#!/usr/bin/env bash
# Push yesterday's Podskrift growth metrics to the Notion metrics database.
#
# Weekday cron (Europe/Oslo), as the app user:
#   50 7 * * 1-5  cd /var/www/vhosts/podskrift.nettsmed.dev/app && \
#     TZ=Europe/Oslo ./ops/notion-daily-metrics.sh \
#     >> data/logs/notion-daily-metrics.log 2>&1
#
# Dry-run (no Notion write):
#   DRY_RUN=1 ./ops/notion-daily-metrics.sh
#   DRY_RUN=1 ./ops/notion-daily-metrics.sh --day 2026-09-10
set -euo pipefail

APP_DIR="${APP_DIR:-/var/www/vhosts/podskrift.nettsmed.dev/app}"
export APP_DIR

exec python3 "$APP_DIR/ops/notion-daily-metrics.py" "$@"
