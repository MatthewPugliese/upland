#!/bin/bash
# Periodic Pi health snapshot — logs RAM/disk/Docker/greymass status to a
# rotating log file. Meant to run via cron every few hours so there's a
# visible history to check after an extended absence, not just whatever the
# current state happens to be when someone next looks.
#
# Safe to run unattended: read-only checks, no writes to any application
# data, no network calls beyond what docker/free/df already do locally.

LOG_DIR="/opt/upland/logs"
LOG_FILE="$LOG_DIR/health_check.log"
MAX_LINES=5000

mkdir -p "$LOG_DIR"

{
    echo "=== $(date -u +"%Y-%m-%dT%H:%M:%SZ") ==="
    free -h
    echo "--- docker ---"
    docker ps --format "table {{.Names}}\t{{.Status}}" 2>&1
    echo "--- disk ---"
    df -h / 2>&1
    echo "--- greymass backfill ---"
    if pgrep -f greymass_backfill.py > /dev/null; then
        tail -n 1 /tmp/greymass_backfill.log 2>/dev/null || echo "(no log output yet)"
    else
        echo "NOT RUNNING"
    fi
    echo ""
} >> "$LOG_FILE" 2>&1

# Keep the log bounded — a 4-hourly cadence over months would otherwise grow forever
if [ -f "$LOG_FILE" ]; then
    tail -n "$MAX_LINES" "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi
