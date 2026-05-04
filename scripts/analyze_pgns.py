from __future__ import annotations

import argparse
import fnmatch
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chess
import chess.pgn


NUM_RE = r"(-?[0-9]+(?:\.[0-9]+)?)"
COMMENT_PATTERNS = {
    "clock_before_ms": re.compile(r"(?:clk_before|clock_before)\s+" + NUM_RE + r"ms", re.IGNORECASE),
    "clock_after_ms": re.compile(r"(?:clk_after|clock_after|clk)\s+" + NUM_RE + r"ms", re.IGNORECASE),
    "wall_elapsed_ms": re.compile(r"(?:wall|elapsed)\s+" + NUM_RE + r"ms", re.IGNORECASE),
    "charged_ms": re.compile(r"charged\s+" + NUM_RE + r"ms", re.IGNORECASE),
    "budget_ms": re.compile(r"budget\s+" + NUM_RE + r"ms", re.IGNORECASE),
    "think_ms": re.compile(r"think\s+" + NUM_RE + r"ms", re.IGNORECASE),
    "eval_cp": re.compile(r"eval\s+(-?[0-9]+)", re.IGNORECASE),
    "source": re.compile(r"source\s+([A-Za-z0-9_-]+)", re.IGNORECASE),
    "blunder": re.compile(r"blunder\s+([^;]+)", re.IGNORECASE),
}


@dataclass
class MoveInfo:
    san: str
    eval_cp: int | None = None
    think_ms: float | None = None
    source: str | None = None
    clock_before_ms: float | None = None
    clock_after_ms: float | None = None
    wall_elapsed_ms: float | None = None
    charged_ms: float | None = None
    budget_ms: float | None = None
    blunder: str | None = None


@dataclass
class GameSummary:
    path: str
    result: str
    ply_count: int
    termination: str
    moves: list[MoveInfo]
    repeated_position_max: int
    has_repetition_loop: bool

    @property
    def move_count(self) -> float:
        return self.ply_count / 2

    @property
    def sources(self) -> Counter[str]:
        return Counter(move.source for move in self.moves if move.source)

    @property
    def stockfish_pct(self) -> float:
        return pct(count_sources(self.moves, "stockfish"), len(self.moves))

    @property
    def forcing_fallback_pct(self) -> float:
        return pct(count_sources(self.moves, "forcing-fallback"), len(self.moves))

    @property
    def least_bad_pct(self) -> float:
        return pct(count_sources(self.moves, "least-bad"), len(self.moves))

    @property
    def avg_think_ms(self) -> float:
        values = [move.think_ms for move in self.moves if move.think_ms is not None]
        return round(sum(values) / len(values), 2) if values else 0.0

    @property
    def max_think_ms(self) -> float:
        values = [move.think_ms for move in self.moves if move.think_ms is not None]
        return round(max(values), 2) if values else 0.0

    @property
    def avg_charged_ms(self) -> float:
        values = [move.charged_ms for move in self.moves if move.charged_ms is not None]
        return round(sum(values) / len(values), 2) if values else 0.0

    @property
    def avg_wall_elapsed_ms(self) -> float:
        values = [move.wall_elapsed_ms for move in self.moves if move.wall_elapsed_ms is not None]
        return round(sum(values) / len(values), 2) if values else 0.0

    @property
    def timeout_side(self) -> str:
        if self.termination.lower() != "timeout":
            return ""
        if self.result == "1-0":
            return "black"
        if self.result == "0-1":
            return "white"
        return "unknown"

    @property
    def is_very_short(self) -> bool:
        return self.ply_count < 40


