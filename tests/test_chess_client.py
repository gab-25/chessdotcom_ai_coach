"""Unit tests for the Chess.com API client wrapper.

The upstream ``ChessDotComClient`` is fully mocked, so these tests exercise
only our parsing/derivation logic and never hit the network.
"""

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


class TestGameDetail:
    def test_finds_game_by_id_and_parses_names(self):
        client = _client(_response([_game()]))

        detail = client.game_detail("944768131")

        assert detail is not None
        assert detail["white_name"] == "MyUser"
        assert detail["black_name"] == "Opponent"
        assert detail["game"]["url"].endswith("944768131")

    def test_defaults_names_when_pgn_missing(self):
        client = _client(_response([_game(pgn="")]))

        detail = client.game_detail("944768131")

        assert detail["white_name"] == "White"
        assert detail["black_name"] == "Black"

    def test_returns_none_when_id_not_found(self):
        client = _client(_response([_game()]))

        assert client.game_detail("does-not-exist") is None
