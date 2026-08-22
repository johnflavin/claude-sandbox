"""
Reproduce pydantic/httpx2#908: RuntimeError: deque mutated during iteration

Many threads share one httpx2.Client with HTTP/2 enabled. All requests are
multiplexed onto a single HTTP/2 connection, so all threads end up inside
h2's `send_headers()` -> hpack Encoder concurrently.
"""

import socket
import sys

sys.setswitchinterval(1e-6)
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import h2.config
import h2.connection
import h2.events

import httpx2

NUM_THREADS = int(sys.argv[1]) if len(sys.argv) > 1 else 16
NUM_REQUESTS = int(sys.argv[2]) if len(sys.argv) > 2 else 400


def serve(sock: socket.socket) -> None:
    """Minimal h2c (prior knowledge, no TLS) server. Answers every request 200."""
    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False))
    conn.initiate_connection()
    conn.local_settings.max_concurrent_streams = 1000
    sock.sendall(conn.data_to_send())
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                return
            for event in conn.receive_data(data):
                if isinstance(event, h2.events.RequestReceived):
                    conn.send_headers(
                        event.stream_id,
                        [(b":status", b"200"), (b"content-type", b"text/plain"), (b"content-length", b"2")],
                    )
                    conn.send_data(event.stream_id, b"ok", end_stream=True)
            out = conn.data_to_send()
            if out:
                sock.sendall(out)
    except Exception:
        pass


def server_thread(listener: socket.socket) -> None:
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=serve, args=(client,), daemon=True).start()


listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", 0))
listener.listen(50)
port = listener.getsockname()[1]
threading.Thread(target=server_thread, args=(listener,), daemon=True).start()

errors: list[BaseException] = []

# http1=False + http2=True => prior-knowledge HTTP/2 over plain TCP.
client = httpx2.Client(
    http1=False,
    http2=True,
    limits=httpx2.Limits(max_connections=1, max_keepalive_connections=1),
)


def do_request(i: int) -> None:
    try:
        # Vary a header value so hpack keeps inserting into / searching the
        # dynamic table on every request.
        headers = {f"x-req-{j}": f"value-{i}-{j}" for j in range(40)}
        r = client.get(f"http://127.0.0.1:{port}/path-{i}", headers=headers)
        assert r.status_code == 200, r.status_code
    except BaseException as exc:  # noqa: BLE001
        errors.append(exc)


with ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
    list(pool.map(do_request, range(NUM_REQUESTS)))

print(f"requests: {NUM_REQUESTS}, failures: {len(errors)}")
seen: dict[str, int] = {}
for exc in errors:
    key = f"{type(exc).__name__}: {exc}"
    seen[key] = seen.get(key, 0) + 1
for key, count in sorted(seen.items(), key=lambda kv: -kv[1]):
    print(f"  {count:4d}  {key}")
if errors:
    print("\n--- first traceback ---")
    traceback.print_exception(type(errors[0]), errors[0], errors[0].__traceback__)
    sys.exit(1)
