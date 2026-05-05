import logging

import pytest
import requests

from config import BotSettings, Settings, parse_targets
from scripts.challenge_whitelist import (
    issue_challenge,
    run_challenges,
    validate_challenge_clock,
    validate_target,
    resolve_challenge_game_id,
    wait_for_challenge_to_become_game,
    wait_for_game_to_finish,
)
from match_lock import MatchLock


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
        enable_prepared_replies_was_set=True,
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
        max_concurrent_games=1,
        enable_auto_resign=False,
        serial_match_mode=True,
        match_lock_path=overrides.pop("match_lock_path", "runtime/active_match.lock"),
        match_lock_stale_seconds=120,
        pending_challenge_timeout_seconds=60,
        challenge_placeholder_stale_seconds=overrides.pop("challenge_placeholder_stale_seconds", 60),
        next_match_cooldown_seconds=5,
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
    instances = []

    def __init__(self, token, username):
        self.token = token
        self.username = username
        self.calls = []
        self.playing_responses = [
            {"nowPlaying": [{"gameId": "game123", "opponent": {"username": "SomeBot"}, "status": "started", "moves": "e2e4"}]},
            {"nowPlaying": []},
        ]
        self.export_response = {"id": "game123", "status": "mate", "winner": "white", "moves": "e2e4 e7e5"}
        self.__class__.instances.append(self)

    def account(self):
        return {"username": self.username, "title": "BOT"}

    def challenge(self, username, seconds=30, increment=0, rated=False, color="random"):
        self.calls.append((username, seconds, increment, rated, color))
        return {"challenge": {"id": "challenge123"}}

    def account_playing(self):
        if self.playing_responses:
            return self.playing_responses.pop(0)
        return {"nowPlaying": []}

    def game_export(self, game_id):
        return self.export_response


class LifecycleClient(FakeClient):
    def __init__(self, token, username):
        super().__init__(token, username)
        self.playing_responses = []
        self.export_responses = [{"id": "game123", "status": "mate", "winner": "white", "moves": "e2e4 e7e5"}]

    def account_playing(self):
        if self.playing_responses:
            return self.playing_responses.pop(0)
        return {"nowPlaying": []}

    def game_export(self, game_id):
        if isinstance(self.export_responses, Exception):
            raise self.export_responses
        if self.export_responses:
            return self.export_responses.pop(0)
        return {"id": game_id, "status": "mate", "winner": "white", "moves": "e2e4 e7e5"}


def test_parse_comma_separated_whitelist():
    assert parse_targets("SomeBot, AnotherBot,,Third") == ("SomeBot", "AnotherBot", "Third")


def test_refuses_non_whitelisted_target():
    with pytest.raises(SystemExit):
        validate_target("Stranger", ("SomeBot",))


def test_refuses_seconds_below_30_by_default():
    with pytest.raises(SystemExit):
        validate_challenge_clock(15, 0)


def test_successful_challenge_returns_challenge_id():
    result = issue_challenge(FakeClient("token", "BotA"), "SomeBot", 30, 0, False, "random")
    assert result.ok
    assert result.challenge_id == "challenge123"


def test_run_challenges_uses_whitelist_without_network(monkeypatch, tmp_path):
    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", lambda _seconds: None)
    assert run_challenges(settings(match_lock_path=tmp_path / "active_match.lock"), once=True, loop=False, target="SomeBot", seconds=30, increment=0, client_factory=FakeClient) == 1


def test_successful_once_challenge_leaves_placeholder_active(monkeypatch, tmp_path):
    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", lambda _seconds: None)
    path = tmp_path / "active_match.lock"
    assert run_challenges(settings(match_lock_path=path), once=True, loop=False, target="SomeBot", seconds=30, increment=0, client_factory=FakeClient) == 1
    info = MatchLock(path).read()
    assert info is not None
    assert info.game_id == "challenge:outbound:SomeBot"


