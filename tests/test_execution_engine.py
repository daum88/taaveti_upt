"""
Tests for the Execution Engine — validates guardrails and trade logic.
Uses in-memory SQLite with full schema.
"""

import sqlite3
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.execution_market import ExecutionMarket, ExecutionQuote


def _execution_market(prices):
    return ExecutionMarket(MappingProxyType({ticker: ExecutionQuote(ticker, price, "2026-01-01T00:00:00+00:00", "test", "live_market") for ticker, price in prices.items()}))


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Patch get_db to use an in-memory SQLite database with full schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    schema_path = Path(__file__).parent.parent / "db" / "schema.sql"
    conn.executescript(schema_path.read_text())

    # Seed a test user + account
    conn.execute("INSERT INTO users (id, username, user_type) VALUES (1, 'testuser', 'human')")
    conn.execute("INSERT INTO accounts (id, user_id, cash_balance_e8) VALUES (1, 1, 1000000000000)")
    conn.commit()

    transaction_depth = 0

    @contextmanager
    def mock_get_db():
        try:
            yield conn
            if not transaction_depth:
                conn.commit()
        except Exception:
            if not transaction_depth:
                conn.rollback()
            raise

    @contextmanager
    def mock_transaction():
        nonlocal transaction_depth
        transaction_depth += 1
        try:
            yield conn
            if transaction_depth == 1:
                conn.commit()
        except Exception:
            if transaction_depth == 1:
                conn.rollback()
            raise
        finally:
            transaction_depth -= 1

    monkeypatch.setattr("db.connection.get_db", mock_get_db)
    monkeypatch.setattr("models.account.get_db", mock_get_db)
    monkeypatch.setattr("models.holding.get_db", mock_get_db)
    monkeypatch.setattr("models.transaction.get_db", mock_get_db)
    monkeypatch.setattr("models.user.get_db", mock_get_db)
    monkeypatch.setattr("services.execution_engine.get_db", mock_get_db)
    monkeypatch.setattr("services.execution_engine.transaction", mock_transaction)

    yield conn
    monkeypatch.undo()
    conn.close()


