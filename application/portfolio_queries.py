"""Portfolio read assembly shared by presentation adapters."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from config import DETAIL_NEWS_LOOKBACK_HOURS
from db.connection import get_db
from db.money import dec, from_e8
from models.holding import Holding
from models.transaction import Transaction
from models.user import User
from services.investment_committee import COMMITTEE_ACCOUNT_LABEL, committee_roster
from services.leaderboard import compute_portfolio_snapshot, get_leaderboard

ChartRange = Literal["1D", "1W", "1M", "3M", "6M", "1Y"]

STOCK_CHART_RANGES = {
    "1D": {"days": 1, "interval": "5m"},
    "1W": {"days": 7, "interval": None},
    "1M": {"days": 30, "interval": None},
    "3M": {"days": 90, "interval": None},
    "6M": {"days": 180, "interval": None},
    "1Y": {"days": 365, "interval": None},
}


class PortfolioNotFound(Exception):
    """Raised when a requested portfolio owner does not exist."""


class PortfolioQueries:
    """Assemble portfolio read models while hiding valuation and persistence details."""

    def leaderboard(self) -> list[dict[str, object]]:
        return get_leaderboard()

    def agents(self) -> dict[str, object]:
        result = []
        for agent in User.llm_agents():
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

    def user_trades(self, username: str, limit: int) -> list[Transaction]:
        user = User.get_by_username(username.lower())
        if user is None:
            raise PortfolioNotFound(username)
        return Transaction.recent_for_user(user.id, limit=limit)

    def watchlist(
        self,
        *,
        limit: int,
        offset: int,
        instrument_type: Literal["equity", "etf"] | None,
        query: str | None,
    ) -> list[dict[str, object]]:
        from services.instrument_universe import list_instruments
        from services.market_data import fetch_prices_batch

        rows, total = list_instruments(
            instrument_type=instrument_type,
            query=query,
            limit=limit,
            offset=offset,
        )
        prices = fetch_prices_batch([row["ticker"] for row in rows])
        return [
            {
                **row,
                "company": row["company_name"] or row["ticker"],
                "price": prices.get(row["ticker"], {}).get("price"),
                "change_percent": prices.get(row["ticker"], {}).get("change_percent", 0),
                "volume": prices.get(row["ticker"], {}).get("volume"),
                "total": total,
            }
            for row in rows
        ]

    def instrument_suggestions(self, query: str, limit: int) -> dict[str, object]:
        from services.instrument_universe import search_instrument_suggestions

        return {"suggestions": search_instrument_suggestions(query, limit=limit)}

    def instruments(
        self,
        *,
        limit: int,
        offset: int,
        instrument_type: Literal["equity", "etf"] | None,
        query: str | None,
        active_only: bool,
    ) -> dict[str, object]:
        from services.instrument_universe import list_instruments

        rows, total = list_instruments(
            instrument_type=instrument_type,
            query=query,
            active_only=active_only,
            limit=limit,
            offset=offset,
        )
        return {"instruments": rows, "total": total}

    def ohlcv(self, ticker: str, days: int) -> list[dict[str, object]]:
        from services.market_data import fetch_ohlcv

        return [
            {key: float(value) if hasattr(value, "item") else value for key, value in row.items()}
            for row in fetch_ohlcv(ticker, days)
        ]

    def recent_news(self, limit: int) -> list[dict[str, object]]:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT t.ticker, n.id, n.title, n.publisher, n.provider, n.canonical_url,
                          n.published_at, n.source_tier
                   FROM news_items n JOIN news_item_tickers t ON t.news_item_id=n.id
                   ORDER BY n.published_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_analyses(self, limit: int) -> list[dict[str, object]]:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT a.*, u.username FROM analyses a JOIN users u ON a.user_id = u.id
                   ORDER BY a.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def history(self) -> dict[str, object]:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT user_id, total_portfolio_value_e8, pnl_total_e8, snapshot_at
                   FROM (
                       SELECT user_id, total_portfolio_value_e8, pnl_total_e8, snapshot_at, id,
                              ROW_NUMBER() OVER (
                                  PARTITION BY user_id
                                  ORDER BY snapshot_at DESC, id DESC
                              ) AS row_number
                       FROM leaderboard_snapshots
                   )
                   WHERE row_number <= 300
                   ORDER BY snapshot_at ASC, id ASC"""
            ).fetchall()
        history: dict[str, list[dict[str, object]]] = {}
        users = {
            str(user.id): COMMITTEE_ACCOUNT_LABEL
            if getattr(user, "decision_architecture", "single_model") == "multi_model"
            else user.username
            for user in User.all()
        }
        for row in rows:
            user_id = str(row["user_id"])
            history.setdefault(user_id, []).append(
                {
                    "time": row["snapshot_at"],
                    "value": from_e8(row["total_portfolio_value_e8"]),
                    "pnl": from_e8(row["pnl_total_e8"]),
                }
            )
        return {"history": history, "users": users}

    def performance(self) -> list[dict[str, object]]:
        stats = []
        for user in User.all():
            trades = Transaction.recent_for_user(user.id, limit=1000)
            snapshot = compute_portfolio_snapshot(user.id)
            buys = [trade for trade in trades if trade.transaction_type == "BUY"]
            sells = [trade for trade in trades if trade.transaction_type == "SELL"]
            total_bought = sum((trade.total_value for trade in buys), Decimal())
            total_sold = sum((trade.total_value for trade in sells), Decimal())
            stats.append(
                {
                    "username": user.username,
                    "display_name": COMMITTEE_ACCOUNT_LABEL
                    if user.decision_architecture == "multi_model"
                    else user.username,
                    "user_type": user.user_type,
                    "decision_architecture": user.decision_architecture,
                    "portfolio_value": snapshot["total_value"],
                    "cash": snapshot["cash_balance"],
                    "pnl_total": snapshot["pnl_total"],
                    "pnl_percent": snapshot["pnl_percent"],
                    "total_trades": len(trades),
                    "buys": len(buys),
                    "sells": len(sells),
                    "total_bought": round(total_bought, 2),
                    "total_sold": round(total_sold, 2),
                    "positions": snapshot["holdings_count"],
                }
            )
        return stats

    def agent_detail(self, username: str) -> dict[str, object]:
        user = User.get_by_username(username.lower())
        if user is None:
            raise PortfolioNotFound(username)

        decision_architecture = getattr(user, "decision_architecture", "single_model")
        snapshot = compute_portfolio_snapshot(user.id)
        all_trades = Transaction.recent_for_user(user.id, limit=100)
        holdings = Holding.all_for_user(user.id)

        sectors: dict[str | None, Decimal] = {}
        with get_db() as conn:
            sector_rows = conn.execute(
                "SELECT ticker, sector FROM watchlist WHERE ticker IN (SELECT ticker FROM holdings WHERE user_id=?)",
                (user.id,),
            ).fetchall()
        sectors_by_ticker = {row["ticker"]: row["sector"] for row in sector_rows}
        for holding in holdings:
            sector = sectors_by_ticker.get(holding.ticker) or "Unknown"
            current_price = next(
                (
                    position.get("current_price", holding.average_cost_per_share)
                    for position in snapshot.get("holdings", [])
                    if position["ticker"] == holding.ticker
                ),
                holding.average_cost_per_share,
            )
            sectors[sector] = sectors.get(sector, Decimal()) + holding.quantity * current_price

        buys = [trade for trade in all_trades if trade.transaction_type == "BUY"]
        sells = [trade for trade in all_trades if trade.transaction_type == "SELL"]
        total_bought = sum((trade.total_value for trade in buys), Decimal())
        total_sold = sum((trade.total_value for trade in sells), Decimal())
        closed_sells = [trade for trade in sells if trade.realized_pnl is not None]
        winning_trades = [trade for trade in closed_sells if trade.realized_pnl > 0]
        win_rate = len(winning_trades) / len(closed_sells) * 100 if closed_sells else 0

        with get_db() as conn:
            analyses = conn.execute(
                "SELECT analysis_text, created_at FROM analyses WHERE user_id=? ORDER BY created_at DESC LIMIT 5",
                (user.id,),
            ).fetchall()
            pnl_history = conn.execute(
                """SELECT pnl_total_e8, pnl_percent, snapshot_at
                   FROM leaderboard_snapshots WHERE user_id=? ORDER BY snapshot_at ASC LIMIT 200""",
                (user.id,),
            ).fetchall()
            committee_steps = (
                conn.execute(
                    """SELECT sequence, phase, role, provider, model_name, pi_session_id, usage_json,
                              estimated_cost_usd, response_status, error, created_at
                       FROM ensemble_decision_steps
                       WHERE user_id=? ORDER BY created_at DESC, sequence LIMIT 20""",
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
            "portfolio": snapshot,
            "trades": [
                {
                    "action": trade.transaction_type,
                    "ticker": trade.ticker,
                    "quantity": trade.quantity,
                    "price": trade.price_per_share,
                    "total": trade.total_value,
                    "reasoning": trade.llm_reasoning,
                    "time": trade.executed_at,
                }
                for trade in all_trades
            ],
            "sectors": {
                sector: round(value, 2) for sector, value in sorted(sectors.items(), key=lambda item: -item[1])
            },
            "stats": {
                "dividend_income": Transaction.dividend_income_for_user(user.id),
                "total_trades": len(all_trades),
                "buys": len(buys),
                "sells": len(sells),
                "total_bought": round(total_bought, 2),
                "total_sold": round(total_sold, 2),
                "win_rate": round(win_rate, 1),
                "avg_trade_size": round(total_bought / len(buys), 2) if buys else 0,
                "largest_trade": round(max(trade.total_value for trade in all_trades), 2) if all_trades else 0,
            },
            "analyses": [
                {"text": analysis["analysis_text"][:500], "created": analysis["created_at"]} for analysis in analyses
            ],
            "committee_steps": [dict(step) for step in committee_steps],
            "no_trade_decision": self._today_no_trade_decision(user.id)
            if decision_architecture == "multi_model"
            else None,
            "pnl_history": [
                {
                    "time": row["snapshot_at"],
                    "pnl": from_e8(row["pnl_total_e8"]),
                    "pnl_pct": row["pnl_percent"],
                }
                for row in pnl_history
            ],
        }

    def instrument_detail(self, ticker: str, chart_range: ChartRange = "1M") -> dict[str, object]:
        ticker = ticker.upper()
        with get_db() as conn:
            instrument = conn.execute("SELECT * FROM watchlist WHERE ticker=?", (ticker,)).fetchone()

        from services.market_data import fetch_ohlcv, fetch_prices_batch

        prices = fetch_prices_batch([ticker])
        price_data = prices.get(ticker, {})
        ohlcv = fetch_ohlcv(ticker, **STOCK_CHART_RANGES[chart_range])

        self._refresh_stock_news(ticker)
        from services.news_research import brief

        research = brief([ticker], as_of=datetime.now(UTC), limit=10)
        news = research[ticker]["evidence"]

        with get_db() as conn:
            trade_rows = conn.execute(
                """SELECT t.*, u.username FROM transactions t JOIN users u ON t.user_id = u.id
                   WHERE t.ticker=? ORDER BY t.executed_at DESC LIMIT 20""",
                (ticker,),
            ).fetchall()

        holders = []
        for user in User.all():
            holding = Holding.get_by_user_and_ticker(user.id, ticker)
            if holding and holding.quantity > 0:
                current_price = (
                    dec(price_data.get("price")) if price_data.get("price") else holding.average_cost_per_share
                )
                pnl = (current_price - holding.average_cost_per_share) * holding.quantity
                pnl_percent = (current_price / holding.average_cost_per_share - 1) * 100
                holders.append(
                    {
                        "username": user.username,
                        "display_name": COMMITTEE_ACCOUNT_LABEL
                        if user.decision_architecture == "multi_model"
                        else user.username,
                        "user_type": user.user_type,
                        "decision_architecture": user.decision_architecture,
                        "quantity": holding.quantity,
                        "avg_cost": holding.average_cost_per_share,
                        "current_price": current_price,
                        "pnl": round(pnl, 2),
                        "pnl_percent": round(pnl_percent, 2),
                    }
                )

        return {
            "ticker": ticker,
            "company": instrument["company_name"] or ticker if instrument else ticker,
            "sector": instrument["sector"] or "Unknown" if instrument else "Unknown",
            "instrument_type": instrument["instrument_type"] if instrument else "equity",
            "exchange": instrument["exchange"] if instrument else None,
            "issuer": instrument["issuer"] if instrument else None,
            "category": instrument["category"] if instrument else None,
            "price": price_data.get("price"),
            "previous_close": price_data.get("previous_close"),
            "change_percent": price_data.get("change_percent", 0),
            "volume": price_data.get("volume"),
            "chart_range": chart_range,
            "ohlcv": ohlcv,
            "news": news,
            "research": research[ticker],
            "recent_trades": [dict(row) for row in trade_rows],
            "holders": holders,
        }

    @staticmethod
    def _refresh_stock_news(ticker: str) -> None:
        from services.news_research import refresh

        refresh([ticker], as_of=datetime.now(UTC), lookback_hours=DETAIL_NEWS_LOOKBACK_HOURS)

    @staticmethod
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
