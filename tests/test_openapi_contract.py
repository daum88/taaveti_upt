import json
from pathlib import Path

from fastapi.testclient import TestClient

from adapters.web.app import create_app
from settings import load_settings

PROJECT_ROOT = Path(__file__).parent.parent
OPENAPI_ARTIFACT = PROJECT_ROOT / "docs" / "openapi.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
ERROR_SCHEMA = {"$ref": "#/components/schemas/ErrorResponse"}


def test_openapi_artifact_matches_the_application_contract():
    assert json.loads(OPENAPI_ARTIFACT.read_text()) == create_app().openapi()


def test_every_json_api_success_has_a_response_schema():
    schema = create_app().openapi()

    for path, path_item in schema["paths"].items():
        if not path.startswith("/api/") or path == "/api/export/csv":
            continue
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            successful_responses = [
                response for status, response in operation["responses"].items() if status.startswith("2")
            ]
            assert successful_responses, f"{method.upper()} {path} has no successful response"
            for response in successful_responses:
                assert response["content"]["application/json"]["schema"], (
                    f"{method.upper()} {path} has no JSON response schema"
                )


def test_every_documented_json_error_uses_the_shared_envelope():
    schema = create_app().openapi()

    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            for status, response in operation["responses"].items():
                if status.startswith("2"):
                    continue
                actual = response.get("content", {}).get("application/json", {}).get("schema")
                assert actual == ERROR_SCHEMA, f"{method.upper()} {path} documents {status} as {actual}"


def test_non_loopback_operator_actions_require_a_bearer_token():
    token = "a" * 32
    app = create_app(settings=load_settings({"SERVER_HOST": "0.0.0.0", "OPERATOR_TOKEN": token}))
    client = TestClient(app, client=("203.0.113.10", 50_000))
    assert TestClient(app).post("/api/cycle/check").status_code == 401

    mutation_requests = (
        ("post", "/api/trade/preview", {"ticker": "AAPL", "action": "BUY", "amount_dollars": 100}),
        (
            "post",
            "/api/trade",
            {
                "ticker": "AAPL",
                "action": "BUY",
                "amount_dollars": 100,
                "client_order_id": "8578787f-4a6b-4fe3-a042-a31b454131f8",
            },
        ),
        ("post", "/api/agents", {"username": "agent", "config": {}}),
        ("post", "/api/build-portfolio/agent", None),
        ("post", "/api/analyze/agent", None),
        ("post", "/api/chat/agent", {"message": "hello"}),
        ("post", "/api/reset", None),
        ("post", "/api/cycle", None),
        ("post", "/api/cycle/check", None),
        ("post", "/api/decision-batches", None),
        ("post", "/api/instruments", {"ticker": "AAPL", "instrument_type": "equity"}),
        ("patch", "/api/instruments/AAPL/active", {"is_active": True}),
        ("post", "/api/instruments/import-etfs", None),
    )

    for method, path, payload in mutation_requests:
        response = getattr(client, method)(path, json=payload)
        assert response.status_code == 401, f"{method.upper()} {path} was not protected"
        assert response.json()["code"] == "http_401"


def test_non_loopback_operator_actions_accept_the_configured_bearer_token():
    token = "a" * 32
    app = create_app(settings=load_settings({"SERVER_HOST": "0.0.0.0", "OPERATOR_TOKEN": token}))

    class Scheduler:
        @staticmethod
        def trigger_if_required():
            return True

        @staticmethod
        def status():
            return {
                "running": True,
                "last_run": None,
                "next_run": None,
                "in_progress": False,
                "last_result": None,
            }

    app.state.runtime.market_refresh_scheduler = Scheduler()
    response = TestClient(app, client=("203.0.113.10", 50_000)).post(
        "/api/cycle/check",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["triggered"] is True


def test_validation_and_access_failures_share_the_runtime_error_envelope():
    validation = TestClient(create_app()).get("/api/watchlist?limit=0")
    forbidden = TestClient(create_app(), client=("203.0.113.10", 50_000)).post("/api/cycle/check")

    assert validation.status_code == 422
    assert validation.json() == {
        "ok": False,
        "error": "Request validation failed.",
        "code": "request_validation_failed",
        "details": [
            {
                "location": ["query", "limit"],
                "message": "Input should be greater than or equal to 1",
                "type": "greater_than_equal",
            }
        ],
    }
    assert forbidden.status_code == 403
    assert forbidden.json() == {
        "ok": False,
        "error": "Operator actions are available only from the local server.",
        "code": "http_403",
    }
