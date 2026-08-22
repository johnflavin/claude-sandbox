"""
Does a cancellation between "read bytes off the socket" and "feed them to h2"
lose those bytes?

On main (and with the ThreadLock approach) `_read_incoming_data` reads and then
calls `h2_state.receive_data(...)` with no await in between, so the pair is
atomic with respect to cancellation. PR #1153 moves `receive_data` behind
`async with self._write_lock`, which is an await point.
"""
import anyio
import hpack
import hyperframe.frame
import pytest

import httpcore2


class ReplayStream(httpcore2.AsyncNetworkStream):
    def __init__(self, buffer):
        self._buffer = list(buffer)
        self.reads = 0

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if not self._buffer:
            await anyio.sleep(999)
        self.reads += 1
        return self._buffer.pop(0)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        pass

    async def aclose(self) -> None:
        pass


@pytest.mark.anyio
async def test_cancellation_between_read_and_receive_data() -> None:
    encoder = hpack.Encoder()
    buffer = [
        hyperframe.frame.SettingsFrame().serialize(),
        hyperframe.frame.HeadersFrame(
            stream_id=1,
            data=encoder.encode([(b":status", b"200"), (b"content-type", b"plain/text")]),
            flags=["END_HEADERS"],
        ).serialize(),
        hyperframe.frame.DataFrame(stream_id=1, data=b"Hello, world!", flags=["END_STREAM"]).serialize(),
    ]
    origin = httpcore2.Origin(b"https", b"example.com", 443)
    stream = ReplayStream(buffer)
    conn = httpcore2.AsyncHTTP2Connection(origin=origin, stream=stream)
    request = httpcore2.Request("GET", "https://example.com/", headers={"Host": "example.com"})
    await conn._send_connection_init(request)

    # Open stream 1 for real, so that the HEADERS frame the server sends back
    # for it is legitimate. (Done via h2 directly so that the test runs
    # unchanged against every variant of `_send_request_headers`.)
    async with conn._write_lock:
        stream_id = conn._h2_state.get_next_available_stream_id()
        assert stream_id == 1
        conn._events[stream_id] = []
        conn._h2_state.send_headers(
            stream_id,
            [(b":method", b"GET"), (b":authority", b"example.com"), (b":scheme", b"https"), (b":path", b"/")],
            end_stream=True,
        )
        conn._h2_state.data_to_send()

    # Consume the server's SETTINGS frame first, so that the read we're
    # interested in is the one that returns the response HEADERS frame.
    await conn._receive_events(request, stream_id=1)
    assert conn._events[1] == []

    holding = anyio.Event()
    release = anyio.Event()

    async def hold_write_lock() -> None:
        # Stand in for another task that is stuck writing to the network.
        async with conn._write_lock:
            holding.set()
            await release.wait()

    async with anyio.create_task_group() as tg:
        tg.start_soon(hold_write_lock)
        await holding.wait()
        # Our task reads a frame off the network, then gets cancelled while
        # waiting for that same lock.
        with anyio.move_on_after(0.05):
            await conn._receive_events(request, stream_id=1)
        release.set()

    frames_read = stream.reads - 1  # not counting the SETTINGS frame
    events_recorded = len(conn._events[1])
    print(f"\nframes read after cancellation point: {frames_read}, events recorded: {events_recorded}")
    assert frames_read == 1, "the test did not get as far as reading the HEADERS frame"
    # If the frame that was read never reached h2, it is gone for good: the
    # response it carried can never be delivered.
    assert events_recorded == 1, "the HEADERS frame read from the network was discarded by the cancellation"
