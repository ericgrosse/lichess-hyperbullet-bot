from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MatchLockInfo:
    game_id: str
    bot_username: str
    pid: int
    created_at: float
    last_heartbeat_at: float


class MatchLock:
    def __init__(self, path: Path | str, stale_seconds: int = 120, placeholder_stale_seconds: int = 20) -> None:
        self.path = Path(path)
        self.stale_seconds = stale_seconds
        self.placeholder_stale_seconds = placeholder_stale_seconds
        self.pid = os.getpid()

    def read(self) -> MatchLockInfo | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return MatchLockInfo(
                game_id=str(data.get("game_id", "")),
                bot_username=str(data.get("bot_username", "")),
                pid=int(data.get("pid", 0)),
                created_at=float(data.get("created_at", 0)),
                last_heartbeat_at=float(data.get("last_heartbeat_at", 0)),
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def is_stale(self, info: MatchLockInfo | None = None) -> bool:
        info = info or self.read()
        if info is None:
            return False
        ttl = self.placeholder_stale_seconds if self.is_placeholder(info) else self.stale_seconds
        return (time.time() - info.last_heartbeat_at) > ttl

    @staticmethod
    def is_placeholder(info: MatchLockInfo | None) -> bool:
        return bool(info and info.game_id.startswith("challenge:"))

    def is_placeholder_stale(self, info: MatchLockInfo | None = None) -> bool:
        info = info or self.read()
        if not self.is_placeholder(info):
            return False
        return (time.time() - info.last_heartbeat_at) > self.placeholder_stale_seconds

    def active_info(self) -> MatchLockInfo | None:
        info = self.read()
        if info and self.is_placeholder_stale(info):
            self.clear_stale()
            return None
        if info and self.is_stale(info):
            self.clear_stale()
            return None
        return info

    def acquire(self, game_id: str, bot_username: str) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clear_stale()
        now = time.time()
        payload = self._payload(game_id, bot_username, now, now)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        return True

    def acquire_or_join_game(self, game_id: str, bot_username: str) -> tuple[bool, MatchLockInfo | None, bool]:
        info = self.active_info()
        if info is None:
            return self.acquire(game_id, bot_username), self.read(), False
        if info.game_id == game_id:
            self.heartbeat(game_id, bot_username, allow_any_pid=True)
            return True, self.read(), False
        if info.game_id.startswith("challenge:"):
            self.replace(info.game_id, game_id, bot_username, allow_any_pid=True)
            return True, self.read(), False
        return False, info, True

    def replace(self, expected_game_id: str, new_game_id: str, bot_username: str, allow_any_pid: bool = False) -> bool:
        info = self.active_info()
        if info is None:
            return self.acquire(new_game_id, bot_username)
        if info.game_id != expected_game_id:
            return False
        if not allow_any_pid and info.pid != self.pid:
            return False
        self._write(new_game_id, bot_username, info.created_at)
        return True

    def heartbeat(self, game_id: str, bot_username: str, allow_any_pid: bool = False) -> bool:
        info = self.active_info()
        if info is None:
            return self.acquire(game_id, bot_username)
        if info.game_id != game_id:
            return False
        if not allow_any_pid and info.pid != self.pid:
            return False
        self._write(game_id, bot_username, info.created_at)
        return True

    def release(self, game_id: str) -> bool:
        info = self.read()
        if info is None:
            return False
        if info.game_id != game_id or info.pid != self.pid:
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False

    def release_if_game(self, game_id: str) -> bool:
        info = self.read()
        if info is None or info.game_id != game_id:
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False

    def clear_stale(self) -> bool:
        info = self.read()
        if info is None or not self.is_stale(info):
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False

    def clear_placeholder(self, force: bool = False) -> bool:
        info = self.read()
        if not self.is_placeholder(info):
            return False
        if not force and not self.is_placeholder_stale(info):
            return False
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False

    def _write(self, game_id: str, bot_username: str, created_at: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._payload(game_id, bot_username, created_at, time.time())
        tmp = self.path.with_suffix(self.path.suffix + f".{self.pid}.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.path)

    def _payload(self, game_id: str, bot_username: str, created_at: float, heartbeat_at: float) -> dict[str, Any]:
        return {
            "game_id": game_id,
            "bot_username": bot_username,
            "pid": self.pid,
            "created_at": created_at,
            "last_heartbeat_at": heartbeat_at,
        }
