#!/usr/bin/env bash
# Keeps tgclone running. Restarts it 10 s after a crash and shows why it stopped.
#   bash scripts/run.sh          (inside tmux:  tmux new -s tgclone 'bash scripts/run.sh')
# Ctrl+C stops it for good.
cd "$(dirname "$0")/.." || exit 1
MODE="${1:-live}"
trap 'echo; echo "Stopped."; exit 0' INT TERM
while true; do
    .venv/bin/python -m tgclone "$MODE"
    code=$?
    [ "$code" -eq 0 ] && { echo "tgclone exited normally."; exit 0; }
    echo
    echo "⚠️  tgclone stopped (exit code $code) at $(date '+%d %b %H:%M:%S'). Last log lines:"
    tail -n 15 data/tgclone.log 2>/dev/null
    echo "↻ Restarting in 10 s ... (Ctrl+C to quit)"
    sleep 10
done
