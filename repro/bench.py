"""Instrumented harness for httpx2 issue #852.

Runs the issue's workload with either client, counting failures by exception
type instead of aborting on the first one, so runs can be compared directly.
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


def payload() -> str:
    image = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
    return base64.b64encode(image.tobytes()).decode()


async def run_httpx2(args, image_data):
    import importlib

    httpx2 = importlib.import_module(args.client)

    limits = httpx2.Limits(
        max_connections=args.max_connections,
        max_keepalive_connections=args.max_keepalive,
        keepalive_expiry=args.keepalive_expiry,
    )

    async def send_request(client):
        response = await client.post(SERVER_URL, json={"image": image_data})
        return response.json()

    async with httpx2.AsyncClient(
        limits=limits, verify=False, timeout=args.timeout
    ) as client:
        tasks = [send_request(client) for _ in range(args.total)]
        return await asyncio.gather(*tasks, return_exceptions=True)


async def run_aiohttp(args, image_data):
    import aiohttp

    connector = aiohttp.TCPConnector(limit=args.max_connections)

    async def send_request(session):
        async with session.post(SERVER_URL, json={"image": image_data}) as response:
            return await response.json()

    async with aiohttp.ClientSession(
        connector=connector, timeout=aiohttp.ClientTimeout(total=args.timeout)
    ) as session:
        tasks = [send_request(session) for _ in range(args.total)]
        return await asyncio.gather(*tasks, return_exceptions=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", choices=["httpx2", "httpx", "aiohttp"], required=True)
    parser.add_argument("--max-connections", type=int, default=300)
    parser.add_argument("--max-keepalive", type=int, default=None)
    parser.add_argument("--keepalive-expiry", type=float, default=5.0)
    parser.add_argument("--total", type=int, default=1000)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--trials", type=int, default=1)
    args = parser.parse_args()

    image_data = payload()
    runner = run_aiohttp if args.client == "aiohttp" else run_httpx2

    for trial in range(1, args.trials + 1):
        started = time.monotonic()
        results = await runner(args, image_data)
        elapsed = time.monotonic() - started

        failures = collections.Counter()
        for result in results:
            if isinstance(result, BaseException):
                failures[type(result).__module__ + "." + type(result).__name__] += 1

        total_failed = sum(failures.values())
        summary = ", ".join(f"{n} x {k}" for k, n in failures.most_common()) or "none"
        print(
            f"[{args.client} conns={args.max_connections} "
            f"keepalive={args.max_keepalive}] trial {trial}: "
            f"{args.total - total_failed}/{args.total} ok in {elapsed:.1f}s "
            f"| failures: {summary}",
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
