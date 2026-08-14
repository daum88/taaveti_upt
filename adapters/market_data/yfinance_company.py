"""Company-profile lookup via yfinance.

This is a true external port: it pulls a ticker's descriptive profile (display
name and sector) from yfinance and degrades to the ticker symbol with an
``Unknown`` sector when the provider has no data. Callers receive a clean,
stable dict and never see the provider's payload shape or error modes.
"""

import yfinance as yf


def fetch_ticker_info(ticker: str) -> dict:
    """Fetch company name and sector for a ticker."""
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        return {
            "company_name": info.get("longName") or info.get("shortName", ticker),
            "sector": info.get("sector", "Unknown"),
            "market_cap": info.get("marketCap"),
        }
    except Exception:
        return {"company_name": ticker, "sector": "Unknown", "market_cap": None}
