import logging

import pytest
import requests

from lichess_client import LichessClient
from run_bot import handle_challenge_event


def response_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://lichess.org/api/challenge/test/accept"
    response._content = b"challenge expired"
    response.headers["content-type"] = "application/json"
    return requests.HTTPError(f"{status_code} error", response=response)


def good_challenge(**overrides):
    challenge = {
        "id": "abc123",
        "rated": False,
        "variant": {"key": "standard"},
        "speed": "bullet",
        "perf": {"key": "bullet"},
        "timeControl": {"type": "clock", "limit": 30, "increment": 0},
        "challenger": {"id": "human", "username": "HumanTester"},
    }
    challenge.update(overrides)
    return challenge


def test_try_accept_http_400_returns_false(monkeypatch, caplog):
    client = LichessClient("token", "bot")
    challenge = good_challenge()
    monkeypatch.setattr(client, "accept_challenge", lambda _challenge_id: (_ for _ in ()).throw(response_error(400)))
    with caplog.at_level(logging.WARNING):
        assert client.try_accept_challenge("abc123", challenge) is False
    assert "Could not accept challenge abc123" in caplog.text
    assert "status=400" in caplog.text
    assert "challenge expired" in caplog.text
    assert "speed=bullet" in caplog.text


def test_try_decline_http_400_returns_false(monkeypatch, caplog):
    client = LichessClient("token", "bot")
    challenge = good_challenge()
    monkeypatch.setattr(client, "decline_challenge", lambda *_args: (_ for _ in ()).throw(response_error(400)))
    with caplog.at_level(logging.WARNING):
        assert client.try_decline_challenge("abc123", "time", challenge) is False
    assert "Could not decline challenge abc123" in caplog.text
    assert "challenge expired" in caplog.text


def test_handle_challenge_event_unexpected_accept_exception_does_not_raise(caplog):
    class BadClient:
        def try_accept_challenge(self, challenge_id):
            raise RuntimeError("boom")

        def try_decline_challenge(self, challenge_id, reason):
            raise AssertionError("should not decline")

    with caplog.at_level(logging.WARNING):
        handle_challenge_event(BadClient(), good_challenge(), allow_human_challenges=True)
    assert "Unexpected error while accepting challenge abc123" in caplog.text


def test_handle_challenge_event_unexpected_decline_exception_does_not_raise(caplog):
    class BadClient:
        def try_accept_challenge(self, challenge_id):
            raise AssertionError("should not accept")

        def try_decline_challenge(self, challenge_id, reason):
            raise RuntimeError("boom")

    bad = good_challenge(timeControl={"type": "clock", "limit": 30, "increment": 1})
    with caplog.at_level(logging.WARNING):
        handle_challenge_event(BadClient(), bad, allow_human_challenges=True)
    assert "Unexpected error while declining challenge abc123" in caplog.text
