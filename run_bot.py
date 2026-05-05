from __future__ import annotations

import argparse
import json
import logging
import random
import statistics
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import chess
import chess.pgn

from config import load_settings, require_bot_token, validate_stockfish_path
from engine_controller import EngineController
from lichess_client import ChallengePolicy, LichessClient, decide_challenge
from match_lock import MatchLock, MatchLockInfo
from move_selector import MoveSelector, SelectionContext


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger(__name__)
DASHBOARD_STATE: dict[str, Any] = {"games": {}}
DASHBOARD_LOCK = threading.Lock()
ACTIVE_GAMES: set[str] = set()
ACTIVE_GAMES_LOCK = threading.Lock()
PREMOVE_LIKE_SOURCES = {
    "prepared-cache",
    "book",
    "tactical-only-legal",
    "tactical-mate",
    "tactical-recapture",
    "hyper-fast-path",
}
BOOK_SOURCES = {"book"}
TACTICAL_SOURCES = {"tactical-only-legal", "tactical-mate", "tactical-recapture", "hyper-fast-path"}
PREPARED_SOURCES = {"prepared-cache"}
SURVIVAL_FAST_SOURCES = {"emergency-cache", "emergency-forcing", "emergency-fallback", "forcing-fallback", "last-legal"}
FALLBACK_PREFIXES = ("fallback-", "forcing-fallback")
IGNORED_GAME_IDS = deque(maxlen=20)
IGNORED_GAME_LOCK = threading.Lock()


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
${['url','opponent','active_games_count','ignored_game_count','ignored_game_ids','game_status','termination','winner','whiteClock','blackClock','clockMs','incrementMs','timeoutSide','lastMove','eval','selectedMove','thinkMs','source','candidatesSeen','preparedHit','hyper_fast_path_used','emergency_mode','true_premove_like_percentage','prepared_cache_percentage','book_moves','tactical_moves','fallback_percentage','engine_percentage','survival_fast_percentage','premove_like_percentage','avg_think_ms','median_think_ms','max_think_ms','engine_search_count','prepared_cache_hits','prepared_cache_misses','background_skipped_busy','recentPositionsCount','blunder','result'].map(k=>`<div><div class="label">${k}</div><div class="value">${g[k]??''}</div></div>`).join('')}
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


def active_games_count() -> int:
    with ACTIVE_GAMES_LOCK:
        return len(ACTIVE_GAMES)


def try_register_game(game_id: str, max_concurrent_games: int) -> bool:
    with ACTIVE_GAMES_LOCK:
        if game_id in ACTIVE_GAMES:
            return True
        if len(ACTIVE_GAMES) >= max_concurrent_games:
            return False
        ACTIVE_GAMES.add(game_id)
        return True


def unregister_game(game_id: str) -> None:
    with ACTIVE_GAMES_LOCK:
        ACTIVE_GAMES.discard(game_id)


def ignored_game_snapshot() -> dict[str, Any]:
    with IGNORED_GAME_LOCK:
        ids = list(IGNORED_GAME_IDS)
    return {"ignored_game_count": len(ids), "ignored_game_ids": ",".join(ids)}


def record_ignored_game(game_id: str) -> dict[str, Any]:
    with IGNORED_GAME_LOCK:
        IGNORED_GAME_IDS.append(game_id)
    return ignored_game_snapshot()


def lock_summary(info: MatchLockInfo | None) -> dict[str, Any]:
    if info is None:
        return {"lock_game_id": "", "lock_bot_username": "", "lock_pid": ""}
    return {"lock_game_id": info.game_id, "lock_bot_username": info.bot_username, "lock_pid": info.pid}


