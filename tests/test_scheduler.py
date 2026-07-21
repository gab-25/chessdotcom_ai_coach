"""Unit tests for the APScheduler auto-analysis scheduler."""

from unittest.mock import MagicMock, patch

import pytest

from chessdotcom_ai_coach.models import Game


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="tester", password="pw12345!")


@pytest.fixture
def active_game(user):
    """A freshly created active game with a FEN but no enqueued analysis."""
    return Game.objects.create(
        user=user,
        game_id="game-001",
        fen="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
        white_name="tester",
        black_name="opponent",
        is_active=True,
        analysis_enqueued_fen="",
    )


# ---------------------------------------------------------------------------
# check_active_games_for_analysis
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCheckActiveGamesForAnalysis:
    """Tests for the main scheduler job function."""

    def test_enqueues_task_for_eligible_game(self, active_game):
        """An active game whose FEN hasn't been enqueued gets a task dispatched."""
        mock_task = MagicMock()
        with patch("chessdotcom_ai_coach.tasks.analyze_game", mock_task):
            from chessdotcom_ai_coach.scheduler import check_active_games_for_analysis

            check_active_games_for_analysis()

        mock_task.delay.assert_called_once_with(
            active_game.game_id,
            active_game.fen,
            active_game.user_id,
        )

    def test_sets_analysis_enqueued_fen_after_enqueue(self, active_game):
        """The game row is updated so the same FEN is not enqueued twice."""
        with patch("chessdotcom_ai_coach.tasks.analyze_game"):
            from chessdotcom_ai_coach.scheduler import check_active_games_for_analysis

            check_active_games_for_analysis()

        active_game.refresh_from_db()
        assert active_game.analysis_enqueued_fen == active_game.fen

    def test_does_not_enqueue_already_enqueued_game(self, active_game):
        """A game whose analysis_enqueued_fen matches the current FEN is skipped."""
        # Pre-mark the game as already enqueued.
        active_game.analysis_enqueued_fen = active_game.fen
        active_game.save(update_fields=["analysis_enqueued_fen"])

        mock_task = MagicMock()
        with patch("chessdotcom_ai_coach.tasks.analyze_game", mock_task):
            from chessdotcom_ai_coach.scheduler import check_active_games_for_analysis

            check_active_games_for_analysis()

        mock_task.delay.assert_not_called()

    def test_skips_inactive_games(self, user):
        """Inactive games are never enqueued."""
        Game.objects.create(
            user=user,
            game_id="inactive-game",
            fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
            is_active=False,
            analysis_enqueued_fen="",
        )
        mock_task = MagicMock()
        with patch("chessdotcom_ai_coach.tasks.analyze_game", mock_task):
            from chessdotcom_ai_coach.scheduler import check_active_games_for_analysis

            check_active_games_for_analysis()

        mock_task.delay.assert_not_called()

    def test_skips_games_with_empty_fen(self, user):
        """Games with no FEN (not yet snapshotted) are never enqueued."""
        Game.objects.create(
            user=user,
            game_id="no-fen-game",
            fen="",
            is_active=True,
            analysis_enqueued_fen="",
        )
        mock_task = MagicMock()
        with patch("chessdotcom_ai_coach.tasks.analyze_game", mock_task):
            from chessdotcom_ai_coach.scheduler import check_active_games_for_analysis

            check_active_games_for_analysis()

        mock_task.delay.assert_not_called()

    def test_enqueues_new_fen_when_position_changes(self, active_game):
        """After a move, the new FEN triggers a fresh analysis task."""
        old_fen = active_game.fen
        new_fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"

        # First tick: enqueue for old_fen.
        with patch("chessdotcom_ai_coach.tasks.analyze_game"):
            from chessdotcom_ai_coach.scheduler import check_active_games_for_analysis

            check_active_games_for_analysis()

        active_game.refresh_from_db()
        assert active_game.analysis_enqueued_fen == old_fen

        # Simulate a move: update the game's FEN to a new position.
        active_game.fen = new_fen
        active_game.save(update_fields=["fen"])

        # Second tick: should enqueue again for the new FEN.
        mock_task = MagicMock()
        with patch("chessdotcom_ai_coach.tasks.analyze_game", mock_task):
            check_active_games_for_analysis()

        mock_task.delay.assert_called_once_with(
            active_game.game_id,
            new_fen,
            active_game.user_id,
        )
        active_game.refresh_from_db()
        assert active_game.analysis_enqueued_fen == new_fen

    def test_enqueues_multiple_eligible_games(self, user):
        """All eligible games in the same tick are enqueued."""
        games = [
            Game.objects.create(
                user=user,
                game_id=f"game-{i}",
                fen=f"fen-{i}",
                is_active=True,
                analysis_enqueued_fen="",
            )
            for i in range(3)
        ]

        mock_task = MagicMock()
        with patch("chessdotcom_ai_coach.tasks.analyze_game", mock_task):
            from chessdotcom_ai_coach.scheduler import check_active_games_for_analysis

            check_active_games_for_analysis()

        assert mock_task.delay.call_count == 3
        called_game_ids = {c.args[0] for c in mock_task.delay.call_args_list}
        assert called_game_ids == {"game-0", "game-1", "game-2"}

    def test_exception_in_task_dispatch_is_caught(self, active_game):
        """A failure during task dispatch does not crash the scheduler tick."""
        mock_task = MagicMock()
        mock_task.delay.side_effect = Exception("broker unavailable")

        with patch("chessdotcom_ai_coach.tasks.analyze_game", mock_task):
            from chessdotcom_ai_coach.scheduler import check_active_games_for_analysis

            # Should not raise.
            check_active_games_for_analysis()
