import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import market_data


def test_fetch_ohlcv_excludes_rows_with_missing_price_values(monkeypatch):
    history = pd.DataFrame(
        {
            "Open": [100.0, float("nan")],
            "High": [101.0, float("nan")],
            "Low": [99.0, float("nan")],
            "Close": [100.5, float("nan")],
            "Volume": [1_000, 2_000],
        },
        index=[datetime(2026, 1, 2), datetime(2026, 1, 3)],
    )

    class Ticker:
        def history(self, **_):
            return history

    monkeypatch.setattr(market_data.yf, "Ticker", lambda _: Ticker())

    assert market_data.fetch_ohlcv("AAPL") == [{"date": "2026-01-02", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1_000}]
