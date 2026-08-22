#!/usr/bin/env bash
# Locate the httpx2 commits that fixed issue #852, by swapping each historical
# version of httpcore2's async connection_pool.py into an otherwise-stock
# httpx2 2.12.0 install and re-running the issue's workload.
#
#   ./bisect_pool.sh /path/to/httpx2-clone
#
# IMPORTANT: .venv-prefix must be installed with UV_LINK_MODE=copy and
# --no-cache. uv hardlinks package files from its shared cache by default, so
# a venv installed the normal way shares inodes with every other venv holding
# the same version -- overwriting the file in one silently rewrites it in all
# of them (and in the cache), and the A/B silently compares a version against
# itself.
set -eu

CLONE=${1:?usage: bisect_pool.sh /path/to/httpx2-clone}
F=src/httpcore2/httpcore2/_async/connection_pool.py
HERE="$(cd "$(dirname "$0")" && pwd)"
PRE="$HERE/../.venv-prefix/lib/python3.11/site-packages/httpcore2/_async/connection_pool.py"
POST="$HERE/../.venv/lib/python3.11/site-packages/httpcore2/_async/connection_pool.py"
PORT=${PORT:-8021}

guard=$(md5sum "$POST" | cut -d' ' -f1)
[ "$(stat -c %h "$PRE")" = "1" ] || { echo "refusing: $PRE is hardlinked (see note above)"; exit 1; }

for c in 60c8262b 8027999f 053cb9c0 b082502b; do
    git -C "$CLONE" show "$c:$F" > "$PRE"
    find "$HERE/../.venv-prefix" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
    [ "$(md5sum "$POST" | cut -d' ' -f1)" = "$guard" ] || { echo "ABORT: .venv was mutated"; exit 1; }
    echo "===== pool @ $c : $(git -C "$CLONE" log -1 --format='%s' "$c" | cut -c1-48) ====="
    PORT=$PORT "$HERE/../.venv-prefix/bin/python" "$HERE/trace_pool.py" httpx2 | tail -4
done
