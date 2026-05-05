from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bool_was_set(name: str) -> bool:
    return os.getenv(name) is not None


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


@dataclass(frozen=True)
class BotSettings:
    username: str
    token: str


@dataclass(frozen=True)
class Settings:
    bot_1: BotSettings
    bot_2: Optional[BotSettings]
    stockfish_path: Path
    dry_run: bool
    log_dir: Path
    dashboard_host: str
    dashboard_port: int
    challenge_cooldown_seconds: int
    default_rated: bool
    enable_prepared_replies: bool
    enable_prepared_replies_was_set: bool
    prepare_reply_budget_ms: int
    allow_human_challenges: bool
    min_clock_limit_seconds: int
    max_clock_limit_seconds: int
    bot_challenge_targets: tuple[str, ...]
    outbound_challenges_enabled: bool
    outbound_challenge_seconds: int
    outbound_challenge_increment: int
    outbound_challenge_rated: bool
    outbound_challenge_cooldown_seconds: int
    outbound_challenge_max_per_session: int
    outbound_challenge_color: str
    max_concurrent_games: int
    enable_auto_resign: bool
    serial_match_mode: bool
    match_lock_path: Path
    match_lock_stale_seconds: int
    challenge_placeholder_stale_seconds: int
    pending_challenge_timeout_seconds: int
    next_match_cooldown_seconds: int

    @property
    def dashboard_url(self) -> str:
        return f"http://{self.dashboard_host}:{self.dashboard_port}"


def load_settings(env_file: Optional[str] = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env")
    bot_1 = BotSettings(
        username=os.getenv("BOT_1_USERNAME", "LocalBot1"),
        token=os.getenv("LICHESS_TOKEN_BOT_1", ""),
    )
    bot_2_token = os.getenv("LICHESS_TOKEN_BOT_2", "")
    bot_2_username = os.getenv("BOT_2_USERNAME", "")
    bot_2 = BotSettings(bot_2_username, bot_2_token) if bot_2_token and bot_2_username else None
    return Settings(
        bot_1=bot_1,
        bot_2=bot_2,
        stockfish_path=Path(os.getenv("STOCKFISH_PATH", "stockfish")).expanduser(),
        dry_run=_bool("DRY_RUN", False),
        log_dir=(PROJECT_ROOT / os.getenv("LOG_DIR", "logs")).resolve(),
        dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1"),
        dashboard_port=_int("DASHBOARD_PORT", 3000),
        challenge_cooldown_seconds=_int("CHALLENGE_COOLDOWN_SECONDS", 20),
        default_rated=_bool("DEFAULT_RATED", False),
        enable_prepared_replies=_bool("ENABLE_PREPARED_REPLIES", False),
        enable_prepared_replies_was_set=_bool_was_set("ENABLE_PREPARED_REPLIES"),
        prepare_reply_budget_ms=_int("PREPARE_REPLY_BUDGET_MS", 10),
        allow_human_challenges=_bool("ALLOW_HUMAN_CHALLENGES", True),
        min_clock_limit_seconds=_int("MIN_CLOCK_LIMIT_SECONDS", 30),
        max_clock_limit_seconds=_int("MAX_CLOCK_LIMIT_SECONDS", 30),
        bot_challenge_targets=parse_targets(os.getenv("BOT_CHALLENGE_TARGETS", "")),
        outbound_challenges_enabled=_bool("OUTBOUND_CHALLENGES_ENABLED", False),
        outbound_challenge_seconds=_int("OUTBOUND_CHALLENGE_SECONDS", 30),
        outbound_challenge_increment=_int("OUTBOUND_CHALLENGE_INCREMENT", 0),
        outbound_challenge_rated=_bool("OUTBOUND_CHALLENGE_RATED", False),
        outbound_challenge_cooldown_seconds=_int("OUTBOUND_CHALLENGE_COOLDOWN_SECONDS", 300),
        outbound_challenge_max_per_session=_int("OUTBOUND_CHALLENGE_MAX_PER_SESSION", 10),
        outbound_challenge_color=os.getenv("OUTBOUND_CHALLENGE_COLOR", "random"),
        max_concurrent_games=_int("MAX_CONCURRENT_GAMES", 1),
        enable_auto_resign=_bool("ENABLE_AUTO_RESIGN", False),
        serial_match_mode=_bool("SERIAL_MATCH_MODE", True),
        match_lock_path=(PROJECT_ROOT / os.getenv("MATCH_LOCK_PATH", "runtime/active_match.lock")).resolve(),
        match_lock_stale_seconds=_int("MATCH_LOCK_STALE_SECONDS", 120),
        pending_challenge_timeout_seconds=_int("PENDING_CHALLENGE_TIMEOUT_SECONDS", 60),
        challenge_placeholder_stale_seconds=_int(
            "CHALLENGE_PLACEHOLDER_STALE_SECONDS",
            _int("PENDING_CHALLENGE_TIMEOUT_SECONDS", _int("OUTBOUND_CHALLENGE_LOCK_TTL_SECONDS", 60)),
        ),
        next_match_cooldown_seconds=_int("NEXT_MATCH_COOLDOWN_SECONDS", 1),
    )


def parse_targets(value: str) -> tuple[str, ...]:
    return tuple(target.strip() for target in value.split(",") if target.strip())


def require_bot_token(settings: Settings, bot_index: int = 1) -> None:
    bot = settings.bot_1 if bot_index == 1 else settings.bot_2
    if bot is None or not bot.token:
        raise SystemExit(f"Missing Lichess BOT token for bot {bot_index}. Copy .env.example to .env and fill it in.")


def validate_stockfish_path(settings: Settings, required: bool = False) -> bool:
    path = settings.stockfish_path
    exists = path.exists() or (len(path.parts) == 1 and shutil.which(str(path)) is not None)
    if required and not exists:
        raise SystemExit(f"Stockfish not found at {path}. Set STOCKFISH_PATH in .env.")
    return exists
