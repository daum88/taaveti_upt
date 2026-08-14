"""Uvicorn entrypoint for the Taaveti portfolio simulator web application."""

import uvicorn

from adapters.web.app import create_app
from config import SERVER_HOST, SERVER_PORT

app = create_app()


def run_server() -> None:
    """Run the configured FastAPI application with Uvicorn."""
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")


if __name__ == "__main__":
    run_server()
