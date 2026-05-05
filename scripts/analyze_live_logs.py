from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def iter_events(log_dir: Path):
    for path in sorted(log_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    yield path, {"type": "error", "error": f"json:{line_number}:{exc}"}
                    continue
                yield path, event


def source_breakdown(moves: list[dict[str, Any]]) -> Counter[str]:
    return Counter(str(move.get("source") or move.get("selection", {}).get("source") or "unknown") for move in moves)


def pct(part: int | float, whole: int | float) -> float:
    return round((part / whole) * 100, 2) if whole else 0.0


def analyze(log_dir: Path) -> dict[str, Any]:
    games: dict[str, dict[str, Any]] = defaultdict(lambda: {"moves": [], "errors": [], "end": None})
    for path, event in iter_events(log_dir):
        game_id = str(event.get("game_id") or path.stem)
        game = games[game_id]
        game["path"] = str(path)
        if event.get("type") == "move":
            game["moves"].append(event)
        elif event.get("type") == "game_end":
            game["end"] = event
        elif event.get("type") == "error":
            game["errors"].append(event.get("error"))

    all_moves = [move for game in games.values() for move in game["moves"]]
    sources = source_breakdown(all_moves)
    think_times = [float(move.get("think_ms", move.get("selection", {}).get("think_time_ms", 0)) or 0) for move in all_moves]
    clock_spent = []
    for move in all_moves:
        before = move.get("clock_before_ms")
        after = move.get("clock_after_ms")
        if before is not None and after is not None:
            clock_spent.append(max(0, float(before) - float(after)))
    endings = Counter()
    for game in games.values():
        end = game.get("end")
        if end:
            endings[str(end.get("termination") or end.get("result") or "unknown")] += 1
    error_games = {game_id: game["errors"] for game_id, game in games.items() if game["errors"]}
    return {
        "total_live_games": len(games),
        "total_moves": len(all_moves),
        "average_think_ms": round(sum(think_times) / len(think_times), 2) if think_times else 0,
        "move_source_breakdown": dict(sources),
        "fallback_percentage": pct(sum(count for source, count in sources.items() if "fallback" in source), len(all_moves)),
        "least_bad_percentage": pct(sum(count for source, count in sources.items() if source.startswith("least-bad")), len(all_moves)),
        "average_clock_spent_ms": round(sum(clock_spent) / len(clock_spent), 2) if clock_spent else 0,
        "games_with_errors": error_games,
        "ending_breakdown": dict(endings),
    }


def print_summary(summary: dict[str, Any]) -> None:
    print("Live Log Analysis")
    print(f"Total live games: {summary['total_live_games']}")
    print(f"Total moves: {summary['total_moves']}")
    print(f"Average think time: {summary['average_think_ms']} ms")
    print(f"Move sources: {summary['move_source_breakdown']}")
    print(f"Fallback percentage: {summary['fallback_percentage']}%")
    print(f"Least-bad percentage: {summary['least_bad_percentage']}%")
    print(f"Average clock spent: {summary['average_clock_spent_ms']} ms")
    print(f"Endings: {summary['ending_breakdown']}")
    print(f"Games with errors: {len(summary['games_with_errors'])}")
    for game_id, errors in summary["games_with_errors"].items():
        print(f"- {game_id}: {errors[:3]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze live Lichess BOT JSONL logs.")
    parser.add_argument("--dir", default="logs", help="Directory containing .jsonl logs.")
    parser.add_argument("--json", dest="json_path", default=None, help="Optional JSON output path.")
    args = parser.parse_args()
    summary = analyze(Path(args.dir))
    print_summary(summary)
    if args.json_path:
        output = Path(args.json_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"JSON written to {output}")


if __name__ == "__main__":
    main()
