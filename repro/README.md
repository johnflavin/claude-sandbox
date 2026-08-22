# Reproduction: httpx2 issue #852

> Intermittent `httpx.ReadError` with high concurrency
> <https://github.com/pydantic/httpx2/issues/852>

## Result

**The issue does not reproduce on httpx2.** It reproduces reliably on the
`httpx 0.27.0` / `httpcore 1.0.5` versions named in the report.

Versions used here: `httpx2 2.12.0` / `httpcore2 2.12.0`, `aiohttp 3.14.3`,
`fastapi 0.141.1`, `uvicorn 0.52.4`, Python 3.11 on Linux.

Issue's workload — 300 concurrent, 1000 requests, 3s server delay, 3 trials each:

| client | failures | wall time | peak server concurrency |
| --- | --- | --- | --- |
| `httpx 0.27.0` (as reported) | **1, 3, 2 x ReadError** | 51s | 300 |
| `httpx2 2.12.0` | none | 13.8s | 300 |
| `aiohttp 3.14.3` @ 300 | none | 12.6s | 300 |
| `aiohttp 3.14.3` @ 100 (as the issue actually ran it) | none | 30.6s | 100 |

## Cause

`httpcore 1.0.5`'s pool has no notion of a connection being **reserved**
between the moment it is assigned to a waiting request and the moment that
request actually sends its first byte. Throughout that window the connection
still reports `is_idle()`, so a later pass of
`_assign_requests_to_connections` is free to treat it as expired and put it in
the `closing` list. That close is then awaited *outside* the pool lock:

```python
with self._optional_thread_lock:
    closing = self._assign_requests_to_connections()
await self._close_connections(closing)      # <-- yields to the event loop
```

so the waiting request can wake up, transition the connection to `ACTIVE` and
start writing, only for `_close_connections` to tear the socket down
underneath it. The request then reads EOF on a socket it has already written
to — `httpx.ReadError`.

Instrumenting the pool (`trace_pool.py`) shows the race directly:

| | `httpx 0.27.0` | `httpx2 2.12.0` |
| --- | --- | --- |
| connections closed mid-run | ~845 | **0** |
| ...closed while still held by a waiting request | 17–47 | **0** |
| `ConnectionNotAvailable` retries | ~2400 | **0** |
| `ReadError` | 0–4 | **0** |

Most of those reserved-connection closes are caught safely — the waiting
request finds the connection already `CLOSED`, gets `ConnectionNotAvailable`
and retries. That is the ~2400 retries, and it is also why the old client
takes 51s to do 12s of work. A handful per run lose the race in the narrow
window where the write has already started, and *those* become the
intermittent `ReadError` from the issue title.

`httpcore2` fixes this by tracking reserved connections explicitly:

```python
# Connections currently referenced by an in-flight request, including
# connections that are in the process of being established and idle
# connections reserved by an assigned-but-not-yet-sent request.
request_connections = {r.connection for r in self._requests}
...
elif not reserved and connection.has_expired():
    closing_connections.append(connection)
```

