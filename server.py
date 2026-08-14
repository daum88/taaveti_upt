"""Uvicorn entrypoint for the Taaveti portfolio simulator web application."""

import uvicorn

from adapters.web.app import create_app
from settings import load_settings

settings = load_settings()
app = create_app(settings=settings)


def run_server() -> None:
    """Run the configured FastAPI application with Uvicorn."""
    uvicorn.run(app, host=settings.server_host, port=settings.server_port, log_level="info")


if __name__ == "__main__":
    run_server()
