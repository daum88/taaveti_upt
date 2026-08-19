"""Application initialization orchestration coverage."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from application import initialization


def test_initialize_orchestrates_database_setup_behind_one_interface(monkeypatch):
    events = []
    settings = object()

    monkeypatch.setattr(initialization, "init_db", lambda: events.append("database"))
    monkeypatch.setattr(
        initialization,
        "InstrumentCommands",
        lambda *, settings: SimpleNamespace(import_etfs=lambda: {"imported": 3}),
    )
    monkeypatch.setattr(initialization, "_seed_default_users", lambda received: events.append(("users", received)) or 4)
    monkeypatch.setattr(
        initialization, "_seed_comparison_profiles", lambda received: events.append(("profiles", received))
    )
    monkeypatch.setattr(initialization, "_seed_committee", lambda received: events.append(("committee", received)))
    monkeypatch.setattr(
        initialization, "_seed_watchlist", lambda received: events.append(("watchlist", received)) or 500
    )

    assert initialization.initialize(settings) == initialization.InitializationResult(4, 500, 3, None)
    assert events == [
        "database",
        ("users", settings),
        ("profiles", settings),
        ("committee", settings),
        ("watchlist", settings),
    ]


def test_warmup_filing_briefs_covers_the_latest_funnel_universe_and_committee_holdings(tmp_path, monkeypatch):
    from adapters.sqlite.connection import close_db, get_db, init_db
    from settings import load_settings

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (id, username, user_type, decision_architecture) VALUES (1, 'committee', 'llm_agent', 'multi_model')"
        )
        conn.execute("INSERT INTO accounts (user_id) VALUES (1)")
        conn.execute(
            "INSERT INTO holdings (user_id, ticker, quantity_e8, average_cost_per_share_e8) VALUES (1, 'TSLA', 100000000, 20000000000)"
        )
        conn.execute("INSERT INTO funnel_cycles (id, status) VALUES (1, 'completed')")
        conn.execute(
            "INSERT INTO decision_batches (id, triggered_at, status) VALUES (1, ?, 'completed')",
            (datetime.now(UTC).isoformat(),),
        )
        conn.execute(
            "INSERT INTO decision_batch_snapshots (batch_id, funnel_cycle_id, captured_at, content_hash, serialized_snapshot)"
            " VALUES (1, 1, ?, 'hash', ?)",
            (
                datetime.now(UTC).isoformat(),
                json.dumps({"funnel_stocks": [{"ticker": "AAPL"}, {"ticker": "MSFT"}]}),
            ),
        )
    captured = {}

    def fake_refresh(tickers, *, settings):
        captured["refresh_tickers"] = tickers
        return {"scanned": 3, "cached": 0, "empty": 0, "failed": 0, "new_documents": 2}

    def fake_briefs(tickers, *, as_of, settings):
        captured["briefs_tickers"] = tickers
        return {"AAPL": [{}], "TSLA": [{}, {}]}

    monkeypatch.setattr("services.filing_briefs.refresh", fake_refresh)
    monkeypatch.setattr("services.filing_briefs.briefs", fake_briefs)

    result = initialization.warmup_filing_briefs(load_settings({}))

    assert captured["refresh_tickers"] == ("AAPL", "MSFT", "TSLA")
    assert captured["briefs_tickers"] == ("AAPL", "MSFT", "TSLA")
    assert result == initialization.FilingBriefsWarmupResult(("AAPL", "MSFT", "TSLA"), 2, 3)
    close_db()


def test_warmup_filing_briefs_short_circuits_without_a_committee_universe(tmp_path, monkeypatch):
    from adapters.sqlite.connection import close_db, init_db
    from settings import load_settings

    close_db()
    monkeypatch.setattr("config.DB_PATH", tmp_path / "portfolio.db")
    init_db()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("no universe means no pipeline work")

    monkeypatch.setattr("services.filing_briefs.refresh", forbidden)
    monkeypatch.setattr("services.filing_briefs.briefs", forbidden)

    assert initialization.warmup_filing_briefs(load_settings({})) == initialization.FilingBriefsWarmupResult((), 0, 0)
    close_db()


def test_initialize_optionally_runs_cache_warmup(monkeypatch):
    settings = object()
    warmup = initialization.WarmupResult(100, 25)

    monkeypatch.setattr(initialization, "init_db", lambda: None)
    monkeypatch.setattr(
        initialization,
        "InstrumentCommands",
        lambda *, settings: SimpleNamespace(import_etfs=lambda: {"imported": 0}),
    )
    monkeypatch.setattr(initialization, "_seed_default_users", lambda _: 0)
    monkeypatch.setattr(initialization, "_seed_comparison_profiles", lambda _: None)
    monkeypatch.setattr(initialization, "_seed_committee", lambda _: None)
    monkeypatch.setattr(initialization, "_seed_watchlist", lambda _: 0)
    monkeypatch.setattr(initialization, "warmup_cache", lambda received: warmup)

    assert initialization.initialize(settings, warmup=True).warmup is warmup
