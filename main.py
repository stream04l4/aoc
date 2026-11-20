"""Entrypoint for running the AOC API server."""

from __future__ import annotations

import uvicorn

from app.api.app import create_app
from app.core.config import get_settings

app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.api.host,
        port=settings.api.port,
        log_level=settings.logging.level.lower(),
    )


if __name__ == "__main__":
    main()
