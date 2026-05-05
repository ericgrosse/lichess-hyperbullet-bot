from __future__ import annotations

import argparse
import json
import logging
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import chess
import chess.pgn

from config import load_settings, require_bot_token, validate_stockfish_path
from engine_controller import EngineController
from lichess_client import ChallengePolicy, LichessClient, decide_challenge
from move_selector import MoveSelector, SelectionContext


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(__name__)
DASHBOARD_STATE: dict[str, Any] = {"games": {}}
DASHBOARD_LOCK = threading.Lock()


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/state.json":
            with DASHBOARD_LOCK:
                body = json.dumps(DASHBOARD_STATE).encode("utf-8")
            self._send_json(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(DASHBOARD_HTML.encode("utf-8"))

    def _send_json(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Lichess BOT Dashboard</title>
<style>
body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#111;color:#eee}
header{padding:18px 24px;background:#1d1d1d;border-bottom:1px solid #333}
main{padding:20px;display:grid;gap:14px}.game{border:1px solid #333;border-radius:8px;padding:14px;background:#191919}
a{color:#8bd3ff}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}
.label{color:#aaa;font-size:12px}.value{font-size:16px;margin-top:2px}
</style></head><body><header><h1>Lichess BOT Dashboard</h1></header><main id="games"></main>
<script>
async function tick(){const s=await fetch('/state.json').then(r=>r.json());const games=Object.values(s.games||{});
document.getElementById('games').innerHTML=games.map(g=>`<section class="game"><h2><a href="${g.url}" target="_blank">${g.id}</a></h2><div class="grid">
${['url','opponent','whiteClock','blackClock','clockMs','incrementMs','timeoutSide','lastMove','eval','selectedMove','thinkMs','source','candidatesSeen','preparedHit','blunder','result'].map(k=>`<div><div class="label">${k}</div><div class="value">${g[k]??''}</div></div>`).join('')}
</div></section>`).join('')||'<p>No active games.</p>'} setInterval(tick,1000); tick();
</script></body></html>"""


def start_dashboard(host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def append_log(log_dir: Path, game_id: str, event: dict[str, Any]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / f"{game_id}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")


def board_from_moves(moves: str) -> chess.Board:
    board = chess.Board()
    for uci in moves.split():
        board.push_uci(uci)
    return board


def dashboard_game_update(game_id: str, values: dict[str, Any]) -> dict[str, Any]:
    with DASHBOARD_LOCK:
        game = DASHBOARD_STATE["games"].setdefault(game_id, {"id": game_id})
        game.update(values)
        return dict(game)


def player_name(player: dict[str, Any]) -> str:
    return player.get("name") or player.get("username") or player.get("id", "")


def player_title(player: dict[str, Any]) -> str:
    return player.get("title") or ""


def live_termination(status: str | None) -> str:
    if status in {"mate", "timeout", "resign", "draw", "stalemate", "outoftime", "aborted"}:
        return status
    return status or ""


def run_game(client: LichessClient, game_id: str, engine_path: Path, log_dir: Path, quality_mode: str = "fast") -> None:
    LOG.info("Game started: https://lichess.org/%s", game_id)
    with EngineController(engine_path) as engine:
        settings = load_settings()
        selector = MoveSelector(engine, settings.enable_prepared_replies, settings.prepare_reply_budget_ms)
        color = None
        base_seconds = 0.5
        opponent_name = ""
        opponent_title = ""
        result_status = "playing"
        for event in client.stream_game(game_id):
            append_log(log_dir, game_id, {"type": "stream", "event": event})
            if event.get("type") == "gameFull":
                white_player = event["white"]
                black_player = event["black"]
                white = white_player.get("id", "").lower()
                black = black_player.get("id", "").lower()
                color = chess.WHITE if white == client.username.lower() else chess.BLACK if black == client.username.lower() else None
                opponent = black_player if color == chess.WHITE else white_player
                opponent_name = player_name(opponent)
                opponent_title = player_title(opponent)
                clock = event.get("clock", {})
                base_seconds = float(clock.get("initial", 500)) / 1000
                dashboard_game_update(
                    game_id,
                    {
                        "id": game_id,
                        "url": f"https://lichess.org/{game_id}",
                        "opponent": f"{opponent_title + ' ' if opponent_title else ''}{opponent_name}",
                        "result": "playing",
                    },
                )
                state = event.get("state", {})
            elif event.get("type") == "gameState":
                state = event
            else:
                continue

            if state.get("status") not in {None, "started"}:
                result_status = state.get("status")
                dashboard_game_update(game_id, {"result": result_status})
                append_log(
                    log_dir,
                    game_id,
                    {
                        "type": "game_end",
                        "game_id": game_id,
                        "url": f"https://lichess.org/{game_id}",
                        "result": result_status,
                        "termination": live_termination(result_status),
                        "opponent_username": opponent_name,
                        "opponent_title": opponent_title,
                    },
                )
                break
            if color is None:
                continue
            moves = state.get("moves", "")
            board = board_from_moves(moves)
            is_my_turn = board.turn == color
            white_ms = int(state.get("wtime", 0))
            black_ms = int(state.get("btime", 0))
            dashboard_game_update(
                game_id,
                {
                    "id": game_id,
                    "url": f"https://lichess.org/{game_id}",
                    "opponent": f"{opponent_title + ' ' if opponent_title else ''}{opponent_name}",
                    "whiteClock": white_ms,
                    "blackClock": black_ms,
                    "lastMove": moves.split()[-1] if moves else "",
                },
            )
            if not is_my_turn or board.is_game_over():
                continue
            remaining = white_ms if color == chess.WHITE else black_ms
            fen_before = board.fen()
            ply = board.ply() + 1
            last_opponent_move = moves.split()[-1] if moves else None
            result = selector.choose_move(board, SelectionContext(remaining, base_seconds, 0, last_opponent_move=last_opponent_move, quality_mode=quality_mode))
            client.make_move(game_id, result.move.uci())
            dashboard_game_update(
                game_id,
                {
                    "eval": result.eval_cp,
                    "selectedMove": result.move.uci(),
                    "thinkMs": round(result.think_time_ms, 2),
                    "source": result.source,
                    "candidatesSeen": result.candidates_seen,
                    "preparedHit": result.prepared_hit,
                    "hyperFastPath": result.hyper_fast_path_used,
                    "blunder": result.blunder.reason,
                }
            )
            append_log(
                log_dir,
                game_id,
                {
                    "type": "move",
                    "game_id": game_id,
                    "url": f"https://lichess.org/{game_id}",
                    "opponent_username": opponent_name,
                    "opponent_title": opponent_title,
                    "ply": ply,
                    "fen_before": fen_before,
                    "move": result.move.uci(),
                    "clock_before_ms": remaining,
                    "clock_after_ms": None,
                    "think_ms": result.think_time_ms,
                    "eval_cp": result.eval_cp,
                    "source": result.source,
                    "blunder_reason": result.blunder.reason,
                    "candidates_seen": result.candidates_seen,
                    "prepared_hit": result.prepared_hit,
                    "hyper_fast_path_used": result.hyper_fast_path_used,
                    "result": result_status,
                    "termination": live_termination(result_status),
                    "selection": {
                        "move": result.move.uci(),
                        "think_time_ms": result.think_time_ms,
                        "eval_cp": result.eval_cp,
                        "source": result.source,
                        "blunder": result.blunder.__dict__,
                        "candidates_seen": result.candidates_seen,
                        "prepared_hit": result.prepared_hit,
                        "hyper_fast_path_used": result.hyper_fast_path_used,
                    },
                },
            )


def run_dry_game(
    max_plies: int = 80,
    clock_ms: int = 30_000,
    increment_ms: int = 0,
    pgn_path: Path | str | None = None,
    bot1_name: str = "OfflineBot1",
    bot2_name: str = "OfflineBot2",
    random_seed: int | None = None,
    quality_mode: str = "fast",
) -> str:
    if random_seed is not None:
        random.seed(random_seed)
    settings = load_settings()
    start_dashboard(settings.dashboard_host, settings.dashboard_port)
    LOG.info("Dry-run dashboard: %s", settings.dashboard_url)
    board = chess.Board()
    game = chess.pgn.Game()
    game.headers["Event"] = "Local BOT Dry Run"
    game.headers["Site"] = "Local"
    game.headers["White"] = bot1_name
    game.headers["Black"] = bot2_name
    game.headers["TimeControl"] = f"{clock_ms / 1000:g}+{increment_ms / 1000:g}"
    game.headers["Variant"] = "Standard"
    pgn_node = game
    game_id = f"dry-{int(time.time())}"
    dashboard_game_update(
        game_id,
        {
            "id": game_id,
            "url": "#",
            "result": "playing",
            "clockMs": clock_ms,
            "incrementMs": increment_ms,
            "timeoutSide": "",
        },
    )
    engine = EngineController(settings.stockfish_path)
    if validate_stockfish_path(settings, required=False):
        try:
            engine.start()
        except Exception as exc:
            LOG.warning("Could not start Stockfish at %s: %s; dry-run will use heuristic fallback moves.", settings.stockfish_path, exc)
    else:
        LOG.warning("Stockfish not found at %s; dry-run will use heuristic fallback moves.", settings.stockfish_path)
    try:
        selector = MoveSelector(engine, settings.enable_prepared_replies, settings.prepare_reply_budget_ms)
        white_ms = black_ms = clock_ms
        base_seconds = clock_ms / 1000
        final_result = "*"
        termination = "max plies reached"
        ply_count = 0
        recent_positions = {MoveSelector.position_key(board)}
        for _ in range(max_plies):
            if board.is_game_over(claim_draw=True):
                final_result = board.result(claim_draw=True)
                termination = "normal"
                break
            remaining = white_ms if board.turn == chess.WHITE else black_ms
            mover = board.turn
            think_budget_ms = selector._think_budget_ms(  # pylint: disable=protected-access
                SelectionContext(
                    remaining,
                    base_seconds,
                    increment_ms / 1000,
                    recent_position_keys=recent_positions,
                    quality_mode=quality_mode,
                )
            )
            before = time.perf_counter()
            result = selector.choose_move(
                board,
                SelectionContext(
                    remaining,
                    base_seconds,
                    increment_ms / 1000,
                    last_opponent_move=board.peek().uci() if board.move_stack else None,
                    recent_position_keys=recent_positions,
                    quality_mode=quality_mode,
                ),
            )
            wall_elapsed_ms = (time.perf_counter() - before) * 1000
            charged_ms = max(1, round(result.think_time_ms))
            pgn_node = pgn_node.add_variation(result.move)
            board.push(result.move)
            ply_count += 1
            timeout_side = ""
            if mover == chess.WHITE:
                white_ms = max(0, white_ms - charged_ms + increment_ms)
                if white_ms <= 0:
                    timeout_side = "white"
                    final_result = "white timeout"
                    termination = "timeout"
            else:
                black_ms = max(0, black_ms - charged_ms + increment_ms)
                if black_ms <= 0:
                    timeout_side = "black"
                    final_result = "black timeout"
                    termination = "timeout"
            after_clock = white_ms if mover == chess.WHITE else black_ms
            pgn_node.comment = (
                f"clk_before {remaining}ms; clk_after {after_clock}ms; "
                f"think {round(result.think_time_ms, 2)}ms; wall {round(wall_elapsed_ms, 2)}ms; "
                f"charged {charged_ms}ms; budget {think_budget_ms}ms; source {result.source}; "
                f"eval {result.eval_cp}; blunder {result.blunder.reason}; hyper_fast_path {result.hyper_fast_path_used}"
            )
            recent_positions.add(MoveSelector.position_key(board))
            if not timeout_side and board.is_game_over(claim_draw=True):
                final_result = board.result(claim_draw=True)
                termination = "normal"
            state = dashboard_game_update(
                game_id,
                {
                    "whiteClock": white_ms,
                    "blackClock": black_ms,
                    "clockMs": clock_ms,
                    "incrementMs": increment_ms,
                    "timeoutSide": timeout_side,
                    "lastMove": result.move.uci(),
                    "eval": result.eval_cp,
                    "selectedMove": result.move.uci(),
                    "thinkMs": round(result.think_time_ms, 2),
                    "blunder": result.blunder.reason,
                    "hyperFastPath": result.hyper_fast_path_used,
                    "result": final_result if timeout_side or board.is_game_over() else "playing",
                }
            )
            append_log(settings.log_dir, game_id, {"type": "dry_move", "fen": board.fen(), "move": result.move.uci(), "result": state})
            if timeout_side:
                break
        if final_result == "*" and board.is_game_over(claim_draw=True):
            final_result = board.result(claim_draw=True)
            termination = "normal"
            dashboard_game_update(game_id, {"result": final_result})
        elif final_result == "*":
            dashboard_game_update(game_id, {"result": "*"})
        pgn_result = {"white timeout": "0-1", "black timeout": "1-0"}.get(final_result, final_result)
        game.headers["Result"] = pgn_result
        game.headers["Termination"] = termination
        game.headers["PlyCount"] = str(ply_count)
        if pgn_path is not None:
            output_path = Path(pgn_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(str(game) + "\n", encoding="utf-8")
            LOG.info("Dry-run PGN written to %s", output_path)
        LOG.info("Dry-run complete: %s", final_result)
        return final_result
    finally:
        engine.close()


def challenge_context(challenge: dict[str, Any], decision: Any) -> str:
    challenger = challenge.get("challenger", {})
    clock = challenge.get("timeControl", {})
    variant = challenge.get("variant", {}).get("key")
    rated = "rated" if challenge.get("rated") else "casual"
    limit = clock.get("limit")
    increment = clock.get("increment")
    username = challenger.get("name") or challenger.get("username") or challenger.get("id", "")
    return f"id={challenge.get('id')} challenger={username} rated={rated} variant={variant} tc={limit}+{increment} decision={decision.reason}"


def handle_challenge_event(
    client: LichessClient,
    challenge: dict[str, Any],
    allow_human_challenges: bool,
    log_dir: Path | None = None,
    policy: ChallengePolicy | None = None,
) -> None:
    decision = decide_challenge(challenge, allow_human_challenges, policy)
    challenge_id = challenge["id"]
    if log_dir is not None:
        append_log(
            log_dir,
            f"challenge-{challenge_id}",
            {
                "type": "challenge_decision",
                "challenge_id": challenge_id,
                "decision": decision.__dict__,
                "raw_challenge": challenge,
            },
        )
    if decision.accept:
        LOG.info("Accepting challenge: %s", challenge_context(challenge, decision))
        try:
            client.try_accept_challenge(challenge_id, challenge)
        except Exception as exc:
            LOG.warning("Unexpected error while accepting challenge %s: %s", challenge_id, exc)
    else:
        LOG.info("Declining challenge: %s", challenge_context(challenge, decision))
        try:
            client.try_decline_challenge(challenge_id, decision.reason, challenge)
        except Exception as exc:
            LOG.warning("Unexpected error while declining challenge %s: %s", challenge_id, exc)


def run_live(bot_index: int = 1, dashboard: bool = True, quality_mode: str = "fast") -> None:
    settings = load_settings()
    require_bot_token(settings, bot_index)
    validate_stockfish_path(settings, required=True)
    bot = settings.bot_1 if bot_index == 1 else settings.bot_2
    assert bot is not None
    if dashboard:
        start_dashboard(settings.dashboard_host, settings.dashboard_port)
        LOG.info("Dashboard: %s", settings.dashboard_url)
    client = LichessClient(bot.token, bot.username)
    client.assert_bot_account()
    challenge_policy = ChallengePolicy(
        allow_human_challenges=settings.allow_human_challenges,
        allow_ultrabullet=settings.allow_ultrabullet,
        min_clock_limit_seconds=settings.min_clock_limit_seconds,
        max_clock_limit_seconds=settings.max_clock_limit_seconds,
    )
    for event in client.stream_events():
        if event.get("type") == "challenge":
            handle_challenge_event(client, event["challenge"], settings.allow_human_challenges, settings.log_dir, challenge_policy)
        elif event.get("type") == "gameStart":
            game_client = client.clone()
            threading.Thread(
                target=run_game,
                args=(game_client, event["game"]["id"], settings.stockfish_path, settings.log_dir, quality_mode),
                daemon=True,
            ).start()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", type=int, choices=[1, 2], default=1)
    parser.add_argument("--dry-run", action="store_true", help="Play a local self-contained game without Lichess.")
    parser.add_argument("--plies", type=int, default=80)
    parser.add_argument("--clock-ms", type=int, default=30_000, help="Dry-run starting clock per side in milliseconds.")
    parser.add_argument("--increment-ms", type=int, default=0, help="Dry-run increment per move in milliseconds.")
    parser.add_argument("--pgn-path", default=None, help="Optional dry-run PGN output path.")
    parser.add_argument("--bot1-name", default="OfflineBot1", help="Dry-run PGN/display name for White.")
    parser.add_argument("--bot2-name", default="OfflineBot2", help="Dry-run PGN/display name for Black.")
    parser.add_argument("--random-seed", type=int, default=None, help="Optional dry-run random seed.")
    parser.add_argument(
        "--quality-mode",
        default="hyper",
        choices=["fast", "sample", "hyper"],
        help="Move selection quality mode (default: hyper)"
    )
    args = parser.parse_args()
    if args.dry_run:
        run_dry_game(
            args.plies,
            args.clock_ms,
            args.increment_ms,
            args.pgn_path,
            args.bot1_name,
            args.bot2_name,
            args.random_seed,
            args.quality_mode,
        )
        return
    run_live(args.bot, quality_mode=args.quality_mode)


if __name__ == "__main__":
    main()