class TestBuyGuardrails:
    """Tests for BUY execution guardrails."""

    def test_buy_successful(self):
        """A valid BUY should deduct cash and create a holding."""
        from services.execution_engine import execute_buy

        prices = {"AAPL": 150.0}
        txn = execute_buy(
            user_id=1,
            ticker="AAPL",
            price_per_share=150.0,
            allocation_percentage=0.10,
            current_prices=prices,
            reasoning="Test buy",
        )

        assert txn.ticker == "AAPL"
        assert txn.transaction_type == "BUY"
        assert txn.total_value == pytest.approx(1000.0, rel=0.01)

        from models.account import Account
        from models.transaction import Transaction

        account = Account.get_by_user_id(1)
        assert account.cash_balance == pytest.approx(8999.0, rel=0.01)

        fee = Transaction.recent_for_user(1, limit=2)[0]
        assert fee.transaction_type == "FEE"
        assert fee.total_value == 1
        assert fee.cash_balance_before == 9000
        assert fee.cash_balance_after == 8999

        from models.holding import Holding

        holding = Holding.get_by_user_and_ticker(1, "AAPL")
        assert holding is not None
        assert float(holding.quantity) == pytest.approx(1000.0 / 150.0, rel=0.001)

    def test_buy_insufficient_cash_downsizes(self):
        """BUY larger than cash should be downsized to available cash."""
        from models.account import Account
        from services.execution_engine import execute_buy

        # Set cash to $500
        account = Account.get_by_user_id(1)
        account.update_balance(500.0)

        prices = {"AAPL": 150.0}
        txn = execute_buy(
            user_id=1,
            ticker="AAPL",
            price_per_share=150.0,
            allocation_percentage=0.20,
            current_prices=prices,
        )

        # Portfolio = $500 cash. 20% = $100. Position cap = 30% of $500 = $150.
        # Allocation $100 < cap, and $100 < cash $500, so full $100 executes.
        assert txn.total_value == pytest.approx(100.0, rel=0.01)

        # Now try an allocation that exceeds cash after cap
        account = Account.get_by_user_id(1)
        remaining = float(account.cash_balance)  # ~$400
        txn2 = execute_buy(
            user_id=1,
            ticker="MSFT",
            price_per_share=200.0,
            allocation_percentage=0.90,
            current_prices={"AAPL": 150.0, "MSFT": 200.0},
        )
        # 90% allocation gets capped to 30% by position cap, then further capped if > cash
        assert float(txn2.total_value) <= remaining + 0.01

    @pytest.mark.parametrize("price", [0, -1, float("inf"), float("nan"), None])
    def test_buy_rejects_invalid_prices_before_writing_state(self, price):
        """Invalid market prices must never mutate a portfolio."""
        from models.account import Account
        from models.holding import Holding
        from services.execution_engine import ExecutionError, execute_buy

        with pytest.raises(ExecutionError, match="Price per share"):
            execute_buy(1, "AAPL", price, 0.10, {"AAPL": price})

        assert Account.get_by_user_id(1).cash_balance == 10000
        assert Holding.get_by_user_and_ticker(1, "AAPL") is None

    @pytest.mark.parametrize("allocation", [0, -0.1, 1.1, float("inf"), float("nan")])
    def test_buy_rejects_invalid_allocations(self, allocation):
        from services.execution_engine import ExecutionError, execute_buy

        with pytest.raises(ExecutionError, match="Allocation percentage"):
            execute_buy(1, "AAPL", 150, allocation, {"AAPL": 150})

    def test_buy_zero_cash_rejected(self):
        """BUY with $0 cash should raise ExecutionError."""
        from models.account import Account
        from services.execution_engine import ExecutionError, execute_buy

        account = Account.get_by_user_id(1)
        account.update_balance(0.0)

        prices = {"AAPL": 150.0}
        with pytest.raises(ExecutionError, match="(Insufficient cash|Trade amount too small)"):
            execute_buy(
                user_id=1,
                ticker="AAPL",
                price_per_share=150.0,
                allocation_percentage=0.10,
                current_prices=prices,
            )

    def test_buy_position_cap_enforced(self):
        """BUY should be capped at 30% of total portfolio value."""
        from services.execution_engine import execute_buy

        prices = {"AAPL": 150.0}
        # Try to allocate 50% — should be capped to 30%
        txn = execute_buy(
            user_id=1,
            ticker="AAPL",
            price_per_share=150.0,
            allocation_percentage=0.50,
            current_prices=prices,
        )

        # Should cap at 30% of $10,000 = $3,000
        assert txn.total_value == pytest.approx(3000.0, rel=0.01)

    def test_buy_position_cap_rejects_at_limit(self):
        """BUY when already over 30% should raise ExecutionError."""
        from models.account import Account
        from models.holding import Holding
        from services.execution_engine import ExecutionError, execute_buy

        # Pre-seed a position that's already >30%.
        # Cash = $10k, buy AAPL worth $4k = 28.6% of $14k total.
        # Actually: we need to deduct cash for it to be realistic.
        account = Account.get_by_user_id(1)
        account.update_balance(6000.0)  # Spent $4k on AAPL
        Holding.add_shares(1, "AAPL", 26.67, 150.0)  # $4,000 worth

        # Total portfolio = $6,000 cash + $4,000 AAPL = $10,000
        # AAPL = 40% of portfolio — already over 30% cap
        prices = {"AAPL": 150.0}
        with pytest.raises(ExecutionError, match="Position cap"):
            execute_buy(
                user_id=1,
                ticker="AAPL",
                price_per_share=150.0,
                allocation_percentage=0.05,
                current_prices=prices,
            )


