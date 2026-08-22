"""httpx client from issue #852, with httpx swapped for httpx2."""

import asyncio
import base64
import os

import httpx2
import numpy as np

PORT = int(os.environ.get("PORT", "8000"))
SERVER_URL = f"http://localhost:{PORT}/process_image"
CONCURRENT_REQUESTS = 300
TOTAL_REQUESTS = 1000
TIMEOUT = 30


async def send_request(client, image_data):
    response = await client.post(SERVER_URL, json={"image": image_data})
    return response.json()


async def main():
    image = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
    image_data = base64.b64encode(image).decode()

    async with httpx2.AsyncClient(
        limits=httpx2.Limits(max_connections=CONCURRENT_REQUESTS),
        verify=False,
        timeout=TIMEOUT,
    ) as client:
        tasks = [send_request(client, image_data) for _ in range(TOTAL_REQUESTS)]
        results = await asyncio.gather(*tasks)

    for i, result in enumerate(results, 1):
        print(f"Response {i}:", result)


if __name__ == "__main__":
    asyncio.run(main())
