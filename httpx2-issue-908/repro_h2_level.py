"""
Isolate the exact failure reported in the issue: concurrent `send_headers()`
on one shared h2 connection (which is exactly what httpcore2's
HTTP2Connection._send_request_headers does, with no lock held).
"""
import sys, threading, traceback
sys.setswitchinterval(1e-9)

import h2.config, h2.connection

conn = h2.connection.H2Connection(config=h2.config.H2Configuration(client_side=True))
conn.initiate_connection()

errors = []
ids = iter(range(1, 200000, 2))
id_lock = threading.Lock()   # stream ids handed out safely, so the ONLY race left is inside h2/hpack
start = threading.Barrier(8)

def worker(n):
    start.wait()
    for i in range(300):
        with id_lock:
            sid = next(ids)
        try:
            conn.send_headers(sid, [
                (b":method", b"GET"), (b":authority", b"example.com"),
                (b":scheme", b"http"), (b":path", f"/p/{n}/{i}".encode()),
                (b"x-req", f"value-{n}-{i}".encode()),
            ], end_stream=True)
        except BaseException as exc:
            errors.append(exc)
            return

threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
[t.start() for t in threads]
[t.join() for t in threads]

print(f"failures: {len(errors)}")
kinds = {}
for e in errors:
    kinds[f"{type(e).__name__}: {e}"] = kinds.get(f"{type(e).__name__}: {e}", 0) + 1
for k, v in kinds.items():
    print(f"  {v:3d}  {k}")
if errors:
    traceback.print_exception(type(errors[0]), errors[0], errors[0].__traceback__)
