"""Same as repro.py but with large request bodies, to exercise concurrent
HTTP/2 flow-control and DATA frame sending on a shared connection."""
import socket, sys, threading, traceback
sys.setswitchinterval(1e-6)
from concurrent.futures import ThreadPoolExecutor

import h2.config, h2.connection, h2.events
import httpx2

NUM_THREADS = int(sys.argv[1]) if len(sys.argv) > 1 else 16
NUM_REQUESTS = int(sys.argv[2]) if len(sys.argv) > 2 else 200
BODY = b"x" * 300_000


def serve(sock):
    conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=False))
    conn.initiate_connection()
    conn.local_settings.max_concurrent_streams = 1000
    conn.increment_flow_control_window(2**24)
    sock.sendall(conn.data_to_send())
    ended = set()
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                return
            for event in conn.receive_data(data):
                if isinstance(event, h2.events.DataReceived):
                    conn.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                if isinstance(event, h2.events.StreamEnded) and event.stream_id not in ended:
                    ended.add(event.stream_id)
                    conn.send_headers(event.stream_id, [(b":status", b"200"), (b"content-length", b"2")])
                    conn.send_data(event.stream_id, b"ok", end_stream=True)
            out = conn.data_to_send()
            if out:
                sock.sendall(out)
    except Exception:
        pass


listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", 0)); listener.listen(50)
port = listener.getsockname()[1]

def accept_loop():
    while True:
        c, _ = listener.accept()
        threading.Thread(target=serve, args=(c,), daemon=True).start()
threading.Thread(target=accept_loop, daemon=True).start()

client = httpx2.Client(http1=False, http2=True,
                       limits=httpx2.Limits(max_connections=1, max_keepalive_connections=1),
                       timeout=30.0)
errors = []

def do_request(i):
    try:
        r = client.post(f"http://127.0.0.1:{port}/p{i}", content=BODY,
                        headers={f"x-h-{j}": f"v-{i}-{j}" for j in range(20)})
        assert r.status_code == 200, r.status_code
    except BaseException as exc:
        errors.append(exc)

with ThreadPoolExecutor(max_workers=NUM_THREADS) as pool:
    list(pool.map(do_request, range(NUM_REQUESTS)))

print(f"requests: {NUM_REQUESTS}, failures: {len(errors)}")
kinds = {}
for e in errors:
    k = f"{type(e).__name__}: {e}"
    kinds[k] = kinds.get(k, 0) + 1
for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])[:6]:
    print(f"  {v:4d}  {k}")
if errors:
    traceback.print_exception(type(errors[0]), errors[0], errors[0].__traceback__)