class GameMetrics:
    def __init__(self) -> None:
        self.think_times: list[float] = []
        self.total_moves = 0
        self.premove_like_moves = 0
        self.true_premove_like_moves = 0
        self.prepared_cache_moves = 0
        self.book_moves = 0
        self.tactical_moves = 0
        self.fallback_moves = 0
        self.engine_moves = 0
        self.survival_fast_moves = 0
        self.engine_search_count = 0
        self.prepared_cache_hits = 0
        self.prepared_cache_misses = 0

    def record(self, result: Any) -> None:
        self.total_moves += 1
        self.think_times.append(float(result.think_time_ms))
        if is_premove_like(result.source):
            self.premove_like_moves += 1
        if is_true_premove_like(result.source):
            self.true_premove_like_moves += 1
        if result.source in PREPARED_SOURCES:
            self.prepared_cache_moves += 1
        if result.source in BOOK_SOURCES:
            self.book_moves += 1
        if result.source in TACTICAL_SOURCES:
            self.tactical_moves += 1
        if is_fallback_source(result.source):
            self.fallback_moves += 1
        if is_survival_fast(result.source):
            self.survival_fast_moves += 1
        if is_engine_source(result.source):
            self.engine_moves += 1
            self.engine_search_count += 1
        if result.prepared_hit:
            self.prepared_cache_hits += 1
        else:
            self.prepared_cache_misses += 1

    def snapshot(self, selector: MoveSelector | None = None) -> dict[str, Any]:
        avg = sum(self.think_times) / len(self.think_times) if self.think_times else 0.0
        median = statistics.median(self.think_times) if self.think_times else 0.0
        max_think = max(self.think_times) if self.think_times else 0.0
        pct = (self.premove_like_moves / self.total_moves * 100) if self.total_moves else 0.0
        true_pct = self._pct(self.true_premove_like_moves)
        prepared_pct = self._pct(self.prepared_cache_moves)
        fallback_pct = self._pct(self.fallback_moves)
        engine_pct = self._pct(self.engine_moves)
        survival_pct = self._pct(self.survival_fast_moves)
        data = {
            "total_moves": self.total_moves,
            "premove_like_moves": self.premove_like_moves,
            "premove_like_percentage": round(pct, 1),
            "prepared_cache_moves": self.prepared_cache_moves,
            "prepared_cache_percentage": round(prepared_pct, 1),
            "book_moves": self.book_moves,
            "tactical_moves": self.tactical_moves,
            "fallback_moves": self.fallback_moves,
            "engine_moves": self.engine_moves,
            "true_premove_like_percentage": round(true_pct, 1),
            "fallback_percentage": round(fallback_pct, 1),
            "engine_percentage": round(engine_pct, 1),
            "survival_fast_percentage": round(survival_pct, 1),
            "avg_think_ms": round(avg, 2),
            "median_think_ms": round(median, 2),
            "max_think_ms": round(max_think, 2),
            "engine_search_count": self.engine_search_count,
            "prepared_cache_hits": self.prepared_cache_hits,
            "prepared_cache_misses": self.prepared_cache_misses,
        }
        if selector is not None:
            data["background_skipped_busy"] = selector.prepared_analysis_skipped_engine_busy
            data["prepared_analysis_started"] = selector.prepared_analysis_started
            data["prepared_analysis_cancelled"] = selector.prepared_analysis_cancelled
        return data

    def _pct(self, count: int) -> float:
        return (count / self.total_moves * 100) if self.total_moves else 0.0


def is_premove_like(source: str) -> bool:
    return is_true_premove_like(source) or is_survival_fast(source)


def is_true_premove_like(source: str) -> bool:
    return source in PREMOVE_LIKE_SOURCES


def is_fallback_source(source: str) -> bool:
    return source in SURVIVAL_FAST_SOURCES or source.startswith(FALLBACK_PREFIXES) or source == "least-bad"


def is_survival_fast(source: str) -> bool:
    return source in SURVIVAL_FAST_SOURCES or source.startswith(FALLBACK_PREFIXES)


def is_engine_source(source: str) -> bool:
    return source == "stockfish" or source.startswith("stockfish") or source == "least-bad"


def player_name(player: dict[str, Any]) -> str:
    return player.get("name") or player.get("username") or player.get("id", "")


def player_title(player: dict[str, Any]) -> str:
    return player.get("title") or ""


def live_termination(status: str | None) -> str:
    if status in {"mate", "timeout", "resign", "draw", "stalemate", "outoftime", "aborted"}:
        return status
    return status or ""


def start_opponent_time_analysis(
    selector: MoveSelector,
    board_after_move: chess.Board,
    ctx: SelectionContext,
    stop_event: threading.Event,
) -> None:
    threading.Thread(
        target=selector.prepare_opponent_time_analysis,
        args=(board_after_move.copy(stack=False), ctx, stop_event),
        daemon=True,
    ).start()


def effective_prepared_replies(settings: Any, quality_mode: str) -> bool:
    if quality_mode in {"hyper", "ultra"} and not settings.enable_prepared_replies_was_set:
        return True
    return settings.enable_prepared_replies


