"""Unit tests for the game-history persistence service."""

from datetime import timedelta

import pytest
from django.utils import timezone

from chessdotcom_ai_coach.models import Game
from chessdotcom_ai_coach.services import game_store


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="MyUser", password="pw12345!")


@pytest.mark.django_db
class TestQueries:
    def test_past_games_returns_only_inactive(self, user):
        Game.objects.create(user=user, game_id="active", is_active=True)
        Game.objects.create(user=user, game_id="past", is_active=False)

        past = game_store.past_games(user)
        assert [g.game_id for g in past] == ["past"]

    def test_stored_game_found_and_missing(self, user):
        Game.objects.create(user=user, game_id="1")
        assert game_store.stored_game(user, "1") is not None
        assert game_store.stored_game(user, "nope") is None


@pytest.mark.django_db
class TestResults:
    def test_new_game_defaults_to_unknown_result(self, user):
        Game.objects.create(user=user, game_id="1")
        assert Game.objects.get(user=user, game_id="1").result == Game.Result.UNKNOWN



def _archive_game(game_id="944768131", **overrides):
    """One normalised entry as `Client.finished_games` returns it."""
    game = {
        "game_id": game_id,
        "url": f"https://www.chess.com/game/live/{game_id}",
        "pgn": '[Event "T"]\n\n1. e4 e5 *',
        "fen": "8/8/8/8/8/8/8/K6k w - - 0 1",
        "time_class": "blitz",
        "end_time": timezone.now(),
        "white": {"username": "MyUser", "rating": "1500"},
        "black": {"username": "Opponent", "rating": "1600"},
        "result": "win",
        "result_detail": "resignation",
    }
    game.update(overrides)
    return game


@pytest.mark.django_db
class TestUpsertFinishedGames:
    """The archive writer — the main way games get into the app."""

    def test_creates_a_game_with_its_outcome(self, user):
        assert game_store.upsert_finished_games(user, [_archive_game()]) == 1

        game = Game.objects.get(user=user, game_id="944768131")
        assert game.time_class == "blitz"
        assert game.result == "win"
        assert game.result_detail == "resignation"
        assert game.white_name == "MyUser"
        assert game.end_time is not None
        assert game.is_active is False

    def test_updates_an_existing_game_instead_of_duplicating(self, user):
        game_store.upsert_finished_games(user, [_archive_game()])

        added = game_store.upsert_finished_games(
            user, [_archive_game(result="draw", result_detail="")]
        )

        assert added == 0
        assert Game.objects.filter(user=user).count() == 1
        assert Game.objects.get(user=user).result == "draw"

    def test_never_marks_a_game_active_again(self, user):
        """An archived game is finished by definition, and `is_active` is what
        decides whether a game is shown at all."""
        Game.objects.create(user=user, game_id="944768131", is_active=True)

        game_store.upsert_finished_games(user, [_archive_game()])

        assert Game.objects.get(user=user, game_id="944768131").is_active is False

    def test_an_empty_pgn_never_clobbers_a_stored_one(self, user):
        """A blank movetext carries no moves and would destroy the only record."""
        Game.objects.create(
            user=user, game_id="944768131", pgn='[Event "T"]\n\n1. d4 *'
        )

        game_store.upsert_finished_games(user, [_archive_game(pgn="")])

        assert Game.objects.get(user=user).pgn == '[Event "T"]\n\n1. d4 *'

    def test_skips_an_entry_without_a_game_id(self, user):
        assert game_store.upsert_finished_games(user, [_archive_game(game_id="")]) == 0
        assert Game.objects.count() == 0


@pytest.mark.django_db
class TestPastGamesFiltering:
    def test_filters_by_time_class(self, user):
        Game.objects.create(user=user, game_id="b", is_active=False, time_class="blitz")
        Game.objects.create(user=user, game_id="d", is_active=False, time_class="daily")

        blitz = game_store.past_games(user, time_class="blitz")
        assert [g.game_id for g in blitz] == ["b"]

    def test_no_filter_returns_every_finished_game(self, user):
        Game.objects.create(user=user, game_id="b", is_active=False, time_class="blitz")
        Game.objects.create(user=user, game_id="d", is_active=False, time_class="daily")

        assert game_store.past_games(user).count() == 2

    def test_orders_newest_first_by_end_time(self, user):
        now = timezone.now()
        Game.objects.create(
            user=user, game_id="older", is_active=False, end_time=now - timedelta(days=2)
        )
        Game.objects.create(user=user, game_id="newer", is_active=False, end_time=now)

        assert [g.game_id for g in game_store.past_games(user)] == ["newer", "older"]

    def test_time_classes_lists_only_what_exists(self, user):
        Game.objects.create(user=user, game_id="b", is_active=False, time_class="blitz")
        Game.objects.create(user=user, game_id="r", is_active=False, time_class="rapid")
        Game.objects.create(user=user, game_id="x", is_active=False, time_class="")
        Game.objects.create(user=user, game_id="a", is_active=True, time_class="daily")

        assert game_store.time_classes(user) == ["blitz", "rapid"]

    def test_time_classes_lists_each_one_once(self, user):
        """One chip per time control, however many games share it.

        The model orders by ``-end_time, -updated_at``; those columns must not
        leak into the SELECT, or DISTINCT would run over them too and return a
        row per game.
        """
        now = timezone.now()
        Game.objects.create(
            user=user, game_id="d1", is_active=False, time_class="daily", end_time=now
        )
        Game.objects.create(
            user=user,
            game_id="d2",
            is_active=False,
            time_class="daily",
            end_time=now - timedelta(days=1),
        )

        assert game_store.time_classes(user) == ["daily"]
