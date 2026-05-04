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
from lichess_client import LichessClient, decide_challenge
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
${['whiteClock','blackClock','clockMs','incrementMs','timeoutSide','lastMove','eval','selectedMove','thinkMs','blunder','result'].map(k=>`<div><div class="label">${k}</div><div class="value">${g[k]??''}</div></div>`).join('')}
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


def run_game(client: LichessClient, game_id: str, engine_path: Path, log_dir: Path) -> None:
    LOG.info("Game started: https://lichess.org/%s", game_id)
    with EngineController(engine_path) as engine:
        settings = load_settings()
        selector = MoveSelector(engine, settings.enable_prepared_replies, settings.prepare_reply_budget_ms)
        color = None
        base_seconds = 0.5
        for event in client.stream_game(game_id):
            append_log(log_dir, game_id, {"type": "stream", "event": event})
            if event.get("type") == "gameFull":
                white = event["white"].get("id", "").lower()
                black = event["black"].get("id", "").lower()
                color = chess.WHITE if white == client.username.lower() else chess.BLACK if black == client.username.lower() else None
                clock = event.get("clock", {})
                base_seconds = float(clock.get("initial", 500)) / 1000
                dashboard_game_update(game_id, {"id": game_id, "url": f"https://lichess.org/{game_id}", "result": "playing"})
                state = event.get("state", {})
            elif event.get("type") == "gameState":
                state = event
            else:
                continue

            if state.get("status") not in {None, "started"}:
                dashboard_game_update(game_id, {"result": state.get("status")})
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
                    "whiteClock": white_ms,
                    "blackClock": black_ms,
                    "lastMove": moves.split()[-1] if moves else "",
                },
            )
            if not is_my_turn or board.is_game_over():
                continue
            remaining = white_ms if color == chess.WHITE else black_ms
            result = selector.choose_move(board, SelectionContext(remaining, base_seconds, 0))
            client.make_move(game_id, result.move.uci())
            dashboard_game_update(
                game_id,
                {
                    "eval": result.eval_cp,
                    "selectedMove": result.move.uci(),
                    "thinkMs": round(result.think_time_ms, 2),
                    "blunder": result.blunder.reason,
                }
            )
            append_log(
                log_dir,
                game_id,
                {
                    "type": "move",
                    "move": result.move.uci(),
                    "selection": {
                        "move": result.move.uci(),
                        "think_time_ms": result.think_time_ms,
                        "eval_cp": result.eval_cp,
                        "source": result.source,
                        "blunder": result.blunder.__dict__,
                        "candidates_seen": result.candidates_seen,
                        "prepared_hit": result.prepared_hit,
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
                f"eval {result.eval_cp}; blunder {result.blunder.reason}"
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


def run_live(bot_index: int = 1, dashboard: bool = True) -> None:
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
    for event in client.stream_events():
        if event.get("type") == "challenge":
            challenge = event["challenge"]
            decision = decide_challenge(challenge)
            if decision.accept:
                client.accept_challenge(challenge["id"])
            else:
                client.decline_challenge(challenge["id"], decision.reason)
        elif event.get("type") == "gameStart":
            game_client = client.clone()
            threading.Thread(target=run_game, args=(game_client, event["game"]["id"], settings.stockfish_path, settings.log_dir), daemon=True).start()


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
    parser.add_argument("--quality-mode", choices=["fast", "sample"], default="fast", help="Dry-run quality mode.")
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
    run_live(args.bot)


if __name__ == "__main__":
    main()
