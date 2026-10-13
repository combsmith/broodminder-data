#!/usr/bin/env bash
# Unattended, forward catch-up sync of your BroodMinder history, for cron.
#
# Automatically resumes from each hive's latest completed window in manifest.json,
# fetches only newly recorded readings and notes, and merges them into the
# analysis-ready NDJSON + CSV outputs without duplicating or dropping data.
#
# Install (e.g. daily at 04:00 UTC):
#   ( crontab -l 2>/dev/null; echo "0 4 * * * /ABS/PATH/TO/scripts/cron_sync.sh" ) | crontab -
#
set -uo pipefail

# Resolve the repo root relative to this script.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$DIR/.venv/bin/python"
LOG="$DIR/data/cron_sync.log"
LOCKFILE="$DIR/data/.sync.lock"

cd "$DIR" || exit 1
[[ -x "$PY" ]] || PY="python3"

ts() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

mkdir -p "$DIR/data"

# Acquire exclusive lock to prevent overlapping sync or backfill runs
if ! command -v flock >/dev/null 2>&1; then
    echo "=== $(ts) sync run failed: flock command not found ===" >> "$LOG"
    exit 1
fi
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "=== $(ts) sync run skipped: another sync or backfill process holds the lock ===" >> "$LOG"
    exit 0
fi

echo "=== $(ts) sync run start ===" >> "$LOG"

# Pull new forward windows since each hive's latest completed manifest window.
extract_rc=0
"$PY" scripts/extract_all.py --catchup >> "$LOG" 2>&1 || extract_rc=$?
echo "--- extract exit $extract_rc ---" >> "$LOG"

if [[ "$extract_rc" -ne 0 ]]; then
    echo "=== $(ts) extract failed (exit $extract_rc); merging any saved windows anyway ===" >> "$LOG"
fi

# Merge new raw windows with existing analysis outputs (no API calls).
# Runs even after a partial extract failure so durable windows get published.
# --completed-only is always used: extract_all.py exits 0 after a rate-limit or
# budget pause that can leave unmanifested (partial) windows, so the exit code
# alone cannot tell whether every window completed.
flatten_rc=0
flatten_args=(--merge --completed-only)
"$PY" scripts/flatten.py "${flatten_args[@]}" >> "$LOG" 2>&1 || flatten_rc=$?
echo "--- flatten exit $flatten_rc ---" >> "$LOG"

if [[ "$flatten_rc" -ne 0 ]]; then
    echo "=== $(ts) sync run failed during flatten (exit $flatten_rc) ===" >> "$LOG"
    exit "$flatten_rc"
fi

if [[ "$extract_rc" -ne 0 ]]; then
    echo "=== $(ts) sync run failed during extract (exit $extract_rc) ===" >> "$LOG"
    exit "$extract_rc"
fi

echo "=== $(ts) run done ===" >> "$LOG"
