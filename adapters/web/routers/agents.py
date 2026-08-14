"""Agent-management HTTP adapter."""

import json
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

import services.agent_service as agent_service
from adapters.web.errors import service_error_response
from adapters.web.serialization import json_default
from api_models import ChatRequest, CreateAgentRequest
from db.connection import get_db
from db.money import from_e8
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction
from models.user import User
from services.investment_committee import COMMITTEE_ACCOUNT_LABEL, committee_roster
from services.leaderboard import compute_portfolio_snapshot

router = APIRouter(tags=["agents"])


@router.get("/api/agents")
async def list_agents():
    agents = User.llm_agents()
    result = []
    for agent in agents:
        try:
            config = json.loads(agent.strategy_config) if agent.strategy_config else None
        except (ValueError, TypeError):
            config = None
        ensemble = agent.decision_architecture == "multi_model"
        result.append(
            {
                "username": agent.username,
                "display_name": COMMITTEE_ACCOUNT_LABEL if ensemble else agent.username,
                "label": agent.strategy_label,
                "summary": agent.strategy_summary,
                "config": config,
                "decision_architecture": agent.decision_architecture,
                "model_roster": committee_roster()
                if ensemble
                else {"provider": agent.model_provider, "model": agent.model_name},
            }
        )
    return {"agents": result}


@router.post("/api/agents")
async def create_agent(request: Request, data: CreateAgentRequest):
    if User.get_by_username(data.username):
        return JSONResponse({"error": f"User '{data.username}' already exists"}, status_code=400)

    config = {
        key: float(value) if isinstance(value, Decimal) else value
        for key, value in data.config.model_dump(exclude_none=True).items()
    }
    config["style"] = data.style
    persona = data.persona or f"A {data.style} trading strategy."
    summary = data.summary or persona
    label = data.label or f"{data.style.title()} strategy"

    user = User.create_agent(data.username, persona, label, summary, json.dumps(config))
    Account.create(user.id)
    await request.app.state.runtime.broadcast_leaderboard_update(json_default=json_default)
    return {"ok": True, "agent": {"username": user.username, "label": label, "summary": summary, "config": config}}


@router.post("/api/build-portfolio/{agent_name}")
async def build_portfolio(agent_name: str, request: Request):
    try:
        result = await agent_service.build_portfolio(
            agent_name,
            portfolio_operation=request.app.state.runtime.market_refresh_scheduler.exclusive_portfolio_operation,
            broadcast=lambda event: request.app.state.runtime.broadcast(event, json_default=json_default),
        )
        await request.app.state.runtime.broadcast_leaderboard_update(json_default=json_default)
        return result
    except agent_service.ServiceError as error:
        return service_error_response(error)


@router.post("/api/analyze/{agent_name}")
async def deep_analysis(agent_name: str, request: Request):
    try:
        return await agent_service.deep_analysis(
            agent_name,
            broadcast=lambda event: request.app.state.runtime.broadcast(event, json_default=json_default),
        )
    except agent_service.ServiceError as error:
        return service_error_response(error)


@router.post("/api/chat/{agent_name}")
async def chat_with_agent(agent_name: str, data: ChatRequest):
    try:
        return await agent_service.chat(agent_name, data.message)
    except agent_service.ServiceError as error:
        return service_error_response(error)


def _today_no_trade_decision(user_id: int) -> dict[str, object] | None:
    today = datetime.now(UTC).date().isoformat()
    with get_db() as conn:
        row = conn.execute(
            """SELECT parsed_decision, execution_status, execution_error, execution_rejection_reason, created_at
               FROM decision_audits
               WHERE user_id=? AND substr(created_at, 1, 10)=? AND execution_status IN ('hold', 'rejected')
               ORDER BY id DESC LIMIT 1""",
            (user_id, today),
        ).fetchone()
    if row is None:
        return None
    try:
        decision = json.loads(row["parsed_decision"] or "{}")
    except json.JSONDecodeError:
        decision = {}
    rejection = row["execution_rejection_reason"] or row["execution_error"]
    try:
        rejection = json.loads(rejection) if rejection else None
    except json.JSONDecodeError:
        pass
    return {
        "decision": decision.get("decision", "HOLD"),
        "ticker": decision.get("ticker"),
        "reasoning": decision.get("reasoning"),
        "execution_status": row["execution_status"],
        "rejection": rejection,
        "time": row["created_at"],
    }


