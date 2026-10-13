import logging

from fastapi import FastAPI

from app.config import get_settings

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper())

app = FastAPI(title="ChangeWatch Crypto")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
