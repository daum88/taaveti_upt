"""
Tests for the Execution Engine — validates guardrails and trade logic.
Uses in-memory SQLite with full schema.
"""

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


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
    conn.execute("INSERT INTO accounts (id, user_id, cash_balance) VALUES (1, 1, 10000.00)")
    conn.commit()

    @contextmanager
    def mock_get_db():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr("db.connection.get_db", mock_get_db)
    monkeypatch.setattr("models.account.get_db", mock_get_db)
    monkeypatch.setattr("models.holding.get_db", mock_get_db)
    monkeypatch.setattr("models.transaction.get_db", mock_get_db)
    monkeypatch.setattr("models.user.get_db", mock_get_db)
    monkeypatch.setattr("services.execution_engine.get_db", mock_get_db)

    yield conn
    conn.close()


class TestBuyGuardrails:
    """Tests for BUY execution guardrails."""

    def test_buy_successful(self):
        """A valid BUY should deduct cash and create a holding."""
        from services.execution_engine import execute_buy

        prices = {"AAPL": 150.0}
        txn = execute_buy(
            user_id=1, ticker="AAPL", price_per_share=150.0,
            allocation_percentage=0.10, current_prices=prices,
            reasoning="Test buy",
        )

        assert txn.ticker == "AAPL"
        assert txn.transaction_type == "BUY"
        assert txn.total_value == pytest.approx(1000.0, rel=0.01)

        from models.account import Account
        account = Account.get_by_user_id(1)
        assert account.cash_balance == pytest.approx(9000.0, rel=0.01)

        from models.holding import Holding
        holding = Holding.get_by_user_and_ticker(1, "AAPL")
        assert holding is not None
        assert holding.quantity == pytest.approx(1000.0 / 150.0, rel=0.001)

    def test_buy_insufficient_cash_downsizes(self):
        """BUY larger than cash should be downsized to available cash."""
        from services.execution_engine import execute_buy
        from models.account import Account

        # Set cash to $500
        account = Account.get_by_user_id(1)
        account.update_balance(500.0)

        prices = {"AAPL": 150.0}
        txn = execute_buy(
            user_id=1, ticker="AAPL", price_per_share=150.0,
            allocation_percentage=0.20, current_prices=prices,
        )

        # Portfolio = $500 cash. 20% = $100. Position cap = 30% of $500 = $150.
        # Allocation $100 < cap, and $100 < cash $500, so full $100 executes.
        assert txn.total_value == pytest.approx(100.0, rel=0.01)

        # Now try an allocation that exceeds cash after cap
        account = Account.get_by_user_id(1)
        remaining = account.cash_balance  # ~$400
        txn2 = execute_buy(
            user_id=1, ticker="MSFT", price_per_share=200.0,
            allocation_percentage=0.90, current_prices={"AAPL": 150.0, "MSFT": 200.0},
        )
        # 90% allocation gets capped to 30% by position cap, then further capped if > cash
        assert txn2.total_value <= remaining + 0.01

    def test_buy_zero_cash_rejected(self):
        """BUY with $0 cash should raise ExecutionError."""
        from services.execution_engine import execute_buy, ExecutionError
        from models.account import Account

        account = Account.get_by_user_id(1)
        account.update_balance(0.0)

        prices = {"AAPL": 150.0}
        with pytest.raises(ExecutionError, match="(Insufficient cash|Trade amount too small)"):
            execute_buy(
                user_id=1, ticker="AAPL", price_per_share=150.0,
                allocation_percentage=0.10, current_prices=prices,
            )

    def test_buy_position_cap_enforced(self):
        """BUY should be capped at 30% of total portfolio value."""
        from services.execution_engine import execute_buy

        prices = {"AAPL": 150.0}
        # Try to allocate 50% — should be capped to 30%
        txn = execute_buy(
            user_id=1, ticker="AAPL", price_per_share=150.0,
            allocation_percentage=0.50, current_prices=prices,
        )

        # Should cap at 30% of $10,000 = $3,000
        assert txn.total_value == pytest.approx(3000.0, rel=0.01)

    def test_buy_position_cap_rejects_at_limit(self):
        """BUY when already over 30% should raise ExecutionError."""
        from services.execution_engine import execute_buy, ExecutionError
        from models.holding import Holding
        from models.account import Account

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
                user_id=1, ticker="AAPL", price_per_share=150.0,
                allocation_percentage=0.05, current_prices=prices,
            )


