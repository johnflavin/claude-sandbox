"""aiohttp client from issue #852."""

import asyncio
import base64
import os

import aiohttp
import numpy as np

PORT = int(os.environ.get("PORT", "8000"))
SERVER_URL = f"http://localhost:{PORT}/process_image"
CONCURRENT_REQUESTS = 300
TOTAL_REQUESTS = 1000
TIMEOUT = 30


async def send_request(session, image_data):
    async with session.post(SERVER_URL, json={"image": image_data}) as response:
        return await response.json()


async def main():
    image = np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
    image_data = base64.b64encode(image.tobytes()).decode()

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=TIMEOUT)
    ) as session:
        tasks = [send_request(session, image_data) for _ in range(TOTAL_REQUESTS)]
        results = await asyncio.gather(*tasks)

    for i, result in enumerate(results, 1):
        print(f"Response {i}:", result)


if __name__ == "__main__":
    asyncio.run(main())
