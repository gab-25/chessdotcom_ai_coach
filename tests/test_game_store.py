"""Unit tests for the game-history persistence service."""

import pytest

from chessdotcom_ai_coach.models import Game
from chessdotcom_ai_coach.services import game_store


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="MyUser", password="pw12345!")


def _game(game_id, **over):
    game = {
        "game_id": game_id,
        "url": f"https://www.chess.com/game/daily/{game_id}",
        "time_class": "daily",
        "white": {"username": "MyUser", "rating": "1500"},
        "black": {"username": "Opponent", "rating": "1600"},
        "pgn": "1. e4 e5",
        "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    }
    game.update(over)
    return game


@pytest.mark.django_db
class TestUpsertCurrentGames:
    def test_creates_rows(self, user):
        game_store.upsert_current_games(user, [_game("1"), _game("2")])

        assert Game.objects.filter(user=user).count() == 2
        stored = Game.objects.get(user=user, game_id="1")
        assert stored.white_name == "MyUser"
        assert stored.black_rating == "1600"
        assert stored.is_active is True

    def test_updates_existing_row_in_place(self, user):
        game_store.upsert_current_games(user, [_game("1", pgn="1. e4")])
        game_store.upsert_current_games(user, [_game("1", pgn="1. e4 e5 2. Nf3")])

        assert Game.objects.filter(user=user, game_id="1").count() == 1
        assert Game.objects.get(user=user, game_id="1").pgn == "1. e4 e5 2. Nf3"

    def test_marks_vanished_games_inactive(self, user):
        game_store.upsert_current_games(user, [_game("1"), _game("2")])
        # Next poll: game "2" is gone (finished).
        game_store.upsert_current_games(user, [_game("1")])

        assert Game.objects.get(user=user, game_id="1").is_active is True
        assert Game.objects.get(user=user, game_id="2").is_active is False

    def test_skips_games_without_id(self, user):
        game_store.upsert_current_games(user, [_game("")])
        assert Game.objects.filter(user=user).count() == 0


@pytest.mark.django_db
class TestQueries:
    def test_current_games_returns_only_active(self, user):
        Game.objects.create(user=user, game_id="active", is_active=True)
        Game.objects.create(user=user, game_id="past", is_active=False)

        current = game_store.current_games(user)
        assert [g.game_id for g in current] == ["active"]

    def test_past_games_returns_only_inactive(self, user):
        Game.objects.create(user=user, game_id="active", is_active=True)
        Game.objects.create(user=user, game_id="past", is_active=False)

        past = game_store.past_games(user)
        assert [g.game_id for g in past] == ["past"]

    def test_stored_game_found_and_missing(self, user):
        Game.objects.create(user=user, game_id="1")
        assert game_store.stored_game(user, "1") is not None
        assert game_store.stored_game(user, "nope") is None
