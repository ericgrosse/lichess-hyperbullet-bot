from __future__ import annotations

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import chess.engine
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import load_settings  # noqa: E402
from lichess_client import LichessClient  # noqa: E402


@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str


def pass_check(name: str, message: str) -> CheckResult:
    return CheckResult(name, True, message)


def fail_check(name: str, message: str) -> CheckResult:
    return CheckResult(name, False, message)


def resolve_stockfish(path: Path) -> str | None:
    if path.exists():
        return str(path)
    if len(path.parts) == 1:
        found = shutil.which(str(path))
        if found:
            return found
    return None


def check_stockfish(stockfish_path: Path) -> list[CheckResult]:
    resolved = resolve_stockfish(stockfish_path)
    if not resolved:
        return [fail_check("Stockfish path", f"Stockfish not found at {stockfish_path}")]
    results = [pass_check("Stockfish path", f"Found {resolved}")]
    engine = None
    try:
        engine = chess.engine.SimpleEngine.popen_uci(resolved)
        engine.ping()
        results.append(pass_check("Stockfish UCI", "Engine started and responded to UCI ping"))
    except Exception as exc:
        results.append(fail_check("Stockfish UCI", f"Could not start Stockfish through UCI: {exc}"))
    finally:
        if engine is not None:
            try:
                engine.quit()
            except Exception:
                pass
    return results


def check_cooldown(seconds: int) -> CheckResult:
    if seconds < 10:
        return fail_check("Challenge cooldown", f"CHALLENGE_COOLDOWN_SECONDS={seconds}; use at least 10 to avoid challenge spam")
    return pass_check("Challenge cooldown", f"CHALLENGE_COOLDOWN_SECONDS={seconds}")


def check_bot_account(token: str, label: str) -> CheckResult:
    if not token:
        return fail_check(label, "Missing token")
    try:
        account = LichessClient(token, label).account()
    except Exception as exc:
        return fail_check(label, f"Could not call /api/account: {exc}")
    username = account.get("username", "<unknown>")
    if account.get("title") != "BOT":
        return fail_check(label, f"{username} is not titled BOT; refusing normal-account usage")
    return pass_check(label, f"{username} is a BOT account")


def print_results(results: list[CheckResult]) -> int:
    ok = True
    for result in results:
        status = "PASS" if result.ok else "FAIL"
        print(f"[{status}] {result.name}: {result.message}")
        ok = ok and result.ok
    print("Preflight PASS" if ok else "Preflight FAIL")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight checks before launching Lichess BOT mode.")
    parser.add_argument("--live", action="store_true", help="Verify live Lichess BOT token(s) through /api/account.")
    parser.add_argument("--bot2", action="store_true", help="With --live, also verify BOT_2 token if configured.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    settings = load_settings()
    results: list[CheckResult] = []
    results.extend(check_stockfish(settings.stockfish_path))
    results.append(check_cooldown(settings.challenge_cooldown_seconds))

    bot1_token = os.getenv("LICHESS_TOKEN_BOT_1", "")
    bot2_token = os.getenv("LICHESS_TOKEN_BOT_2", "")
    if args.live:
        if not bot1_token:
            results.append(fail_check("BOT_1 token", "LICHESS_TOKEN_BOT_1 is required for --live"))
        else:
            results.append(check_bot_account(bot1_token, "BOT_1 account"))
        if args.bot2:
            if bot2_token:
                results.append(check_bot_account(bot2_token, "BOT_2 account"))
            else:
                results.append(fail_check("BOT_2 token", "LICHESS_TOKEN_BOT_2 is required for --live --bot2"))
    else:
        results.append(pass_check("Live account checks", "Skipped; pass --live to verify Lichess BOT account title"))

    return print_results(results)


if __name__ == "__main__":
    raise SystemExit(main())
