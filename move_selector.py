from __future__ import annotations

import random
import time
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import chess

from blunder_check import BlunderChecker, BlunderResult
from engine_controller import CandidateMove, EngineController


BOOK: dict[str, list[str]] = {
    chess.STARTING_FEN: ["e2e4", "d2d4", "g1f3", "c2c4"],
    "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1": ["c7c5", "e7e5", "e7e6"],
    "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq - 0 1": ["g8f6", "d7d5", "e7e6"],
    "rnbqkbnr/pppppppp/8/8/8/5N2/PPPPPPPP/RNBQKB1R b KQkq - 1 1": ["d7d5", "g8f6", "c7c5"],
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

    def choose_move(self, board: chess.Board, ctx: SelectionContext) -> SelectionResult:
        start = time.perf_counter()
        think_ms = self._think_budget_ms(ctx)
        if ctx.remaining_ms <= 20:
            cached = self.emergency.get(board)
            if cached and cached in board.legal_moves:
                check = self.blunder_checker.check(board, cached, 0)
                if check.ok:
                    return SelectionResult(cached, self._elapsed(start), 0, "emergency-cache", check, 1, True)
            move = self._fastest_safe_fallback(board, ctx)
            check = self.blunder_checker.check(board, move, 0)
            return SelectionResult(move, self._elapsed(start), 0, "emergency-forcing", check, 1)

        cached = self.prepared.get(board)
        if cached and cached in board.legal_moves and ctx.remaining_ms < 1000:
            check = self.blunder_checker.check(board, cached, 0)
            if check.ok:
                return SelectionResult(cached, self._elapsed(start), 0, "prepared-cache", check, 1, True)

        book_move = self._book_move(board)
        if book_move and ctx.remaining_ms > 1000:
            check = self.blunder_checker.check(board, book_move, self._verify_ms(ctx.remaining_ms))
            if check.ok:
                self._prepare_common_replies(board, book_move, ctx)
                return SelectionResult(book_move, self._elapsed(start), 0, "book", check, 1)

        if think_ms <= 0:
            candidates = self._merge_candidates(board, [])
        else:
            multipv = self._multipv_for(ctx, think_ms)
            engine_result = self.engine.analyse_candidates(board, think_ms, multipv=multipv)
            candidates = self._merge_candidates(board, engine_result.candidates)
        candidates = self._anti_repetition_order(board, candidates, ctx)
        verify_ms = self._verify_ms(ctx.remaining_ms)
        rejected: list[tuple[CandidateMove, BlunderResult]] = []
        safe_repeat: Optional[tuple[CandidateMove, BlunderResult]] = None
        for candidate in candidates:
            check = self.blunder_checker.check(board, candidate.move, verify_ms)
            if check.ok:
                if self._move_repeats(board, candidate.move, ctx):
                    safe_repeat = safe_repeat or (candidate, check)
                    continue
                self.emergency.put(board, candidate.move)
                self._prepare_common_replies(board, candidate.move, ctx)
                return SelectionResult(candidate.move, self._elapsed(start), candidate.score_cp, candidate.source, check, len(candidates))
            rejected.append((candidate, check))

        if safe_repeat:
            candidate, check = safe_repeat
            self.emergency.put(board, candidate.move)
            self._prepare_common_replies(board, candidate.move, ctx)
            return SelectionResult(candidate.move, self._elapsed(start), candidate.score_cp, f"{candidate.source}-repeat", check, len(candidates))

        least_bad = min(rejected, key=lambda item: item[1].severity_cp)[0] if rejected else candidates[0]
        check = rejected[0][1] if rejected else BlunderResult(False, "forced fallback")
        return SelectionResult(least_bad.move, self._elapsed(start), least_bad.score_cp, "least-bad", check, len(candidates))

    def _think_budget_ms(self, ctx: SelectionContext) -> int:
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

    @staticmethod
    def _multipv_for(ctx: SelectionContext, think_ms: int) -> int:
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
    def _elapsed(start: float) -> float:
        return (time.perf_counter() - start) * 1000

    def _book_move(self, board: chess.Board) -> Optional[chess.Move]:
        moves = BOOK.get(board.fen())
        if not moves:
            return None
        legal = [chess.Move.from_uci(uci) for uci in moves if chess.Move.from_uci(uci) in board.legal_moves]
        return random.choice(legal) if legal else None

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
