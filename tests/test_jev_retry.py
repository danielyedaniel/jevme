"""The Jev client rides out short outages (a burst of 503s was seen live) instead of failing the task."""
import httpx
import pytest

from jevme import config
from jevme.jev import JevClient

OK = {"answers": {"intent": {"type": "choice", "choice": "Chrome", "confidence": 0.9, "probabilities": {}}}}


def client(responses):
    config.TYPESAFE_API_KEY = config.TYPESAFE_API_KEY or "test"
    j = JevClient()
    calls = iter(responses)
    j._client = httpx.Client(transport=httpx.MockTransport(lambda req: next(calls)))
    return j


def test_retries_through_503s(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    j = client([httpx.Response(503), httpx.Response(503), httpx.Response(200, json=OK)])
    answers, _ = j.ask({}, {})
    assert answers["intent"].choice == "Chrome"


def test_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    j = client([httpx.Response(503)] * 3)
    with pytest.raises(httpx.HTTPStatusError):
        j.ask({}, {})


def test_bad_request_is_not_retried():
    j = client([httpx.Response(400), httpx.Response(200, json=OK)])
    with pytest.raises(httpx.HTTPStatusError):
        j.ask({}, {})
