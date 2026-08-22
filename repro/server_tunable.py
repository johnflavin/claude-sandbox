"""Same endpoint as server.py, with the response delay and uvicorn's
keep-alive timeout exposed as env vars, plus a /stats endpoint reporting the
peak number of concurrently in-flight requests the server actually saw.
"""

import asyncio
import os

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI()

PORT = int(os.environ.get("PORT", "8001"))
DELAY = float(os.environ.get("DELAY", "3"))
KEEPALIVE = int(os.environ.get("KEEPALIVE", "5"))

_inflight = 0
_peak = 0
_served = 0


class ImagePayload(BaseModel):
    image: str = Field(..., description="Base64 encoded image")


@app.post("/process_image")
async def process_image(payload: ImagePayload):
    global _inflight, _peak, _served
    _inflight += 1
    _peak = max(_peak, _inflight)
    try:
        if DELAY:
            await asyncio.sleep(DELAY)
        _served += 1
        return {"message": "Image processed successfully after a delay"}
    finally:
        _inflight -= 1


@app.get("/stats")
async def stats():
    return {"peak_concurrency": _peak, "served": _served, "inflight": _inflight}


@app.post("/reset")
async def reset():
    global _peak, _served
    _peak = _inflight
    _served = 0
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="warning",
        timeout_keep_alive=KEEPALIVE,
    )
