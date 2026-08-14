"""Decision execution for one AI account in an immutable decision batch."""

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import exchange_calendars as xcals

from adapters.sqlite.decision_audits import DecisionAuditRecorder, record_execution_quotes
from adapters.sqlite.decision_batches import BatchRecord, DecisionBatchStore
from application.trading import Trading, TradingError
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
from settings import Settings, load_settings

logger = logging.getLogger(__name__)


def _agent_runner(settings: Settings) -> Any:
    return lambda **kwargs: run_agent(settings=settings, **kwargs)


def _committee_runner(settings: Settings) -> Any:
    return lambda request, **kwargs: run_investment_committee(request, settings=settings, **kwargs)


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


def _process_agent(
    agent_user: Any,
    decision_input: DecisionInput,
    batch_id: int,
    trading: Trading,
    market_refresher: Any,
    risk_enforcer: Any,
    agent_runner: Any,
    committee_runner: Any,
) -> list[dict[str, Any]]:
    """Process one account using immutable decision context and fresh execution quotes."""
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
            record_execution_quotes(risk_market, None, forced_transaction.id)
    else:
        record_execution_quotes(risk_market, None)
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
    audit = DecisionAuditRecorder(batch_id, agent_user.id, market_snapshot_at, cycle_id)
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
            step_audit=audit.record_committee_step,
            decision_audit=audit.record_decision,
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
            decision_audit=audit.record_decision,
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
                    audit.order_reference,
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
    execution_status = "executed" if item else ("hold" if action == "HOLD" else "rejected")
    audit.complete(execution_market, item.transaction_id if item else None, execution_status, rejection)
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
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._trading = trading or Trading(settings=self._settings)
        self._market_refresher = market_refresher or refresh_execution_market
        self._risk_enforcer = risk_enforcer or auto_enforce_risk_rules
        self._agent_runner = agent_runner or _agent_runner(self._settings)
        self._committee_runner = committee_runner or _committee_runner(self._settings)

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
BatchStarter = Callable[[int], None]


