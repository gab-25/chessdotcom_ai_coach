"""Unit tests for the custom User model and the history models."""

import pytest
from django.db import IntegrityError

from chessdotcom_ai_coach.models import CoachSuggestion, Game, User


@pytest.mark.django_db
class TestChessUsername:
    def test_uses_chessdotcom_username_when_set(self):
        user = User(username="login_name", chessdotcom_username="chess_name")
        assert user.chess_username == "chess_name"

    def test_falls_back_to_login_username_when_unset(self):
        user = User(username="login_name", chessdotcom_username=None)
        assert user.chess_username == "login_name"

    def test_falls_back_to_login_username_when_blank(self):
        user = User(username="login_name", chessdotcom_username="")
        assert user.chess_username == "login_name"


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="MyUser", password="pw12345!")


@pytest.mark.django_db
class TestGame:
    def test_unique_per_user_and_game_id(self, user):
        Game.objects.create(user=user, game_id="944768131")
        with pytest.raises(IntegrityError):
            Game.objects.create(user=user, game_id="944768131")

    def test_defaults_to_active(self, user):
        game = Game.objects.create(user=user, game_id="1")
        assert game.is_active is True

    def test_defaults_to_unknown_result_without_label(self, user):
        game = Game.objects.create(user=user, game_id="1")
        assert game.result == Game.Result.UNKNOWN
        assert game.has_result is False
        assert game.result_label == ""

    def test_result_label_reflects_resolved_outcome(self, user):
        game = Game.objects.create(user=user, game_id="1", result=Game.Result.WIN)
        assert game.has_result is True
        assert game.result_label == "Win"


@pytest.mark.django_db
class TestCoachSuggestion:
    def test_unique_per_user_game_and_fen(self, user):
        CoachSuggestion.objects.create(
            user=user, game_id="1", fen="fen-a", eval_text="x", analysis="y"
        )
        with pytest.raises(IntegrityError):
            CoachSuggestion.objects.create(
                user=user, game_id="1", fen="fen-a", eval_text="x", analysis="z"
            )

    def test_same_fen_different_game_is_allowed(self, user):
        CoachSuggestion.objects.create(
            user=user, game_id="1", fen="fen-a", eval_text="x", analysis="y"
        )
        CoachSuggestion.objects.create(
            user=user, game_id="2", fen="fen-a", eval_text="x", analysis="y"
        )
        assert CoachSuggestion.objects.count() == 2

    def test_ordering_by_move_number(self, user):
        CoachSuggestion.objects.create(
            user=user, game_id="1", fen="b", move_no=5, eval_text="x", analysis="y"
        )
        CoachSuggestion.objects.create(
            user=user, game_id="1", fen="a", move_no=2, eval_text="x", analysis="y"
        )
        move_numbers = list(
            CoachSuggestion.objects.filter(game_id="1").values_list("move_no", flat=True)
        )
        assert move_numbers == [2, 5]