@router.get("/api/agent-detail/{username}")
async def agent_detail(username: str):
    """Comprehensive agent view: all trades, sector breakdown, stats, P&L history."""
    user = User.get_by_username(username.lower())
    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)

    decision_architecture = getattr(user, "decision_architecture", "single_model")
    snap = compute_portfolio_snapshot(user.id)
    all_trades = Transaction.recent_for_user(user.id, limit=100)
    holdings = Holding.all_for_user(user.id)

    # Sector breakdown
    sectors = {}
    for h in holdings:
        with get_db() as conn:
            w = conn.execute("SELECT sector FROM watchlist WHERE ticker=?", (h.ticker,)).fetchone()
        sec = w["sector"] if w else "Unknown"
        cur_price = next(
            (
                p.get("current_price", h.average_cost_per_share)
                for p in snap.get("holdings", [])
                if p["ticker"] == h.ticker
            ),
            h.average_cost_per_share,
        )
        val = h.quantity * cur_price
        sectors[sec] = sectors.get(sec, 0) + val

    # Stats
    buys = [t for t in all_trades if t.transaction_type == "BUY"]
    sells = [t for t in all_trades if t.transaction_type == "SELL"]
    total_bought = sum(t.total_value for t in buys)
    total_sold = sum(t.total_value for t in sells)
    closed_sells = [trade for trade in sells if trade.realized_pnl is not None]
    winning_trades = [trade for trade in closed_sells if trade.realized_pnl > 0]
    win_rate = (len(winning_trades) / len(closed_sells) * 100) if closed_sells else 0

    # Analyses
    with get_db() as conn:
        analyses = conn.execute(
            "SELECT * FROM analyses WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
            (user.id,),
        ).fetchall()

    # P&L history
    with get_db() as conn:
        pnl_history = conn.execute(
            "SELECT pnl_total_e8, pnl_percent, snapshot_at FROM leaderboard_snapshots WHERE user_id=? ORDER BY snapshot_at ASC LIMIT 200",
            (user.id,),
        ).fetchall()
        committee_steps = (
            conn.execute(
                """SELECT sequence, phase, role, provider, model_name, pi_session_id, usage_json,
                      estimated_cost_usd, response_status, error, created_at
               FROM ensemble_decision_steps WHERE user_id=? ORDER BY created_at DESC, sequence LIMIT 20""",
                (user.id,),
            ).fetchall()
            if decision_architecture == "multi_model"
            else []
        )

    return {
        "username": user.username,
        "display_name": COMMITTEE_ACCOUNT_LABEL if decision_architecture == "multi_model" else user.username,
        "user_type": user.user_type,
        "decision_architecture": decision_architecture,
        "model_roster": committee_roster()
        if decision_architecture == "multi_model"
        else {"provider": getattr(user, "model_provider", None), "model": getattr(user, "model_name", None)},
        "strategy": {
            "label": user.strategy_label,
            "summary": user.strategy_summary,
            "config": json.loads(user.strategy_config) if user.strategy_config else None,
        },
        "portfolio": snap,
        "trades": [
            {
                "action": t.transaction_type,
                "ticker": t.ticker,
                "quantity": t.quantity,
                "price": t.price_per_share,
                "total": t.total_value,
                "reasoning": t.llm_reasoning,
                "time": t.executed_at,
            }
            for t in all_trades
        ],
        "sectors": {s: round(v, 2) for s, v in sorted(sectors.items(), key=lambda x: -x[1])},
        "stats": {
            "dividend_income": Transaction.dividend_income_for_user(user.id),
            "total_trades": len(all_trades),
            "buys": len(buys),
            "sells": len(sells),
            "total_bought": round(total_bought, 2),
            "total_sold": round(total_sold, 2),
            "win_rate": round(win_rate, 1),
            "avg_trade_size": round(total_bought / len(buys), 2) if buys else 0,
            "largest_trade": round(max(t.total_value for t in all_trades), 2) if all_trades else 0,
        },
        "analyses": [{"text": a["analysis_text"][:500], "created": a["created_at"]} for a in analyses],
        "committee_steps": [dict(step) for step in committee_steps],
        "no_trade_decision": _today_no_trade_decision(user.id) if decision_architecture == "multi_model" else None,
        "pnl_history": [
            {"time": r["snapshot_at"], "pnl": from_e8(r["pnl_total_e8"]), "pnl_pct": r["pnl_percent"]}
            for r in pnl_history
        ],
    }


@router.get("/api/analyses")
async def get_analyses(limit: int = Query(default=20, ge=1, le=100)):
    """Get past deep analyses."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT a.*, u.username FROM analyses a JOIN users u ON a.user_id = u.id ORDER BY a.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/trades/{username}")
async def user_trades(username: str, limit: int = Query(default=10, ge=1, le=100)):
    """Get recent trades for a specific user."""
    user = User.get_by_username(username.lower())
    if not user:
        return JSONResponse({"error": "User not found"}, status_code=404)
    return Transaction.recent_for_user(user.id, limit=limit)
