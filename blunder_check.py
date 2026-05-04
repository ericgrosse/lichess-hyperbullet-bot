from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import chess

from engine_controller import PIECE_VALUES, EngineController


@dataclass
class BlunderResult:
    ok: bool
    reason: str
    severity_cp: int = 0


class BlunderChecker:
    """Fast tactical sanity filter for ultrabullet candidates.

    This does not try to prove the best move. It catches one-ply disasters that
    engines may still output under tiny limits: hanging queens, mate in one, and
    major pieces left to clean captures.
    """

    def __init__(self, engine: Optional[EngineController] = None) -> None:
        self.engine = engine

    def check(self, board: chess.Board, move: chess.Move, verify_ms: int) -> BlunderResult:
        if move not in board.legal_moves:
            return BlunderResult(False, "illegal move", 100000)
        if board.is_into_check(move):
            return BlunderResult(False, "moves into check", 100000)

        after = board.copy(stack=False)
        mover = board.turn
        after.push(move)

        if after.is_checkmate():
            return BlunderResult(True, "delivers mate")
        mate_threat = self._opponent_mate_in_one(after)
        if mate_threat:
            return BlunderResult(False, f"allows mate in 1: {mate_threat.uci()}", 100000)

        major_issue = self._major_piece_issue(board, after, move, mover, verify_ms)
        if major_issue:
            return major_issue

        if self.engine and verify_ms > 0:
            score = self.engine.verify_move(board, move, verify_ms)
            if score < -900:
                return BlunderResult(False, f"shallow engine veto {score}cp", -score)
        return BlunderResult(True, "safe")

    @staticmethod
    def _opponent_mate_in_one(board: chess.Board) -> Optional[chess.Move]:
        for reply in board.legal_moves:
            probe = board.copy(stack=False)
            probe.push(reply)
            if probe.is_checkmate():
                return reply
        return None

    def _major_piece_issue(
        self,
        original: chess.Board,
        board: chess.Board,
        move: chess.Move,
        mover: chess.Color,
        verify_ms: int,
    ) -> Optional[BlunderResult]:
        for piece_type in (chess.QUEEN, chess.ROOK):
            for square in board.pieces(piece_type, mover):
                attackers = self._legal_attackers_to(board, square, not mover)
                if not attackers:
                    continue
                defenders = board.attackers(mover, square)
                value = PIECE_VALUES[piece_type]
                label = "queen" if piece_type == chess.QUEEN else "rook"
                name = chess.square_name(square)
                if not defenders:
                    return BlunderResult(False, f"{label} hanging on {name}", value)
                worst_loss = self._worst_exchange_loss(board, square, mover, attackers)
                if piece_type == chess.QUEEN and worst_loss >= 400:
                    if self._has_shallow_compensation(original, move, verify_ms):
                        continue
                    return BlunderResult(False, f"queen loses exchange on {name}", worst_loss)
                if piece_type == chess.ROOK and worst_loss >= 500:
                    if self._has_shallow_compensation(original, move, verify_ms):
                        continue
                    return BlunderResult(False, f"rook loses exchange on {name}", worst_loss)
        return None

    @staticmethod
    def _legal_attackers_to(board: chess.Board, square: chess.Square, color: chess.Color) -> list[chess.Move]:
        return [move for move in board.legal_moves if move.to_square == square and board.color_at(move.from_square) == color]

    def _worst_exchange_loss(
        self,
        board: chess.Board,
        target: chess.Square,
        mover: chess.Color,
        attack_moves: list[chess.Move],
    ) -> int:
        victim = board.piece_at(target)
        if victim is None:
            return 0
        victim_value = PIECE_VALUES[victim.piece_type]
        worst = 0
        for attack in attack_moves:
            attacker = board.piece_at(attack.from_square)
            attacker_value = PIECE_VALUES.get(attacker.piece_type, 0) if attacker else 0
            if attacker_value >= victim_value:
                continue
            probe = board.copy(stack=False)
            probe.push(attack)
            recapture_values = []
            for reply in probe.legal_moves:
                if reply.to_square != target or probe.color_at(reply.from_square) != mover:
                    continue
                recapturer = probe.piece_at(reply.from_square)
                if recapturer:
                    recapture_values.append(PIECE_VALUES[recapturer.piece_type])
            if recapture_values:
                loss = max(0, victim_value - attacker_value)
            else:
                loss = victim_value
            worst = max(worst, loss)
        return worst

    def _has_shallow_compensation(self, board: chess.Board, move: chess.Move, verify_ms: int) -> bool:
        if not self.engine or verify_ms <= 0:
            return False
        return self.engine.verify_move(board, move, verify_ms) >= -250