def probe_stockfish_launch(stockfish_path: Path) -> bool:
    engine = EngineController(stockfish_path)
    try:
        engine.start()
        return True
    except Exception as exc:
        LOG.warning("Stockfish launch probe failed at %s: %s", stockfish_path, exc)
        return False
    finally:
        engine.close()


def log_startup_diagnostics(settings: Any, quality_mode: str, stockfish_launched: bool) -> None:
    enabled = effective_prepared_replies(settings, quality_mode)
    LOG.info(
        "Startup diagnostics: quality_mode=%s max_concurrent_games=%s enable_prepared_replies=%s "
        "prepare_reply_budget_ms=%s enable_auto_resign=%s stockfish_path=%s stockfish_launched=%s",
        quality_mode,
        settings.max_concurrent_games,
        enabled,
        settings.prepare_reply_budget_ms,
        settings.enable_auto_resign,
        settings.stockfish_path,
        stockfish_launched,
    )


def run_game(client: LichessClient, game_id: str, engine_path: Path, log_dir: Path, quality_mode: str = "fast") -> None:
    LOG.info("Game started: https://lichess.org/%s", game_id)
    stop_event = threading.Event()
    with EngineController(engine_path) as engine:
        settings = load_settings()
        match_lock = MatchLock(settings.match_lock_path, settings.match_lock_stale_seconds, settings.pending_challenge_timeout_seconds)
        if settings.serial_match_mode:
            ok, info, overlap = match_lock.acquire_or_join_game(game_id, client.username)
            if not ok:
                LOG.error("OVERLAP DETECTED in run_game: game_id=%s lock=%s", game_id, info)
                dashboard_game_update(
                    "system",
                    {
                        "id": "system",
                        "url": "#",
                        "overlap_game_detected": True,
                        "overlap_game_id": game_id,
                        **lock_summary(info),
                        "active_games_count": active_games_count(),
                    },
                )
                append_log(log_dir, f"overlap-{game_id}", {"type": "overlap_detected", "game_id": game_id, "lock": info.__dict__ if info else None, "overlap": overlap})
                unregister_game(game_id)
                return
        prepared_enabled = effective_prepared_replies(settings, quality_mode)
        LOG.info("Game config: game_id=%s quality_mode=%s prepared_replies=%s budget_ms=%s", game_id, quality_mode, prepared_enabled, settings.prepare_reply_budget_ms)
        selector = MoveSelector(engine, prepared_enabled, settings.prepare_reply_budget_ms)
        color = None
        base_seconds = 0.5
        opponent_name = ""
        opponent_title = ""
        result_status = "playing"
        recent_positions = deque(maxlen=24)
        recent_position_keys: set[str] = set()
        metrics = GameMetrics()
        last_state: dict[str, Any] = {}
        final_winner = ""
        final_termination = ""
        started_at = time.time()
        ended_at = 0.0
        last_heartbeat_at = 0.0
        try:
            for event in client.stream_game(game_id):
                now = time.time()
                if settings.serial_match_mode and now - last_heartbeat_at >= 2:
                    match_lock.heartbeat(game_id, client.username, allow_any_pid=True)
                    last_heartbeat_at = now
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
                            "watch_warning": "For stable viewing, open the exact game URL. Lichess TV may auto-switch when another game starts.",
                            "opponent": f"{opponent_title + ' ' if opponent_title else ''}{opponent_name}",
                            "result": "playing",
                            "game_status": "started",
                            "active_games_count": active_games_count(),
                        },
                    )
                    state = event.get("state", {})
                elif event.get("type") == "gameState":
                    state = event
                else:
                    continue

                last_state = dict(state)
                if state.get("status") not in {None, "started"}:
                    ended_at = time.time()
                    result_status = state.get("status")
                    stop_event.set()
                    winner = state.get("winner", "")
                    termination = live_termination(result_status)
                    final_winner = winner
                    final_termination = termination
                    LOG.info(
                        "Game ended: game_id=%s status=%s winner=%s termination=%s state=%s",
                        game_id,
                        result_status,
                        winner,
                        termination,
                        state,
                    )
                    summary = metrics.snapshot(selector)
                    total_plies = len(state.get("moves", "").split()) if state.get("moves") else 0
                    dashboard_game_update(
                        game_id,
                        {
                            "result": result_status,
                            "game_status": result_status,
                            "termination": termination,
                            "winner": winner,
                            "exact_game_url": f"https://lichess.org/{game_id}",
                            "duration_seconds": round(ended_at - started_at, 2),
                            "total_plies": total_plies,
                            "active_games_count": active_games_count(),
                            **ignored_game_snapshot(),
                            **summary,
                        },
                    )
                    append_log(
                        log_dir,
                        game_id,
                        {
                            "type": "game_end",
                            "game_id": game_id,
                            "url": f"https://lichess.org/{game_id}",
                            "result": result_status,
                            "status": result_status,
                        "winner": winner,
                        "termination": termination,
                        "started_at": started_at,
                        "ended_at": ended_at,
                        "duration_seconds": round(ended_at - started_at, 2),
                        "total_plies": total_plies,
                        "exact_game_url": f"https://lichess.org/{game_id}",
                        "next_game_started_at": None,
                        "last_state": state,
                            "metrics": summary,
                            "opponent_username": opponent_name,
                            "opponent_title": opponent_title,
                        },
                    )
                    break
                if color is None:
                    continue
                moves = state.get("moves", "")
                board = board_from_moves(moves)
                current_key = MoveSelector.position_key(board)
                if not recent_positions or recent_positions[-1] != current_key:
                    recent_positions.append(current_key)
                    recent_position_keys = set(recent_positions)
                is_my_turn = board.turn == color
                white_ms = int(state.get("wtime", 0))
                black_ms = int(state.get("btime", 0))
                dashboard_game_update(
                    game_id,
                    {
                        "id": game_id,
                        "url": f"https://lichess.org/{game_id}",
                        "watch_warning": "For stable viewing, open the exact game URL. Lichess TV may auto-switch when another game starts.",
                        "opponent": f"{opponent_title + ' ' if opponent_title else ''}{opponent_name}",
                        "whiteClock": white_ms,
                        "blackClock": black_ms,
                        "lastMove": moves.split()[-1] if moves else "",
                        "active_games_count": active_games_count(),
                        "game_status": state.get("status", "started"),
                        **ignored_game_snapshot(),
                    },
                )
                if not is_my_turn or board.is_game_over():
                    continue
                remaining = white_ms if color == chess.WHITE else black_ms
                fen_before = board.fen()
                ply = board.ply() + 1
                last_opponent_move = moves.split()[-1] if moves else None
                selection_ctx = SelectionContext(
                    remaining,
                    base_seconds,
                    0,
                    last_opponent_move=last_opponent_move,
                    recent_position_keys=recent_position_keys,
                    quality_mode=quality_mode,
                )
                result = selector.choose_move(board, selection_ctx)
                client.make_move(game_id, result.move.uci())
                metrics.record(result)
                board_after_move = board.copy(stack=False)
                board_after_move.push(result.move)
                after_key = MoveSelector.position_key(board_after_move)
                recent_positions.append(after_key)
                recent_position_keys = set(recent_positions)
                start_opponent_time_analysis(selector, board_after_move, selection_ctx, stop_event)
                metric_snapshot = metrics.snapshot(selector)
                dashboard_game_update(
                    game_id,
                    {
                        "eval": result.eval_cp,
                        "selectedMove": result.move.uci(),
                        "thinkMs": round(result.think_time_ms, 2),
                        "source": result.source,
                        "candidatesSeen": result.candidates_seen,
                        "preparedHit": result.prepared_hit,
                        "hyper_fast_path_used": result.hyper_fast_path_used,
                        "emergency_mode": result.emergency_mode,
                        "recentPositionsCount": len(recent_position_keys),
                        "active_games_count": active_games_count(),
                        "game_status": state.get("status", "started"),
                        **ignored_game_snapshot(),
                        **metric_snapshot,
                        "blunder": result.blunder.reason,
                    },
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
                        "emergency_mode": result.emergency_mode,
                        "recent_positions_count": len(recent_position_keys),
                        **metric_snapshot,
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
                            "emergency_mode": result.emergency_mode,
                            "recent_positions_count": len(recent_position_keys),
                        },
                    },
                )
                append_log(log_dir, game_id, {"type": "stream_after_move", "event": event})
        finally:
            stop_event.set()
            summary = metrics.snapshot(selector)
            if not final_winner:
                final_winner = last_state.get("winner", "")
            if not final_termination:
                final_termination = live_termination(last_state.get("status"))
            LOG.info(
                "Game final summary: game_id=%s total_moves=%s true_premove_like_percentage=%s "
                "prepared_cache_percentage=%s fallback_percentage=%s engine_percentage=%s avg_think_ms=%s "
                "median_think_ms=%s max_think_ms=%s termination=%s winner=%s",
                game_id,
                summary["total_moves"],
                summary["true_premove_like_percentage"],
                summary["prepared_cache_percentage"],
                summary["fallback_percentage"],
                summary["engine_percentage"],
                summary["avg_think_ms"],
                summary["median_think_ms"],
                summary["max_think_ms"],
                final_termination,
                final_winner,
            )
            LOG.info("Game cleanup: game_id=%s active_games=%s metrics=%s last_state=%s", game_id, active_games_count(), summary, last_state)
            dashboard_game_update(game_id, {"active_games_count": active_games_count(), "termination": final_termination, "winner": final_winner, **ignored_game_snapshot(), **summary})
            if settings.serial_match_mode:
                time.sleep(0.5)
                if settings.next_match_cooldown_seconds > 0:
                    LOG.info("Holding match lock cooldown before next match: game_id=%s cooldown=%ss", game_id, settings.next_match_cooldown_seconds)
                    time.sleep(settings.next_match_cooldown_seconds)
                match_lock.release_if_game(game_id)
            unregister_game(game_id)


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
    stockfish_launched = False
    if validate_stockfish_path(settings, required=False):
        try:
            engine.start()
            stockfish_launched = True
        except Exception as exc:
            LOG.warning("Could not start Stockfish at %s: %s; dry-run will use heuristic fallback moves.", settings.stockfish_path, exc)
    else:
        LOG.warning("Stockfish not found at %s; dry-run will use heuristic fallback moves.", settings.stockfish_path)
    log_startup_diagnostics(settings, quality_mode, stockfish_launched)
    try:
        prepared_enabled = effective_prepared_replies(settings, quality_mode)
        LOG.info("Dry-run config: quality_mode=%s prepared_replies=%s budget_ms=%s", quality_mode, prepared_enabled, settings.prepare_reply_budget_ms)
        selector = MoveSelector(engine, prepared_enabled, settings.prepare_reply_budget_ms)
        white_ms = black_ms = clock_ms
        base_seconds = clock_ms / 1000
        final_result = "*"
        termination = "max plies reached"
        ply_count = 0
        recent_positions = {MoveSelector.position_key(board)}
        metrics = GameMetrics()
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
            metrics.record(result)
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
                f"eval {result.eval_cp}; blunder {result.blunder.reason}; hyper_fast_path {result.hyper_fast_path_used}; "
                f"emergency {result.emergency_mode}"
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
                    "hyper_fast_path_used": result.hyper_fast_path_used,
                    "emergency_mode": result.emergency_mode,
                    **metrics.snapshot(selector),
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
        LOG.info("Dry-run complete: %s metrics=%s", final_result, metrics.snapshot(selector))
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
    log_dir: Path | None = None,
    policy: ChallengePolicy | None = None,
    match_lock: MatchLock | None = None,
    bot_username: str = "",
) -> None:
    decision = decide_challenge(challenge, policy=policy)
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
        placeholder_id = f"challenge:{challenge_id}"
        if match_lock is not None:
            info = match_lock.active_info()
            if info is not None:
                LOG.info("Declining challenge because match lock active: challenge_id=%s active_game_id=%s lock=%s", challenge_id, info.game_id, info)
                client.try_decline_challenge(challenge_id, "later", challenge)
                if log_dir is not None:
                    append_log(log_dir, f"challenge-{challenge_id}", {"type": "challenge_declined_match_lock", "challenge_id": challenge_id, "active_game_id": info.game_id, "lock": info.__dict__})
                return
            if not match_lock.acquire(placeholder_id, bot_username or client.username):
                info = match_lock.active_info()
                LOG.info("Declining challenge because match lock became active: challenge_id=%s lock=%s", challenge_id, info)
                client.try_decline_challenge(challenge_id, "later", challenge)
                return
        LOG.info("Accepting challenge: %s", challenge_context(challenge, decision))
        try:
            accepted = client.try_accept_challenge(challenge_id, challenge)
            if not accepted and match_lock is not None:
                match_lock.release(placeholder_id)
        except Exception as exc:
            LOG.warning("Unexpected error while accepting challenge %s: %s", challenge_id, exc)
            if match_lock is not None:
                match_lock.release(placeholder_id)
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
    stockfish_launched = probe_stockfish_launch(settings.stockfish_path)
    log_startup_diagnostics(settings, quality_mode, stockfish_launched)
    bot = settings.bot_1 if bot_index == 1 else settings.bot_2
    assert bot is not None
    if dashboard:
        start_dashboard(settings.dashboard_host, settings.dashboard_port)
        LOG.info("Dashboard: %s", settings.dashboard_url)
    client = LichessClient(bot.token, bot.username)
    client.assert_bot_account()
    match_lock = MatchLock(settings.match_lock_path, settings.match_lock_stale_seconds, settings.pending_challenge_timeout_seconds)
    challenge_policy = ChallengePolicy(
        allow_human_challenges=settings.allow_human_challenges,
        min_clock_limit_seconds=settings.min_clock_limit_seconds,
        max_clock_limit_seconds=settings.max_clock_limit_seconds,
    )
    for event in client.stream_events():
        if event.get("type") == "challenge":
            lock_info = match_lock.active_info() if settings.serial_match_mode else None
            if lock_info is not None:
                challenge = event["challenge"]
                LOG.info("Declined challenge because match lock active: active_game_id=%s challenge=%s", lock_info.game_id, challenge)
                client.try_decline_challenge(challenge["id"], "later", challenge)
                append_log(settings.log_dir, f"challenge-{challenge['id']}", {"type": "challenge_declined_match_lock", "active_game_id": lock_info.game_id, "lock": lock_info.__dict__, "challenge": challenge})
                continue
            if active_games_count() >= settings.max_concurrent_games:
                challenge = event["challenge"]
                LOG.info(
                    "Declining challenge while at max concurrent games: active=%s max=%s challenge=%s",
                    active_games_count(),
                    settings.max_concurrent_games,
                    challenge,
                )
                client.try_decline_challenge(challenge["id"], "later", challenge)
                continue
            handle_challenge_event(client, event["challenge"], settings.log_dir, challenge_policy, match_lock if settings.serial_match_mode else None, bot.username)
        elif event.get("type") == "gameStart":
            game_id = event["game"]["id"]
            if settings.serial_match_mode:
                ok, info, overlap = match_lock.acquire_or_join_game(game_id, bot.username)
                if not ok:
                    ignored = record_ignored_game(game_id)
                    LOG.error("OVERLAP DETECTED: new gameStart while match lock active: game_id=%s lock_game_id=%s lock=%s event=%s", game_id, info.game_id if info else "", info, event)
                    dashboard_game_update(
                        "system",
                        {
                            "id": "system",
                            "url": "#",
                            "overlap_game_detected": True,
                            "overlap_game_id": game_id,
                            **lock_summary(info),
                            "active_games_count": active_games_count(),
                            **ignored,
                        },
                    )
                    append_log(settings.log_dir, f"overlap-{game_id}", {"type": "overlap_detected", "game_id": game_id, "lock": info.__dict__ if info else None, "event": event, **ignored})
                    continue
            if not try_register_game(game_id, settings.max_concurrent_games):
                ignored = record_ignored_game(game_id)
                LOG.warning(
                    "IGNORING gameStart because max concurrent games is reached: game_id=%s active=%s max=%s ignored_ids=%s event=%s",
                    game_id,
                    active_games_count(),
                    settings.max_concurrent_games,
                    ignored["ignored_game_ids"],
                    event,
                )
                dashboard_game_update(
                    "system",
                    {
                        "id": "system",
                        "url": "#",
                        "result": "monitoring",
                        "active_games_count": active_games_count(),
                        **ignored,
                    },
                )
                append_log(
                    settings.log_dir,
                    f"ignored-{game_id}",
                    {"type": "ignored_game_start", "game_id": game_id, "active_games_count": active_games_count(), **ignored, "event": event},
                )
                continue
            game_client = client.clone()
            threading.Thread(
                target=run_game,
                args=(game_client, game_id, settings.stockfish_path, settings.log_dir, quality_mode),
                daemon=True,
            ).start()


def main() -> None:
    EngineController.install_signal_handlers()
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
        choices=["fast", "sample", "hyper", "ultra"],
        help="Move selection quality mode (default: hyper)"
    )
    args = parser.parse_args()
    try:
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
    except KeyboardInterrupt:
        LOG.info("Interrupted; shutting down Stockfish engines")
        EngineController.close_all()


if __name__ == "__main__":
    main()
