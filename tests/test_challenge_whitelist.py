import logging

import pytest
import requests

from config import BotSettings, Settings, parse_targets
from scripts.challenge_whitelist import issue_challenge, run_challenges, validate_challenge_clock, validate_target


def settings(**overrides):
    data = dict(
        bot_1=BotSettings(username="HyperBulletBot", token="token"),
        bot_2=None,
        stockfish_path="stockfish",
        dry_run=False,
        log_dir="logs",
        dashboard_host="127.0.0.1",
        dashboard_port=3000,
        challenge_cooldown_seconds=20,
        default_rated=False,
        enable_prepared_replies=False,
        prepare_reply_budget_ms=0,
        allow_human_challenges=True,
        min_clock_limit_seconds=30,
        max_clock_limit_seconds=30,
        bot_challenge_targets=("SomeBot", "AnotherBot"),
        outbound_challenges_enabled=True,
        outbound_challenge_seconds=30,
        outbound_challenge_increment=0,
        outbound_challenge_rated=False,
        outbound_challenge_cooldown_seconds=300,
        outbound_challenge_max_per_session=10,
        outbound_challenge_color="random",
    )
    data.update(overrides)
    return Settings(**data)


def response_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://lichess.org/api/challenge/SomeBot"
    response._content = b"challenge failed"
    response.headers["content-type"] = "application/json"
    return requests.HTTPError(f"{status_code} error", response=response)


class FakeClient:
    def __init__(self, token, username):
        self.token = token
        self.username = username
        self.calls = []

    def account(self):
        return {"username": self.username, "title": "BOT"}

    def challenge(self, username, seconds=30, increment=0, rated=False, color="random"):
        self.calls.append((username, seconds, increment, rated, color))
        return {"challenge": {"id": "challenge123"}}


def test_parse_comma_separated_whitelist():
    assert parse_targets("SomeBot, AnotherBot,,Third") == ("SomeBot", "AnotherBot", "Third")


def test_refuses_non_whitelisted_target():
    with pytest.raises(SystemExit):
        validate_target("Stranger", ("SomeBot",))


def test_refuses_seconds_below_30_by_default():
    with pytest.raises(SystemExit):
        validate_challenge_clock(15, 0)


def test_run_challenges_uses_whitelist_without_network(monkeypatch):
    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", lambda _seconds: None)
    assert run_challenges(settings(), once=True, loop=False, target="SomeBot", seconds=30, increment=0, client_factory=FakeClient) == 1


def test_issue_challenge_handles_http_429_without_crashing(monkeypatch, caplog):
    class RateLimitedClient:
        def challenge(self, *args, **kwargs):
            raise response_error(429)

    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", lambda _seconds: None)
    with caplog.at_level(logging.WARNING):
        assert issue_challenge(RateLimitedClient(), "SomeBot", 30, 0, False, "random") is False
    assert "rate limited" in caplog.text.lower()
    assert "challenge failed" in caplog.text


def test_issue_challenge_handles_http_400_without_crashing(caplog):
    class BadRequestClient:
        def challenge(self, *args, **kwargs):
            raise response_error(400)

    with caplog.at_level(logging.WARNING):
        assert issue_challenge(BadRequestClient(), "SomeBot", 30, 0, False, "random") is False
    assert "status=400" in caplog.text
    assert "challenge failed" in caplog.text
