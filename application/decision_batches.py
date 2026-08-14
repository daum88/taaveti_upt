"""Decision execution for one AI account in an immutable decision batch."""

import json
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from application.trading import Trading, TradingError
from db.connection import get_db, transaction
from db.money import dec
from domain.trading import DecisionOrder
from models.account import Account
from models.holding import Holding
from models.transaction import Transaction
from models.user import User
from services.corporate_actions import scan_all_corporate_actions
from services.decision_input import DecisionInput, capture_decision_input
from services.execution_engine import auto_enforce_risk_rules
from services.execution_market import ExecutionMarket, refresh_execution_market
from services.funnel import run_funnel_cycle
from services.investment_committee import CommitteeDecisionRequest
from services.investment_committee import decide as run_investment_committee
from services.leaderboard import persist_leaderboard_snapshots
from services.llm_agent import run_agent
from services.market_features import capture_market_features, eligible
from services.strategy_policy import StrategyPolicy

logger = logging.getLogger(__name__)
decision_trading = Trading()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _trade_payload(agent_name: str, transaction: Any, reasoning: str = "") -> dict[str, Any]:
    return {
        "trader": agent_name.title(),
        "action": getattr(transaction, "transaction_type", None) or transaction.action,
        "ticker": transaction.ticker,
        "quantity": transaction.quantity,
        "price": getattr(transaction, "price_per_share", None) or transaction.price,
        "total": getattr(transaction, "total_value", None) or transaction.total,
        "reasoning": getattr(transaction, "llm_reasoning", "") or reasoning,
        "status": "EXECUTED",
        "timestamp": _now(),
    }


def _hold_payload(agent_name: str, decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "trader": agent_name.title(),
        "action": decision.get("decision", "HOLD").upper(),
        "ticker": decision.get("ticker", ""),
        "reasoning": decision.get("reasoning", ""),
        "status": "HOLD",
        "timestamp": _now(),
    }


