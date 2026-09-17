#!/bin/bash
set -euo pipefail

RUN_ID="p1_resume_$(date +%Y%m%d_%H%M%S)"
LOG_DIR="runs/$RUN_ID"

mkdir -p "$LOG_DIR"

PYTHON="${PYTHON:-.venv/bin/python}"

echo "=== P1 Interrupt/Resume Test ==="
echo "RUN_ID=$RUN_ID"

echo ""
echo "[1/5] Snapshot before run"

"$PYTHON" -m xhs_ingest.status_report \
  > "$LOG_DIR/before.json"

echo "before saved"

echo ""
echo "[2/5] Start collector"

# Actual sync command with explicit required argument --max-notes all
"$PYTHON" -m xhs_ingest.cli sync --max-notes all \
  > "$LOG_DIR/first_run.log" 2>&1 &

PID=$!

echo "PID=$PID"

echo ""
echo "[3/5] Wait for active processing"

sleep 120

echo ""
echo "[4/5] Send SIGTERM"

kill -TERM "$PID" || true

wait "$PID" || true

echo ""
echo "Interrupted."

echo ""
echo "[5/5] Resume run"

# Allow non-zero exit codes (e.g. exit 3 for unproven/slice) without aborting before report
"$PYTHON" -m xhs_ingest.cli sync --max-notes all \
  > "$LOG_DIR/resume_run.log" 2>&1 || true

echo ""
echo "Generate final report"

"$PYTHON" -m xhs_ingest.status_report \
  > "$LOG_DIR/after.json"

echo ""
echo "Done:"
echo "$LOG_DIR"
