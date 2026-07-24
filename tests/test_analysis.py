"""Tests for whole-game analysis: the service and the management command."""

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from chessdotcom_ai_coach.models import CoachSuggestion, Game
from chessdotcom_ai_coach.services import board as board_utils
from chessdotcom_ai_coach.services.analysis import enqueue_game_analysis

PGN = '[Event "Test"]\n\n1. e4 e5 2. Nf3 Nc6 *'


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="MyUser", password="pw")


def _game(user, **overrides):
    defaults = dict(
        user=user,
        game_id="g1",
        white_name="MyUser",
        black_name="Opponent",
        pgn=PGN,
        fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        is_active=False,
    )
    defaults.update(overrides)
    return Game.objects.create(**defaults)


def _white_fens():
    return [m["fen_before"] for m in board_utils.moves_from_pgn(PGN) if m["color"] == "white"]


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.analysis.analyze_game_task")
class TestEnqueueGameAnalysis:
    def test_enqueues_each_user_move_and_skips_opponent(self, mock_task, user):
        _game(user)

        result = enqueue_game_analysis(user, "g1")

        # White (the user) played e4 and Nf3 — two moves; e5/Nc6 are the opponent's.
        assert result["enqueued"] == 2
        assert result["total"] == 2
        assert mock_task.delay.call_count == 2
        rows = CoachSuggestion.objects.filter(user=user, game_id="g1")
        assert rows.count() == 2
        assert set(rows.values_list("fen", flat=True)) == set(_white_fens())
        assert all(r.status == CoachSuggestion.Status.PENDING for r in rows)

    def test_is_idempotent(self, mock_task, user):
        _game(user)
        # One white move already analysed — it must not be re-enqueued.
        CoachSuggestion.objects.create(
            user=user, game_id="g1", fen=_white_fens()[0],
            status=CoachSuggestion.Status.DONE, best_move_san="e4", analysis="x",
        )

        result = enqueue_game_analysis(user, "g1")

        assert result["enqueued"] == 1
        assert result["total"] == 2
        assert mock_task.delay.call_count == 1
        assert CoachSuggestion.objects.filter(user=user, game_id="g1").count() == 2

    def test_uses_user_color_for_black(self, mock_task, user):
        _game(user, white_name="Opponent", black_name="MyUser")

        result = enqueue_game_analysis(user, "g1")

        black_fens = [
            m["fen_before"] for m in board_utils.moves_from_pgn(PGN) if m["color"] == "black"
        ]
        assert result["enqueued"] == 2
        assert set(
            CoachSuggestion.objects.filter(user=user, game_id="g1").values_list("fen", flat=True)
        ) == set(black_fens)

    def test_returns_none_when_game_missing(self, mock_task, user):
        assert enqueue_game_analysis(user, "nope") is None
        mock_task.delay.assert_not_called()


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.analysis.analyze_game_task")
class TestAnalyzeGameCommand:
    def test_enqueues_and_reports(self, mock_task, user):
        _game(user)
        out = StringIO()

        call_command("analyze_game", "g1", "--user", "MyUser", stdout=out)

        assert "Queued 2 new analyses" in out.getvalue()
        assert mock_task.delay.call_count == 2

    def test_errors_when_game_not_found(self, mock_task, user):
        with pytest.raises(CommandError, match="No stored game"):
            call_command("analyze_game", "nope")

    def test_errors_when_ambiguous(self, mock_task, user, django_user_model):
        other = django_user_model.objects.create_user(username="Other", password="pw")
        _game(user)
        _game(other)

        with pytest.raises(CommandError, match="multiple users"):
            call_command("analyze_game", "g1")