def _persist_execution_quotes(
    execution_market: ExecutionMarket, decision_audit_id: int | None, transaction_id: int | None = None
) -> None:
    rejection = json.dumps(execution_market.rejection, sort_keys=True) if execution_market.rejection else None
    with get_db() as conn:
        transaction_ticker = (
            conn.execute("SELECT ticker FROM transactions WHERE id=?", (transaction_id,)).fetchone()["ticker"]
            if transaction_id
            else None
        )
        quotes = dict(execution_market.quotes)
        for ticker in execution_market.requested_tickers:
            quote = quotes.get(ticker)
            cursor = conn.execute(
                """INSERT INTO execution_quote_audits
                   (decision_audit_id, transaction_id, ticker, price, captured_at, source, market_state, rejection_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_audit_id,
                    transaction_id if quote and quote.ticker == transaction_ticker else None,
                    ticker,
                    quote.price if quote else None,
                    quote.captured_at if quote else _now(),
                    quote.source if quote else "yfinance",
                    quote.market_state if quote else "unavailable",
                    rejection,
                ),
            )
            if transaction_id and ticker == transaction_ticker:
                conn.execute(
                    "UPDATE transactions SET execution_quote_audit_id=? WHERE id=?", (cursor.lastrowid, transaction_id)
                )


def _process_agent(
    agent_user: Any,
    decision_input: DecisionInput,
    batch_id: int,
    trading: Trading | None = None,
    market_refresher: Any = None,
    risk_enforcer: Any = None,
    agent_runner: Any = None,
    committee_runner: Any = None,
) -> list[dict[str, Any]]:
    """Process one account using immutable decision context and fresh execution quotes."""
    trading = trading or decision_trading
    market_refresher = market_refresher or refresh_execution_market
    risk_enforcer = risk_enforcer or auto_enforce_risk_rules
    agent_runner = agent_runner or run_agent
    committee_runner = committee_runner or run_investment_committee
    stocks = decision_input.context()["funnel_stocks"]
    cycle_id = decision_input.funnel_cycle_id
    market_open = decision_input.market_open
    market_snapshot_at = decision_input.captured_at
    account = Account.get_by_user_id(agent_user.id)
    if account is None:
        logger.warning("Skipping agent %s: account is missing", agent_user.username)
        return []
    risk_market = market_refresher(decision={}, holdings=Holding.all_for_user(agent_user.id), market_open=market_open)
    forced = risk_enforcer(agent_user.id, risk_market.prices, cycle_id) if not risk_market.rejection else []
    if forced:
        for forced_transaction in forced:
            _persist_execution_quotes(risk_market, None, forced_transaction.id)
    else:
        _persist_execution_quotes(risk_market, None)
    trades = [_trade_payload(agent_user.username, item) for item in forced]
    account = Account.get_by_user_id(agent_user.id)
    if account is None:
        return trades
    holdings = Holding.all_for_user(agent_user.id)
    holdings_data = [
        {"ticker": h.ticker, "quantity": h.quantity, "average_cost_per_share": h.average_cost_per_share}
        for h in holdings
    ]
    snapshot_prices = {ticker: quote["price"] for ticker, quote in decision_input.prices.items()}
    holdings_value = sum(
        (h.quantity * dec(snapshot_prices.get(h.ticker, h.average_cost_per_share)) for h in holdings), dec(0)
    )
    history = [
        {
            "action": t.transaction_type,
            "ticker": t.ticker,
            "quantity": t.quantity,
            "price": t.price_per_share,
            "total": t.total_value,
            "reasoning": t.llm_reasoning,
            "time": t.executed_at,
        }
        for t in Transaction.recent_for_user(agent_user.id, limit=5)
    ]
    audit_id: int | None = None
    strategy_config = getattr(agent_user, "strategy_config", None)
    strategy = json.loads(strategy_config) if strategy_config else {}
    policy = StrategyPolicy.from_config(strategy)
    eligible_tickers = frozenset(
        stock["ticker"]
        for stock in decision_input.funnel_stocks
        if not decision_input.features or eligible(decision_input.features.get(stock["ticker"], {}))
    )
    policy = replace(
        policy,
        eligible_instruments=(policy.eligible_instruments & eligible_tickers)
        if policy.eligible_instruments is not None
        else eligible_tickers,
    )

    def persist_audit(metadata: dict[str, Any]) -> None:
        nonlocal audit_id
        with transaction() as conn:
            batch_agent = conn.execute(
                "SELECT id FROM decision_batch_agents WHERE batch_id=? AND user_id=?", (batch_id, agent_user.id)
            ).fetchone()
            snapshot = conn.execute("SELECT id FROM decision_batch_snapshots WHERE batch_id=?", (batch_id,)).fetchone()
            cursor = conn.execute(
                """INSERT INTO decision_audits
                   (batch_agent_id, user_id, provider, model_name, prompt_hash, context_hash,
                    raw_response, parsed_decision, market_snapshot_id, market_snapshot_at,
                    response_status, execution_status, execution_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_agent["id"] if batch_agent else None,
                    agent_user.id,
                    metadata.get("provider"),
                    metadata.get("model_name"),
                    metadata.get("prompt_hash"),
                    metadata.get("context_hash"),
                    metadata.get("raw_response"),
                    json.dumps(metadata["parsed_decision"], sort_keys=True)
                    if metadata.get("parsed_decision")
                    else None,
                    f"decision_batch_snapshot:{snapshot['id']}" if snapshot else f"funnel_cycle:{cycle_id}",
                    market_snapshot_at,
                    metadata["response_status"],
                    metadata.get("execution_status", "pending"),
                    metadata.get("error"),
                ),
            )
            audit_id = cursor.lastrowid

    def persist_committee_step(metadata: dict[str, Any]) -> None:
        with transaction() as conn:
            batch_agent = conn.execute(
                "SELECT id FROM decision_batch_agents WHERE batch_id=? AND user_id=?", (batch_id, agent_user.id)
            ).fetchone()
            conn.execute(
                """INSERT INTO ensemble_decision_steps
                   (batch_agent_id, user_id, sequence, phase, role, provider, model_name,
                    prompt_hash, context_hash, pi_session_id, usage_json, estimated_cost_usd,
                    raw_response, parsed_decision, response_status, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_agent["id"] if batch_agent else None,
                    agent_user.id,
                    metadata["sequence"],
                    metadata["phase"],
                    metadata["role"],
                    metadata["provider"],
                    metadata["model_name"],
                    metadata["prompt_hash"],
                    metadata["context_hash"],
                    metadata.get("pi_session_id"),
                    metadata.get("usage_json"),
                    metadata.get("estimated_cost_usd"),
                    metadata.get("raw_response"),
                    json.dumps(metadata["parsed_decision"], sort_keys=True)
                    if metadata.get("parsed_decision")
                    else None,
                    metadata["response_status"],
                    metadata.get("error"),
                ),
            )

    if getattr(agent_user, "decision_architecture", "single_model") == "multi_model":
        decision = committee_runner(
            CommitteeDecisionRequest(
                agent_name=agent_user.username,
                strategy=strategy,
                persona_prompt=getattr(agent_user, "persona_prompt", None) or "",
                holdings=holdings_data,
                cash=float(account.cash_balance),
                portfolio_value=float(account.cash_balance + holdings_value),
                market_open=market_open,
                trade_history=history,
                decision_input=decision_input,
            ),
            step_audit=persist_committee_step,
            decision_audit=persist_audit,
        )
    else:
        decision = agent_runner(
            agent_name=agent_user.username,
            funnel_stocks=stocks,
            holdings=holdings_data,
            cash=float(account.cash_balance),
            portfolio_value=float(account.cash_balance + holdings_value),
            market_open=market_open,
            trade_history=history,
            decision_audit=persist_audit,
            decision_input=decision_input,
        )
    if not decision:
        return trades
    rejection: dict[str, str] | None = None
    action = decision.get("decision", "HOLD").upper() if isinstance(decision.get("decision", "HOLD"), str) else "HOLD"
    execution_market = (
        market_refresher(decision=decision, holdings=holdings, market_open=market_open)
        if action in {"BUY", "SELL"}
        else ExecutionMarket(MappingProxyType({}))
    )
    item = None
    if action in {"BUY", "SELL"}:
        try:
            result = trading.execute_decision(
                DecisionOrder(
                    agent_user.id,
                    decision.get("ticker", ""),
                    action,
                    dec(decision.get("allocation_percentage", 0)),
                    f"decision-audit:{audit_id}" if audit_id is not None else f"decision:{batch_id}:{agent_user.id}",
                    decision.get("reasoning") if isinstance(decision.get("reasoning"), str) else None,
                    cycle_id,
                    not market_open,
                    policy,
                ),
                execution_market,
            )
            item = result.order
        except TradingError as error:
            rejection = {"code": error.code, "message": str(error)}
    _persist_execution_quotes(execution_market, audit_id, item.transaction_id if item else None)
    execution_status = "executed" if item else ("hold" if action == "HOLD" else "rejected")
    if audit_id is not None:
        with get_db() as conn:
            conn.execute(
                """UPDATE decision_audits
                   SET execution_status=?, execution_error=?, execution_quote_captured_at=?, execution_rejection_reason=?
                   WHERE id=?""",
                (
                    execution_status,
                    json.dumps(rejection, sort_keys=True) if rejection else None,
                    execution_market.captured_at,
                    json.dumps(rejection, sort_keys=True) if rejection else None,
                    audit_id,
                ),
            )
    return (
        [
            *trades,
            _trade_payload(
                agent_user.username,
                item,
                decision.get("reasoning", "") if isinstance(decision.get("reasoning"), str) else "",
            ),
        ]
        if item
        else [*trades, _hold_payload(agent_user.username, decision)]
    )


class AgentDecisionProcessor:
    """Execute one agent's immutable decision against fresh quotes and the shared trading module."""

    def __init__(
        self,
        trading: Trading | None = None,
        *,
        market_refresher: Any = None,
        risk_enforcer: Any = None,
        agent_runner: Any = None,
        committee_runner: Any = None,
    ) -> None:
        self._trading = trading or Trading()
        self._market_refresher = market_refresher
        self._risk_enforcer = risk_enforcer
        self._agent_runner = agent_runner
        self._committee_runner = committee_runner

    def process(self, agent_user: Any, decision_input: DecisionInput, batch_id: int) -> list[dict[str, Any]]:
        return _process_agent(
            agent_user,
            decision_input,
            batch_id,
            self._trading,
            self._market_refresher,
            self._risk_enforcer,
            self._agent_runner,
            self._committee_runner,
        )


TradePublisher = Callable[[dict[str, Any]], None]
StatusPublisher = Callable[[], None]
AgentProcessor = Callable[[Any, DecisionInput, int], list[dict[str, Any]]]


class DecisionBatchRunner:
    """Run a durable decision batch without holding a portfolio lock across external work."""

    def __init__(
        self,
        processor: AgentProcessor | None = None,
        *,
        funnel_runner: Callable[[], dict[str, Any] | None] = run_funnel_cycle,
        agent_loader: Callable[[], list[Any]] = User.llm_agents,
        decision_input_capturer: Callable[..., DecisionInput] = capture_decision_input,
        feature_builder: Callable[..., dict[str, Any]] = capture_market_features,
        corporate_action_scanner: Callable[[], Any] = scan_all_corporate_actions,
        leaderboard_persister: Callable[[dict[str, Any]], Any] = persist_leaderboard_snapshots,
        trade_publisher: TradePublisher | None = None,
        status_publisher: StatusPublisher | None = None,
    ) -> None:
        self._processor = processor or AgentDecisionProcessor().process
        self._funnel_runner = funnel_runner
        self._agent_loader = agent_loader
        self._decision_input_capturer = decision_input_capturer
        self._feature_builder = feature_builder
        self._corporate_action_scanner = corporate_action_scanner
        self._leaderboard_persister = leaderboard_persister
        self._trade_publisher = trade_publisher or (lambda _: None)
        self._status_publisher = status_publisher or (lambda: None)

    def run(self, batch_id: int) -> None:
        try:
            result = self._funnel_runner()
            agents = self._agent_loader()
            decision_input = self._decision_input_capturer(
                result or {},
                additional_tickers=self._held_tickers(agents),
                feature_builder=lambda prices, captured_at: self._feature_builder(prices, as_of=captured_at),
            )
            if not decision_input.funnel_stocks:
                raise RuntimeError("No market data available for this decision batch")
            prices = {ticker: quote["price"] for ticker, quote in decision_input.prices.items()}
            with transaction() as conn:
                conn.execute(
                    "UPDATE decision_batches SET funnel_cycle_id=? WHERE id=?",
                    (decision_input.funnel_cycle_id, batch_id),
                )
            self._persist_snapshot(batch_id, decision_input)
            try:
                self._corporate_action_scanner()
            except (ConnectionError, OSError, ValueError):
                logger.exception("Corporate-actions scan failed")
            for agent in agents:
                self._mark_agent_running(batch_id, agent.id)
                self._status_publisher()
                try:
                    trades = self._processor(agent, decision_input, batch_id)
                    for trade in trades:
                        self._trade_publisher(trade)
                    self._mark_agent_completed(batch_id, agent.id, trades)
                except Exception as error:
                    logger.exception("Agent %s failed", agent.username)
                    self._mark_agent_failed(batch_id, agent.id, error)
                self._status_publisher()
            self._leaderboard_persister(prices)
            self._mark_batch_completed(batch_id)
        except Exception as error:
            logger.exception("Decision batch %s failed", batch_id)
            with get_db() as conn:
                conn.execute(
                    "UPDATE decision_batches SET status='failed', completed_at=?, error=? WHERE id=?",
                    (_now(), str(error), batch_id),
                )
        finally:
            self._status_publisher()

    @staticmethod
    def _held_tickers(agents: list[Any]) -> set[str]:
        return {holding.ticker for agent in agents for holding in Holding.all_for_user(agent.id)}

    @staticmethod
    def _persist_snapshot(batch_id: int, decision_input: DecisionInput) -> None:
        with transaction() as conn:
            conn.execute(
                """INSERT INTO decision_batch_snapshots
                   (batch_id, funnel_cycle_id, captured_at, content_hash, serialized_snapshot)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    batch_id,
                    decision_input.funnel_cycle_id,
                    decision_input.captured_at,
                    decision_input.content_hash,
                    decision_input.serialized,
                ),
            )

    @staticmethod
    def _mark_agent_running(batch_id: int, user_id: int) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE decision_batch_agents SET status='running', started_at=? WHERE batch_id=? AND user_id=?",
                (_now(), batch_id, user_id),
            )

    @staticmethod
    def _mark_agent_completed(batch_id: int, user_id: int, trades: list[dict[str, Any]]) -> None:
        with get_db() as conn:
            conn.execute(
                """UPDATE decision_batch_agents
                   SET status='completed', completed_at=?, trade_count=?
                   WHERE batch_id=? AND user_id=?""",
                (_now(), sum(trade.get("status") == "EXECUTED" for trade in trades), batch_id, user_id),
            )

    @staticmethod
    def _mark_agent_failed(batch_id: int, user_id: int, error: Exception) -> None:
        with get_db() as conn:
            conn.execute(
                "UPDATE decision_batch_agents SET status='failed', completed_at=?, error=? WHERE batch_id=? AND user_id=?",
                (_now(), str(error), batch_id, user_id),
            )

    @staticmethod
    def _mark_batch_completed(batch_id: int) -> None:
        with get_db() as conn:
            failed = conn.execute(
                "SELECT COUNT(*) FROM decision_batch_agents WHERE batch_id=? AND status='failed'", (batch_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE decision_batches SET status=?, completed_at=? WHERE id=?",
                ("completed_with_errors" if failed else "completed", _now(), batch_id),
            )
