import chess

from blunder_check import BlunderChecker


class FakeEngine:
    def __init__(self, score):
        self.score = score

    def verify_move(self, board, move, verify_ms):
        return self.score


def test_queen_truly_hanging_is_rejected():
    board = chess.Board("4k3/8/8/5n2/3Q4/8/8/4K3 w - - 0 1")
    result = BlunderChecker().check(board, chess.Move.from_uci("e1f1"), verify_ms=0)
    assert not result.ok
    assert "queen" in result.reason


def test_defended_queen_losing_to_minor_is_rejected():
    board = chess.Board("4k3/8/8/5n2/3Q4/4B3/8/4K3 w - - 0 1")
    result = BlunderChecker().check(board, chess.Move.from_uci("e1f1"), verify_ms=0)
    assert not result.ok
    assert "queen loses exchange" in result.reason


def test_normal_rook_trade_is_allowed():
    board = chess.Board("r3k3/8/8/8/8/8/4K3/R6R w - - 0 1")
    result = BlunderChecker().check(board, chess.Move.from_uci("e2e3"), verify_ms=0)
    assert result.ok


def test_engine_approved_sacrifice_is_not_blindly_rejected():
    board = chess.Board("4k3/8/8/5n2/3Q4/4B3/8/4K3 w - - 0 1")
    result = BlunderChecker(FakeEngine(25)).check(board, chess.Move.from_uci("e1f1"), verify_ms=10)
    assert result.ok


def test_allowing_mate_in_one_is_rejected():
    board = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 2")
    result = BlunderChecker().check(board, chess.Move.from_uci("g2g4"), verify_ms=0)
    assert not result.ok
    assert "mate in 1" in result.reason
