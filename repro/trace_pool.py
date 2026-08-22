"""Proof-of-mechanism for httpx2 issue #852.

Hypothesis: httpcore 1.0.5's pool has no notion of a connection being
*reserved* between the moment it is assigned to a waiting request and the
moment that request actually sends its first byte. While a connection sits in
that window it still reports `is_idle()`, so a later pass of
`_assign_requests_to_connections` is free to treat it as expired/surplus and
hand it to `_close_connections`. That close is awaited *outside* the pool lock,
so the waiting request can start writing on the socket before it is torn down
-- which surfaces as `httpx.ReadError`.

This script counts exactly that event -- a connection closed while a pool
request still holds it -- for either library:

    python trace_pool.py httpx      # httpx 0.27.0 / httpcore 1.0.5
    python trace_pool.py httpx2     # httpx2 2.12.0 / httpcore2 2.12.0
"""

import asyncio
import base64
import collections
import importlib
import os
import sys

import numpy as np

WHICH = sys.argv[1] if len(sys.argv) > 1 else "httpx"
CORE = {"httpx": "httpcore", "httpx2": "httpcore2"}[WHICH]

httpx = importlib.import_module(WHICH)
httpcore = importlib.import_module(CORE)
pool_mod = importlib.import_module(f"{CORE}._async.connection_pool")

PORT = int(os.environ.get("PORT", "8010"))
SERVER_URL = f"http://localhost:{PORT}/process_image"
CONNS = int(os.environ.get("CONNS", "300"))
TOTAL = int(os.environ.get("TOTAL", "1000"))

EVENTS = collections.Counter()

Pool = pool_mod.AsyncConnectionPool
PoolRequest = pool_mod.AsyncPoolRequest

_original_assign = Pool._assign_requests_to_connections


def _traced_assign(self):
    closing = _original_assign(self)
    if closing:
        reserved = {id(r.connection) for r in self._requests if r.connection is not None}
        for connection in closing:
            EVENTS["closed_total"] += 1
            if id(connection) in reserved:
                EVENTS["closed_while_reserved"] += 1
    return closing


Pool._assign_requests_to_connections = _traced_assign

_original_clear = PoolRequest.clear_connection


def _traced_clear(self):
    EVENTS["connection_not_available_retry"] += 1
    return _original_clear(self)


PoolRequest.clear_connection = _traced_clear


async def send_request(client, image_data):
    response = await client.post(SERVER_URL, json={"image": image_data})
    return response.json()


async def main():
    image_data = base64.b64encode(
        np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8).tobytes()
    ).decode()

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=CONNS), verify=False, timeout=30
    ) as client:
        results = await asyncio.gather(
            *[send_request(client, image_data) for _ in range(TOTAL)],
            return_exceptions=True,
        )

    failures = collections.Counter(
        type(r).__name__ for r in results if isinstance(r, BaseException)
    )

    print(f"{WHICH} {httpx.__version__} / {CORE} {httpcore.__version__}")
    print(f"  {TOTAL} requests @ {CONNS} concurrent")
    print(f"  failures                                : {dict(failures) or 'none'}")
    print(f"  connections closed                      : {EVENTS['closed_total']}")
    print(f"  ...closed while held by a waiting request: {EVENTS['closed_while_reserved']}")
    print(f"  ConnectionNotAvailable retries          : {EVENTS['connection_not_available_retry']}")


if __name__ == "__main__":
    asyncio.run(main())
