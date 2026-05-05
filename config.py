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
    prepare_reply_budget_ms: int
    allow_human_challenges: bool
    allow_ultrabullet: bool
    min_clock_limit_seconds: int
    max_clock_limit_seconds: int

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
        prepare_reply_budget_ms=_int("PREPARE_REPLY_BUDGET_MS", 10),
        allow_human_challenges=_bool("ALLOW_HUMAN_CHALLENGES", True),
        allow_ultrabullet=_bool("ALLOW_ULTRABULLET", True),
        min_clock_limit_seconds=_int("MIN_CLOCK_LIMIT_SECONDS", 30),
        max_clock_limit_seconds=_int("MAX_CLOCK_LIMIT_SECONDS", 30),
    )


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
