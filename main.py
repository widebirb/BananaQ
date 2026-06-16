"""
BananaQ — entry point.

Starts the FastAPI application with uvicorn.
"""
from __future__ import annotations

import logging

import uvicorn
from fastapi import FastAPI

from api.webhook import router as webhook_router
from config.settings import get_settings

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="BananaQ 🍌",
    description="AI-powered pull request reviewer agent",
    version="0.1.0",
)

app.include_router(webhook_router)


@app.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    return {"status": "ok", "service": "BananaQ"}


# ---------------------------------------------------------------------------
# Dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    settings = get_settings()
    logger.info("Starting BananaQ on %s:%d", settings.host, settings.port)
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
