import chess

from run_bot import board_from_moves


def test_board_from_moves_reconstructs_legal_position():
    board = board_from_moves("e2e4 e7e5 g1f3")
    expected = chess.Board()
    expected.push_uci("e2e4")
    expected.push_uci("e7e5")
    expected.push_uci("g1f3")
    assert board.board_fen() == expected.board_fen()
    assert board.turn == expected.turn
    assert board.is_valid()
