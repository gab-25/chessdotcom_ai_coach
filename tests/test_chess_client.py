"""Unit tests for the Chess.com API client wrapper.

The upstream ``ChessDotComClient`` is fully mocked, so these tests exercise
only our parsing/derivation logic and never hit the network.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from chessdotcom_ai_coach.services.chess_client import Client

PGN_TEMPLATE = (
    '[White "{white}"]\n'
    '[Black "{black}"]\n'
    '[WhiteElo "{white_elo}"]\n'
    '[BlackElo "{black_elo}"]\n\n'
    "1. e4 e5 *"
)


def _response(games):
    """Fake the object returned by the library (exposes a ``.json`` attribute)."""
    return SimpleNamespace(json={"games": games})


def _game(**overrides):
    game = {
        "url": "https://www.chess.com/game/daily/944768131",
        "pgn": PGN_TEMPLATE.format(
            white="MyUser", black="Opponent", white_elo="1500", black_elo="1600"
        ),
        "turn": "white",
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    }
    game.update(overrides)
    return game


def _client(response, username="MyUser"):
    """Build a Client whose underlying API call returns ``response``."""
    with patch(
        "chessdotcom_ai_coach.services.chess_client.ChessDotComClient"
    ) as mock_cls:
        client = Client(username=username)
    client._chessdotcomclient.get_player_current_games.return_value = response
    return client


class TestMyCurrentGames:
    def test_parses_players_and_ratings_from_pgn(self):
        client = _client(_response([_game()]))

        games = client.my_current_games()

        assert len(games) == 1
        game = games[0]
        assert game["white"] == {"username": "MyUser", "rating": "1500"}
        assert game["black"] == {"username": "Opponent", "rating": "1600"}

    def test_extracts_game_id_from_url(self):
        client = _client(_response([_game()]))

        game = client.my_current_games()[0]

        assert game["game_id"] == "944768131"

    def test_is_my_turn_true_when_users_side_matches_turn(self):
        # User plays White and it is White's turn.
        client = _client(_response([_game(turn="white")]), username="MyUser")

        assert client.my_current_games()[0]["is_my_turn"] is True

    def test_is_my_turn_false_when_opponents_turn(self):
        # User plays White but it is Black's turn.
        client = _client(_response([_game(turn="black")]), username="MyUser")

        assert client.my_current_games()[0]["is_my_turn"] is False

    def test_falls_back_to_url_when_pgn_lacks_headers(self):
        game = _game(
            pgn="1. e4 e5 *",  # no [White]/[Black] headers
            white="https://api.chess.com/pub/player/whiteuser",
            black="https://api.chess.com/pub/player/blackuser",
        )
        client = _client(_response([game]))

        parsed = client.my_current_games()[0]

        assert parsed["white"]["username"] == "whiteuser"
        assert parsed["black"]["username"] == "blackuser"
        assert parsed["white"]["rating"] == "?"
        assert parsed["black"]["rating"] == "?"

    def test_defaults_to_unknown_when_no_pgn_and_no_url(self):
        game = _game(pgn="")
        game.pop("white", None)
        game.pop("black", None)
        client = _client(_response([game]))

        parsed = client.my_current_games()[0]

        assert parsed["white"]["username"] == "Unknown"
        assert parsed["black"]["username"] == "Unknown"

    def test_returns_empty_list_when_json_is_not_a_dict(self):
        client = _client(SimpleNamespace(json=None))

        assert client.my_current_games() == []


def _archive_game(**overrides):
    """Archive-shaped game: white/black are dicts carrying a `result` code."""
    game = {
        "url": "https://www.chess.com/game/daily/944768131",
        "white": {"username": "MyUser", "rating": 1500, "result": "win"},
        "black": {"username": "Opponent", "rating": 1600, "result": "resigned"},
        "pgn": '[Result "1-0"]\n\n1. e4 e5 2. Nf3 1-0',
        "time_class": "daily",
        "rules": "chess",
        "end_time": 1717200000,
    }
    game.update(overrides)
    return game


def _archive_client(response, username="MyUser"):
    """Build a Client whose monthly-archive call returns ``response``."""
    with patch(
        "chessdotcom_ai_coach.services.chess_client.ChessDotComClient"
    ) as mock_cls:
        client = Client(username=username)
    client._chessdotcomclient.get_player_games_by_month.return_value = response
    return client


def _archives_client(urls, username="MyUser"):
    """Build a Client whose archive-list call returns ``urls``."""
    with patch(
        "chessdotcom_ai_coach.services.chess_client.ChessDotComClient"
    ) as mock_cls:
        client = Client(username=username)
    client._chessdotcomclient.get_player_game_archives.return_value = SimpleNamespace(
        json={"archives": urls}
    )
    return client


class TestArchiveMonths:
    """Reading how far back an account goes, from the archive-list endpoint."""

    def test_parses_year_and_month_from_the_urls(self):
        client = _archives_client(
            [
                "https://api.chess.com/pub/player/myuser/games/2023/11",
                "https://api.chess.com/pub/player/myuser/games/2024/01",
            ]
        )

        assert client.archive_months() == [(2023, 11), (2024, 1)]

    def test_tolerates_a_trailing_slash(self):
        client = _archives_client(
            ["https://api.chess.com/pub/player/myuser/games/2024/06/"]
        )

        assert client.archive_months() == [(2024, 6)]

    def test_skips_urls_that_are_not_a_month(self):
        client = _archives_client(
            [
                "https://api.chess.com/pub/player/myuser/games/2024/06",
                "https://api.chess.com/pub/player/myuser/games/all",
            ]
        )

        assert client.archive_months() == [(2024, 6)]

    def test_returns_empty_when_json_is_not_a_dict(self):
        with patch(
            "chessdotcom_ai_coach.services.chess_client.ChessDotComClient"
        ):
            client = Client(username="MyUser")
        client._chessdotcomclient.get_player_game_archives.return_value = (
            SimpleNamespace(json=None)
        )

        assert client.archive_months() == []


class TestFinishedGames:
    """The monthly archive — the only endpoint carrying live games at all."""

    def _one(self, client):
        games = client.finished_games(2024, 6)
        assert len(games) == 1
        return games[0]

    def test_win_uses_opponent_result_as_detail(self):
        client = _archive_client(_response([_archive_game()]))

        game = self._one(client)

        assert game["game_id"] == "944768131"
        assert game["result"] == "win"
        assert game["result_detail"] == "resignation"

    def test_carries_the_archive_pgn(self):
        """The final movetext travels with the game: our own snapshot of a daily
        game stops at the last sync before it left "current games"."""
        client = _archive_client(
            _response([_archive_game(pgn='[Result "0-1"]\n\n1. d4 d5 2. c4 e6 0-1')])
        )

        assert self._one(client)["pgn"] == '[Result "0-1"]\n\n1. d4 d5 2. c4 e6 0-1'

    def test_pgn_is_empty_when_the_archive_omits_it(self):
        game = _archive_game()
        del game["pgn"]
        client = _archive_client(_response([game]))

        assert self._one(client)["pgn"] == ""

    def test_carries_the_fields_a_game_row_needs(self):
        client = _archive_client(
            _response(
                [
                    _archive_game(
                        time_class="blitz",
                        end_time=1717200000,
                        fen="8/8/8/8/8/8/8/K6k w - - 0 1",
                    )
                ]
            )
        )

        game = self._one(client)

        assert game["time_class"] == "blitz"
        assert game["fen"] == "8/8/8/8/8/8/8/K6k w - - 0 1"
        assert game["white"] == {"username": "MyUser", "rating": "1500"}
        assert game["black"] == {"username": "Opponent", "rating": "1600"}
        assert game["end_time"] == datetime(2024, 6, 1, tzinfo=timezone.utc)

    def test_end_time_is_none_when_unusable(self):
        client = _archive_client(_response([_archive_game(end_time="soon")]))

        assert self._one(client)["end_time"] is None

    def test_includes_live_games(self):
        """The whole point: bullet/blitz/rapid never appear in current games."""
        client = _archive_client(
            _response(
                [
                    _archive_game(url=".../1", time_class="bullet"),
                    _archive_game(url=".../2", time_class="rapid"),
                ]
            )
        )

        assert [g["time_class"] for g in client.finished_games(2024, 6)] == [
            "bullet",
            "rapid",
        ]

    def test_skips_variants(self):
        """Stockfish and the PGN replay assume standard chess, so a variant would
        come out truncated and wrongly evaluated rather than merely unusual."""
        client = _archive_client(
            _response(
                [
                    _archive_game(url=".../1", rules="chess960"),
                    _archive_game(url=".../2", rules="crazyhouse"),
                    _archive_game(url=".../3", rules="chess"),
                ]
            )
        )

        games = client.finished_games(2024, 6)

        assert [g["game_id"] for g in games] == ["3"]

    def test_keeps_games_with_no_rules_field(self):
        game = _archive_game()
        game.pop("rules", None)

        client = _archive_client(_response([game]))

        assert len(client.finished_games(2024, 6)) == 1

    def test_loss_uses_own_result_as_detail(self):
        client = _archive_client(
            _response(
                [
                    _archive_game(
                        white={"username": "MyUser", "result": "checkmated"},
                        black={"username": "Opponent", "result": "win"},
                    )
                ]
            )
        )

        game = self._one(client)

        assert (game["result"], game["result_detail"]) == ("loss", "checkmate")

    def test_draw_has_no_detail(self):
        client = _archive_client(
            _response(
                [
                    _archive_game(
                        white={"username": "MyUser", "result": "agreed"},
                        black={"username": "Opponent", "result": "agreed"},
                    )
                ]
            )
        )

        game = self._one(client)

        assert (game["result"], game["result_detail"]) == ("draw", "")

    def test_matches_user_on_black_side_case_insensitively(self):
        client = _archive_client(
            _response(
                [
                    _archive_game(
                        white={"username": "Opponent", "result": "win"},
                        black={"username": "myuser", "result": "timeout"},
                    )
                ]
            ),
            username="MyUser",
        )

        game = self._one(client)

        assert (game["result"], game["result_detail"]) == ("loss", "timeout")

    def test_skips_games_the_user_did_not_play(self):
        """The archive can hold games played under an alias we cannot attribute."""
        client = _archive_client(
            _response(
                [
                    _archive_game(
                        white={"username": "Someone", "result": "win"},
                        black={"username": "Else", "result": "checkmated"},
                    )
                ]
            )
        )

        assert client.finished_games(2024, 6) == []

    def test_skips_a_game_with_no_per_side_result(self):
        client = _archive_client(
            _response(
                [
                    _archive_game(
                        white={"username": "MyUser"},
                        black={"username": "Opponent"},
                    )
                ]
            )
        )

        assert client.finished_games(2024, 6) == []

    def test_returns_empty_when_json_is_not_a_dict(self):
        client = _archive_client(SimpleNamespace(json=None))

        assert client.finished_games(2024, 6) == []

    def test_passes_year_and_month_through(self):
        client = _archive_client(_response([]))

        client.finished_games(2026, 7)

        client._chessdotcomclient.get_player_games_by_month.assert_called_once_with(
            "MyUser", 2026, 7
        )
