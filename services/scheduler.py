"""
Background Scheduler — funnel + auto risk enforcement + agent decisions.
Runs on configurable interval in a daemon thread.
"""

import logging, threading, time
from datetime import datetime
from typing import Callable, Optional

from config import FUNNEL_INTERVAL_SECONDS
from services.funnel import run_funnel_cycle
from services.llm_agent import run_agent
from services.execution_engine import process_agent_decision, auto_enforce_risk_rules
from services.corporate_actions import scan_all_corporate_actions
from models.user import User
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction

logger = logging.getLogger(__name__)

_scheduler_thread: threading.Thread | None = None
_stop_event = threading.Event()
_last_run_time: datetime | None = None
_last_run_result: dict | None = None
_is_running = False
_on_trade_callback: Optional[Callable] = None
_last_trigger_at: dict[str, float] = {}
TRIGGER_COOLDOWN_SECONDS = int(__import__("os").getenv("TRIGGER_COOLDOWN_SECONDS", "60"))

def set_trade_callback(cb: Callable):
    global _on_trade_callback; _on_trade_callback = cb

def _process_agent(agent_user, stocks, current_prices, cycle_id, market_open) -> list[dict]:
    """Run full decision pipeline for one agent. Returns list of executed-trade broadcast dicts."""
    trades: list[dict] = []
    account = Account.get_by_user_id(agent_user.id)
    if not account:
        return trades

    # STEP A: Auto-enforce risk rules (stop-loss -8%, take-profit +15%)
    forced = auto_enforce_risk_rules(agent_user.id, current_prices, cycle_id)
    for ft in forced:
        trades.append({"trader": agent_user.username.title(), "action": ft.transaction_type, "ticker": ft.ticker, "quantity": ft.quantity, "price": ft.price_per_share, "total": ft.total_value, "reasoning": ft.llm_reasoning or "", "status": "EXECUTED", "timestamp": datetime.now().isoformat()})

    # STEP B: Refresh after forced sells
    account = Account.get_by_user_id(agent_user.id)
    holdings = Holding.all_for_user(agent_user.id)
    hd = [{"ticker": h.ticker, "quantity": h.quantity, "average_cost_per_share": h.average_cost_per_share} for h in holdings]
    hv = sum(h.quantity * current_prices.get(h.ticker, h.average_cost_per_share) for h in holdings)
    pv = account.cash_balance + hv

    # STEP C: Agent decides
    recent = Transaction.recent_for_user(agent_user.id, limit=5)
    th = [{"action": t.transaction_type, "ticker": t.ticker, "quantity": t.quantity, "price": t.price_per_share, "total": t.total_value, "reasoning": t.llm_reasoning, "time": t.executed_at} for t in recent]
    decision = run_agent(agent_name=agent_user.username, funnel_stocks=stocks, holdings=hd, cash=account.cash_balance, portfolio_value=pv, market_open=market_open, trade_history=th)
    if not decision:
        return trades

    # STEP D: Execute
    txn = process_agent_decision(user_id=agent_user.id, decision=decision, current_prices=current_prices, cycle_id=cycle_id, market_closed=not market_open)
    if txn:
        trades.append({"trader": agent_user.username.title(), "action": txn.transaction_type, "ticker": txn.ticker, "quantity": txn.quantity, "price": txn.price_per_share, "total": txn.total_value, "reasoning": txn.llm_reasoning or "", "status": "EXECUTED", "timestamp": datetime.now().isoformat()})
    else:
        trades.append({"trader": agent_user.username.title(), "action": decision.get("decision", "HOLD").upper(), "ticker": decision.get("ticker", ""), "reasoning": decision.get("reasoning", ""), "status": "HOLD", "timestamp": datetime.now().isoformat()})
    return trades


