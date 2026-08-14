"""
FastAPI WebSocket Server — real-time fintech trading dashboard.
Serves SPA, streams market data, agent reasoning, and gatekeeper alerts.
"""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from adapters.sqlite.connection import init_db
from adapters.web.errors import http_exception_response, unexpected_error_response, validation_error_response
from adapters.web.routers import agents, dashboard, decisions, instruments, operations, trades
from adapters.web.runtime import AppRuntime
from adapters.web.serialization import json_default as _json_default
from application.agent_commands import AgentCommands
from application.instrument_commands import InstrumentCommands
from application.portfolio_queries import PortfolioQueries
from application.simulation_operations import SimulationOperations
from application.trading import Trading
from config import ETF_UNIVERSE_ENABLED, SERVER_HOST, SERVER_PORT


class DecimalJSONResponse(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(content, default=_json_default, ensure_ascii=False).encode("utf-8")


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("server")


WEB_DIR = Path(__file__).parents[2] / "ui" / "web"
STATIC_DIR = WEB_DIR / "assets"
WEB_DIR.mkdir(parents=True, exist_ok=True)

router = APIRouter()


# ── Lifespan ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from services.comparison_profiles import seed_comparison_profiles

    seed_comparison_profiles()
    from services.committee_profile import seed_investment_committee

    seed_investment_committee()
    from services.instrument_universe import import_etf_catalogue

    import_etf_catalogue(active=ETF_UNIVERSE_ENABLED)
    app_runtime: AppRuntime = app.state.runtime
    await app_runtime.start()
    logger.info("Server started — http://%s:%s", SERVER_HOST, SERVER_PORT)
    try:
        yield
    finally:
        await app_runtime.stop()


# ── HTML ─────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(
        (WEB_DIR / "index.html").read_text(),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


@router.get("/favicon.svg", include_in_schema=False)
async def favicon():
    return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")


# ── WebSocket ────────────────────────────────────────────
@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    app_runtime = ws.app.state.runtime
    await app_runtime.serve_websocket(
        ws,
        health_payload=ws.app.state.simulation_operations.health,
        json_default=_json_default,
    )


# ── Run ──────────────────────────────────────────────────
def create_app(
    runtime: AppRuntime | None = None,
    trading: Trading | None = None,
    portfolio_queries: PortfolioQueries | None = None,
    instrument_commands: InstrumentCommands | None = None,
    simulation_operations: SimulationOperations | None = None,
    agent_commands: AgentCommands | None = None,
) -> FastAPI:
    """Create an independently lifecycle-managed FastAPI application."""
    app = FastAPI(
        title="Portfolio Simulator",
        version="0.1.0",
        lifespan=lifespan,
        default_response_class=DecimalJSONResponse,
    )

    @app.middleware("http")
    async def add_security_headers(_: Request, call_next):
        response = await call_next(_)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; connect-src 'self' ws: wss:; font-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; img-src 'self' data:; script-src 'self'; style-src 'self'"
        )
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    app.add_exception_handler(HTTPException, http_exception_response)
    app.add_exception_handler(RequestValidationError, validation_error_response)
    app.add_exception_handler(Exception, unexpected_error_response)
    app_runtime = runtime or AppRuntime(portfolio_queries=portfolio_queries)
    app.state.runtime = app_runtime
    app.state.trading = trading or Trading()
    app.state.portfolio_queries = portfolio_queries or app_runtime.portfolio_queries
    app.state.agent_commands = agent_commands or AgentCommands()
    app.state.instrument_commands = instrument_commands or InstrumentCommands()
    app.state.simulation_operations = simulation_operations or SimulationOperations(
        app_runtime.market_refresh_scheduler
    )
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
    app.include_router(router)
    app.include_router(agents.router)
    app.include_router(dashboard.router)
    app.include_router(decisions.router)
    app.include_router(instruments.router)
    app.include_router(operations.router)
    app.include_router(trades.router)
    return app
