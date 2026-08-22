"""Does `await client.post(...)` vs `async with` explain issue #852?

The issue's httpx client uses `await client.post(...)` while its aiohttp client
uses `async with session.post(...)`. This runs the *same* httpx workload in
both styles to see whether the calling style changes the failure rate.

  style=await   : response = await client.post(...)          (as filed)
  style=stream  : async with client.stream("POST", ...) as r (explicit release)
"""

import asyncio
import base64
import collections
import importlib
import os
import sys

import numpy as np

WHICH = os.environ.get("MOD", "httpx")
STYLE = sys.argv[1] if len(sys.argv) > 1 else "await"
httpx = importlib.import_module(WHICH)

PORT = int(os.environ.get("PORT", "8010"))
SERVER_URL = f"http://localhost:{PORT}/process_image"
CONNS = int(os.environ.get("CONNS", "300"))
TOTAL = int(os.environ.get("TOTAL", "1000"))

IMAGE = base64.b64encode(
    np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8).tobytes()
).decode()


async def send_await(client):
    response = await client.post(SERVER_URL, json={"image": IMAGE})
    return response.json()


async def send_stream(client):
    async with client.stream("POST", SERVER_URL, json={"image": IMAGE}) as response:
        await response.aread()
        return response.json()


async def main():
    send = send_await if STYLE == "await" else send_stream
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=CONNS), verify=False, timeout=30
    ) as client:
        results = await asyncio.gather(
            *[send(client) for _ in range(TOTAL)], return_exceptions=True
        )

    failures = collections.Counter(
        type(r).__name__ for r in results if isinstance(r, BaseException)
    )
    ok = TOTAL - sum(failures.values())
    print(f"  {WHICH:6s} style={STYLE:6s} -> {ok}/{TOTAL} ok | failures: {dict(failures) or 'none'}")


if __name__ == "__main__":
    asyncio.run(main())