class TestStrategyPolicyExecution:
    def test_policy_rejects_ineligible_instrument(self):
        from services.execution_engine import ExecutionError, execute_buy
        from services.strategy_policy import StrategyPolicy

        with pytest.raises(ExecutionError, match="not eligible"):
            execute_buy(1, "MSFT", 100, 0.1, {"MSFT": 100}, policy=StrategyPolicy(eligible_instruments=frozenset({"AAPL"})))

    def test_policy_enforces_maximum_open_positions(self):
        from models.holding import Holding
        from services.execution_engine import ExecutionError, execute_buy
        from services.strategy_policy import StrategyPolicy

        Holding.add_shares(1, "AAPL", 1, 100)
        with pytest.raises(ExecutionError, match="Maximum open positions"):
            execute_buy(1, "MSFT", 100, 0.1, {"AAPL": 100, "MSFT": 100}, policy=StrategyPolicy(max_positions=1))

    def test_policy_preserves_cash_reserve(self):
        from services.execution_engine import execute_buy
        from services.strategy_policy import StrategyPolicy

        transaction = execute_buy(1, "AAPL", 100, 1, {"AAPL": 100}, policy=StrategyPolicy(max_allocation=Decimal("1"), cash_reserve=Decimal("0.2")))

        assert transaction.total_value == 7_999

    def test_rejected_decision_reports_structured_reason(self):
        from services.execution_engine import process_agent_decision
        from services.strategy_policy import StrategyPolicy

        rejections = []
        result = process_agent_decision(
            1,
            {"ticker": "MSFT", "decision": "BUY", "allocation_percentage": 0.1},
            _execution_market({"MSFT": 100}),
            policy=StrategyPolicy(eligible_instruments=frozenset({"AAPL"})),
            on_rejected=rejections.append,
        )

        assert result is None
        assert rejections == [{"code": "execution_rejected", "message": "Instrument MSFT is not eligible for this strategy"}]


class TestHoldingOpeningDate:
    def test_additional_buy_retains_opening_date_and_reopening_sets_a_new_one(self, in_memory_db):
        from models.holding import Holding

        first = Holding.add_shares(1, "AAPL", 10, 100)
        in_memory_db.execute("UPDATE holdings SET opened_at = '2025-01-01T00:00:00.000Z' WHERE id = ?", (first.id,))
        in_memory_db.commit()

        additional = Holding.add_shares(1, "AAPL", 5, 110)
        assert additional.opened_at == "2025-01-01T00:00:00.000Z"

        assert Holding.remove_shares(1, "AAPL", 15) is None
        reopened = Holding.add_shares(1, "AAPL", 5, 120)
        assert reopened.opened_at != "2025-01-01T00:00:00.000Z"


class TestSellGuardrails:
    """Tests for SELL execution guardrails."""

    @pytest.mark.parametrize("price", [0, -1, float("inf"), float("nan"), None])
    def test_sell_rejects_invalid_prices_before_writing_state(self, price):
        from models.account import Account
        from models.holding import Holding
        from services.execution_engine import ExecutionError, execute_sell

        Holding.add_shares(1, "AAPL", 10, 100)
        with pytest.raises(ExecutionError, match="Price per share"):
            execute_sell(1, "AAPL", price, 0.10, {"AAPL": price})

        assert Account.get_by_user_id(1).cash_balance == 10000
        assert Holding.get_by_user_and_ticker(1, "AAPL").quantity == 10

    def test_sell_without_holdings_rejected(self):
        """SELL without owning the ticker should raise ExecutionError."""
        from services.execution_engine import ExecutionError, execute_sell

        prices = {"AAPL": 150.0}
        with pytest.raises(ExecutionError, match="No holdings"):
            execute_sell(
                user_id=1,
                ticker="AAPL",
                price_per_share=150.0,
                allocation_percentage=0.10,
                current_prices=prices,
            )

    def test_sell_successful(self):
        """A valid SELL should credit cash and reduce holding."""
        from models.account import Account
        from models.holding import Holding
        from models.transaction import Transaction
        from services.execution_engine import execute_sell

        # Pre-seed holding
        Holding.add_shares(1, "AAPL", 10.0, 100.0)

        prices = {"AAPL": 150.0}
        txn = execute_sell(
            user_id=1,
            ticker="AAPL",
            price_per_share=150.0,
            allocation_percentage=0.05,
            current_prices=prices,
        )

        assert txn.ticker == "AAPL"
        assert txn.transaction_type == "SELL"
        # Sold at $150 vs $100 cost basis -> positive realized P&L
        assert txn.realized_pnl is not None
        assert float(txn.realized_pnl) == pytest.approx(float(txn.quantity) * 50.0, rel=0.01)

        account = Account.get_by_user_id(1)
        assert account.cash_balance > 10000.0

        fee = Transaction.recent_for_user(1, limit=2)[0]
        assert fee.transaction_type == "FEE"
        assert fee.total_value == 1

    def test_buy_reserves_cash_for_transaction_fee(self):
        """A buy must leave enough cash to pay its fixed fee."""
        from models.account import Account
        from services.execution_engine import ExecutionError, execute_buy

        Account.get_by_user_id(1).update_balance(1)

        with pytest.raises(ExecutionError, match="transaction fee"):
            execute_buy(1, "AAPL", 100, 0.10, {"AAPL": 100})

    def test_sell_capped_to_available_shares(self):
        """SELL more than owned should be capped to available quantity."""
        from models.holding import Holding
        from services.execution_engine import execute_sell

        # Own 5 shares at $100
        Holding.add_shares(1, "AAPL", 5.0, 100.0)

        prices = {"AAPL": 150.0}
        # Try to sell 90% of portfolio ($9,450 worth) but only own $750
        txn = execute_sell(
            user_id=1,
            ticker="AAPL",
            price_per_share=150.0,
            allocation_percentage=0.90,
            current_prices=prices,
        )

        # Should sell all 5 shares
        assert txn.quantity == pytest.approx(5.0, rel=0.01)
        assert txn.total_value == pytest.approx(750.0, rel=0.01)


