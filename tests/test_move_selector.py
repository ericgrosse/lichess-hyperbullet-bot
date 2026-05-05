import chess

from engine_controller import CandidateMove
from engine_controller import EngineController
from move_selector import MoveSelector, SelectionContext


def test_move_selector_returns_legal_starting_move_in_fallback_mode():
    board = chess.Board()
    selector = MoveSelector(EngineController("missing-stockfish"), enable_prepared_replies=False)
    result = selector.choose_move(board, SelectionContext(remaining_ms=30_000, base_seconds=0.5))
    assert result.move in board.legal_moves


def test_choose_move_works_under_1000ms():
    board = chess.Board()
    selector = MoveSelector(EngineController("missing-stockfish"), enable_prepared_replies=False)
    result = selector.choose_move(board, SelectionContext(remaining_ms=750, base_seconds=0.25))
    assert result.move in board.legal_moves


class MockEngine:
    def __init__(self, candidates=None):
        self.calls = []
        self.candidates = candidates if candidates is not None else [CandidateMove(chess.Move.from_uci("e2e4"), 20, [], "stockfish")]

    def analyse_candidates(self, board, limit_ms, multipv=1):
        self.calls.append((limit_ms, multipv))
        return type("Result", (), {"candidates": self.candidates})()

    def verify_move(self, board, move, verify_ms):
        return 0


def assert_engine_called_at(remaining_ms, expected_budget):
    board = chess.Board()
    engine = MockEngine()
    selector = MoveSelector(engine, enable_prepared_replies=False)
    result = selector.choose_move(board, SelectionContext(remaining_ms=remaining_ms, base_seconds=0.5, quality_mode="fast"))
    assert result.move in board.legal_moves
    assert engine.calls
    assert engine.calls[0][0] == expected_budget


def test_fast_mode_500ms_calls_stockfish():
    assert_engine_called_at(500, 5)


def test_fast_mode_250ms_calls_stockfish():
    assert_engine_called_at(250, 3)


def test_fast_mode_100ms_calls_stockfish():
    assert_engine_called_at(100, 2)


def test_fast_mode_50ms_calls_stockfish():
    assert_engine_called_at(50, 1)


def test_fallback_only_when_engine_returns_no_candidates():
    class MockEngine:
        def __init__(self):
            self.calls = []

        def analyse_candidates(self, board, limit_ms, multipv=1):
            self.calls.append((limit_ms, multipv))
            return type("Result", (), {"candidates": []})()

        def verify_move(self, board, move, verify_ms):
            return 0

    board = chess.Board()
    engine = MockEngine()
    selector = MoveSelector(engine, enable_prepared_replies=False)
    result = selector.choose_move(board, SelectionContext(remaining_ms=500, base_seconds=0.5))
    assert result.move in board.legal_moves
    assert engine.calls
    assert result.source == "forcing-fallback"


def test_hyper_mode_500ms_uses_5ms_single_pv():
    board = chess.Board()
    engine = MockEngine()
    selector = MoveSelector(engine, enable_prepared_replies=False)
    result = selector.choose_move(board, SelectionContext(remaining_ms=500, base_seconds=0.5, quality_mode="hyper"))
    assert result.move in board.legal_moves
    assert engine.calls[0] == (5, 1)


def test_hyper_mode_250ms_uses_3ms_single_pv():
    board = chess.Board()
    engine = MockEngine()
    selector = MoveSelector(engine, enable_prepared_replies=False)
    result = selector.choose_move(board, SelectionContext(remaining_ms=250, base_seconds=0.25, quality_mode="hyper"))
    assert result.move in board.legal_moves
    assert engine.calls[0] == (3, 1)


def test_hyper_mode_50ms_skips_stockfish():
    board = chess.Board()
    engine = MockEngine()
    selector = MoveSelector(engine, enable_prepared_replies=False)
    result = selector.choose_move(board, SelectionContext(remaining_ms=50, base_seconds=0.25, quality_mode="hyper"))
    assert result.move in board.legal_moves
    assert not engine.calls
    assert result.hyper_fast_path_used


def test_hyper_mode_caps_candidates_seen():
    board = chess.Board()
    candidates = [CandidateMove(move, 0, [], "stockfish") for move in list(board.legal_moves)[:8]]
    engine = MockEngine(candidates)
    selector = MoveSelector(engine, enable_prepared_replies=False)
    result = selector.choose_move(board, SelectionContext(remaining_ms=500, base_seconds=0.5, quality_mode="hyper"))
    assert result.move in board.legal_moves
    assert result.candidates_seen <= 4
