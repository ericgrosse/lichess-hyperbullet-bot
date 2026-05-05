from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator

import requests


LICHESS_API = "https://lichess.org"
LOG = logging.getLogger(__name__)


@dataclass
class ChallengeDecision:
    accept: bool
    reason: str


@dataclass(frozen=True)
class ChallengePolicy:
    allow_human_challenges: bool = True
    min_clock_limit_seconds: int = 30
    max_clock_limit_seconds: int = 30


class LichessClient:
    """Minimal Lichess BOT API client.

    It deliberately uses BOT endpoints only and never opens browser pages,
    lobby seeks, pools, tournaments, or simuls.
    """

    _request_lock = threading.RLock()
    _last_request_at = 0.0
    _resume_after = 0.0

    def __init__(self, token: str, username: str, base_url: str = LICHESS_API) -> None:
        self.token = token
        self.username = username
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/x-ndjson, application/json;q=0.9",
            "User-Agent": "codex-ultrabullet-bot/1.0",
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)

    def clone(self) -> "LichessClient":
        return LichessClient(self.token, self.username, self.base_url)

    def account(self) -> dict[str, Any]:
        return self._request("GET", "/api/account").json()

    def assert_bot_account(self) -> None:
        account = self.account()
        if account.get("title") != "BOT":
            raise RuntimeError(
                f"{account.get('username', self.username)} is not a Lichess BOT account. "
                "Refusing to run on normal user accounts."
            )

    def stream_events(self) -> Iterator[dict[str, Any]]:
        yield from self._stream_ndjson("/api/stream/event")

    def stream_game(self, game_id: str) -> Iterator[dict[str, Any]]:
        yield from self._stream_ndjson(f"/api/bot/game/stream/{game_id}")

    def accept_challenge(self, challenge_id: str) -> None:
        self._request("POST", f"/api/challenge/{challenge_id}/accept")

    def decline_challenge(self, challenge_id: str, reason: str = "standard") -> None:
        self._request("POST", f"/api/challenge/{challenge_id}/decline", data={"reason": reason})

    def try_accept_challenge(self, challenge_id: str, challenge: dict[str, Any] | None = None) -> bool:
        try:
            self.accept_challenge(challenge_id)
            return True
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in {400, 404, 410}:
                LOG.warning("Could not accept challenge %s; expected lifecycle/API rejection. %s", challenge_id, http_error_context(exc, challenge))
            else:
                LOG.warning("Unexpected HTTP error accepting challenge %s: %s %s", challenge_id, exc, http_error_context(exc, challenge))
            return False
        except Exception as exc:
            LOG.warning("Unexpected error accepting challenge %s: %s", challenge_id, exc)
            return False

    def try_decline_challenge(self, challenge_id: str, reason: str = "standard", challenge: dict[str, Any] | None = None) -> bool:
        try:
            self.decline_challenge(challenge_id, reason)
            return True
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in {400, 404, 410}:
                LOG.warning("Could not decline challenge %s; expected lifecycle/API rejection. %s", challenge_id, http_error_context(exc, challenge))
            else:
                LOG.warning("Unexpected HTTP error declining challenge %s: %s %s", challenge_id, exc, http_error_context(exc, challenge))
            return False
        except Exception as exc:
            LOG.warning("Unexpected error declining challenge %s: %s", challenge_id, exc)
            return False

    def make_move(self, game_id: str, uci: str) -> None:
        self._request("POST", f"/api/bot/game/{game_id}/move/{uci}")

    def challenge(
        self,
        username: str,
        seconds: float = 30,
        increment: int = 0,
        rated: bool = False,
        color: str = "random",
    ) -> dict[str, Any]:
        data = {
            "rated": str(rated).lower(),
            "clock.limit": str(seconds),
            "clock.increment": str(increment),
            "variant": "standard",
            "color": color,
        }
        return self._request("POST", f"/api/challenge/{username}", data=data).json()

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{self.base_url}{path}"
        with self._request_lock:
            self._pace_locked()
            response = self.session.request(method, url, timeout=20, **kwargs)
            self.__class__._last_request_at = time.monotonic()
            if response.status_code == 429:
                retry = max(60, int(response.headers.get("Retry-After", "60")))
                self.__class__._resume_after = time.monotonic() + retry
                LOG.warning("Rate limited by Lichess; sleeping %ss before resuming API calls", retry)
                time.sleep(retry)
        response.raise_for_status()
        return response

    def _stream_ndjson(self, path: str) -> Iterator[dict[str, Any]]:
        backoff = 1
        while True:
            stream_session = requests.Session()
            response: requests.Response | None = None
            try:
                stream_session.headers.update(self.headers)
                with self._request_lock:
                    self._pace_locked()
                    response = stream_session.get(f"{self.base_url}{path}", stream=True, timeout=90)
                    self.__class__._last_request_at = time.monotonic()
                    if response.status_code == 429:
                        response.close()
                        self.__class__._resume_after = time.monotonic() + 60
                        LOG.warning("Rate limited by Lichess while opening stream; sleeping 60s")
                        time.sleep(60)
                        continue
                    response.raise_for_status()
                with response:
                    backoff = 1
                    for raw in response.iter_lines():
                        if not raw:
                            continue
                        yield json.loads(raw.decode("utf-8"))
            except (requests.RequestException, json.JSONDecodeError) as exc:
                LOG.warning("Lichess stream interrupted: %s; reconnecting in %ss", exc, backoff)
                time.sleep(backoff)
                backoff = min(60, backoff * 2)
            finally:
                stream_session.close()

    @classmethod
    def _pace_locked(cls) -> None:
        # Lichess API tips recommend one request at a time. The class lock
        # serializes request starts across client instances and threads.
        now = time.monotonic()
        if now < cls._resume_after:
            time.sleep(cls._resume_after - now)
        elapsed = time.monotonic() - cls._last_request_at
        if elapsed < 0.05:
            time.sleep(0.05 - elapsed)