class DecisionBatchRunner:
    """Run a durable decision batch without holding a portfolio lock across external work."""

    def __init__(
        self,
        processor: AgentProcessor | None = None,
        *,
        funnel_runner: Callable[[], dict[str, Any] | None] | None = None,
        agent_loader: Callable[[], list[Any]] | None = None,
        decision_input_capturer: Callable[..., DecisionInput] | None = None,
        feature_builder: Callable[..., dict[str, Any]] | None = None,
        corporate_action_scanner: Callable[[], Any] | None = None,
        leaderboard_persister: Callable[[dict[str, Any]], Any] | None = None,
        trade_publisher: TradePublisher | None = None,
        status_publisher: StatusPublisher | None = None,
        batch_starter: BatchStarter | None = None,
        store: DecisionBatchStore | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or load_settings()
        self._processor = processor or AgentDecisionProcessor(settings=self._settings).process
        self._funnel_runner = funnel_runner or partial(run_funnel_cycle, settings=self._settings)
        self._agent_loader = agent_loader or User.llm_agents
        self._decision_input_capturer = decision_input_capturer or capture_decision_input
        self._feature_builder = feature_builder or capture_market_features
        self._corporate_action_scanner = corporate_action_scanner or partial(
            scan_all_corporate_actions, settings=self._settings
        )
        self._leaderboard_persister = leaderboard_persister or partial(
            persist_leaderboard_snapshots, settings=self._settings
        )
        self._trade_publisher = trade_publisher or (lambda _: None)
        self._status_publisher = status_publisher or (lambda: None)
        self._batch_starter = batch_starter or self._start_worker
        self._store = store or DecisionBatchStore()

    def start(self, now: datetime) -> dict[str, Any]:
        """Create one durable batch and dispatch its worker unless execution is blocked."""
        created = self._begin(now)
        if isinstance(created, dict):
            return created
        self._batch_starter(created)
        status = self.status()
        self._publish_status()
        return status

    def _begin(self, now: datetime) -> int | dict[str, Any]:
        """Create one durable batch unless another batch or its cooldown blocks it."""
        started = self._store.start(
            now,
            timedelta(seconds=self._settings.decision_batch_cooldown_seconds),
            (agent.id for agent in self._agent_loader()),
        )
        if started.batch_id is not None:
            return started.batch_id
        if started.blocked_reason == "active":
            return {"error": "A decision batch is already in progress.", "reason": "active"}
        return {
            "error": "Manual decision batch cooldown is active.",
            "reason": "cooldown",
            "next_eligible_at": started.next_eligible_at,
        }

    def _start_worker(self, batch_id: int) -> None:
        threading.Thread(
            target=self.run,
            args=(batch_id,),
            daemon=True,
            name=f"decision-batch-{batch_id}",
        ).start()

    def _publish_trade(self, trade: dict[str, Any]) -> None:
        try:
            self._trade_publisher(trade)
        except (RuntimeError, TypeError, ValueError):
            logger.exception("Trade publisher rejected update")

    def _publish_status(self) -> None:
        try:
            self._status_publisher()
        except (RuntimeError, TypeError, ValueError):
            logger.exception("Decision batch publisher rejected update")

    def recover_interrupted(self) -> None:
        self._store.recover_interrupted(_now())

    def status(self) -> dict[str, Any]:
        batch = self._store.latest()
        if batch is None:
            return {
                "batch_id": None,
                "status": "idle",
                "last_triggered_at": None,
                "last_completed_at": None,
                "next_eligible_at": None,
                "counts": {"total": 0, "completed": 0, "failed": 0},
                "agents": {},
            }
        return self._load_status(batch)

    def week_status(
        self,
        week_start: date | str | None = None,
        timezone: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return the complete manual-decision reminder state for one local Monday–Sunday week."""
        timezone = timezone or self._settings.decision_reminder_timezone
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Unknown timezone") from error
        current = (now or datetime.now(UTC)).astimezone(zone)
        if isinstance(week_start, str):
            try:
                week_start = date.fromisoformat(week_start)
            except ValueError as error:
                raise ValueError("week_start must be an ISO date") from error
        if week_start is not None and week_start.weekday() != 0:
            raise ValueError("week_start must be a Monday")
        start = week_start or current.date() - timedelta(days=current.date().weekday())
        schedule = self._reminder_schedule(zone)
        end = start + timedelta(days=7)
        lower = datetime.combine(start, time.min, zone).astimezone(UTC).isoformat()
        upper = datetime.combine(end, time.min, zone).astimezone(UTC).isoformat()
        batches = self._store.during(lower, upper)
        ai_account_count = self._store.agent_count()
        summaries = [self._load_status(batch) for batch in batches]
        by_day: dict[date, list[dict[str, Any]]] = {}
        for summary in summaries:
            local_day = datetime.fromisoformat(summary["last_triggered_at"]).astimezone(zone).date()
            if start <= local_day < end:
                by_day.setdefault(local_day, []).append(summary)
        scheduled: dict[date, datetime] = {}
        for offset in range(7):
            nominal = start + timedelta(days=offset)
            if nominal.weekday() in schedule["weekdays"]:
                due_day = self._next_open_day(nominal)
                if start <= due_day < end:
                    scheduled[due_day] = datetime.combine(due_day, schedule["time"], zone)
        days = []
        for offset in range(7):
            day = start + timedelta(days=offset)
            history = by_day.get(day, [])
            latest = history[0] if history else None
            due_at = scheduled.get(day)
            state = latest["status"] if latest else "not_due"
            if latest is None and due_at and current >= due_at:
                state = "due"
            days.append(
                {
                    "date": day.isoformat(),
                    "weekday": day.strftime("%A"),
                    "is_today": day == current.date(),
                    "state": state,
                    "due_at": due_at.isoformat() if due_at else None,
                    "batch": latest,
                    "run_count": len(history),
                }
            )
        current_batch = next((summary for summary in summaries if summary["status"] == "running"), None)
        latest = summaries[0] if summaries else None
        next_due = next((due for due in sorted(scheduled.values()) if due > current), None)
        return {
            "week_start": start.isoformat(),
            "timezone": timezone,
            "schedule": {
                "kind": "reminder",
                "weekdays": list(schedule["weekdays"]),
                "time": self._settings.decision_reminder_time,
            },
            "days": days,
            "current_batch": current_batch,
            "latest_batch": latest,
            "next_reminder_at": next_due.isoformat() if next_due else None,
            "ai_account_count": ai_account_count,
        }

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
            self._store.record_input(batch_id, decision_input)
            try:
                self._corporate_action_scanner()
            except (ConnectionError, OSError, ValueError):
                logger.exception("Corporate-actions scan failed")
            for agent in agents:
                self._mark_agent_running(batch_id, agent.id)
                self._publish_status()
                try:
                    trades = self._processor(agent, decision_input, batch_id)
                    for trade in trades:
                        self._publish_trade(trade)
                    self._mark_agent_completed(batch_id, agent.id, trades)
                except Exception as error:
                    logger.exception("Agent %s failed", agent.username)
                    self._mark_agent_failed(batch_id, agent.id, error)
                self._publish_status()
            self._leaderboard_persister(prices)
            self._mark_batch_completed(batch_id)
        except Exception as error:
            logger.exception("Decision batch %s failed", batch_id)
            self._store.fail(batch_id, _now(), str(error))
        finally:
            self._publish_status()

    def _reminder_schedule(self, timezone: ZoneInfo) -> dict[str, Any]:
        try:
            hour, minute = (int(part) for part in self._settings.decision_reminder_time.split(":", maxsplit=1))
            reminder_time = time(hour, minute)
        except ValueError as error:
            raise ValueError("DECISION_REMINDER_TIME must be HH:MM") from error
        weekdays = self._settings.decision_reminder_weekdays
        if any(day not in range(7) for day in weekdays):
            raise ValueError("DECISION_REMINDER_WEEKDAYS must contain ISO weekdays from 0 through 6")
        return {"timezone": timezone, "weekdays": weekdays, "time": reminder_time}

    @staticmethod
    def _next_open_day(day: date) -> date:
        calendar = xcals.get_calendar("XNYS")
        session = calendar.date_to_session(day, direction="next")
        return session.date()

    def _load_status(self, batch: BatchRecord) -> dict[str, Any]:
        agents = self._store.agent_statuses(batch.id)
        triggered = datetime.fromisoformat(batch.triggered_at)
        return {
            "batch_id": batch.id,
            "status": batch.status,
            "last_triggered_at": batch.triggered_at,
            "last_completed_at": batch.completed_at,
            "next_eligible_at": (
                triggered + timedelta(seconds=self._settings.decision_batch_cooldown_seconds)
            ).isoformat(),
            "counts": {
                "total": len(agents),
                "completed": sum(agent.status == "completed" for agent in agents),
                "failed": sum(agent.status == "failed" for agent in agents),
            },
            "error": batch.error,
            "agents": {
                agent.username: {
                    "status": agent.status,
                    "completed_at": agent.completed_at,
                    "error": agent.error,
                    "trade_count": agent.trade_count,
                }
                for agent in agents
            },
        }

    @staticmethod
    def _held_tickers(agents: list[Any]) -> set[str]:
        return {holding.ticker for agent in agents for holding in Holding.all_for_user(agent.id)}

    def _mark_agent_running(self, batch_id: int, user_id: int) -> None:
        self._store.mark_agent_running(batch_id, user_id, _now())

    def _mark_agent_completed(self, batch_id: int, user_id: int, trades: list[dict[str, Any]]) -> None:
        self._store.mark_agent_completed(
            batch_id,
            user_id,
            _now(),
            sum(trade.get("status") == "EXECUTED" for trade in trades),
        )

    def _mark_agent_failed(self, batch_id: int, user_id: int, error: Exception) -> None:
        self._store.mark_agent_failed(batch_id, user_id, _now(), str(error))

    def _mark_batch_completed(self, batch_id: int) -> None:
        self._store.complete(batch_id, _now())
