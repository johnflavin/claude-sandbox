"""Idle keep-alive reuse test.

The issue's workload keeps every pooled connection continuously busy, so a
connection is never idle long enough to be reaped. This harness instead sends
bursts separated by an idle gap, which is what actually exercises the
"reuse a connection the server has already closed" path.
"""

import argparse
import asyncio
import base64
import collections
import os
import time

import numpy as np

PORT = int(os.environ.get("PORT", "8000"))
SERVER_URL = f"http://localhost:{PORT}/process_image"

IMAGE = base64.b64encode(
    np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8).tobytes()
).decode()


def tally(results):
    failures = collections.Counter()
    for result in results:
        if isinstance(result, BaseException):
            failures[type(result).__module__ + "." + type(result).__name__] += 1
    return failures


async def burst_httpx2(args):
    import importlib

    httpx2 = importlib.import_module(args.client)

    limits = httpx2.Limits(
        max_connections=args.conns,
        max_keepalive_connections=args.max_keepalive,
        keepalive_expiry=args.keepalive_expiry,
    )

    async def send(client):
        response = await client.post(SERVER_URL, json={"image": IMAGE})
        return response.json()

    failures = collections.Counter()
    async with httpx2.AsyncClient(limits=limits, timeout=args.timeout) as client:
        for i in range(args.bursts):
            results = await asyncio.gather(
                *[send(client) for _ in range(args.conns)], return_exceptions=True
            )
            failures += tally(results)
            if i != args.bursts - 1:
                await asyncio.sleep(args.gap)
    return failures


async def burst_aiohttp(args):
    import aiohttp

    connector = aiohttp.TCPConnector(
        limit=args.conns, keepalive_timeout=args.keepalive_expiry
    )

    async def send(session):
        async with session.post(SERVER_URL, json={"image": IMAGE}) as response:
            return await response.json()

    failures = collections.Counter()
    async with aiohttp.ClientSession(
        connector=connector, timeout=aiohttp.ClientTimeout(total=args.timeout)
    ) as session:
        for i in range(args.bursts):
            results = await asyncio.gather(
                *[send(session) for _ in range(args.conns)], return_exceptions=True
            )
            failures += tally(results)
            if i != args.bursts - 1:
                await asyncio.sleep(args.gap)
    return failures


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=["httpx2", "httpx", "aiohttp"], required=True)
    parser.add_argument("--conns", type=int, default=300)
    parser.add_argument("--bursts", type=int, default=6)
    parser.add_argument("--gap", type=float, default=1.2)
    parser.add_argument("--max-keepalive", type=int, default=None)
    parser.add_argument("--keepalive-expiry", type=float, default=60.0)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()

    runner = burst_aiohttp if args.client == "aiohttp" else burst_httpx2
    started = time.monotonic()
    failures = runner(args)
    failures = await failures
    elapsed = time.monotonic() - started

    total = args.conns * args.bursts
    failed = sum(failures.values())
    summary = ", ".join(f"{n} x {k}" for k, n in failures.most_common()) or "none"
    print(
        f"[{args.client} conns={args.conns} bursts={args.bursts} gap={args.gap}s "
        f"client_keepalive={args.keepalive_expiry}s] "
        f"{total - failed}/{total} ok in {elapsed:.1f}s | failures: {summary}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
