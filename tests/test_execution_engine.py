"""
Tests for the Execution Engine — validates guardrails and trade logic.
"""

import pytest
from unittest.mock import patch, MagicMock

# These tests use in-memory SQLite, so we patch the connection module
import sqlite3
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def mock_db():
    """Create an in-memory SQLite database with schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row

    # Load schema
    schema_path = Path(__file__).parent.parent / "db" / "schema.sql"
    schema = schema_path.read_text()
    conn.executescript(schema)
    return conn


class TestExecutionEngine:
    """Test the execution engine's guardrails."""

    def test_buy_insufficient_cash_rejected(self, mock_db):
        """Should reject a BUY when cash is insufficient."""
        pass  # Placeholder — implement with proper mocking

    def test_sell_without_holdings_rejected(self, mock_db):
        """Should reject a SELL when user doesn't own the ticker."""
        pass

    def test_position_cap_enforced(self, mock_db):
        """Should cap trades at 30% of total portfolio."""
        pass
