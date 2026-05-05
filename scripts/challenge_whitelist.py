from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Settings, load_settings  # noqa: E402
from lichess_client import LichessClient, http_error_context  # noqa: E402
from match_lock import MatchLock  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(__name__)


FINAL_STATUSES = {"mate", "timeout", "resign", "draw", "stalemate", "outoftime", "aborted", "variantEnd", "cheat", "noStart", "unknownFinish"}


@dataclass
class ChallengeCreateResult:
    ok: bool
    challenge_id: str = ""
    url: str = ""
    raw: dict[str, Any] | None = None
    error: str = ""

    def __bool__(self) -> bool:
        return self.ok


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
) -> ChallengeCreateResult:
    try:
        response = client.challenge(target, seconds=seconds, increment=increment, rated=rated, color=color)
        challenge_id = response.get("challenge", {}).get("id") or response.get("id")
        url = response.get("challenge", {}).get("url") or response.get("url") or (f"https://lichess.org/{challenge_id}" if challenge_id else "")
        LOG.info("Challenge created: target=%s id=%s status=ok response=%s", target, challenge_id, response)
        return ChallengeCreateResult(True, str(challenge_id or ""), str(url or ""), response)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 429:
            LOG.warning("Challenge rate limited; waiting 300s. %s", http_error_context(exc, None))
            time.sleep(300)
        elif status in {400, 403, 404}:
            LOG.warning("Challenge failed: target=%s status=%s %s", target, status, http_error_context(exc, None))
        else:
            LOG.warning("Unexpected challenge HTTP error: target=%s status=%s %s", target, status, http_error_context(exc, None))
        return ChallengeCreateResult(False, error=str(exc))
    except Exception as exc:
        LOG.warning("Unexpected challenge error: target=%s error=%s", target, exc)
        return ChallengeCreateResult(False, error=str(exc))


def cooldown_sleep(cooldown_seconds: int) -> None:
    sleep_time = cooldown_seconds + random.uniform(-10, 10)
    sleep_time = max(10, sleep_time)
    LOG.info("Cooldown sleep: %.1fs", sleep_time)
    time.sleep(sleep_time)


def opponent_name_from_game(game: dict[str, Any]) -> str:
    opponent = game.get("opponent", {})
    if isinstance(opponent, dict):
        return str(opponent.get("username") or opponent.get("name") or opponent.get("id") or "")
    players = game.get("players", {})
    for player in players.values() if isinstance(players, dict) else []:
        user = player.get("user", {}) if isinstance(player, dict) else {}
        username = user.get("name") or user.get("username") or user.get("id")
        if username:
            return str(username)
    return ""


def game_id_from_now_playing(game: dict[str, Any]) -> str:
    return str(game.get("gameId") or game.get("id") or game.get("fullId") or "")


def now_playing_games(client: LichessClient) -> list[dict[str, Any]]:
    data = client.account_playing()
    games = data.get("nowPlaying", data if isinstance(data, list) else [])
    return games if isinstance(games, list) else []


def find_active_game_against(client: LichessClient, target: str) -> str | None:
    target_lower = target.lower()
    for game in now_playing_games(client):
        opponent = opponent_name_from_game(game).lower()
        game_id = game_id_from_now_playing(game)
        if game_id and (not target_lower or opponent == target_lower):
            return game_id
    return None


