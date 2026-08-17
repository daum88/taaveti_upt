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
