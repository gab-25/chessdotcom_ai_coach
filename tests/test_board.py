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


class TestLastPlyFromPgn:
    """The home card's position source: one replay, last ply + reached FEN."""

    def test_returns_the_last_played_ply(self):
        last = board.last_ply_from_pgn(PGN)
        assert last["move_no"] == 2
        assert last["color"] == "black"
        assert last["san"] == "Nc6"
        assert last["uci"] == "b8c6"

    def test_fen_after_is_the_position_the_ply_reached(self):
        # Anchors the helper to positions_from_pgn, which the detail page renders
        # from — the card and the detail page cannot drift apart by a ply.
        last = board.last_ply_from_pgn(PGN)
        assert last["fen_after"] == board.positions_from_pgn(PGN)[-1]

    def test_move_number_describes_the_played_move_not_the_next(self):
        # After 2. Nf3 the *played* ply is White's move 2, but the position is at
        # move 2 with Black to move; after 2... Nc6 the position moves on to 3.
        last = board.last_ply_from_pgn('[Event "Test"]\n\n1. e4 e5 2. Nf3 *')
        assert (last["move_no"], last["color"]) == (2, "white")
        assert board.fullmove_number(last["fen_after"]) == 2
        assert board.active_color(last["fen_after"]) == "black"

        assert board.fullmove_number(board.last_ply_from_pgn(PGN)["fen_after"]) == 3

    def test_none_for_missing_or_moveless_pgn(self):
        assert board.last_ply_from_pgn("") is None
        assert board.last_ply_from_pgn(None) is None
        assert board.last_ply_from_pgn('[Event "Test"]\n\n*') is None

    def test_stops_at_the_last_legal_ply(self):
        last = board.last_ply_from_pgn('[Event "Test"]\n\n1. e4 e5 2. Qq9 *')
        assert last["san"] == "e5"

    def test_the_returned_dict_is_safe_to_mutate(self):
        # The replay is memoised on the PGN text; the cache holds an immutable
        # tuple so a caller mutating its dict can't poison later hits.
        first = board.last_ply_from_pgn(PGN)
        first["san"] = "tampered"

        assert board.last_ply_from_pgn(PGN)["san"] == "Nc6"


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
