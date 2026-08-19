"""EDGAR request pacing: shared interval, 429 backoff, Retry-After honoring."""

import pytest

from adapters.edgar import throttle


class _FakeResponse:
    def __init__(self, status_code: int, retry_after: str | None = None):
        self.status_code = status_code
        self.headers = {"Retry-After": retry_after} if retry_after is not None else {}


@pytest.fixture(autouse=True)
def _reset_pacing(monkeypatch):
    monkeypatch.setattr(throttle, "_next_slot", 0.0)
    monkeypatch.setattr(throttle, "_MIN_INTERVAL_SECONDS", 0.0)


def _stub_get(monkeypatch, responses):
    calls = []

    def fake_get(url, **_kwargs):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(throttle.requests, "get", fake_get)
    return calls


def test_successful_response_is_returned_without_retry(monkeypatch):
    calls = _stub_get(monkeypatch, [_FakeResponse(200)])

    response = throttle.get("https://www.sec.gov/x", timeout=1.0, headers={})

    assert response.status_code == 200
    assert len(calls) == 1


def test_non_429_error_is_not_retried(monkeypatch):
    calls = _stub_get(monkeypatch, [_FakeResponse(404)])

    response = throttle.get("https://www.sec.gov/x", timeout=1.0, headers={})

    assert response.status_code == 404
    assert len(calls) == 1


def test_429_is_retried_until_success(monkeypatch):
    calls = _stub_get(monkeypatch, [_FakeResponse(429), _FakeResponse(200)])
    monkeypatch.setattr(throttle.time, "sleep", lambda _seconds: None)

    response = throttle.get("https://www.sec.gov/x", timeout=1.0, headers={})

    assert response.status_code == 200
    assert len(calls) == 2


def test_persistent_429_surfaces_the_final_response(monkeypatch):
    calls = _stub_get(monkeypatch, [_FakeResponse(429)] * throttle._MAX_ATTEMPTS)
    monkeypatch.setattr(throttle.time, "sleep", lambda _seconds: None)

    response = throttle.get("https://www.sec.gov/x", timeout=1.0, headers={})

    assert response.status_code == 429
    assert len(calls) == throttle._MAX_ATTEMPTS


def test_retry_after_header_drives_the_backoff(monkeypatch):
    _stub_get(monkeypatch, [_FakeResponse(429, retry_after="7"), _FakeResponse(200)])
    waits = []
    monkeypatch.setattr(throttle.time, "sleep", waits.append)

    throttle.get("https://www.sec.gov/x", timeout=1.0, headers={})

    assert waits == [7.0]


def test_backoff_grows_exponentially_without_retry_after(monkeypatch):
    _stub_get(monkeypatch, [_FakeResponse(429)] * throttle._MAX_ATTEMPTS)
    waits = []
    monkeypatch.setattr(throttle.time, "sleep", waits.append)

    throttle.get("https://www.sec.gov/x", timeout=1.0, headers={})

    assert waits == [throttle._BACKOFF_BASE_SECONDS, throttle._BACKOFF_BASE_SECONDS * 2]