with the surplus-idle pass excluding them too ("Reserved connections are not
surplus: a request is about to be sent on them"). With that, the pool closes
zero connections mid-run on this workload and the churn disappears entirely.

The two halves of that landed in two different commits — see
[Where it was fixed in httpx2](#where-it-was-fixed-in-httpx2) below. The
`ReadError` itself stops reproducing one commit *earlier* than the reserved-
connection work, because the branch that did the closing was deleted first.

## Where it was fixed in httpx2

Two commits in `pydantic/httpx2`, both in the vendored
`src/httpcore2/httpcore2/_async/connection_pool.py`. Only seven commits have
ever touched that file, so this is the whole story:

### `8027999f` — perf: Rewrite `_assign_requests_to_connections` as a single-pass loop ([#974](https://github.com/pydantic/httpx2/pull/974), 2026-06-01)

This is the one that kills the `ReadError`. It deletes the third assignment
branch inherited from httpcore 1.0.5:

```python
elif idle_connections:
    # log: "closing idle connection"
    connection = idle_connections[0]      # no check that another waiting
    self._connections.remove(connection)  # request was already assigned it
    closing_connections.append(connection)
    connection = self.create_connection(origin)
    ...
```

When the pool is saturated, that branch closes a live idle connection to make
room for a new one — picking `idle_connections[0]` with no regard for whether
a waiting request had already been assigned it. It is replaced by a
`new_connection_budget` counter that simply declines to over-allocate, so the
pool stops closing connections mid-run at all. Measured: connections closed
per run goes 505 → 0, and `ReadError` goes 1 → 0.

### `b082502b` — Eliminate multi-assignment churn in the connection pool ([#1075](https://github.com/pydantic/httpx2/pull/1075), 2026-07-23)

This is the one that kills the churn, and that makes the pool *structurally*
immune to the race rather than merely not exercising it. It introduces the
notion of a **reserved** connection — one assigned to a request that has not
yet sent — and excludes reserved connections from every path that could close
or re-hand-out a connection:

```python
elif not reserved and connection.has_expired():          # expiry pass
...
sum(connection.is_idle() and connection not in request_connections ...)  # surplus-idle pass
...
available_connections = [                                 # assignment pass
    connection for connection in self._connections
    if connection.is_available()
    and not (connection.is_idle() and connection in request_connections
             and not connection.can_multiplex())
]
```

Its own comment names the exact failure mode measured above: *"an idle
connection already assigned to an in-flight request is reserved: it stays IDLE
until the winning task sends on it, so without this exclusion the next pass
would assign it again and the loser would churn through
`ConnectionNotAvailable`."*

### Bisect

`bisect_pool.sh` swaps each historical version of the pool file into an
otherwise-stock httpx2 2.12.0 and re-runs the issue's workload:

| pool version | connections closed | ReadError | `ConnectionNotAvailable` retries |
| --- | --- | --- | --- |
| `60c8262b` (pre-#974, the 1.0.5-era pool) | 505 | 1 | 1058 |
| `8027999f` (#974) | **0** | **0** | 4244 |
| `053cb9c0` (#983) | 0 | 0 | 4145 |
| `b082502b` (#1075) | 0 | 0 | **0** |

The `60c8262b` row is indicative rather than exact: that pool file is being
run against httpx2 2.12.0's supporting modules, and the mismatch also produces
307 spurious `PoolTimeout`s. The clean measurement of the 1.0.5-era behaviour
is the real `httpcore 1.0.5` install in the table above (845 closes, 17–47 of
them while reserved).

### Methodology note: uv hardlinks

`uv` hardlinks package files from its shared cache, so two venvs holding the
same version share inodes. Overwriting the pool file in one venv silently
rewrites it in the other *and* in the cache — an A/B set up that way compares
a version against itself and shows no difference. Install the comparison venv
with `UV_LINK_MODE=copy` and `--no-cache`, and assert `stat -c %h` is 1 before
patching; `bisect_pool.sh` does both.

## Two flaws in the report's comparison

1. **The two clients did not run at the same concurrency.**
   `aiohttp.TCPConnector` defaults to `limit=100`, and the issue's aiohttp
   client never overrides it. So httpx ran at 300 concurrent connections and
   aiohttp at 100. Raising aiohttp to 300 (see table above) still passes, so
   this is not the whole story — but the comparison as filed is not
   like-for-like.

2. **`await` vs `async with` is not the cause.** `client.post()` is the
   non-streaming API: it reads the body to completion and releases the
   connection back to the pool before it returns, so there is nothing left to
   release. `style_test.py` runs the same workload both ways:

   ```
   httpx  style=await  -> 999/1000 ok | {'ReadError': 1}
   httpx  style=stream -> 998/1000 ok | {'ReadError': 2}
   httpx2 style=await  -> 1000/1000 ok | none
   httpx2 style=stream -> 1000/1000 ok | none
   ```

   The failure rate tracks the library version, not the calling style.

## Note on aiohttp

aiohttp is not inherently immune to stale-connection races — the issue's
workload just never exercises one, because every pooled connection is
continuously busy and never sits idle long enough to be reaped. Under a
workload that *does* idle connections across a server-side keep-alive timeout
(`burst.py`, server `timeout_keep_alive=1s`, 1s idle gap), aiohttp fails and
httpx2 does not:

```
httpx  0.27.0  1600/1600 ok | none
httpx2 2.12.0  1600/1600 ok | none
aiohttp 3.14.3 1531/1600 ok | 69 x ClientOSError
aiohttp 3.14.3 1600/1600 ok | none
aiohttp 3.14.3 1599/1600 ok | 1 x ClientOSError
```

## Files

| file | what it is |
| --- | --- |
| `server.py` | the issue's FastAPI server, verbatim |
| `client_httpx2.py` | the issue's httpx client, with `httpx` -> `httpx2` |
| `client_aiohttp.py` | the issue's aiohttp client, verbatim |
| `server_tunable.py` | same endpoint, with delay / keep-alive / concurrency stats |
| `bench.py` | the issue's workload, counting failures instead of aborting |
| `burst.py` | bursts separated by an idle gap, to exercise keep-alive reuse |
| `trace_pool.py` | instruments the connection pool to count the race directly |
| `style_test.py` | same workload via `await` and via `async with client.stream` |
| `run_matrix.sh` | controlled driver: one server, runs sequential, reports peaks |
| `bisect_pool.sh` | swaps historical pool versions in to locate the fix commits |

## Running it

```sh
uv venv .venv     && uv pip install --python .venv/bin/python     httpx2 aiohttp fastapi uvicorn numpy
uv venv .venv-old && uv pip install --python .venv-old/bin/python 'httpx==0.27.0' 'httpcore==1.0.5' numpy

TRIALS=3 PORT=8010 DELAY=3    KEEPALIVE=5 ./run_matrix.sh issue
TRIALS=3 PORT=8011 DELAY=0.05 KEEPALIVE=1 GAP=1.0 ./run_matrix.sh burst

# For bisect_pool.sh: a scratch venv whose files are NOT hardlinked to the cache
uv venv .venv-prefix
UV_LINK_MODE=copy uv pip install --python .venv-prefix/bin/python \
    --no-cache httpx2==2.12.0 numpy
git clone https://github.com/pydantic/httpx2 /tmp/httpx2
PORT=8021 ./bisect_pool.sh /tmp/httpx2
```

`run_matrix.sh` runs each client sequentially against a single server on
purpose: running two clients concurrently starves the event loop and produces
failure counts that are artifacts of CPU contention rather than of the bug.
