from __future__ import annotations

import argparse
import logging
import random
import time

import requests

from config import load_settings, require_bot_token
from lichess_client import LichessClient
from run_bot import run_live


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(__name__)


def loop_challenges(seconds: float, rated: bool) -> None:
    settings = load_settings()
    require_bot_token(settings, 1)
    if not settings.bot_2:
        raise SystemExit("BOT_2_USERNAME and LICHESS_TOKEN_BOT_2 are required for repeated bot-vs-bot challenges.")
    client = LichessClient(settings.bot_1.token, settings.bot_1.username)
    client.assert_bot_account()
    cooldown = max(10, settings.challenge_cooldown_seconds)
    while True:
        try:
            LOG.info("Challenging %s at %ss+0 rated=%s", settings.bot_2.username, seconds, rated)
            client.challenge(settings.bot_2.username, seconds=seconds, rated=rated, color=random.choice(["white", "black", "random"]))
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in {400, 422}:
                LOG.warning("Challenge rejected by Lichess validation: %s", exc.response.text if exc.response is not None else exc)
                if seconds in {0.25, 0.5}:
                    LOG.warning(
                        "The public challenge endpoint may not support creating %s+0 games directly. "
                        "Try --seconds 15, --seconds 30, or run --accept-only and accept supported incoming challenges.",
                        seconds,
                    )
            else:
                LOG.warning("Challenge request failed: %s", exc)
        except Exception as exc:
            LOG.warning("Challenge failed: %s", exc)
        time.sleep(cooldown)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=0.5, choices=[0.25, 0.5, 15, 30])
    parser.add_argument("--rated", action="store_true")
    parser.add_argument("--accept-only", action="store_true", help="Do not create challenges; only accept supported incoming BOT challenges.")
    args = parser.parse_args()
    if args.accept_only:
        run_live(1)
        return
    loop_challenges(args.seconds, args.rated)


if __name__ == "__main__":
    main()
