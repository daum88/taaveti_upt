import json
from pathlib import Path

from fastapi.testclient import TestClient

from adapters.web.app import create_app

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
