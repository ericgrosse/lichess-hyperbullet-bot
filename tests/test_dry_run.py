from pathlib import Path

from config import BotSettings, Settings
from run_bot import run_dry_game


def offline_settings(tmp_path):
    return Settings(
        bot_1=BotSettings(username="OfflineBot1", token=""),
        bot_2=None,
        stockfish_path=Path("missing-stockfish"),
        dry_run=True,
        log_dir=tmp_path / "logs",
        dashboard_host="127.0.0.1",
        dashboard_port=3000,
        challenge_cooldown_seconds=20,
        default_rated=False,
        enable_prepared_replies=False,
        prepare_reply_budget_ms=0,
        allow_human_challenges=True,
        allow_ultrabullet=True,
        min_clock_limit_seconds=15,
        max_clock_limit_seconds=30,
    )


def patch_offline_dry_run(monkeypatch, tmp_path):
    class DummyServer:
        pass

    monkeypatch.setattr("run_bot.start_dashboard", lambda *_: DummyServer())
    monkeypatch.setattr("run_bot.validate_stockfish_path", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("run_bot.load_settings", lambda: offline_settings(tmp_path))


def test_dry_run_can_run_10_plies_without_stockfish(monkeypatch, tmp_path):
    patch_offline_dry_run(monkeypatch, tmp_path)
    run_dry_game(10)


def test_dry_run_ultrabullet_500ms_without_stockfish(monkeypatch, tmp_path):
    patch_offline_dry_run(monkeypatch, tmp_path)
    result = run_dry_game(max_plies=200, clock_ms=500, increment_ms=0)
    assert result in {"*", "1-0", "0-1", "1/2-1/2", "white timeout", "black timeout"}


def test_dry_run_hyperbullet_250ms_without_stockfish(monkeypatch, tmp_path):
    patch_offline_dry_run(monkeypatch, tmp_path)
    result = run_dry_game(max_plies=200, clock_ms=250, increment_ms=0)
    assert result in {"*", "1-0", "0-1", "1/2-1/2", "white timeout", "black timeout"}


def test_dry_run_writes_pgn(monkeypatch, tmp_path):
    patch_offline_dry_run(monkeypatch, tmp_path)
    pgn_path = tmp_path / "sample.pgn"
    run_dry_game(max_plies=10, clock_ms=500, increment_ms=0, pgn_path=pgn_path)
    text = pgn_path.read_text(encoding="utf-8")
    assert pgn_path.exists()
    assert "[Event " in text
    assert "[TimeControl " in text
    assert any(token in text for token in ("1.", "1-0", "0-1", "1/2-1/2", "*"))


def test_dry_run_pgn_max_plies_is_unfinished_not_forced_draw(monkeypatch, tmp_path):
    patch_offline_dry_run(monkeypatch, tmp_path)
    pgn_path = tmp_path / "max-plies.pgn"
    result = run_dry_game(max_plies=2, clock_ms=500, increment_ms=0, pgn_path=pgn_path)
    text = pgn_path.read_text(encoding="utf-8")
    assert result == "*"
    assert '[Result "*"]' in text
    assert '[Termination "max plies reached"]' in text


def test_dry_run_pgn_contains_plycount_and_termination(monkeypatch, tmp_path):
    patch_offline_dry_run(monkeypatch, tmp_path)
    pgn_path = tmp_path / "headers.pgn"
    run_dry_game(max_plies=4, clock_ms=500, increment_ms=0, pgn_path=pgn_path)
    text = pgn_path.read_text(encoding="utf-8")
    assert "[PlyCount " in text
    assert "[Termination " in text
    assert "clk_before" in text
    assert "clk_after" in text
    assert "think" in text
    assert "wall" in text
    assert "charged" in text
    assert "source" in text
    assert "blunder" in text


def test_dry_run_hyper_pgn_contains_fast_path_flag(monkeypatch, tmp_path):
    patch_offline_dry_run(monkeypatch, tmp_path)
    pgn_path = tmp_path / "hyper.pgn"
    run_dry_game(max_plies=4, clock_ms=250, increment_ms=0, quality_mode="hyper", pgn_path=pgn_path)
    text = pgn_path.read_text(encoding="utf-8")
    assert "hyper_fast_path" in text