def _run_cycle():
    global _last_run_time, _last_run_result, _is_running
    _is_running = True; _last_run_time = datetime.now()
    try:
        funnel_result = run_funnel_cycle()
        if not funnel_result or not funnel_result["stocks"]:
            _last_run_result = {"stocks_processed": 0, "trades_executed": 0, "error": None}
            _is_running = False; return

        stocks = funnel_result["stocks"]
        cycle_id = funnel_result["cycle_id"]
        market_open = funnel_result["market_open"]
        current_prices = {s["ticker"]: s["price"] for s in stocks if s.get("price")}

        try:
            ca = scan_all_corporate_actions()
            if ca["splits"] or ca["dividends"]:
                logger.info(f"Corporate actions applied: {ca['splits']} splits, {ca['dividends']} dividends")
        except Exception as e:
            logger.debug(f"Corporate-actions scan failed: {e}")

        agents = User.llm_agents()
        trades_executed = 0

        for agent_user in agents:
            logger.info(f"Running agent: {agent_user.username}")
            for t in _process_agent(agent_user, stocks, current_prices, cycle_id, market_open):
                if t.get("status") == "EXECUTED":
                    trades_executed += 1
                if _on_trade_callback:
                    try: _on_trade_callback(t)
                    except Exception: pass

        _last_run_result = {"stocks_processed": len(stocks), "trades_executed": trades_executed, "error": None}
        logger.info(f"Cycle complete: {trades_executed} trades executed")
    except Exception as e:
        logger.error(f"Cycle failed: {e}", exc_info=True)
        _last_run_result = {"stocks_processed": 0, "trades_executed": 0, "error": str(e)}
    finally:
        _is_running = False

def _scheduler_loop():
    logger.info(f"Scheduler started ({FUNNEL_INTERVAL_SECONDS}s / {FUNNEL_INTERVAL_SECONDS/3600:.1f}h)")
    while not _stop_event.is_set():
        _run_cycle()
        for _ in range(int(FUNNEL_INTERVAL_SECONDS)):
            if _stop_event.is_set(): break
            time.sleep(1)

def start_scheduler():
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive(): return
    _stop_event.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler")
    _scheduler_thread.start()

def stop_scheduler():
    _stop_event.set()
    if _scheduler_thread and _scheduler_thread.is_alive(): _scheduler_thread.join(timeout=10)

def get_scheduler_status() -> dict:
    return {"running": _scheduler_thread is not None and _scheduler_thread.is_alive(), "last_run": _last_run_time.isoformat() if _last_run_time else None, "next_run": (_last_run_time.timestamp() + FUNNEL_INTERVAL_SECONDS) if _last_run_time else None, "in_progress": _is_running, "last_result": _last_run_result}

def trigger_manual_cycle():
    if _is_running: return False
    threading.Thread(target=_run_cycle, daemon=True).start()
    return True

def trigger_agent_decision(agent_name: str) -> dict:
    """Run the full decision pipeline for a single agent on demand.

    Returns {"agent", "trades": [...], "error": None} or {"error": "..."}.
    """
    global _is_running
    if _is_running:
        return {"error": "A cycle is already in progress. Try again shortly."}

    agent_user = User.get_by_username(agent_name)
    if not agent_user or agent_user.user_type != "llm_agent":
        return {"error": f"Agent '{agent_name}' not found"}

    now = time.time()
    last = _last_trigger_at.get(agent_user.username, 0)
    remaining = TRIGGER_COOLDOWN_SECONDS - (now - last)
    if remaining > 0:
        return {"error": f"Cooldown active — wait {int(remaining) + 1}s before triggering {agent_user.username} again.", "cooldown": int(remaining) + 1}
    _last_trigger_at[agent_user.username] = now

    _is_running = True
    try:
        funnel_result = run_funnel_cycle()
        if not funnel_result or not funnel_result["stocks"]:
            return {"error": "No market data available for this cycle"}

        stocks = funnel_result["stocks"]
        current_prices = {s["ticker"]: s["price"] for s in stocks if s.get("price")}
        trades = _process_agent(agent_user, stocks, current_prices, funnel_result["cycle_id"], funnel_result["market_open"])
        return {"agent": agent_user.username, "trades": trades, "error": None}
    except Exception as e:
        logger.error(f"trigger_agent_decision failed: {e}", exc_info=True)
        return {"error": str(e)}
    finally:
        _is_running = False
