from __future__ import annotations

import contextlib
import random
import signal
import threading
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
    status: str = "ok"


class EngineController:
    """Thin UCI wrapper with strict wall-clock limits for bullet play."""

    _registry_lock = threading.Lock()
    _registry: set["EngineController"] = set()
    _signals_installed = False

    def __init__(self, stockfish_path: Path | str, threads: int = 1, hash_mb: int = 16) -> None:
        self.stockfish_path = str(stockfish_path)
        self.engine: chess.engine.SimpleEngine | None = None
        self.threads = threads
        self.hash_mb = hash_mb
        self._analysis_lock = threading.RLock()
        self._foreground_waiting = 0
        self._foreground_waiting_lock = threading.Lock()

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
        with self._registry_lock:
            self._registry.add(self)

    def close(self) -> None:
        if self.engine is not None:
            with contextlib.suppress(Exception):
                self.engine.quit()
            self.engine = None
        with self._registry_lock:
            self._registry.discard(self)

    def analyse_candidates(self, board: chess.Board, limit_ms: int, multipv: int = 3) -> EngineResult:
        start = time.perf_counter()
        if self.engine is None:
            return self._fallback_candidates(board, start, "fallback-no-engine")
        limit_ms = max(1, min(1000, limit_ms))
        waiting_registered = True
        with self._foreground_waiting_lock:
            self._foreground_waiting += 1
        try:
            with self._analysis_lock:
                if waiting_registered:
                    with self._foreground_waiting_lock:
                        self._foreground_waiting = max(0, self._foreground_waiting - 1)
                    waiting_registered = False
                if self.engine is None:
                    return self._fallback_candidates(board, start, "fallback-no-engine")
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
        finally:
            if waiting_registered:
                with self._foreground_waiting_lock:
                    self._foreground_waiting = max(0, self._foreground_waiting - 1)

    def analyse_candidates_if_idle(self, board: chess.Board, limit_ms: int, multipv: int = 1) -> EngineResult:
        start = time.perf_counter()
        if self.engine is None:
            return EngineResult([], (time.perf_counter() - start) * 1000, status="fallback-no-engine")
        with self._foreground_waiting_lock:
            if self._foreground_waiting > 0:
                return EngineResult([], (time.perf_counter() - start) * 1000, status="fallback-engine-busy")
        acquired = self._analysis_lock.acquire(blocking=False)
        if not acquired:
            return EngineResult([], (time.perf_counter() - start) * 1000, status="fallback-engine-busy")
        try:
            with self._foreground_waiting_lock:
                if self._foreground_waiting > 0:
                    return EngineResult([], (time.perf_counter() - start) * 1000, status="fallback-engine-busy")
            if self.engine is None:
                return EngineResult([], (time.perf_counter() - start) * 1000, status="fallback-no-engine")
            infos = self.engine.analyse(
                board,
                chess.engine.Limit(time=max(1, min(5, limit_ms)) / 1000),
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
                candidates.append(CandidateMove(pv[0], self._score_to_cp(score, board.turn), pv, "stockfish-background"))
            return EngineResult(candidates, (time.perf_counter() - start) * 1000, infos[0].get("depth") if infos else None, "ok")
        except Exception:
            return EngineResult([], (time.perf_counter() - start) * 1000, status="fallback-engine-error")
        finally:
            self._analysis_lock.release()

    def verify_move(self, board: chess.Board, move: chess.Move, limit_ms: int) -> int:
        if self.engine is None:
            return 0
        probe = board.copy(stack=False)
        probe.push(move)
        try:
            with self._analysis_lock:
                if self.engine is None:
                    return 0
                info = self.engine.analyse(probe, chess.engine.Limit(time=max(1, limit_ms) / 1000))
            score = info.get("score")
            # The resulting position is evaluated from the side to move after the candidate.
            return -self._score_to_cp(score, probe.turn)
        except Exception:
            return 0

    @classmethod
    def close_all(cls) -> None:
        with cls._registry_lock:
            engines = list(cls._registry)
        for controller in engines:
            controller.close()

    @classmethod
    def install_signal_handlers(cls) -> None:
        if cls._signals_installed:
            return
        cls._signals_installed = True
        previous_int = signal.getsignal(signal.SIGINT)

        def handle_sigint(signum: int, frame: object) -> None:
            cls.close_all()
            if callable(previous_int):
                previous_int(signum, frame)
            else:
                raise KeyboardInterrupt

        signal.signal(signal.SIGINT, handle_sigint)

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
        tail = ordered[8:]
        random.shuffle(tail)
        ordered[8:] = tail
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
