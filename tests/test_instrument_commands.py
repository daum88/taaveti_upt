import pytest
from fastapi.testclient import TestClient

from adapters.web.app import create_app
from application.instrument_commands import (
    InstrumentCommandError,
    InstrumentCommands,
    InstrumentDefinition,
    InstrumentNotFound,
)
from services.instrument_universe import InstrumentValidationError


def _instrument(ticker: str = "AAPL") -> dict:
    return {
        "ticker": ticker,
        "company_name": "Apple Inc.",
        "sector": "Technology",
        "instrument_type": "equity",
        "exchange": "NASDAQ",
        "issuer": None,
        "category": None,
        "is_active": True,
    }


def test_add_hides_provider_validation_and_maps_its_error():
    definition = InstrumentDefinition("AAPL", "equity", company_name="Apple Inc.")
    captured = []
    commands = InstrumentCommands(writer=lambda **values: captured.append(values) or _instrument())

    assert commands.add(definition) == _instrument()
    assert captured == [
        {
            "ticker": "AAPL",
            "instrument_type": "equity",
            "company_name": "Apple Inc.",
            "sector": None,
            "exchange": None,
            "issuer": None,
            "category": None,
            "is_active": True,
        }
    ]

    failing = InstrumentCommands(writer=lambda **_: (_ for _ in ()).throw(InstrumentValidationError("bad ticker")))
    with pytest.raises(InstrumentCommandError, match="bad ticker"):
        failing.add(definition)


def test_activation_exposes_a_stable_not_found_error():
    commands = InstrumentCommands(
        activator=lambda *_: (_ for _ in ()).throw(InstrumentValidationError("Ticker is unavailable"))
    )

    with pytest.raises(InstrumentNotFound, match="Ticker is unavailable"):
        commands.set_active("MISSING", True)


def test_etf_import_owns_the_configured_activation_policy():
    captured = []
    commands = InstrumentCommands(
        importer=lambda **values: (
            captured.append(values) or {"version": 1, "count": 17, "imported": 0, "dry_run": True}
        ),
        etf_universe_enabled=False,
    )

    assert commands.import_etfs(dry_run=True) == {"version": 1, "count": 17, "imported": 0, "dry_run": True}
    assert captured == [{"active": False, "dry_run": True}]


def test_instrument_route_uses_the_injected_command_module():
    captured = []

    class Commands:
        @staticmethod
        def add(definition):
            captured.append(definition)
            return _instrument()

    app = create_app(instrument_commands=Commands())
    response = TestClient(app).post(
        "/api/instruments",
        json={"ticker": "aapl", "instrument_type": "equity", "company_name": "Apple Inc."},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "instrument": _instrument()}
    assert captured == [InstrumentDefinition("AAPL", "equity", company_name="Apple Inc.")]
