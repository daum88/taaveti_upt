import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from services import market_data


def test_fetch_prices_batch_uses_latest_intraday_quote_while_market_is_open(monkeypatch):
    index = pd.DatetimeIndex([
        "2026-08-03 15:59:00-04:00",
        "2026-08-04 09:30:00-04:00",
        "2026-08-04 09:31:00-04:00",
    ])
    prices = pd.DataFrame({"AAPL": [99.0, 100.0, 102.0]}, index=index)
    volumes = pd.DataFrame({"AAPL": [1_000, 1_100, 1_200]}, index=index)
    download = pd.concat({"Close": prices, "Volume": volumes}, axis=1)
    requested = {}

    def fake_download(*args, **kwargs):
        requested.update(kwargs)
        return download

    monkeypatch.setattr(market_data, "is_market_open", lambda: True)
    monkeypatch.setattr(market_data.yf, "download", fake_download)

    assert market_data.fetch_prices_batch(["AAPL"]) == {
        "AAPL": {"price": 102.0, "previous_close": 99.0, "change_percent": 3.0303, "volume": 1_200}
    }
    assert requested["period"] == "2d"
    assert requested["interval"] == "1m"


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
