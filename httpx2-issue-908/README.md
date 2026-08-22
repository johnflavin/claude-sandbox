# httpx2 #908 — `RuntimeError: deque mutated during iteration`

Investigation of https://github.com/pydantic/httpx2/issues/908 against
`pydantic/httpx2` at `5465b4e` (Python 3.11, `h2` + `hpack` from the repo's
`uv.lock`).

**Status: still reproducible on `main`.**

## Reproducing

Each script starts an in-process h2c server (prior-knowledge HTTP/2, no TLS) and
drives it with a thread pool sharing a single `httpx2.Client` limited to one
connection, so every request is multiplexed onto the same HTTP/2 connection —
the same shape as the issue reporter's `concurrent.futures` + `backoff` code.
`sys.setswitchinterval(1e-6)` only makes the GIL preempt more often; it does not
create the race, it just stops it from being a once-a-week production mystery.

Run them from a checkout of `pydantic/httpx2`:

```
uv run python repro_get.py 32 2000     # GET, 32 threads, 2000 requests
uv run python repro_post.py 16 200     # POST with 300 KB bodies
uv run python repro_h2_level.py        # no httpx2: h2/hpack used the way httpcore2 uses it
```

Unpatched `main`, `repro_post.py`:

```
requests: 200, failures: 200
   107  RuntimeError: deque mutated during iteration
    60  ReadTimeout: timed out
     9  LocalProtocolError: Trailers must have END_STREAM set.
     6  LocalProtocolError: Received pseudo-header in trailer {b':method', ...}
     4  RuntimeError: dictionary changed size during iteration
```

The reported traceback appears verbatim, raised out of `client.get(...)` via
`hpack/table.py:184` in `HeaderTable.search`. The other errors are the same race
seen from different angles; once the HPACK table or the h2 stream table is
corrupted, every later request on that connection fails too.

## Cause

`httpcore2`'s `HTTP2Connection` shares one `h2.connection.H2Connection` — and
the single HPACK encoder inside it — between every request multiplexed on the
connection. Its locks only cover network I/O (`_read_lock`, `_write_lock`) and
connection bookkeeping (`_state_lock`). The calls into the h2 state machine
itself are unguarded, so two threads run them at the same time:

* `_send_request_headers` → `h2_state.send_headers()` → HPACK encode. Thread A
  is iterating `self.dynamic_entries` in `HeaderTable.search` while thread B
  calls `add_entry` and `appendleft`s onto the same deque → **`RuntimeError:
  deque mutated during iteration`**. This is the reported failure; `hpack` is
  simply not thread-safe, which is what python-hyper/hpack#275 and #281 say.
* `handle_request` calls `h2_state.get_next_available_stream_id()` and only
  later sends the headers. h2 advances that counter when the headers are *sent*,
  so two threads are handed the **same stream ID** → `Invalid input
  StreamInputs.SEND_HEADERS in state 5`, `StreamIDTooLowError`, `Received
  pseudo-header in trailer` (two header blocks land on one stream).
* `receive_data`, `send_data`, `end_stream`, `acknowledge_received_data`,
  `data_to_send` and `close_connection` all mutate the same state machine and
  `streams` dict concurrently → `RuntimeError: dictionary changed size during
  iteration`, flow-control errors.

It is not specific to `backoff`; retries just increase the number of concurrent
requests landing on one connection. The issue reporter's `ConnectionTerminated`
errors are a symptom, not the trigger. Async is unaffected: within one event
loop these regions contain no `await`, so they can't interleave.

Note that `src/httpcore2/docs/connection-pools.md` states "Connection pools are
designed to be thread-safe", so this is a bug rather than unsupported usage.

## Fix

`fix.patch` (against `5465b4e`; edit `_async/http2.py`, then
`python scripts/unasync.py` regenerates `_sync/http2.py`).

It adds a `ThreadLock` (`AsyncThreadLock` unasyncs to it, and is a no-op under
async, so async pays nothing) guarding every interaction with `self._h2_state`.
Two details matter:

1. **Allocating the stream ID and encoding the headers happen in one critical
   section.** `_send_request_headers` now allocates the ID itself and returns
   it, rather than being handed one that another thread may also hold. This also
   guarantees header blocks are HPACK-encoded in stream ID order, as HTTP/2
   requires. The network flush moved to the caller so the stream ID is available
   for cleanup if the write fails (this keeps the cancellation behaviour that
   `tests/httpcore2/test_cancellations.py` checks).
2. **The lock is never held across I/O.** `_write_outgoing_data` takes the write
   lock first and only then the h2 lock to drain `data_to_send()`, so frames
   still reach the network in generation order; the network write happens
   outside the h2 lock. `_send_stream_data` re-checks the flow-control window
   under the lock before `send_data`, since another stream may have consumed it
   after `_wait_for_outgoing_flow` returned.

The patch also adds `tests/httpcore2/test_http2_threading.py`, which delays
`send_headers` and asserts four concurrent threads get stream IDs `1, 3, 5, 7`.
It fails on `main` (`h2.exceptions.ProtocolError: Invalid input
StreamInputs.SEND_HEADERS in state 5`) and passes with the fix.

## Verification

| | `main` | patched |
| --- | --- | --- |
| `repro_get.py 32 2000` | 1998/2000 failed | 0 failed |
| `repro_post.py 16 200` | 200/200 failed | 0 failed |
| `pytest tests/httpcore2` | 235 passed | 236 passed (incl. new test) |
| `pytest tests` | 41 failures | identical 41 (pre-existing, sandbox proxy/env tests) |
| `ruff format` / `ruff check` / `mypy` / `unasync --check` | — | clean |

The remaining `hpack` non-thread-safety is unchanged upstream; this fix works
around it by never letting two threads into the encoder at once, which is the
only thing httpcore2 can do from its side.
