Nice work — the core of this is right, and the stream-ID/`send_headers` atomicity
is exactly the fix. I ran it against a threaded reproduction of #908 (real h2c
server, one shared connection) and it takes it from ~100% failures to zero, with
no measurable throughput cost. One thing I'd raise before it lands, and it's
about the choice of lock rather than any individual line.

## Why the lock choice matters

There are two different kinds of critical section in this file, and they have
opposite requirements:

* **Protecting the h2 state machine.** Pure CPU work, microseconds long, and it
  must be *indivisible* — the invariant is "bytes that left the socket have been
  handed to h2", "the stream ID we were given is the one we send headers on".
* **Serializing the socket.** Held across `await network_stream.write(...)`.
  Long, blocking, and — this is the part that bites — *suspendable*.

`_write_lock` is the second kind. Reusing it as the h2 mutex makes every h2
critical section inherit the properties of an I/O lock: in async, acquiring a
contended lock is an `await`, and an `await` is a cancellation point. So each
place the PR wraps h2 state in `_write_lock` becomes a place a task can be
suspended and torn out mid-invariant. I think that's also visible in the commit
history here — several of the follow-up commits (restoring idle state, cleaning
up pre-allocation state, narrowing the `NoAvailableStreamIDError` handling) are
repairs for states that only became reachable once `handle_request` grew a
suspension point between acquiring the semaphore and allocating the stream.

The second half of the argument: **the async path never needed this lock.**
The bug is a thread-safety bug, not a task-safety bug. Within one event loop
these regions contain no `await`, so tasks cannot interleave inside them — the
PR description says as much ("async locks remain no-op under single event
loop"), but `_write_lock` is a real lock in async, so async pays the cost and
takes on the new failure mode without getting anything back.

`httpcore` already has a primitive for precisely this case, and
`connection_pool.py` already uses it:

```python
class AsyncThreadLock:
    """
    This is a threading-only lock for no-I/O contexts.

    In the sync case `ThreadLock` provides thread locking.
    In the async case `AsyncThreadLock` is a no-op.
    """
```

Choosing it isn't a micro-optimization — it's a statement that the section
contains no I/O, and it makes the cancellation hazard structurally impossible
rather than something reviewers have to keep re-checking.

## The concrete failure

`_receive_events` now reads from the network and *then* awaits the lock before
handing the bytes to h2:

```python
data = await self._read_incoming_data(request)   # bytes are off the socket
async with self._write_lock:                     # <-- await == cancellation point
    events: list[h2.events.Event] = self._h2_state.receive_data(data)
```

If the task is cancelled while waiting for that lock, the bytes it already
consumed are gone. h2 never sees them, so framing is corrupted for **every**
stream on that connection, not just the cancelled one. On `main` there is no
`await` between the read and `receive_data`, so the pair is atomic with respect
to cancellation. This is reachable from ordinary code — a per-request timeout,
or a task group cancelling siblings, on an async client multiplexing over one
connection, i.e. exactly when the write lock is contended.

Test below (drop it in `tests/httpcore2/`); it opens a stream, lets another task
hold the write lock, and cancels a `_receive_events` call while it waits:

| | asyncio | trio |
| --- | --- | --- |
| `main` | frame recorded — PASS | PASS |
| this PR | frame read and discarded — FAIL | FAIL |

The sync path is unaffected, since `threading.Lock.acquire()` isn't a
cancellation point. That asymmetry is itself the tell: the regression exists
only in the half of the codebase that didn't have the bug.

## Options

1. Use `AsyncThreadLock`/`ThreadLock` for the h2-state sections and leave
   `_write_lock` owning the socket. Async goes back to a no-op, sync gets the
   serialization it needs, and read/`receive_data` stays atomic. Ordering is
   still safe: `data_to_send()` drains FIFO under the write lock, so wire order
   still matches encode order.
2. Keep `_write_lock`, but re-join the read and `receive_data` — either both
   under the lock, or by stashing unconsumed bytes on the connection so a later
   call feeds them to h2 instead of dropping them.

Happy to open a PR with either if that's useful.
