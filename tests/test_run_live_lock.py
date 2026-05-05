from pathlib import Path

import pytest

from config import BotSettings, Settings
from match_lock import MatchLock
from run_bot import run_live
from run_bot import run_game


def live_settings(tmp_path):
    return Settings(
        bot_1=BotSettings(username="BotA", token="token"),
        bot_2=None,
        stockfish_path=Path("stockfish"),
        dry_run=False,
        log_dir=tmp_path / "logs",
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
        bot_challenge_targets=(),
        outbound_challenges_enabled=False,
        outbound_challenge_seconds=30,
        outbound_challenge_increment=0,
        outbound_challenge_rated=False,
        outbound_challenge_cooldown_seconds=300,
        outbound_challenge_max_per_session=10,
        outbound_challenge_color="random",
        max_concurrent_games=1,
        enable_auto_resign=False,
        serial_match_mode=True,
        match_lock_path=tmp_path / "runtime" / "active_match.lock",
        match_lock_stale_seconds=120,
        pending_challenge_timeout_seconds=60,
        challenge_placeholder_stale_seconds=60,
        next_match_cooldown_seconds=0,
    )


def test_run_live_logs_overlapping_game_start_while_lock_active(monkeypatch, tmp_path, caplog):
    settings = live_settings(tmp_path)
    lock = MatchLock(settings.match_lock_path, settings.match_lock_stale_seconds)
    assert lock.acquire("existing-game", "BotA")

    class FakeClient:
        def __init__(self, token, username):
            self.token = token
            self.username = username

        def assert_bot_account(self):
            return None

        def stream_events(self):
            yield {"type": "gameStart", "game": {"id": "new-game"}}
            raise KeyboardInterrupt

    monkeypatch.setattr("run_bot.load_settings", lambda: settings)
    monkeypatch.setattr("run_bot.require_bot_token", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("run_bot.validate_stockfish_path", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("run_bot.probe_stockfish_launch", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("run_bot.LichessClient", FakeClient)
    monkeypatch.setattr("run_bot.start_dashboard", lambda *_args, **_kwargs: None)

    with caplog.at_level("ERROR"), pytest.raises(KeyboardInterrupt):
        run_live(bot_index=1, dashboard=False, quality_mode="hyper")
    assert "OVERLAP DETECTED" in caplog.text
    assert "new-game" in caplog.text


def test_run_game_releases_real_lock_after_game_end_cooldown(monkeypatch, tmp_path):
    settings = live_settings(tmp_path)
    lock = MatchLock(settings.match_lock_path, settings.match_lock_stale_seconds, settings.pending_challenge_timeout_seconds)
    assert lock.acquire("real-game", "BotA")

    class FakeClient:
        username = "BotA"

        def stream_game(self, game_id):
            yield {
                "type": "gameFull",
                "white": {"id": "bota", "name": "BotA"},
                "black": {"id": "botb", "name": "BotB"},
                "clock": {"initial": 30000},
                "state": {"status": "mate", "winner": "white", "moves": "e2e4"},
            }

    class FakeEngine:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def verify_move(self, *_args, **_kwargs):
            return 0

        def analyse_candidates(self, *_args, **_kwargs):
            return type("Result", (), {"candidates": []})()

    monkeypatch.setattr("run_bot.load_settings", lambda: settings)
    monkeypatch.setattr("run_bot.EngineController", FakeEngine)
    monkeypatch.setattr("run_bot.time.sleep", lambda _seconds: None)
    run_game(FakeClient(), "real-game", settings.stockfish_path, settings.log_dir, quality_mode="hyper")
    assert MatchLock(settings.match_lock_path, settings.match_lock_stale_seconds, settings.pending_challenge_timeout_seconds).read() is None