def parse_comment(comment: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, pattern in COMMENT_PATTERNS.items():
        match = pattern.search(comment or "")
        if not match:
            continue
        value = match.group(1).strip()
        if key in {"source", "blunder"}:
            parsed[key] = value
        elif key == "eval_cp":
            parsed[key] = int(value)
        else:
            parsed[key] = float(value)
    return parsed


def position_key(board: chess.Board) -> str:
    ep = chess.square_name(board.ep_square) if board.ep_square is not None else "-"
    return f"{board.board_fen()} {board.turn} {board.castling_rights} {ep}"


def analyze_game(path: Path, game: chess.pgn.Game) -> GameSummary:
    board = game.board()
    moves: list[MoveInfo] = []
    positions: Counter[str] = Counter({position_key(board): 1})
    recent_keys: list[str] = [position_key(board)]

    for node in game.mainline():
        san = board.san(node.move)
        parsed = parse_comment(node.comment or "")
        moves.append(MoveInfo(san=san, **parsed))
        board.push(node.move)
        key = position_key(board)
        positions[key] += 1
        recent_keys.append(key)

    header_ply = game.headers.get("PlyCount")
    ply_count = int(header_ply) if header_ply and header_ply.isdigit() else len(moves)
    return GameSummary(
        path=str(path),
        result=game.headers.get("Result", "*"),
        ply_count=ply_count,
        termination=game.headers.get("Termination", ""),
        moves=moves,
        repeated_position_max=max(positions.values(), default=0),
        has_repetition_loop=len(recent_keys) >= 8 and len(set(recent_keys[-8:])) <= 3,
    )


def should_exclude(path: Path, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(str(path), pattern) for pattern in patterns)


def load_games(directory: Path, exclude: list[str]) -> list[GameSummary]:
    summaries: list[GameSummary] = []
    for path in sorted(directory.glob("*.pgn")):
        if should_exclude(path, exclude):
            continue
        with path.open(encoding="utf-8") as handle:
            while True:
                game = chess.pgn.read_game(handle)
                if game is None:
                    break
                summaries.append(analyze_game(path, game))
    return summaries


def pct(part: int | float, whole: int | float) -> float:
    return round((part / whole) * 100, 2) if whole else 0.0


def source_matches(source: str | None, prefix: str) -> bool:
    return bool(source and source.startswith(prefix))


def count_sources(moves: list[MoveInfo], prefix: str) -> int:
    return sum(1 for move in moves if source_matches(move.source, prefix))


def suspicious_eval_swing(game: GameSummary) -> bool:
    evals = [move.eval_cp for move in game.moves if move.eval_cp is not None]
    return any(abs(later - earlier) >= 600 for earlier, later in zip(evals, evals[2:]))


def mate_score_blunder(game: GameSummary) -> bool:
    evals = [move.eval_cp for move in game.moves if move.eval_cp is not None]
    for earlier, later in zip(evals, evals[1:]):
        if abs(earlier) < 100000 and abs(later) >= 100000:
            return True
    return False


def per_game_row(game: GameSummary) -> dict[str, Any]:
    return {
        "file": game.path,
        "result": game.result,
        "termination": game.termination,
        "final_ply": game.ply_count,
        "under_40_plies": game.is_very_short,
        "timeout_side": game.timeout_side,
        "stockfish_pct": game.stockfish_pct,
        "forcing_fallback_pct": game.forcing_fallback_pct,
        "least_bad_pct": game.least_bad_pct,
        "avg_think_ms": game.avg_think_ms,
        "max_think_ms": game.max_think_ms,
        "avg_charged_ms": game.avg_charged_ms,
        "avg_wall_elapsed_ms": game.avg_wall_elapsed_ms,
        "repeated_position_count": game.repeated_position_max,
    }


def phase_for_ply(ply: int) -> str:
    if ply <= 20:
        return "opening"
    if ply <= 80:
        return "middlegame"
    return "late"


def source_breakdown(moves: list[MoveInfo]) -> dict[str, float]:
    total = len(moves)
    return {
        "stockfish": pct(count_sources(moves, "stockfish"), total),
        "forcing_fallback": pct(count_sources(moves, "forcing-fallback"), total),
        "least_bad": pct(count_sources(moves, "least-bad"), total),
    }


def fallback_rate_by_phase(games: list[GameSummary]) -> dict[str, float]:
    phase_moves: dict[str, list[MoveInfo]] = {"opening": [], "middlegame": [], "late": []}
    for game in games:
        for index, move in enumerate(game.moves, start=1):
            phase_moves[phase_for_ply(index)].append(move)
    return {phase: pct(count_sources(moves, "forcing-fallback"), len(moves)) for phase, moves in phase_moves.items()}


def aggregate(games: list[GameSummary], min_stockfish_pct: float) -> dict[str, Any]:
    all_sources = Counter()
    all_think: list[float] = []
    all_charged: list[float] = []
    all_wall: list[float] = []
    result_counts = Counter(game.result for game in games)
    for game in games:
        all_sources.update(game.sources)
        all_think.extend(move.think_ms for move in game.moves if move.think_ms is not None)
        all_charged.extend(move.charged_ms for move in game.moves if move.charged_ms is not None)
        all_wall.extend(move.wall_elapsed_ms for move in game.moves if move.wall_elapsed_ms is not None)
    total_moves = sum(len(game.moves) for game in games)
    timeout_games = [game for game in games if game.termination.lower() == "timeout"]
    timeout_moves = [move for game in timeout_games for move in game.moves]
    problems = {
        "high_fallback_usage": [game.path for game in games if game.forcing_fallback_pct > 20],
        "high_least_bad_usage": [game.path for game in games if game.least_bad_pct > 5],
        "timeout_games": [game.path for game in games if game.termination.lower() == "timeout"],
        "low_stockfish_usage": [game.path for game in games if game.stockfish_pct < 80],
        "below_min_stockfish_pct": [game.path for game in games if game.stockfish_pct < min_stockfish_pct],
        "very_short_games": [game.path for game in games if game.is_very_short],
        "suspicious_eval_swings": [game.path for game in games if suspicious_eval_swing(game)],
        "mate_score_blunders": [game.path for game in games if mate_score_blunder(game)],
        "repeated_positions_over_3": [game.path for game in games if game.repeated_position_max > 3],
        "repetition_loop_endings": [game.path for game in games if game.has_repetition_loop],
    }
    openings = Counter(" ".join(move.san for move in game.moves[:6]) for game in games if game.moves)
    return {
        "total_games": len(games),
        "results": {
            "white_wins": result_counts.get("1-0", 0),
            "black_wins": result_counts.get("0-1", 0),
            "draws": result_counts.get("1/2-1/2", 0),
            "unfinished": result_counts.get("*", 0),
        },
        "average_ply_count": round(sum(game.ply_count for game in games) / len(games), 2) if games else 0,
        "average_game_length_moves": round(sum(game.move_count for game in games) / len(games), 2) if games else 0,
        "average_think_time_ms": round(sum(all_think) / len(all_think), 2) if all_think else 0,
        "average_charged_ms": round(sum(all_charged) / len(all_charged), 2) if all_charged else 0,
        "average_wall_elapsed_ms": round(sum(all_wall) / len(all_wall), 2) if all_wall else 0,
        "source_percentages": {
            "stockfish": pct(sum(count_sources(game.moves, "stockfish") for game in games), total_moves),
            "forcing_fallback": pct(sum(count_sources(game.moves, "forcing-fallback") for game in games), total_moves),
            "least_bad": pct(sum(count_sources(game.moves, "least-bad") for game in games), total_moves),
        },
        "fallback_rate_by_phase": fallback_rate_by_phase(games),
        "timeout_source_breakdown": source_breakdown(timeout_moves),
        "timeout_average_ply": round(sum(game.ply_count for game in timeout_games) / len(timeout_games), 2) if timeout_games else 0,
        "ending_percentages": {
            "checkmate": pct(sum(1 for game in games if "mate" in game.termination.lower()), len(games)),
            "timeout": pct(sum(1 for game in games if game.termination.lower() == "timeout"), len(games)),
            "draw": pct(result_counts.get("1/2-1/2", 0), len(games)),
            "early_termination_under_20_moves": pct(sum(1 for game in games if game.move_count < 20), len(games)),
        },
        "per_game": [per_game_row(game) for game in games],
        "problems": problems,
        "top_5_longest_games": [per_game_row(game) for game in sorted(games, key=lambda item: item.ply_count, reverse=True)[:5]],
        "top_5_shortest_games": [per_game_row(game) for game in sorted(games, key=lambda item: item.ply_count)[:5]],
        "most_common_openings_first_3_moves": openings.most_common(5),
    }


def print_table(title: str, rows: list[dict[str, Any]]) -> None:
    print(title)
    if not rows:
        print("- none")
        return
    for row in rows:
        print(
            f"- {Path(row['file']).name}: result={row['result']} term={row['termination']} "
            f"ply={row['final_ply']} sf={row['stockfish_pct']}% ff={row['forcing_fallback_pct']}% "
            f"lb={row['least_bad_pct']}% avgThink={row['avg_think_ms']}ms maxThink={row['max_think_ms']}ms "
            f"repeat={row['repeated_position_count']} timeout={row['timeout_side'] or '-'}"
        )


def print_summary(summary: dict[str, Any], show_problems: bool) -> None:
    print("PGN Analysis")
    print(f"Total games: {summary['total_games']}")
    print(f"Results: {summary['results']}")
    print(f"Average ply count: {summary['average_ply_count']}")
    print(f"Average game length: {summary['average_game_length_moves']} moves")
    print(f"Average think time: {summary['average_think_time_ms']} ms")
    print(f"Average charged time: {summary['average_charged_ms']} ms")
    print(f"Average wall elapsed: {summary['average_wall_elapsed_ms']} ms")
    print(f"Move sources: {summary['source_percentages']}")
    print(f"Fallback by phase: {summary['fallback_rate_by_phase']}")
    print(f"Timeout source breakdown: {summary['timeout_source_breakdown']}")
    print(f"Timeout average ply: {summary['timeout_average_ply']}")
    print(f"Endings: {summary['ending_percentages']}")
    print()
    print_table("Per-game diagnostics", summary["per_game"])
    print()
    print("Problem tables")
    for key, files in summary["problems"].items():
        print(f"- {key}: {len(files)}")
        if show_problems:
            for file in files:
                print(f"  {file}")
    print()
    print_table("Top 5 longest games", summary["top_5_longest_games"])
    print()
    print_table("Top 5 shortest games", summary["top_5_shortest_games"])
    print()
    print("Most common openings, first 3 moves")
    for opening, count in summary["most_common_openings_first_3_moves"]:
        print(f"- {count}x {opening}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze local dry-run PGN batches.")
    parser.add_argument("--dir", default="logs", help="Directory containing .pgn files.")
    parser.add_argument("--exclude", action="append", default=[], help="Exclude PGN filename/path glob. Repeatable.")
    parser.add_argument("--min-stockfish-pct", type=float, default=1, help="Minimum per-game Stockfish percentage for the threshold problem table.")
    parser.add_argument("--show-problems", action="store_true", help="Print filenames in each problem category.")
    parser.add_argument("--json", dest="json_path", default=None, help="Optional JSON summary output path.")
    args = parser.parse_args()

    games = load_games(Path(args.dir), args.exclude)
    summary = aggregate(games, args.min_stockfish_pct)
    print_summary(summary, args.show_problems)
    if args.json_path:
        output = Path(args.json_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"JSON written to {output}")


if __name__ == "__main__":
    main()