def test_second_once_challenge_skips_while_placeholder_active(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", lambda _seconds: None)
    path = tmp_path / "active_match.lock"
    assert MatchLock(path, placeholder_stale_seconds=60).acquire("challenge:outbound:SomeBot", "BotA")
    with caplog.at_level(logging.INFO):
        assert run_challenges(settings(match_lock_path=path), once=True, loop=False, target="SomeBot", seconds=30, increment=0, client_factory=FakeClient) == 1
    assert "challenge pending" in caplog.text
    assert MatchLock(path).read().game_id == "challenge:outbound:SomeBot"


def test_real_game_lock_blocks_outbound_challenge(monkeypatch, tmp_path):
    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", lambda _seconds: None)
    path = tmp_path / "active_match.lock"
    assert MatchLock(path).acquire("real-game", "BotA")
    assert run_challenges(settings(match_lock_path=path), once=True, loop=False, target="SomeBot", seconds=30, increment=0, client_factory=FakeClient) == 1
    assert MatchLock(path).read().game_id == "real-game"


def test_stale_challenge_placeholder_does_not_block(monkeypatch, tmp_path):
    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", lambda _seconds: None)
    path = tmp_path / "active_match.lock"
    lock = MatchLock(path, stale_seconds=120, placeholder_stale_seconds=0)
    assert lock.acquire("challenge:outbound:SomeBot", "BotA")
    assert run_challenges(
        settings(match_lock_path=path, pending_challenge_timeout_seconds=0, challenge_placeholder_stale_seconds=0),
        once=True,
        loop=False,
        target="SomeBot",
        seconds=30,
        increment=0,
        client_factory=FakeClient,
    ) == 1
    info = MatchLock(path).read()
    assert info is not None
    assert info.game_id == "challenge:outbound:SomeBot"
    assert info.bot_username == "HyperBulletBot"


def test_non_stale_real_game_lock_still_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", lambda _seconds: None)
    path = tmp_path / "active_match.lock"
    assert MatchLock(path, stale_seconds=120, placeholder_stale_seconds=0).acquire("real-game", "BotA")
    assert run_challenges(
        settings(match_lock_path=path, pending_challenge_timeout_seconds=0, challenge_placeholder_stale_seconds=0),
        once=True,
        loop=False,
        target="SomeBot",
        seconds=30,
        increment=0,
        client_factory=FakeClient,
    ) == 1
    assert MatchLock(path).read().game_id == "real-game"


def test_wait_mode_waits_while_placeholder_lock_exists(monkeypatch, tmp_path):
    path = tmp_path / "active_match.lock"
    lock = MatchLock(path, stale_seconds=120, placeholder_stale_seconds=60)
    assert lock.acquire("challenge:outbound:SomeBot", "BotA")
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            lock.release_if_game("challenge:outbound:SomeBot")

    FakeClient.instances = []
    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", fake_sleep)
    assert run_challenges(
        settings(match_lock_path=path),
        once=False,
        loop=True,
        target="SomeBot",
        seconds=30,
        increment=0,
        client_factory=FakeClient,
        wait_for_free=True,
        count_limit=1,
    ) == 1
    assert FakeClient.instances[-1].calls == [("SomeBot", 30, 0, False, "random")]


def test_wait_for_challenge_to_become_game_detects_game_id(monkeypatch):
    client = LifecycleClient("token", "BotA")
    client.export_responses = [{"id": "challenge123", "status": "started", "moves": ""}]
    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", lambda _seconds: None)
    assert wait_for_challenge_to_become_game(client, "challenge123", "SomeBot", 10) == "challenge123"


def test_resolve_falls_back_to_account_playing_if_export_unavailable(monkeypatch):
    client = LifecycleClient("token", "BotA")
    response = requests.Response()
    response.status_code = 404
    client.export_responses = requests.HTTPError("not found", response=response)
    client.playing_responses = [{"nowPlaying": [{"gameId": "game123", "opponent": {"username": "SomeBot"}, "status": "started"}]}]
    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", lambda _seconds: None)
    assert resolve_challenge_game_id(client, "challenge123", "SomeBot", 10) == "game123"


def test_wait_for_game_to_finish_waits_until_final(monkeypatch):
    client = LifecycleClient("token", "BotA")
    client.export_responses = [
        {"id": "game123", "status": "started", "moves": "e2e4"},
        {"id": "game123", "status": "mate", "winner": "white", "moves": "e2e4 e7e5"},
    ]
    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", lambda _seconds: None)
    final = wait_for_game_to_finish(client, "game123")
    assert final["status"] == "mate"
    assert final["winner"] == "white"
    assert final["ply_count"] == 2


def test_wait_mode_waits_while_real_game_lock_exists(monkeypatch, tmp_path):
    path = tmp_path / "active_match.lock"
    lock = MatchLock(path, stale_seconds=120, placeholder_stale_seconds=60)
    assert lock.acquire("real-game", "BotA")

    def fake_sleep(_seconds):
        lock.release_if_game("real-game")

    FakeClient.instances = []
    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", fake_sleep)
    assert run_challenges(
        settings(match_lock_path=path),
        once=False,
        loop=True,
        target="SomeBot",
        seconds=30,
        increment=0,
        client_factory=FakeClient,
        wait_for_free=True,
        count_limit=1,
    ) == 1
    assert FakeClient.instances[-1].calls == [("SomeBot", 30, 0, False, "random")]


def test_count_two_issues_two_challenges_without_overlap(monkeypatch, tmp_path):
    path = tmp_path / "active_match.lock"

    FakeClient.instances = []
    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", lambda _seconds: None)
    lifecycle = {"finished": 0}

    def fake_resolve(_client, challenge_id, _target, _timeout):
        assert lifecycle["finished"] == len(FakeClient.instances[-1].calls) - 1
        return challenge_id

    def fake_finish(_client, _game_id):
        lifecycle["finished"] += 1
        return {"status": "mate", "winner": "white", "ply_count": 10, "url": "https://lichess.org/game123"}

    monkeypatch.setattr("scripts.challenge_whitelist.resolve_challenge_game_id", fake_resolve)
    monkeypatch.setattr("scripts.challenge_whitelist.wait_for_game_to_finish", fake_finish)
    assert run_challenges(
        settings(match_lock_path=path),
        once=False,
        loop=True,
        target="SomeBot",
        seconds=30,
        increment=0,
        client_factory=FakeClient,
        wait_for_free=True,
        count_limit=2,
    ) == 2
    assert FakeClient.instances[-1].calls == [("SomeBot", 30, 0, False, "random"), ("SomeBot", 30, 0, False, "random")]
    assert lifecycle["finished"] == 2


def test_wait_mode_locked_state_does_not_increment_failures(monkeypatch, tmp_path, caplog):
    path = tmp_path / "active_match.lock"
    lock = MatchLock(path, stale_seconds=120, placeholder_stale_seconds=60)
    assert lock.acquire("real-game", "BotA")

    def fake_sleep(_seconds):
        lock.release_if_game("real-game")

    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", fake_sleep)
    with caplog.at_level(logging.INFO):
        assert run_challenges(
            settings(match_lock_path=path),
            once=False,
            loop=True,
            target="SomeBot",
            seconds=30,
            increment=0,
            client_factory=FakeClient,
            wait_for_free=True,
            count_limit=1,
        ) == 1
    assert "failures=0" in caplog.text


def test_issue_challenge_handles_http_429_without_crashing(monkeypatch, caplog):
    class RateLimitedClient:
        def challenge(self, *args, **kwargs):
            raise response_error(429)

    monkeypatch.setattr("scripts.challenge_whitelist.time.sleep", lambda _seconds: None)
    with caplog.at_level(logging.WARNING):
        assert not issue_challenge(RateLimitedClient(), "SomeBot", 30, 0, False, "random")
    assert "rate limited" in caplog.text.lower()
    assert "challenge failed" in caplog.text


def test_issue_challenge_handles_http_400_without_crashing(caplog):
    class BadRequestClient:
        def challenge(self, *args, **kwargs):
            raise response_error(400)

    with caplog.at_level(logging.WARNING):
        assert not issue_challenge(BadRequestClient(), "SomeBot", 30, 0, False, "random")
    assert "status=400" in caplog.text
    assert "challenge failed" in caplog.text