def resolve_challenge_game_id(
    client: LichessClient,
    challenge_id: str,
    target: str,
    timeout_seconds: int,
) -> str | None:
    deadline = time.time() + timeout_seconds
    last_log = 0.0
    LOG.info("Challenge created; resolving game id: challenge_id=%s target=%s", challenge_id, target)
    while time.time() < deadline:
        try:
            game = client.game_export(challenge_id)
            if game:
                LOG.info("Game detected: game_id=%s status=%s url=https://lichess.org/%s", challenge_id, game.get("status"), challenge_id)
                return challenge_id
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in {404, 410}:
                LOG.warning("Unexpected export error while resolving challenge: challenge_id=%s status=%s error=%s", challenge_id, status, exc)
        except Exception as exc:
            LOG.debug("Game export not ready while resolving challenge_id=%s: %s", challenge_id, exc)
        game_id = find_active_game_against(client, target)
        if game_id:
            LOG.info("Game detected via account_playing fallback: challenge_id=%s game_id=%s url=https://lichess.org/%s", challenge_id, game_id, game_id)
            return game_id
        now = time.time()
        if now - last_log >= 5:
            age = timeout_seconds - max(0, int(deadline - now))
            LOG.info("Resolving challenge into game: challenge_id=%s target=%s age=%ss", challenge_id, target, age)
            last_log = now
        time.sleep(1)
    return None


def wait_for_challenge_to_become_game(
    client: LichessClient,
    challenge_id: str,
    target: str,
    timeout_seconds: int,
) -> str | None:
    return resolve_challenge_game_id(client, challenge_id, target, timeout_seconds)


def wait_for_game_to_finish(client: LichessClient, game_id: str, log_interval_seconds: int = 5) -> dict[str, Any]:
    last_log = 0.0
    last_seen: dict[str, Any] = {"game_id": game_id, "status": "started", "winner": "", "ply_count": 0, "url": f"https://lichess.org/{game_id}"}
    while True:
        try:
            exported = client.game_export(game_id)
            status = str(exported.get("status", "finished"))
            winner = str(exported.get("winner", ""))
            moves = str(exported.get("moves", ""))
            ply_count = len(moves.split()) if moves else int(exported.get("ply", exported.get("turns", 0)) or 0)
            last_seen = {
                "game_id": game_id,
                "status": status,
                "winner": winner,
                "ply_count": ply_count,
                "url": f"https://lichess.org/{game_id}",
                "raw": exported,
            }
            if status != "started":
                return last_seen
            now = time.time()
            if now - last_log >= log_interval_seconds:
                LOG.info("Waiting for game to finish: game_id=%s ply=%s status=%s", game_id, ply_count, status)
                last_log = now
        except Exception as exc:
            LOG.debug("Game export unavailable while waiting for %s: %s", game_id, exc)
            active_game = None
            for game in now_playing_games(client):
                if game_id_from_now_playing(game) == game_id:
                    active_game = game
                    break
            if active_game is not None:
                moves = str(active_game.get("moves", ""))
                ply_count = len(moves.split()) if moves else int(active_game.get("ply", active_game.get("plyCount", 0)) or 0)
                last_seen.update({"status": active_game.get("status", "started"), "winner": active_game.get("winner", ""), "ply_count": ply_count, "raw": active_game})
                if last_seen["status"] != "started":
                    return last_seen
        time.sleep(1)


def lock_wait_message(lock: MatchLock) -> str:
    info = lock.active_info()
    if info is None:
        return "serial lock clear"
    age = time.time() - info.last_heartbeat_at
    if lock.is_placeholder(info):
        return f"challenge pending game_id={info.game_id} age={age:.1f}s"
    return f"real game active game_id={info.game_id} age={age:.1f}s"


def describe_lock(lock: MatchLock) -> str:
    info = lock.active_info()
    if info is None:
        return "none"
    kind = "challenge pending" if lock.is_placeholder(info) else "real active game"
    age = time.time() - info.last_heartbeat_at
    return f"{kind}: game_id={info.game_id} age={age:.1f}s pid={info.pid}"


