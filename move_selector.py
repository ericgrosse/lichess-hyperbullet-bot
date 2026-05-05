from __future__ import annotations

import random
import time
import os
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import chess

from blunder_check import BlunderChecker, BlunderResult
from engine_controller import CandidateMove, EngineController


LOG = logging.getLogger(__name__)
MIN_MOVE_DELAY_MS = 10
MAX_BOOK_DELAY_MS = 80
HYPER_MAX_BOOK_DELAY_MS = 20
ULTRA_MAX_BOOK_DELAY_MS = 5
NORMAL_THINK_MS = (50, 120)
TACTICAL_THINK_MS = (150, 350)
CRITICAL_THINK_MS = (400, 900)
EMERGENCY_THINK_MS = (10, 50)
ENABLE_OPPONENT_TIME_ANALYSIS = True
ENABLE_OPENING_BOOK = True
ENABLE_RANDOMIZED_DELAY = True

BOOK: dict[str, list[str]] = {
    chess.STARTING_FEN: ["e2e4", "d2d4", "g1f3", "c2c4"],
    "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1": ["c7c5", "e7e5", "e7e6"],
    "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1": ["g8f6", "d7d5", "e7e6"],
    "rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 1": ["d7d5", "g8f6", "c7c5"],
}

BOOK_BY_SEQUENCE: dict[str, list[str]] = {
    "": ["e2e4", "d2d4", "g1f3", "c2c4"],
    "e2e4": ["c7c5", "e7e5", "e7e6", "d7d5"],
    "d2d4": ["g8f6", "d7d5", "e7e6"],
    "g1f3": ["d7d5", "g8f6", "c7c5"],
    "c2c4": ["g8f6", "e7e5", "c7c5"],
    "e2e4 c7c5": ["g1f3", "b1c3", "d2d4"],
    "e2e4 e7e5": ["g1f3", "b1c3"],
    "e2e4 e7e6": ["d2d4", "g1f3"],
    "e2e4 d7d5": ["e4d5", "b1c3"],
    "d2d4 d7d5": ["c2c4", "g1f3"],
    "d2d4 g8f6": ["c2c4", "g1f3"],
    "d2d4 d7d5 c2c4": ["e7e6", "c7c6", "d5c4"],
    "e2e4 c7c5 g1f3": ["d7d6", "b8c6", "e7e6"],
    "e2e4 e7e5 g1f3": ["b8c6", "g8f6"],
}


@dataclass
class SelectionContext:
    remaining_ms: int
    base_seconds: float
    increment_seconds: float = 0.0
    last_opponent_move: Optional[str] = None
    recent_position_keys: Optional[set[str]] = None
    quality_mode: str = "fast"


@dataclass
class SelectionResult:
    move: chess.Move
    think_time_ms: float
    eval_cp: int
    source: str
    blunder: BlunderResult
    candidates_seen: int
    prepared_hit: bool = False
    hyper_fast_path_used: bool = False
    emergency_mode: bool = False


@dataclass
class PreparedReplyCache:
    max_size: int = 512
    _items: OrderedDict[str, chess.Move] = field(default_factory=OrderedDict)

    def get(self, board: chess.Board) -> Optional[chess.Move]:
        key = board.transposition_key() if hasattr(board, "transposition_key") else board.fen()
        move = self._items.get(str(key))
        if move:
            self._items.move_to_end(str(key))
        return move

    def put(self, board: chess.Board, move: chess.Move) -> None:
        key = str(board.transposition_key() if hasattr(board, "transposition_key") else board.fen())
        self._items[key] = move
        self._items.move_to_end(key)
        while len(self._items) > self.max_size:
            self._items.popitem(last=False)


