from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path
from typing import Iterable

import requests


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Settings, load_settings  # noqa: E402
from lichess_client import LichessClient, http_error_context  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(__name__)


def validate_target(target: str, whitelist: Iterable[str]) -> str:
    allowed = {name.lower(): name for name in whitelist}
    if target.lower() not in allowed:
        raise SystemExit(f"Refusing non-whitelisted target: {target}")
    return allowed[target.lower()]


def validate_challenge_clock(seconds: int, increment: int) -> None:
    if seconds < 30:
        raise SystemExit("Refusing outbound challenge below 30 seconds; Lichess rejected 15+0 as BOT-incompatible.")
    if increment != 0:
        raise SystemExit("Refusing outbound challenge with increment; configured workflow is standard 30+0 bot testing.")


def verify_bot_account(client: LichessClient) -> str:
    account = client.account()
    username = account.get("username", client.username)
    if account.get("title") != "BOT":
        raise SystemExit(f"Refusing outbound challenges: {username} is not a BOT account.")
    LOG.info("Verified BOT account: %s", username)
    return username


def issue_challenge(
    client: LichessClient,
    target: str,
    seconds: int,
    increment: int,
    rated: bool,
    color: str,
) -> bool:
    try:
        response = client.challenge(target, seconds=seconds, increment=increment, rated=rated, color=color)
        challenge_id = response.get("challenge", {}).get("id") or response.get("id")
        LOG.info("Challenge created: target=%s id=%s status=ok response=%s", target, challenge_id, response)
        return True
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 429:
            LOG.warning("Challenge rate limited; waiting 300s. %s", http_error_context(exc, None))
            time.sleep(300)
        elif status in {400, 403, 404}:
            LOG.warning("Challenge failed: target=%s status=%s %s", target, status, http_error_context(exc, None))
        else:
            LOG.warning("Unexpected challenge HTTP error: target=%s status=%s %s", target, status, http_error_context(exc, None))
        return False
    except Exception as exc:
        LOG.warning("Unexpected challenge error: target=%s error=%s", target, exc)
        return False


def run_challenges(
    settings: Settings,
    once: bool,
    loop: bool,
    target: str | None,
    seconds: int | None,
    increment: int | None,
    client_factory=LichessClient,
) -> int:
    if not settings.outbound_challenges_enabled:
        raise SystemExit("OUTBOUND_CHALLENGES_ENABLED=false; refusing outbound challenges.")
    if not settings.bot_1.token:
        raise SystemExit("LICHESS_TOKEN_BOT_1 is required for outbound challenges.")
    whitelist = settings.bot_challenge_targets
    if not whitelist:
        raise SystemExit("BOT_CHALLENGE_TARGETS is empty; refusing outbound challenges.")

    seconds = settings.outbound_challenge_seconds if seconds is None else seconds
    increment = settings.outbound_challenge_increment if increment is None else increment
    validate_challenge_clock(seconds, increment)

    targets = [validate_target(target, whitelist)] if target else list(whitelist)
    client = client_factory(settings.bot_1.token, settings.bot_1.username)
    verify_bot_account(client)

    max_attempts = settings.outbound_challenge_max_per_session if not loop else float("inf")
    count = 0
    successes = 0
    failures = 0
    while count < max_attempts:
        for current_target in targets:
            if count >= max_attempts:
                break
            count += 1
            LOG.info(
                "Outbound challenge attempt %s: target=%s seconds=%s increment=%s rated=%s color=%s",
                count,
                current_target,
                seconds,
                increment,
                settings.outbound_challenge_rated,
                settings.outbound_challenge_color,
            )
            ok = issue_challenge(
                client,
                current_target,
                seconds,
                increment,
                settings.outbound_challenge_rated,
                settings.outbound_challenge_color,
            )
            if ok:
                successes += 1
            else:
                failures += 1
            if once:
                LOG.info("Outbound challenge session complete: attempts=%s successes=%s failures=%s", count, successes, failures)
                return count
            if count < max_attempts:
                sleep_time = settings.outbound_challenge_cooldown_seconds + random.uniform(-10, 10)
                sleep_time = max(10, sleep_time)
                LOG.info("Cooldown sleep: %.1fs", sleep_time)
                time.sleep(sleep_time)
        if not loop:
            break
    LOG.info("Outbound challenge session complete: attempts=%s successes=%s failures=%s", count, successes, failures)
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description="Challenge whitelisted Lichess BOT accounts safely.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Challenge one whitelisted target once.")
    mode.add_argument("--loop", action="store_true", help="Continue cycling through the whitelist until interrupted.")
    parser.add_argument("--target", default=None, help="Specific target; must still be in BOT_CHALLENGE_TARGETS.")
    parser.add_argument("--seconds", type=int, default=None)
    parser.add_argument("--increment", type=int, default=None)
    args = parser.parse_args()

    settings = load_settings()
    attempts = run_challenges(settings, once=args.once or not args.loop, loop=args.loop, target=args.target, seconds=args.seconds, increment=args.increment)
    return 0 if attempts >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