def challenge_details(challenge: dict[str, Any] | None) -> str:
    if not challenge:
        return "challenge=<not provided>"
    clock = challenge.get("timeControl", {})
    challenger = challenge.get("challenger", {})
    return (
        f"challenge_id={challenge.get('id')} challenger={challenger.get('name') or challenger.get('username') or challenger.get('id')} "
        f"rated={challenge.get('rated')} variant={challenge.get('variant', {}).get('key')} "
        f"speed={challenge.get('speed')} perf={challenge.get('perf', {}).get('key')} "
        f"tc={clock.get('limit')}+{clock.get('increment')}"
    )


def http_error_context(exc: requests.HTTPError, challenge: dict[str, Any] | None = None) -> str:
    response = exc.response
    if response is None:
        return f"{challenge_details(challenge)} response=<none>"
    useful_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in {"content-type", "retry-after", "x-ratelimit-remaining", "x-ratelimit-limit"}
    }
    return (
        f"{challenge_details(challenge)} status={response.status_code} body={response.text!r} "
        f"headers={dict(useful_headers)}"
    )


def normalize_key(value: Any) -> str:
    return str(value or "").replace("_", "").replace("-", "").lower()


def decide_challenge(
    challenge: dict[str, Any],
    allow_human_challenges: bool = True,
    policy: ChallengePolicy | None = None,
) -> ChallengeDecision:
    if policy is None:
        policy = ChallengePolicy(allow_human_challenges=allow_human_challenges)
    variant = normalize_key(challenge.get("variant", {}).get("key"))
    speed = normalize_key(challenge.get("speed"))
    perf = normalize_key(challenge.get("perf", {}).get("key"))
    clock = challenge.get("timeControl", {})
    challenger = challenge.get("challenger", {})
    if not policy.allow_human_challenges and challenger.get("title") != "BOT":
        return ChallengeDecision(False, "botOnly")
    if variant != "standard":
        return ChallengeDecision(False, "non-standard")
    if "ultrabullet" in {speed, perf}:
        return ChallengeDecision(False, "ultraBullet")
    if speed != "bullet" and perf != "bullet":
        return ChallengeDecision(False, "time")
    if clock.get("type") != "clock":
        return ChallengeDecision(False, "correspondence")
    raw_limit = clock.get("limit")
    raw_increment = clock.get("increment")
    limit = float(raw_limit) if raw_limit is not None else 999
    increment = float(raw_increment) if raw_increment is not None else 999
    if increment != 0:
        return ChallengeDecision(False, "increment")
    if limit < policy.min_clock_limit_seconds:
        return ChallengeDecision(False, "time")
    if limit > policy.max_clock_limit_seconds:
        return ChallengeDecision(False, "time")
    return ChallengeDecision(True, "ok")
