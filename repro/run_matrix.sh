#!/usr/bin/env bash
# Controlled comparison driver for httpx2 issue #852.
#
# Starts exactly one instrumented server, then runs each client config
# sequentially (never in parallel, so runs don't contend for CPU) and reports
# failures alongside the peak concurrency the server actually observed.
set -u

REPRO_DIR="$(cd "$(dirname "$0")" && pwd)"
NEW_PY="$REPRO_DIR/../.venv/bin/python"
OLD_PY="$REPRO_DIR/../.venv-old/bin/python"
PIDFILE=/tmp/httpx2-repro-server.pid
PORT=${PORT:-8010}
DELAY=${DELAY:-3}
KEEPALIVE=${KEEPALIVE:-5}
TRIALS=${TRIALS:-3}

stop_server() {
    if [ -f "$PIDFILE" ]; then
        kill "$(cat "$PIDFILE")" 2>/dev/null
        wait "$(cat "$PIDFILE")" 2>/dev/null
        rm -f "$PIDFILE"
    fi
}
trap stop_server EXIT

start_server() {
    stop_server
    PORT=$PORT DELAY=$DELAY KEEPALIVE=$KEEPALIVE "$NEW_PY" "$REPRO_DIR/server_tunable.py" \
        > /tmp/httpx2-repro-server.log 2>&1 &
    echo $! > "$PIDFILE"
    "$NEW_PY" - "$PORT" <<'PY'
import socket, sys, time
port = int(sys.argv[1])
for _ in range(80):
    try:
        socket.create_connection(("127.0.0.1", port), 0.5).close()
        sys.exit(0)
    except OSError:
        time.sleep(0.25)
sys.exit("server did not start")
PY
}

stats() { "$NEW_PY" -c "
import json,urllib.request
print(json.load(urllib.request.urlopen('http://127.0.0.1:$PORT/stats'))['peak_concurrency'])"; }
reset() { "$NEW_PY" -c "
import urllib.request
urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:$PORT/reset', method='POST')).read()"; }

# run <label> <python> <script> <args...>
run() {
    local label=$1 py=$2; shift 2
    for t in $(seq 1 "$TRIALS"); do
        reset
        local out
        out=$(PORT=$PORT "$py" "$REPRO_DIR/$@" 2>&1 | tail -1)
        printf '  %-34s %s | server peak concurrency: %s\n' "$label" "$out" "$(stats)"
    done
}

start_server
echo "server: port=$PORT delay=${DELAY}s uvicorn timeout_keep_alive=${KEEPALIVE}s"

case "${1:-issue}" in
  issue)
    echo "=== Experiment A: the issue's literal workload (300 conns, 1000 requests) ==="
    run "httpx 0.27.0 (issue's version)" "$OLD_PY" bench.py --client httpx   --max-connections 300 --trials 1
    run "httpx2 2.12.0"                  "$NEW_PY" bench.py --client httpx2  --max-connections 300 --trials 1
    run "aiohttp 3.14.3 @300"            "$NEW_PY" bench.py --client aiohttp --max-connections 300 --trials 1
    run "aiohttp 3.14.3 @100 (as filed)" "$NEW_PY" bench.py --client aiohttp --max-connections 100 --trials 1
    ;;
  burst)
    echo "=== Experiment B: idle keep-alive reuse (200 conns x 8 bursts, ${GAP:-1.0}s idle gap) ==="
    run "httpx 0.27.0 (issue's version)" "$OLD_PY" burst.py --client httpx   --conns 200 --bursts 8 --gap "${GAP:-1.0}"
    run "httpx2 2.12.0"                  "$NEW_PY" burst.py --client httpx2  --conns 200 --bursts 8 --gap "${GAP:-1.0}"
    run "aiohttp 3.14.3"                 "$NEW_PY" burst.py --client aiohttp --conns 200 --bursts 8 --gap "${GAP:-1.0}"
    ;;
esac
