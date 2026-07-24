"""Unit tests for the board helpers: move parsing and suggestion annotation."""

from types import SimpleNamespace

from chessdotcom_ai_coach.services import board

PGN = '[Event "Test"]\n\n1. e4 e5 2. Nf3 Nc6 *'


class TestMovesFromPgn:
    def test_parses_all_plies_in_order(self):
        moves = board.moves_from_pgn(PGN)
        assert [m["san"] for m in moves] == ["e4", "e5", "Nf3", "Nc6"]

    def test_tags_colour_and_move_number(self):
        moves = board.moves_from_pgn(PGN)
        assert moves[0]["color"] == "white"
        assert moves[0]["move_no"] == 1
        assert moves[1]["color"] == "black"
        assert moves[2]["move_no"] == 2

    def test_first_move_fen_is_starting_position(self):
        moves = board.moves_from_pgn(PGN)
        assert moves[0]["fen_before"].startswith("rnbqkbnr/pppppppp")
        # The position with White to move at move 2 carries the move counter.
        assert " w " in moves[2]["fen_before"]

    def test_empty_or_missing_pgn(self):
        assert board.moves_from_pgn("") == []
        assert board.moves_from_pgn(None) == []


class TestPositionsFromPgn:
    def test_returns_one_more_than_plies(self):
        # 4 plies -> 5 positions (initial + after each ply), index-aligned with
        # moves_from_pgn (positions[i + 1] is the position moves[i] reaches).
        moves = board.moves_from_pgn(PGN)
        positions = board.positions_from_pgn(PGN)
        assert len(positions) == len(moves) + 1

    def test_first_is_initial_and_matches_moves(self):
        moves = board.moves_from_pgn(PGN)
        positions = board.positions_from_pgn(PGN)
        assert positions[0].startswith("rnbqkbnr/pppppppp")
        # positions[i] is the position *before* moves[i].
        assert positions[0] == moves[0]["fen_before"]
        assert positions[2] == moves[2]["fen_before"]

    def test_last_reflects_the_played_moves(self):
        positions = board.positions_from_pgn(PGN)
        # After 1.e4 e5 2.Nf3 Nc6 the e/knight moves are on the board and it is
        # White to move again.
        assert " w " in positions[-1]
        assert positions[-1] != positions[0]

    def test_empty_or_missing_pgn(self):
        assert board.positions_from_pgn("") == []
        assert board.positions_from_pgn(None) == []


class TestAnnotateMoves:
    def _suggestion(self, fen, best):
        return SimpleNamespace(fen=fen, move_no=None, best_move_san=best)

    def test_marks_analyzed_and_followed(self):
        moves = board.moves_from_pgn(PGN)
        # Coach analysed the move-2 position and recommended the played move.
        s = self._suggestion(moves[2]["fen_before"], "Nf3")
        board.annotate_moves(moves, [s])

        assert moves[2]["analyzed"] is True
        assert moves[2]["followed"] is True
        assert moves[2]["suggestion"] is s
        # Moves without a matching suggestion stay untouched.
        assert moves[0]["analyzed"] is False
        assert moves[0]["followed"] is False

    def test_not_followed_when_best_differs_from_played(self):
        moves = board.moves_from_pgn(PGN)
        s = self._suggestion(moves[2]["fen_before"], "Bc4")
        board.annotate_moves(moves, [s])

        assert moves[2]["analyzed"] is True
        assert moves[2]["followed"] is False
