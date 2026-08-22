"""FastAPI server from httpx2 issue #852, unchanged apart from the port."""

import asyncio
import os

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI()

PORT = int(os.environ.get("PORT", "8000"))


class ImagePayload(BaseModel):
    image: str = Field(..., description="Base64 encoded image")


@app.post("/process_image")
async def process_image(payload: ImagePayload):
    await asyncio.sleep(3)

    return {"message": "Image processed successfully after a delay"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
