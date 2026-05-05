import json
import os
import time

from match_lock import MatchLock


def test_acquire_when_no_lock(tmp_path):
    lock = MatchLock(tmp_path / "active_match.lock", stale_seconds=120)
    assert lock.acquire("game1", "BotA")
    info = lock.read()
    assert info is not None
    assert info.game_id == "game1"
    assert info.pid == os.getpid()


def test_refuse_when_fresh_lock_exists(tmp_path):
    path = tmp_path / "active_match.lock"
    assert MatchLock(path, stale_seconds=120).acquire("game1", "BotA")
    assert not MatchLock(path, stale_seconds=120).acquire("game2", "BotB")


def test_clear_stale_lock(tmp_path):
    path = tmp_path / "active_match.lock"
    old = time.time() - 999
    path.write_text(json.dumps({"game_id": "old", "bot_username": "BotA", "pid": 1, "created_at": old, "last_heartbeat_at": old}), encoding="utf-8")
    lock = MatchLock(path, stale_seconds=120)
    assert lock.acquire("new", "BotB")
    assert lock.read().game_id == "new"


def test_release_only_by_owner_and_game_id(tmp_path):
    path = tmp_path / "active_match.lock"
    lock = MatchLock(path, stale_seconds=120)
    assert lock.acquire("game1", "BotA")
    assert not lock.release("game2")
    assert path.exists()
    assert lock.release("game1")
    assert not path.exists()


def test_heartbeat_updates_lock(tmp_path):
    lock = MatchLock(tmp_path / "active_match.lock", stale_seconds=120)
    assert lock.acquire("game1", "BotA")
    before = lock.read().last_heartbeat_at
    time.sleep(0.01)
    assert lock.heartbeat("game1", "BotA")
    after = lock.read().last_heartbeat_at
    assert after > before


def test_placeholder_lock_expires_faster_than_real_game_lock(tmp_path):
    path = tmp_path / "active_match.lock"
    assert MatchLock(path, stale_seconds=120, placeholder_stale_seconds=0).acquire("challenge:outbound:Bot", "BotA")
    assert MatchLock(path, stale_seconds=120, placeholder_stale_seconds=0).active_info() is None
    assert MatchLock(path, stale_seconds=120, placeholder_stale_seconds=0).acquire("real-game", "BotA")
    assert MatchLock(path, stale_seconds=120, placeholder_stale_seconds=0).active_info().game_id == "real-game"


def test_game_start_replaces_placeholder_with_real_game_id(tmp_path):
    path = tmp_path / "active_match.lock"
    lock = MatchLock(path, stale_seconds=120, placeholder_stale_seconds=60)
    assert lock.acquire("challenge:outbound:SomeBot", "BotA")
    ok, info, overlap = lock.acquire_or_join_game("real-game", "BotA")
    assert ok
    assert not overlap
    assert info.game_id == "real-game"