def wait_for_free_lock(lock: MatchLock, cooldown_seconds: int, max_wait_seconds: int | None = None) -> bool:
    start = time.time()
    last_log = 0.0
    waited = False
    while True:
        info = lock.active_info()
        if info is None:
            if waited:
                LOG.info("Serial lock cleared; issuing next challenge")
            if waited and cooldown_seconds > 0:
                LOG.info("Next match cooldown: %ss", cooldown_seconds)
                time.sleep(cooldown_seconds)
            return True
        waited = True
        now = time.time()
        if max_wait_seconds is not None and now - start >= max_wait_seconds:
            LOG.info("Timed out waiting for serial match lock to clear after %ss: %s", max_wait_seconds, lock_wait_message(lock))
            return False
        if now - last_log >= 5:
            LOG.info("Waiting for serial match lock to clear: %s", lock_wait_message(lock))
            last_log = now
        time.sleep(1)


def clear_lock_command(settings: Settings, stale_only: bool, placeholders_only: bool = True) -> int:
    lock = MatchLock(settings.match_lock_path, settings.match_lock_stale_seconds, settings.pending_challenge_timeout_seconds)
    before = lock.read()
    if before is None:
        LOG.info("No match lock present")
        return 0
    if stale_only:
        cleared = lock.clear_stale()
    elif placeholders_only:
        cleared = lock.clear_placeholder(force=True)
    else:
        cleared = lock.release_if_game(before.game_id)
    after = lock.read()
    LOG.info("Lock cleanup: before=%s cleared=%s after=%s", before, cleared, after)
    return 0 if cleared or after == before else 1