class TestIndexFundAllocation:
    def test_initial_index_fund_purchase_invests_all_cash_without_a_fee(self):
        from models.account import Account
        from models.transaction import Transaction
        from services.index_fund import seed_index_fund

        assert seed_index_fund(1, price=100)

        transactions = Transaction.recent_for_user(1, limit=2)
        assert [transaction.transaction_type for transaction in transactions] == ["BUY"]
        assert transactions[0].total_value == 10_000
        assert Account.get_by_user_id(1).cash_balance == 0


class TestTradeAtomicity:
    """Tests that a failed trade leaves all persisted trade state unchanged."""

    def test_buy_rolls_back_cash_and_holding_when_log_insert_fails(self, monkeypatch, in_memory_db):
        from models.account import Account
        from models.holding import Holding
        from models.transaction import Transaction
        from services.execution_engine import execute_buy

        def fail_create(*_, **__):
            raise RuntimeError("transaction log unavailable")

        monkeypatch.setattr(Transaction, "create", fail_create)

        with pytest.raises(RuntimeError, match="transaction log unavailable"):
            execute_buy(1, "AAPL", 150.0, 0.10, {"AAPL": 150.0})

        assert Account.get_by_user_id(1).cash_balance == 10000
        assert Holding.get_by_user_and_ticker(1, "AAPL") is None
        assert in_memory_db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0

    def test_sell_rolls_back_cash_and_holding_when_log_insert_fails(self, monkeypatch, in_memory_db):
        from models.account import Account
        from models.holding import Holding
        from models.transaction import Transaction
        from services.execution_engine import execute_sell

        Holding.add_shares(1, "AAPL", 10, 100)

        def fail_create(*_, **__):
            raise RuntimeError("transaction log unavailable")

        monkeypatch.setattr(Transaction, "create", fail_create)

        with pytest.raises(RuntimeError, match="transaction log unavailable"):
            execute_sell(1, "AAPL", 150.0, 0.10, {"AAPL": 150.0})

        assert Account.get_by_user_id(1).cash_balance == 10000
        holding = Holding.get_by_user_and_ticker(1, "AAPL")
        assert holding is not None
        assert holding.quantity == 10
        assert in_memory_db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


