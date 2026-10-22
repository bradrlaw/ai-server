#!/usr/bin/env bash
# open | close | status for the benchmark window (see window-daemon.sh).
# `open` unloads the production llama-swap models; `close` re-warms the daily trio.
set -uo pipefail
STATE=/srv/ai/tmp/lcpp-test/window
HERE=$(cd "$(dirname "$0")" && pwd)
alive() { [[ -f $STATE/pid ]] && kill -0 "$(cat "$STATE/pid")" 2>/dev/null; }

case "${1:-status}" in
  open)
    if alive; then echo "window already open (pid $(cat "$STATE/pid"))"; exit 0; fi
    mkdir -p "$STATE"; rm -f "$STATE/close"
    setsid nohup "$HERE/window-daemon.sh" "$STATE" "${MAX_S:-10800}" >/dev/null 2>&1 &
    echo $! > "$STATE/pid"
    echo "window opened (pid $!, self-expires after ${MAX_S:-10800}s); log: $STATE/window.log"
    ;;
  close)
    if ! alive; then echo "window not open"; exit 0; fi
    touch "$STATE/close"
    for _ in $(seq 1 90); do alive || break; sleep 2; done
    alive && echo "still closing (daily re-warm can take a few minutes); see $STATE/window.log" || echo "window closed"
    tail -n 4 "$STATE/window.log"
    ;;
  status)
    if alive; then echo "OPEN (pid $(cat "$STATE/pid"))"; else echo "closed"; fi
    tail -n 4 "$STATE/window.log" 2>/dev/null
    ;;
  *) echo "usage: $0 open|close|status"; exit 2;;
esac
