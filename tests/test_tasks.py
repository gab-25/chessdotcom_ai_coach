from unittest.mock import AsyncMock, patch

import pytest

from chessdotcom_ai_coach.models import CoachSuggestion, Game
from chessdotcom_ai_coach.tasks import analyze_game_position, schedule_active_game_analyses


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(
        username="MyUser", chessdotcom_username="MyUser"
    )


def _fen(active="w"):
    return f"rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR {active} KQkq - 0 1"


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.tasks.analyze_game_position.apply_async")
def test_scheduler_enqueues_only_eligible_games(mock_apply_async, monkeypatch, user):
    monkeypatch.setenv("ANALYSIS_SCHEDULER_BATCH_SIZE", "10")
    eligible = Game.objects.create(
        user=user, game_id="1", white_name="MyUser", black_name="Other", fen=_fen("w")
    )
    Game.objects.create(
        user=user, game_id="2", white_name="MyUser", black_name="Other", fen=_fen("b")
    )
    analyzed = Game.objects.create(
        user=user, game_id="3", white_name="MyUser", black_name="Other", fen=_fen("w")
    )
    CoachSuggestion.objects.create(
        user=user,
        game_id=analyzed.game_id,
        fen=analyzed.fen,
        eval_text="ok",
        analysis="done",
    )

    result = schedule_active_game_analyses.run()

    assert result["analyses_started"] == 1
    mock_apply_async.assert_called_once()
    eligible.refresh_from_db()
    assert eligible.analysis_enqueued_fen == eligible.fen


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.tasks.analyze_game_position.apply_async")
def test_scheduler_is_idempotent_for_same_fen(mock_apply_async, monkeypatch, user):
    monkeypatch.setenv("ANALYSIS_SCHEDULER_BATCH_SIZE", "10")
    game = Game.objects.create(
        user=user, game_id="10", white_name="MyUser", black_name="Other", fen=_fen("w")
    )

    schedule_active_game_analyses.run()
    schedule_active_game_analyses.run()

    assert mock_apply_async.call_count == 1
    game.refresh_from_db()
    assert game.analysis_enqueued_fen == game.fen


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.tasks.get_best_move", new_callable=AsyncMock)
def test_analyze_game_position_creates_suggestion(mock_get_best_move, user):
    fen = _fen("w")
    game = Game.objects.create(
        user=user,
        game_id="20",
        white_name="MyUser",
        black_name="Other",
        fen=fen,
        pgn='[Event "T"]\n\n1. e4 *',
        analysis_enqueued_fen=fen,
    )
    mock_get_best_move.return_value = {
        "eval_text": "The position is balanced (+0.20).",
        "eval_cp": 0.2,
        "best_move_san": "e4",
        "best_move_uci": "e2e4",
        "analysis": "Play e4.",
    }

    result = analyze_game_position.run(game.pk, fen)

    assert result == "ok"
    assert CoachSuggestion.objects.filter(user=user, game_id=game.game_id, fen=fen).count() == 1
