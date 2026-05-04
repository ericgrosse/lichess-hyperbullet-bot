from __future__ import annotations

import contextlib
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import chess
import chess.engine


PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}


@dataclass
class CandidateMove:
    move: chess.Move
    score_cp: int
    pv: list[chess.Move]
    source: str


@dataclass
class EngineResult:
    candidates: list[CandidateMove]
    elapsed_ms: float
    depth: Optional[int] = None


class EngineController:
    """Thin UCI wrapper with strict wall-clock limits for bullet play."""

    def __init__(self, stockfish_path: Path | str, threads: int = 1, hash_mb: int = 16) -> None:
        self.stockfish_path = str(stockfish_path)
        self.engine: chess.engine.SimpleEngine | None = None
        self.threads = threads
        self.hash_mb = hash_mb

    def __enter__(self) -> "EngineController":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self) -> None:
        if self.engine is not None:
            return
        self.engine = chess.engine.SimpleEngine.popen_uci(self.stockfish_path)
        with contextlib.suppress(Exception):
            self.engine.configure({"Threads": self.threads, "Hash": self.hash_mb})

    def close(self) -> None:
        if self.engine is not None:
            with contextlib.suppress(Exception):
                self.engine.quit()
            self.engine = None

    def analyse_candidates(self, board: chess.Board, limit_ms: int, multipv: int = 3) -> EngineResult:
        start = time.perf_counter()
        if self.engine is None:
            return self._fallback_candidates(board, start, "fallback-no-engine")
        limit_ms = max(1, min(1000, limit_ms))
        try:
            infos = self.engine.analyse(
                board,
                chess.engine.Limit(time=limit_ms / 1000),
                multipv=max(1, min(5, multipv)),
            )
            if isinstance(infos, dict):
                infos = [infos]
            candidates: list[CandidateMove] = []
            for info in infos:
                pv = list(info.get("pv") or [])
                if not pv:
                    continue
                score = info.get("score")
                score_cp = self._score_to_cp(score, board.turn)
                candidates.append(CandidateMove(pv[0], score_cp, pv, "stockfish"))
            if not candidates:
                return self._fallback_candidates(board, start, "fallback-empty-engine")
            return EngineResult(candidates, (time.perf_counter() - start) * 1000, infos[0].get("depth"))
        except Exception:
            return self._fallback_candidates(board, start, "fallback-engine-error")

    def verify_move(self, board: chess.Board, move: chess.Move, limit_ms: int) -> int:
        if self.engine is None:
            return 0
        probe = board.copy(stack=False)
        probe.push(move)
        try:
            info = self.engine.analyse(probe, chess.engine.Limit(time=max(1, limit_ms) / 1000))
            score = info.get("score")
            # The resulting position is evaluated from the side to move after the candidate.
            return -self._score_to_cp(score, probe.turn)
        except Exception:
            return 0

    @staticmethod
    def _score_to_cp(score: object, turn: chess.Color) -> int:
        if not isinstance(score, chess.engine.PovScore):
            return 0
        pov = score.pov(turn)
        if pov.is_mate():
            mate = pov.mate()
            if mate is None:
                return 0
            return 100000 if mate > 0 else -100000
        return int(pov.score(mate_score=100000) or 0)

    def _fallback_candidates(self, board: chess.Board, start: float, source: str) -> EngineResult:
        legal = list(board.legal_moves)
        ordered = sorted(legal, key=lambda move: self._move_heuristic(board, move), reverse=True)
        random.shuffle(ordered[8:])
        candidates = [CandidateMove(move, 0, [move], source) for move in ordered[:5]]
        return EngineResult(candidates, (time.perf_counter() - start) * 1000)

    @staticmethod
    def _move_heuristic(board: chess.Board, move: chess.Move) -> int:
        score = 0
        victim = board.piece_at(move.to_square)
        attacker = board.piece_at(move.from_square)
        if victim and attacker:
            score += PIECE_VALUES[victim.piece_type] - PIECE_VALUES[attacker.piece_type] // 10
        if board.gives_check(move):
            score += 90
        if move.promotion:
            score += PIECE_VALUES.get(move.promotion, 0)
        if board.is_capture(move):
            score += 30
        return score