class TestRiskEnforcement:
    """Tests for automatic stop-loss and take-profit."""

    def test_stop_loss_triggers_at_minus_8_percent(self):
        """Positions down >8% should be force-sold."""
        from models.holding import Holding
        from services.execution_engine import auto_enforce_risk_rules

        # Buy at $100, now at $91 (down 9%)
        Holding.add_shares(1, "AAPL", 10.0, 100.0)

        prices = {"AAPL": 91.0}
        forced = auto_enforce_risk_rules(1, prices)

        assert len(forced) == 1
        assert forced[0].ticker == "AAPL"
        assert forced[0].transaction_type == "SELL"

        # Holding should be gone
        holding = Holding.get_by_user_and_ticker(1, "AAPL")
        assert holding is None

    def test_take_profit_triggers_at_plus_15_percent(self):
        """Positions up >15% should be force-sold."""
        from models.holding import Holding
        from services.execution_engine import auto_enforce_risk_rules

        # Buy at $100, now at $116 (up 16%)
        Holding.add_shares(1, "AAPL", 10.0, 100.0)

        prices = {"AAPL": 116.0}
        forced = auto_enforce_risk_rules(1, prices)

        assert len(forced) == 1
        assert forced[0].ticker == "AAPL"
        assert forced[0].transaction_type == "SELL"

    def test_no_trigger_within_bounds(self):
        """Positions within -8% to +15% should NOT be force-sold."""
        from models.holding import Holding
        from services.execution_engine import auto_enforce_risk_rules

        # Buy at $100, now at $105 (up 5%) — within bounds
        Holding.add_shares(1, "AAPL", 10.0, 100.0)

        prices = {"AAPL": 105.0}
        forced = auto_enforce_risk_rules(1, prices)

        assert len(forced) == 0

    @pytest.mark.parametrize("price", [None, 0, -1, float("nan")])
    def test_missing_or_invalid_quote_skips_risk_enforcement(self, price, caplog):
        """Risk rules must not value an unavailable quote at the purchase price."""
        from models.holding import Holding
        from services.execution_engine import auto_enforce_risk_rules

        Holding.add_shares(1, "AAPL", 10.0, 100.0)

        forced = auto_enforce_risk_rules(1, {"AAPL": price})

        assert forced == []
        assert "Skipping risk enforcement" in caplog.text
        holding = Holding.get_by_user_and_ticker(1, "AAPL")
        assert holding is not None
        assert holding.quantity == 10


class TestAgentDecisionProcessing:
    """Tests for process_agent_decision."""

    def test_hold_decision_returns_none(self):
        """A HOLD decision should not produce a transaction."""
        from services.execution_engine import process_agent_decision

        decision = {"ticker": "AAPL", "decision": "HOLD", "allocation_percentage": 0.0, "reasoning": "No action"}
        prices = {"AAPL": 150.0}

        result = process_agent_decision(1, decision, prices)
        assert result is None

    def test_buy_decision_executes(self):
        """A BUY decision with valid params should execute."""
        from services.execution_engine import process_agent_decision

        decision = {"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.10, "reasoning": "Momentum signal"}
        prices = {"AAPL": 150.0}

        txn = process_agent_decision(1, decision, _execution_market(prices))
        assert txn is not None
        assert txn.transaction_type == "BUY"
        assert txn.ticker == "AAPL"

    def test_invalid_decision_returns_none(self):
        """A decision with missing ticker should return None."""
        from services.execution_engine import process_agent_decision

        decision = {"ticker": "", "decision": "BUY", "allocation_percentage": 0.10}
        prices = {"AAPL": 150.0}

        result = process_agent_decision(1, decision, _execution_market(prices))
        assert result is None

    @pytest.mark.parametrize(
        "decision, prices",
        [
            (None, {"AAPL": 150.0}),
            ({"ticker": "AAPL", "decision": "BUY", "allocation_percentage": "not-a-number"}, {"AAPL": 150.0}),
            ({"ticker": "AAPL", "decision": "BUY", "allocation_percentage": 0.1}, {"AAPL": 0}),
            ({"ticker": "NOT A TICKER", "decision": "BUY", "allocation_percentage": 0.1}, {"AAPL": 150.0}),
        ],
    )
    def test_malformed_decision_is_rejected_as_hold(self, decision, prices):
        from services.execution_engine import process_agent_decision

        assert process_agent_decision(1, decision, _execution_market(prices)) is None