class MoveSelector:
    def __init__(
        self,
        engine: EngineController,
        enable_prepared_replies: Optional[bool] = None,
        prepare_reply_budget_ms: Optional[int] = None,
    ) -> None:
        self.engine = engine
        self.blunder_checker = BlunderChecker(engine)
        self.prepared = PreparedReplyCache()
        self.emergency = PreparedReplyCache()
        self.enable_prepared_replies = (
            self._env_bool("ENABLE_PREPARED_REPLIES", False)
            if enable_prepared_replies is None
            else enable_prepared_replies
        )
        self.prepare_reply_budget_ms = (
            self._env_int("PREPARE_REPLY_BUDGET_MS", 10)
            if prepare_reply_budget_ms is None
            else prepare_reply_budget_ms
        )
        self.prepared_cache_hits = 0
        self.prepared_cache_misses = 0
        self.prepared_analysis_started = 0
        self.prepared_analysis_skipped_engine_busy = 0
        self.prepared_analysis_cancelled = 0

    def choose_move(self, board: chess.Board, ctx: SelectionContext) -> SelectionResult:
        start = time.perf_counter()
        setattr(ctx, "_board", board)
        think_ms = self._think_budget_ms(ctx)
        emergency_mode = self._is_emergency(ctx)

        cached = self._prepared_result(board, ctx, start, emergency_mode)
        if cached:
            return cached

        book = self._book_result(board, ctx, start, emergency_mode)
        if book:
            return book

        tactical = self._tactical_result(board, ctx, start, emergency_mode)
        if tactical:
            return tactical

        if ctx.quality_mode in {"hyper", "ultra"}:
            fast_move = self._hyper_fast_path_move(board, ctx)
            if fast_move:
                check = self.blunder_checker.check(board, fast_move, 0)
                if check.ok:
                    self.emergency.put(board, fast_move)
                    return SelectionResult(fast_move, self._elapsed(start), 0, "hyper-fast-path", check, 1, False, True, emergency_mode)
            if self._instant_fallback_mode(ctx):
                move = self._fastest_safe_fallback(board, ctx)
                check = self.blunder_checker.check(board, move, 0)
                return SelectionResult(move, self._elapsed(start), 0, "emergency-fallback", check, 1, False, True, True)

        if ctx.remaining_ms <= 20 or emergency_mode:
            cached = self.emergency.get(board)
            if cached and cached in board.legal_moves:
                check = self.blunder_checker.check(board, cached, 0)
                if check.ok:
                    return SelectionResult(cached, self._elapsed(start), 0, "emergency-cache", check, 1, True, False, True)
            move = self._fastest_safe_fallback(board, ctx)
            check = self.blunder_checker.check(board, move, 0)
            return SelectionResult(move, self._elapsed(start), 0, "emergency-forcing", check, 1, False, False, True)

        if think_ms <= 0:
            candidates = self._merge_candidates(board, [])
        else:
            multipv = self._multipv_for(ctx, think_ms)
            engine_result = self.engine.analyse_candidates(board, think_ms, multipv=multipv)
            candidates = self._merge_candidates(board, engine_result.candidates)
        candidates = self._anti_repetition_order(board, candidates, ctx)
        if ctx.quality_mode in {"hyper", "ultra"}:
            candidates = candidates[:4]
        verify_ms = self._verify_ms_for(ctx)
        rejected: list[tuple[CandidateMove, BlunderResult]] = []
        safe_repeat: Optional[tuple[CandidateMove, BlunderResult]] = None
        for candidate in candidates:
            check = self.blunder_checker.check(board, candidate.move, verify_ms)
            if check.ok:
                if self._move_repeats(board, candidate.move, ctx):
                    safe_repeat = safe_repeat or (candidate, check)
                    continue
                self.emergency.put(board, candidate.move)
                return SelectionResult(candidate.move, self._elapsed(start), candidate.score_cp, candidate.source, check, len(candidates), False, False, emergency_mode)
            rejected.append((candidate, check))

        if safe_repeat:
            candidate, check = safe_repeat
            self.emergency.put(board, candidate.move)
            return SelectionResult(candidate.move, self._elapsed(start), candidate.score_cp, f"{candidate.source}-repeat", check, len(candidates), False, False, emergency_mode)

        least_bad = min(rejected, key=lambda item: item[1].severity_cp)[0] if rejected else candidates[0]
        check = rejected[0][1] if rejected else BlunderResult(False, "forced fallback")
        return SelectionResult(least_bad.move, self._elapsed(start), least_bad.score_cp, "least-bad", check, len(candidates), False, False, emergency_mode)

    def _think_budget_ms(self, ctx: SelectionContext) -> int:
        if ctx.quality_mode == "ultra":
            if ctx.remaining_ms <= 15000:
                return 0
            return random.randint(30, 50)
        if ctx.quality_mode == "hyper":
            if ctx.base_seconds >= 10:
                return self._live_hyper_budget_ms(ctx)
            if ctx.remaining_ms <= 20:
                return 0
            if ctx.remaining_ms <= 50:
                return 1
            if ctx.remaining_ms <= 100:
                return 2
            if ctx.remaining_ms <= 250:
                return 3
            if ctx.remaining_ms <= 500:
                return 5
            return 8
        if ctx.quality_mode == "sample":
            if ctx.remaining_ms <= 20:
                return 0
            if ctx.remaining_ms <= 75:
                return 5
            if ctx.remaining_ms <= 150:
                return 10
            if ctx.remaining_ms <= 250:
                return 15
            if ctx.remaining_ms <= 500:
                return 25
            if ctx.remaining_ms <= 1000:
                return 35
            if ctx.remaining_ms <= 2000:
                return 50
            return 75
        if ctx.remaining_ms <= 20:
            return 0
        if ctx.remaining_ms <= 75:
            return 1
        if ctx.remaining_ms <= 150:
            return 2
        if ctx.remaining_ms <= 250:
            return 3
        if ctx.remaining_ms <= 500:
            return 5
        if ctx.remaining_ms <= 1000:
            return 8
        if ctx.remaining_ms <= 2000:
            return 12
        if ctx.remaining_ms <= 5000:
            return 18
        return 25

    def _live_hyper_budget_ms(self, ctx: SelectionContext) -> int:
        if ctx.remaining_ms <= 10000:
            return 0
        if ctx.remaining_ms <= 20000:
            return random.randint(20, 50)
        board = getattr(ctx, "_board", None)
        if isinstance(board, chess.Board):
            legal_count = board.legal_moves.count()
            forcing_count = self._forcing_move_count(board)
            if board.is_check() or legal_count <= 3:
                return random.randint(150, 300)
            if self._previous_move_was_capture(board, ctx) or forcing_count >= 6:
                return random.randint(80, 150)
            if forcing_count >= 3 or legal_count <= 8:
                return random.randint(40, 80)
        return random.randint(40, 80)

    @staticmethod
    def _multipv_for(ctx: SelectionContext, think_ms: int) -> int:
        if ctx.quality_mode in {"hyper", "ultra"}:
            return 1 if think_ms <= 8 else 2
        if think_ms <= 10:
            return 1
        if think_ms <= 25:
            return 2
        return 3 if ctx.quality_mode == "sample" or think_ms > 25 else 2

    @staticmethod
    def _verify_ms(remaining_ms: int) -> int:
        if remaining_ms < 1000:
            return 0
        if remaining_ms < 2000:
            return 5
        if remaining_ms < 5000:
            return 10
        return 35

    @staticmethod
    def _verify_ms_for(ctx: SelectionContext) -> int:
        if ctx.quality_mode in {"hyper", "ultra"}:
            return 0 if ctx.remaining_ms <= 50 else 1
        return MoveSelector._verify_ms(ctx.remaining_ms)

    @staticmethod
    def _elapsed(start: float) -> float:
        return (time.perf_counter() - start) * 1000

    @staticmethod
    def _rand_ms(bounds: tuple[int, int]) -> int:
        lo, hi = bounds
        return random.randint(lo, hi)

    @staticmethod
    def _is_emergency(ctx: SelectionContext) -> bool:
        return ctx.base_seconds >= 10 and ctx.remaining_ms <= 3000

    @staticmethod
    def _instant_fallback_mode(ctx: SelectionContext) -> bool:
        if ctx.quality_mode == "ultra":
            return ctx.base_seconds >= 10 and ctx.remaining_ms <= 15000
        if ctx.quality_mode == "hyper":
            return ctx.remaining_ms <= 50 or (ctx.base_seconds >= 10 and ctx.remaining_ms <= 10000)
        return ctx.remaining_ms <= 20 or MoveSelector._is_emergency(ctx)

    def _maybe_delay(self, ctx: SelectionContext, max_delay_ms: int = MAX_BOOK_DELAY_MS) -> None:
        if not ENABLE_RANDOMIZED_DELAY or ctx.remaining_ms <= 5000:
            return
        if ctx.quality_mode == "ultra":
            max_delay_ms = min(max_delay_ms, ULTRA_MAX_BOOK_DELAY_MS)
            min_delay_ms = 0
        elif ctx.quality_mode == "hyper":
            max_delay_ms = min(max_delay_ms, HYPER_MAX_BOOK_DELAY_MS)
            min_delay_ms = 0
        else:
            min_delay_ms = MIN_MOVE_DELAY_MS
        if max_delay_ms <= 0:
            return
        delay_ms = random.randint(min_delay_ms, max(min_delay_ms, max_delay_ms))
        if delay_ms <= 0:
            return
        time.sleep(delay_ms / 1000)

    def _prepared_result(
        self,
        board: chess.Board,
        ctx: SelectionContext,
        start: float,
        emergency_mode: bool,
    ) -> Optional[SelectionResult]:
        cached = self.prepared.get(board)
        if not cached or cached not in board.legal_moves:
            self.prepared_cache_misses += 1
            return None
        check = self.blunder_checker.check(board, cached, 0 if emergency_mode else self._verify_ms_for(ctx))
        if not check.ok:
            self.prepared_cache_misses += 1
            return None
        self.prepared_cache_hits += 1
        if not emergency_mode:
            self._maybe_delay(ctx, 10 if ctx.quality_mode == "hyper" else 5 if ctx.quality_mode == "ultra" else MAX_BOOK_DELAY_MS)
        return SelectionResult(cached, self._elapsed(start), 0, "prepared-cache", check, 1, True, False, emergency_mode)

    def _book_result(
        self,
        board: chess.Board,
        ctx: SelectionContext,
        start: float,
        emergency_mode: bool,
    ) -> Optional[SelectionResult]:
        if not ENABLE_OPENING_BOOK or emergency_mode or ctx.remaining_ms <= 1000:
            return None
        book_move = self._book_move(board)
        if not book_move:
            return None
        check = self.blunder_checker.check(board, book_move, self._verify_ms_for(ctx))
        if not check.ok:
            return None
        self._maybe_delay(ctx, MAX_BOOK_DELAY_MS)
        self.emergency.put(board, book_move)
        return SelectionResult(book_move, self._elapsed(start), 0, "book", check, 1, False, False, False)

    def _tactical_result(
        self,
        board: chess.Board,
        ctx: SelectionContext,
        start: float,
        emergency_mode: bool,
    ) -> Optional[SelectionResult]:
        legal = list(board.legal_moves)
        if len(legal) == 1:
            move = legal[0]
            check = self.blunder_checker.check(board, move, 0)
            return SelectionResult(move, self._elapsed(start), 0, "tactical-only-legal", check, 1, False, True, emergency_mode)
        mate = self._mate_in_one(board)
        if mate:
            check = self.blunder_checker.check(board, mate, 0)
            return SelectionResult(mate, self._elapsed(start), 100000, "tactical-mate", check, len(legal), False, True, emergency_mode)
        recapture = self._recapture_move(board, ctx)
        if recapture:
            check = self.blunder_checker.check(board, recapture, 0 if ctx.remaining_ms < 5000 else 1)
            if check.ok:
                return SelectionResult(recapture, self._elapsed(start), 0, "tactical-recapture", check, len(legal), False, True, emergency_mode)
        return None

    def _book_move(self, board: chess.Board) -> Optional[chess.Move]:
        moves = BOOK_BY_SEQUENCE.get(self._move_sequence(board)) or BOOK.get(board.fen())
        if not moves:
            return None
        legal = [chess.Move.from_uci(uci) for uci in moves if chess.Move.from_uci(uci) in board.legal_moves]
        return random.choice(legal) if legal else None

    @staticmethod
    def _move_sequence(board: chess.Board, max_plies: int = 8) -> str:
        return " ".join(move.uci() for move in board.move_stack[:max_plies])

    def _merge_candidates(self, board: chess.Board, candidates: list[CandidateMove]) -> list[CandidateMove]:
        seen: set[chess.Move] = set()
        merged: list[CandidateMove] = []
        for candidate in candidates:
            if candidate.move in board.legal_moves and candidate.move not in seen:
                merged.append(candidate)
                seen.add(candidate.move)
        forcing = sorted(board.legal_moves, key=lambda m: self._fallback_score(board, m), reverse=True)
        for move in forcing[:8]:
            if move not in seen:
                merged.append(CandidateMove(move, 0, [move], "forcing-fallback"))
                seen.add(move)
        return merged or [CandidateMove(next(iter(board.legal_moves)), 0, [], "last-legal")]

    def _anti_repetition_order(
        self,
        board: chess.Board,
        candidates: list[CandidateMove],
        ctx: SelectionContext,
    ) -> list[CandidateMove]:
        if not ctx.recent_position_keys:
            return candidates
        return sorted(candidates, key=lambda candidate: self._repetition_penalty(board, candidate.move, ctx))

    def _repetition_penalty(self, board: chess.Board, move: chess.Move, ctx: SelectionContext) -> int:
        penalty = 0
        if self._move_repeats(board, move, ctx):
            penalty += 1000
        piece = board.piece_at(move.from_square)
        if piece and piece.piece_type in {chess.ROOK, chess.KNIGHT} and self._move_repeats(board, move, ctx):
            penalty += 250
        return penalty

    def _move_repeats(self, board: chess.Board, move: chess.Move, ctx: SelectionContext) -> bool:
        if not ctx.recent_position_keys:
            return False
        probe = board.copy(stack=False)
        probe.push(move)
        return self.position_key(probe) in ctx.recent_position_keys

    @staticmethod
    def position_key(board: chess.Board) -> str:
        ep = chess.square_name(board.ep_square) if board.ep_square is not None else "-"
        return f"{board.board_fen()} {board.turn} {board.castling_rights} {ep}"

    @staticmethod
    def _forcing_score(board: chess.Board, move: chess.Move) -> int:
        score = 0
        if board.gives_check(move):
            score += 100
        if board.is_capture(move):
            score += 70
        if move.promotion:
            score += 200
        return score

    def _fastest_safe_fallback(self, board: chess.Board, ctx: SelectionContext) -> chess.Move:
        for move in sorted(board.legal_moves, key=lambda m: self._fallback_score(board, m, ctx), reverse=True):
            check = self.blunder_checker.check(board, move, 0)
            if check.ok:
                return move
        return next(iter(board.legal_moves))

    def _hyper_prepared_move(self, board: chess.Board, ctx: SelectionContext, start: float) -> Optional[SelectionResult]:
        if not self._last_opponent_move_forcing(board, ctx):
            return None
        cached = self.prepared.get(board) or self.emergency.get(board)
        if cached and cached in board.legal_moves:
            check = self.blunder_checker.check(board, cached, 0)
            if check.ok:
                return SelectionResult(cached, self._elapsed(start), 0, "hyper-prepared", check, 1, True, True)
        return None

    def _hyper_fast_path_move(self, board: chess.Board, ctx: SelectionContext) -> Optional[chess.Move]:
        mate = self._mate_in_one(board)
        if mate:
            return mate
        recapture = self._recapture_move(board, ctx)
        if recapture:
            return recapture
        for move in sorted(board.legal_moves, key=lambda m: self._fallback_score(board, m, ctx), reverse=True):
            if board.gives_check(move) or board.is_capture(move):
                return move
        return None

    @staticmethod
    def _mate_in_one(board: chess.Board) -> Optional[chess.Move]:
        for move in board.legal_moves:
            probe = board.copy(stack=False)
            probe.push(move)
            if probe.is_checkmate():
                return move
        return None

    def _recapture_move(self, board: chess.Board, ctx: SelectionContext) -> Optional[chess.Move]:
        if not ctx.last_opponent_move:
            return None
        try:
            last = chess.Move.from_uci(ctx.last_opponent_move)
        except ValueError:
            return None
        captures = [move for move in board.legal_moves if move.to_square == last.to_square and board.is_capture(move)]
        safe = []
        for move in captures:
            check = self.blunder_checker.check(board, move, 0)
            if check.ok:
                safe.append(move)
        if not safe:
            return None
        return max(safe, key=lambda move: self._fallback_score(board, move, ctx))

    @staticmethod
    def _last_opponent_move_forcing(board: chess.Board, ctx: SelectionContext) -> bool:
        if not ctx.last_opponent_move or not board.move_stack:
            return False
        last = board.peek()
        if last.uci() != ctx.last_opponent_move:
            return False
        probe = board.copy()
        probe.pop()
        return probe.is_capture(last) or probe.gives_check(last) or bool(last.promotion)

    @staticmethod
    def _previous_move_was_capture(board: chess.Board, ctx: SelectionContext) -> bool:
        if not ctx.last_opponent_move or not board.move_stack:
            return False
        last = board.peek()
        if last.uci() != ctx.last_opponent_move:
            return False
        probe = board.copy()
        probe.pop()
        return probe.is_capture(last)

    def _forcing_move_count(self, board: chess.Board) -> int:
        count = 0
        for move in board.legal_moves:
            if board.gives_check(move) or board.is_capture(move) or move.promotion:
                count += 1
        return count

    def _fallback_score(self, board: chess.Board, move: chess.Move, ctx: Optional[SelectionContext] = None) -> int:
        probe = board.copy(stack=False)
        probe.push(move)
        if probe.is_checkmate():
            score = 100000
        else:
            score = 0
        if board.gives_check(move):
            score += 500
        victim = board.piece_at(move.to_square)
        attacker = board.piece_at(move.from_square)
        if victim:
            attacker_value = self._piece_value(attacker.piece_type) if attacker else 0
            score += 1000 + self._piece_value(victim.piece_type) - attacker_value // 10
        if move.promotion:
            score += 900 + self._piece_value(move.promotion)
        if ctx and self._move_repeats(board, move, ctx):
            score -= 800
            piece = board.piece_at(move.from_square)
            if piece and piece.piece_type in {chess.ROOK, chess.KNIGHT}:
                score -= 400
        if not ctx:
            piece = board.piece_at(move.from_square)
            if piece and piece.piece_type in {chess.ROOK, chess.KNIGHT} and abs(move.to_square - move.from_square) <= 8:
                score -= 50
        score += self._development_score(board, move)
        return score

    @staticmethod
    def _piece_value(piece_type: chess.PieceType) -> int:
        return {chess.PAWN: 100, chess.KNIGHT: 320, chess.BISHOP: 330, chess.ROOK: 500, chess.QUEEN: 900, chess.KING: 0}[piece_type]

    @staticmethod
    def _development_score(board: chess.Board, move: chess.Move) -> int:
        piece = board.piece_at(move.from_square)
        score = 0
        if chess.square_file(move.to_square) in {3, 4} and chess.square_rank(move.to_square) in {2, 3, 4, 5}:
            score += 40
        if piece and piece.piece_type in {chess.KNIGHT, chess.BISHOP}:
            back_rank = 0 if piece.color == chess.WHITE else 7
            if chess.square_rank(move.from_square) == back_rank:
                score += 60
        if piece and piece.piece_type == chess.PAWN and chess.square_file(move.to_square) in {3, 4}:
            score += 30
        return score

    def _prepare_common_replies(self, board: chess.Board, move: chess.Move, ctx: SelectionContext) -> None:
        # Bot API has no real premove. We cheaply predict likely opponent replies
        # and cache legal responses, so matching game states can move instantly.
        if not self.enable_prepared_replies or ctx.remaining_ms < 2000 or self.prepare_reply_budget_ms <= 0:
            return
        deadline = time.perf_counter() + (self.prepare_reply_budget_ms / 1000)
        future = board.copy(stack=False)
        future.push(move)
        likely_replies = sorted(future.legal_moves, key=lambda m: self._forcing_score(future, m), reverse=True)[:4]
        for reply in likely_replies:
            remaining_ms = int((deadline - time.perf_counter()) * 1000)
            if remaining_ms <= 0:
                break
            line = future.copy(stack=False)
            line.push(reply)
            result = self.engine.analyse_candidates(line, min(5, remaining_ms), multipv=1)
            if result.candidates:
                cached = result.candidates[0].move
                if cached in line.legal_moves:
                    self.prepared.put(line, cached)

    def prepare_opponent_time_analysis(
        self,
        board_after_our_move: chess.Board,
        ctx: SelectionContext,
        stop_event: threading.Event | None = None,
    ) -> None:
        # Runs after our move is already submitted, so this spends opponent time.
        # It predicts forcing opponent replies and stores best responses keyed by
        # the exact resulting board state.
        if not self.enable_prepared_replies or not ENABLE_OPPONENT_TIME_ANALYSIS or ctx.remaining_ms <= 3000:
            return
        if stop_event and stop_event.is_set():
            self.prepared_analysis_cancelled += 1
            return
        self.prepared_analysis_started += 1
        budget_ms = 30 if ctx.quality_mode == "ultra" else 50 if ctx.quality_mode == "hyper" else 80 if ctx.base_seconds >= 10 else self.prepare_reply_budget_ms
        if budget_ms <= 0:
            return
        deadline = time.perf_counter() + budget_ms / 1000
        likely_replies = sorted(
            board_after_our_move.legal_moves,
            key=lambda move: self._fallback_score(board_after_our_move, move, ctx),
            reverse=True,
        )[:6]
        for reply in likely_replies:
            if stop_event and stop_event.is_set():
                self.prepared_analysis_cancelled += 1
                break
            remaining_ms = int((deadline - time.perf_counter()) * 1000)
            if remaining_ms <= 0:
                break
            line = board_after_our_move.copy(stack=False)
            line.push(reply)
            if hasattr(self.engine, "analyse_candidates_if_idle"):
                result = self.engine.analyse_candidates_if_idle(line, min(25, remaining_ms), multipv=1)
            else:
                result = self.engine.analyse_candidates(line, min(25, remaining_ms), multipv=1)
            if not result.candidates:
                if getattr(result, "status", "") == "fallback-engine-busy":
                    self.prepared_analysis_skipped_engine_busy += 1
                LOG.debug("Skipping opponent-time prepared analysis; engine busy or no candidates")
                continue
            move = result.candidates[0].move
            if move in line.legal_moves:
                check = self.blunder_checker.check(line, move, 0)
                if check.ok:
                    self.prepared.put(line, move)

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        value = os.getenv(name)
        if value is None or value.strip() == "":
            return default
        return int(value)