class TestSellGuardrails:
    """Tests for SELL execution guardrails."""

    def test_sell_without_holdings_rejected(self):
        """SELL without owning the ticker should raise ExecutionError."""
        from services.execution_engine import execute_sell, ExecutionError

        prices = {"AAPL": 150.0}
        with pytest.raises(ExecutionError, match="No holdings"):
            execute_sell(
                user_id=1, ticker="AAPL", price_per_share=150.0,
                allocation_percentage=0.10, current_prices=prices,
            )

    def test_sell_successful(self):
        """A valid SELL should credit cash and reduce holding."""
        from services.execution_engine import execute_sell
        from models.holding import Holding
        from models.account import Account

        # Pre-seed holding
        Holding.add_shares(1, "AAPL", 10.0, 100.0)

        prices = {"AAPL": 150.0}
        txn = execute_sell(
            user_id=1, ticker="AAPL", price_per_share=150.0,
            allocation_percentage=0.05, current_prices=prices,
        )

        assert txn.ticker == "AAPL"
        assert txn.transaction_type == "SELL"

        account = Account.get_by_user_id(1)
        assert account.cash_balance > 10000.0

    def test_sell_capped_to_available_shares(self):
        """SELL more than owned should be capped to available quantity."""
        from services.execution_engine import execute_sell
        from models.holding import Holding
        from models.account import Account

        # Own 5 shares at $100
        Holding.add_shares(1, "AAPL", 5.0, 100.0)

        prices = {"AAPL": 150.0}
        # Try to sell 90% of portfolio ($9,450 worth) but only own $750
        txn = execute_sell(
            user_id=1, ticker="AAPL", price_per_share=150.0,
            allocation_percentage=0.90, current_prices=prices,
        )

        # Should sell all 5 shares
        assert txn.quantity == pytest.approx(5.0, rel=0.01)
        assert txn.total_value == pytest.approx(750.0, rel=0.01)


class TestRiskEnforcement:
    """Tests for automatic stop-loss and take-profit."""

    def test_stop_loss_triggers_at_minus_8_percent(self):
        """Positions down >8% should be force-sold."""
        from services.execution_engine import auto_enforce_risk_rules
        from models.holding import Holding

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
        from services.execution_engine import auto_enforce_risk_rules
        from models.holding import Holding

        # Buy at $100, now at $116 (up 16%)
        Holding.add_shares(1, "AAPL", 10.0, 100.0)

        prices = {"AAPL": 116.0}
        forced = auto_enforce_risk_rules(1, prices)

        assert len(forced) == 1
        assert forced[0].ticker == "AAPL"
        assert forced[0].transaction_type == "SELL"

    def test_no_trigger_within_bounds(self):
        """Positions within -8% to +15% should NOT be force-sold."""
        from services.execution_engine import auto_enforce_risk_rules
        from models.holding import Holding

        # Buy at $100, now at $105 (up 5%) — within bounds
        Holding.add_shares(1, "AAPL", 10.0, 100.0)

        prices = {"AAPL": 105.0}
        forced = auto_enforce_risk_rules(1, prices)

        assert len(forced) == 0


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

        txn = process_agent_decision(1, decision, prices)
        assert txn is not None
        assert txn.transaction_type == "BUY"
        assert txn.ticker == "AAPL"

    def test_invalid_decision_returns_none(self):
        """A decision with missing ticker should return None."""
        from services.execution_engine import process_agent_decision

        decision = {"ticker": "", "decision": "BUY", "allocation_percentage": 0.10}
        prices = {"AAPL": 150.0}

        result = process_agent_decision(1, decision, prices)
        assert result is None