def run_challenges(
    settings: Settings,
    once: bool,
    loop: bool,
    target: str | None,
    seconds: int | None,
    increment: int | None,
    client_factory=LichessClient,
    wait: bool = False,
    wait_for_free: bool = False,
    count_limit: int | None = None,
    max_wait_seconds: int | None = None,
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
    match_lock = MatchLock(settings.match_lock_path, settings.match_lock_stale_seconds, settings.pending_challenge_timeout_seconds)

    max_attempts = count_limit or (settings.outbound_challenge_max_per_session if not loop else float("inf"))
    count = 0
    successes = 0
    failures = 0
    target_index = 0
    while count < max_attempts:
        if wait_for_free and settings.serial_match_mode:
            if not wait_for_free_lock(match_lock, settings.next_match_cooldown_seconds, max_wait_seconds):
                break
        for current_target in targets:
            if count >= max_attempts:
                break
            if wait_for_free and len(targets) > 1:
                current_target = targets[target_index % len(targets)]
                target_index += 1
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
            placeholder_id = f"challenge:outbound:{current_target}"
            if settings.serial_match_mode:
                info = match_lock.active_info()
                if info is not None:
                    if match_lock.is_placeholder(info):
                        LOG.info("Skipping outbound challenge because challenge pending: target=%s %s", current_target, describe_lock(match_lock))
                    else:
                        LOG.info("Skipping challenge because real game active: target=%s game_id=%s", current_target, info.game_id)
                    if not wait_for_free:
                        failures += 1
                    if once:
                        LOG.info("Outbound challenge session complete: attempts=%s successes=%s failures=%s", count, successes, failures)
                        return count
                    if wait_for_free:
                        count -= 1
                        break
                    cooldown_sleep(settings.outbound_challenge_cooldown_seconds)
                    continue
                if not match_lock.acquire(placeholder_id, settings.bot_1.username):
                    info = match_lock.active_info()
                    LOG.info("Skipping outbound challenge because match lock became active: target=%s lock=%s", current_target, info)
                    if not wait_for_free:
                        failures += 1
                    if once:
                        LOG.info("Outbound challenge session complete: attempts=%s successes=%s failures=%s", count, successes, failures)
                        return count
                    if wait_for_free:
                        count -= 1
                        break
                    cooldown_sleep(settings.outbound_challenge_cooldown_seconds)
                    continue
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
                if settings.serial_match_mode:
                    LOG.info("Challenge pending; keeping serial lock until gameStart or timeout: %s", placeholder_id)
                    if wait_for_free:
                        game_id = resolve_challenge_game_id(client, ok.challenge_id, current_target, settings.pending_challenge_timeout_seconds)
                        if game_id is None:
                            match_lock.release(placeholder_id)
                            LOG.info("Challenge did not become a game; clearing pending placeholder: challenge_id=%s", ok.challenge_id)
                        else:
                            match_lock.replace(placeholder_id, game_id, settings.bot_1.username)
                            final = wait_for_game_to_finish(client, game_id)
                            LOG.info(
                                "Game finished: game_id=%s status=%s winner=%s ply_count=%s url=%s",
                                game_id,
                                final.get("status"),
                                final.get("winner"),
                                final.get("ply_count"),
                                final.get("url"),
                            )
                            match_lock.release_if_game(game_id)
                            if settings.next_match_cooldown_seconds > 0 and count < max_attempts:
                                LOG.info("Next match cooldown: %ss", settings.next_match_cooldown_seconds)
                                time.sleep(settings.next_match_cooldown_seconds)
                    elif wait:
                        wait_for_pending_resolution(match_lock, placeholder_id, settings.pending_challenge_timeout_seconds)
            else:
                failures += 1
                if settings.serial_match_mode:
                    match_lock.release(placeholder_id)
            if once:
                LOG.info("Outbound challenge session complete: attempts=%s successes=%s failures=%s", count, successes, failures)
                return count
            if wait_for_free:
                break
            if count < max_attempts:
                cooldown_sleep(settings.outbound_challenge_cooldown_seconds)
        if not loop:
            break
    LOG.info("Outbound challenge session complete: attempts=%s successes=%s failures=%s", count, successes, failures)
    return count


def wait_for_pending_resolution(match_lock: MatchLock, placeholder_id: str, timeout_seconds: int) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        info = match_lock.active_info()
        if info is None:
            LOG.info("Pending challenge lock cleared before gameStart: %s", placeholder_id)
            return
        if info.game_id != placeholder_id:
            LOG.info("Pending challenge became real game lock: placeholder=%s game_id=%s", placeholder_id, info.game_id)
            return
        time.sleep(1)
    if match_lock.clear_placeholder(force=False):
        LOG.info("Pending challenge timed out and stale placeholder was cleared: %s", placeholder_id)
    else:
        LOG.info("Pending challenge still active after wait timeout: %s", placeholder_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Challenge whitelisted Lichess BOT accounts safely.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Challenge one whitelisted target once.")
    mode.add_argument("--loop", action="store_true", help="Continue cycling through the whitelist until interrupted.")
    parser.add_argument("--target", default=None, help="Specific target; must still be in BOT_CHALLENGE_TARGETS.")
    parser.add_argument("--seconds", type=int, default=None)
    parser.add_argument("--increment", type=int, default=None)
    parser.add_argument("--wait", action="store_true", help="After creating a challenge, wait until it becomes a game lock or times out.")
    parser.add_argument("--wait-for-free", action="store_true", help="Wait for the serial match lock to clear before each challenge.")
    parser.add_argument("--count", type=int, default=None, help="Number of challenge creation attempts to make.")
    parser.add_argument("--max-wait-seconds", type=int, default=None, help="Maximum seconds to wait for the serial lock to clear.")
    parser.add_argument("--clear-stale-lock", action="store_true", help="Clear stale real locks or stale challenge placeholders.")
    parser.add_argument("--clear-lock", action="store_true", help="Clear any challenge:* placeholder lock without touching real game locks.")
    args = parser.parse_args()

    settings = load_settings()
    if args.clear_stale_lock:
        return clear_lock_command(settings, stale_only=True)
    if args.clear_lock:
        return clear_lock_command(settings, stale_only=False, placeholders_only=True)
    attempts = run_challenges(
        settings,
        once=args.once or (not args.loop and not args.wait_for_free and not args.count),
        loop=args.loop or args.wait_for_free,
        target=args.target,
        seconds=args.seconds,
        increment=args.increment,
        wait=args.wait,
        wait_for_free=args.wait_for_free,
        count_limit=args.count,
        max_wait_seconds=args.max_wait_seconds,
    )
    return 0 if attempts >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
