# Review of pydantic/httpx2#1153

"Serialize h2 state machine access for thread-safe HTTP/2 connections"
(`61cbed3`, 6 commits), reviewed against `origin/main` (`5465b4e`) and against
the fix in `fix.patch` here.

**Bottom line: it fixes the bug, and in several respects it is a better
restructuring than mine. One thing I would block on: it introduces an async
regression where a cancelled task can silently discard frames it has already
read off the socket.** That regression comes directly from its central design
choice — reusing `_write_lock` (a real async lock) as the h2 mutex, rather than
a lock that is never held across I/O.

## Does it work?

Yes. Against the reproductions in this directory (unpatched `main` fails
essentially every request in both):

| | main | PR #1153 | fix.patch |
| --- | --- | --- | --- |
| `repro_get.py 32 2000` | 1998 failures | 0 | 0 |
| `repro_post.py 16 200` | 200 failures | 0 | 0 |
| `pytest tests/httpcore2` | 235 passed | 237 passed | 236 passed |
| its own concurrency test | fails 5/10 runs | passes | passes |
| `test_http2_threading.py` (mine) | fails 10/10 runs | n/a (API differs) | passes |

Throughput is a wash — 16 threads, one connection, server in a separate
process, 500 requests of 64 KB each (3 runs, best/median):

| | GET rps | POST rps |
| --- | --- | --- |
| PR #1153 | 838 / 824 | 536 / 498 |
| fix.patch | 791 / 781 | 490 / 471 |

So the coarser lock does not cost measurable throughput on loopback, and I would
not argue against it on performance grounds.

## Coverage compared with mine

The two fixes serialize the same critical sections, and both get the central
point right: **stream ID allocation and `send_headers` in one atomic section**.
Also covered by both: `send_data` + the flow-control check, `end_stream`,
`acknowledge_received_data`, `receive_data`, `data_to_send`, and handing back
the stream slot when a request fails before a stream exists.

Things the PR does that mine does not, and that I'd adopt:

* Folding `_wait_for_outgoing_flow` into `_send_stream_data` so the window check
  and the `send_data` that consumes it are one section. Mine keeps the old
  helper and re-checks the window under the lock, which is correct but does a
  redundant loop iteration. Theirs is tighter.
* Moving `_receive_remote_settings_change` out of the locked section — it can
  block on the semaphore and it runs a user trace callback. (Mine doesn't need
  this, since my lock only wraps `receive_data`, but the reasoning is right.)
* Keeping trace callbacks outside the lock generally, `_read_incoming_data`
  returning bytes instead of events, `_flush_outgoing_data` documented as
  "must hold the write lock", and a CHANGELOG entry. Mine had no changelog entry.

Things mine covers that the PR does not:

* `close()` still calls `self._h2_state.close_connection()` with no lock, so a
  connection being closed (pool eviction, or `_response_closed` after
  `ConnectionTerminated`) can mutate the state machine and the outgoing buffer
  while another thread is inside the write lock encoding headers. Narrow, and
  pre-existing on `main`, but it is the one remaining unguarded mutation.
* `_send_connection_init`'s `initiate_connection()` / `increment_flow_control_window()`
  are outside the lock — safe in practice because `_init_lock` excludes everyone
  else, so this is a consistency nit rather than a bug.

## The regression: cancelled async task drops frames

`_receive_events` now reads from the network and *then* awaits `_write_lock`
before handing the bytes to h2:

```python
data = await self._read_incoming_data(request)      # bytes are off the socket
async with self._write_lock:                        # <- await point
    events = self._h2_state.receive_data(data)
```

If the task is cancelled while waiting for that lock, the bytes it already
consumed are gone. h2 never sees them, so the connection's framing is corrupted
for **every** stream on it, not just the cancelled one. On `main` there is no
await between the read and `receive_data`, so the pair is atomic with respect to
cancellation. This is reachable from ordinary code: a per-request timeout or a
task group cancelling siblings, on an async client multiplexing several requests
over one connection — precisely when the write lock is contended.

`test_cancel_loss.py` in this directory demonstrates it (drop it into
`tests/httpcore2/`, it runs on both the asyncio and trio backends):

```
main       frames read after cancellation point: 1, events recorded: 1   PASS
fix.patch  frames read after cancellation point: 1, events recorded: 1   PASS
PR #1153   frames read after cancellation point: 1, events recorded: 0   FAIL
```

The sync path is unaffected — `threading.Lock.acquire()` is not a cancellation
point — so this is purely a cost of routing the async path through the same
async lock. Which is the deeper point: **async never needed this serialization
at all.** Within one event loop these regions contain no `await`, so tasks
cannot interleave inside them; the PR's own description says as much ("async
locks remain no-op under single event loop"), but `_write_lock` is not a no-op
in async. Using `AsyncThreadLock`/`ThreadLock` (as `connection_pool.py` already
does for exactly this "no-I/O critical section" case) gives thread safety in
sync, costs async nothing, and makes the cancellation hazard structurally
impossible. If the author prefers to keep `_write_lock`, the read and
`receive_data` need to be re-joined — either both inside the lock, or by
stashing unprocessed bytes so a later call can feed them.

A second, milder consequence of the same choice: `_receive_events` now takes
`_write_lock` *while holding* `_read_lock`. While any thread is blocked in
`network_stream.write`, no other thread can drain the socket at all. `main`
already gates readers on the write lock for their trailing flush, so this
narrows an existing window rather than opening a new one — but with a peer that
stops reading, "nobody may read" is a worse place to be than "one reader may be
mid-read".

## Smaller notes

* **The new test is a weak regression guard.** On this machine it fails on
  unpatched code in only 5 of 10 runs, so a future regression can land green;
  the PR description's "fails deterministically on main" doesn't hold here.
  Making the race deterministic is cheap — delay `send_headers` (e.g. patch it
  to `time.sleep(0.05)` first) and assert the four threads get stream IDs
  1, 3, 5, 7; that fails 10/10 on `main`. Worth adding alongside the
  end-to-end test, which is otherwise the better-integrated of the two (it
  covers sync and async through the public API, which mine does not).
* `stream_ids_exhausted` as a flag, then `if stream_ids_exhausted: raise` inside
  the generic handler, is awkward; a dedicated
  `except h2.exceptions.NoAvailableStreamIDError:` clause says the same thing
  directly.
* The pre-allocation failure path re-implements `_response_closed`'s
  ACTIVE→IDLE + keepalive logic inline, and omits its "connection terminated and
  no streams left → close()" case. Letting `_response_closed` take
  `stream_id: int | None` keeps one code path and picks that case up for free.
* `docs/extensions.md` documents the `http2.send_request_headers` trace kwargs;
  `stream_id` is no longer present on the `.started` event (true of my version
  too). Worth a line in the docs or changelog.
