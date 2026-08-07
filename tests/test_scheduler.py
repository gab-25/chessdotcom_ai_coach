"""Unit tests for the scheduler tick body: `sync_current_games` (Chess.com ->
DB), `enqueue_due_analyses` (DB -> Celery), `backfill_results` (archives -> DB,
then whole-game analysis) and `requeue_stale_analyses` (reviving lost tasks).

The Celery task and the Chess.com `Client` are mocked, so no broker, worker or
network is needed.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from chessdotcom_ai_coach.models import CoachSuggestion, Game
from chessdotcom_ai_coach.services.scheduler import (
    MAX_ANALYSIS_ATTEMPTS,
    STALE_PENDING_AFTER,
    backfill_results,
    enqueue_due_analyses,
    requeue_stale_analyses,
    sync_current_games,
)

# White to move (FEN field 2 = "w") vs. black to move.
WHITE_TO_MOVE = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
BLACK_TO_MOVE = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="MyUser", password="pw12345!")


def _game(user, **kwargs):
    defaults = {
        "game_id": "944768131",
        "white_name": "MyUser",
        "black_name": "Opponent",
        "fen": WHITE_TO_MOVE,
        "is_active": True,
    }
    defaults.update(kwargs)
    return Game.objects.create(user=user, **defaults)


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.scheduler.analyze_game_task")
class TestEnqueueDueAnalyses:
    def test_enqueues_when_user_to_move(self, mock_task, user):
        # User plays White, White to move → enqueue.
        _game(user, fen=WHITE_TO_MOVE)

        enqueued = enqueue_due_analyses()

        assert enqueued == 1
        mock_task.delay.assert_called_once()
        row = CoachSuggestion.objects.get(user=user, game_id="944768131")
        assert row.status == CoachSuggestion.Status.PENDING

    def test_skips_when_opponent_to_move(self, mock_task, user):
        # User plays White, but it's Black to move → skip.
        _game(user, fen=BLACK_TO_MOVE)

        enqueued = enqueue_due_analyses()

        assert enqueued == 0
        mock_task.delay.assert_not_called()
        assert CoachSuggestion.objects.count() == 0

    def test_skips_inactive_games(self, mock_task, user):
        _game(user, is_active=False, fen=WHITE_TO_MOVE)

        enqueued = enqueue_due_analyses()

        assert enqueued == 0
        mock_task.delay.assert_not_called()

    def test_skips_games_without_fen(self, mock_task, user):
        _game(user, fen="")

        enqueued = enqueue_due_analyses()

        assert enqueued == 0
        mock_task.delay.assert_not_called()

    def test_skips_when_user_not_a_player(self, mock_task, user):
        # Neither player matches the user's chess username.
        _game(user, white_name="Foo", black_name="Bar", fen=WHITE_TO_MOVE)

        enqueued = enqueue_due_analyses()

        assert enqueued == 0
        mock_task.delay.assert_not_called()

    def test_dedup_across_ticks(self, mock_task, user):
        # Same position on two consecutive ticks → enqueued only once.
        _game(user, fen=WHITE_TO_MOVE)

        first = enqueue_due_analyses()
        second = enqueue_due_analyses()

        assert first == 1
        assert second == 0
        assert mock_task.delay.call_count == 1
        assert CoachSuggestion.objects.filter(game_id="944768131").count() == 1

    def test_user_playing_black_to_move(self, mock_task, user):
        # User plays Black and it's Black to move → enqueue.
        _game(user, white_name="Opponent", black_name="MyUser", fen=BLACK_TO_MOVE)

        enqueued = enqueue_due_analyses()

        assert enqueued == 1
        mock_task.delay.assert_called_once()


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.scheduler.game_store.upsert_current_games")
@patch("chessdotcom_ai_coach.services.scheduler.Client")
class TestSyncCurrentGames:
    def test_syncs_user_with_linked_chess_username(
        self, mock_client_cls, mock_upsert, django_user_model
    ):
        user = django_user_model.objects.create_user(
            username="login_name",
            password="pw12345!",
            chessdotcom_username="ChessHandle",
        )
        mock_client_cls.return_value.my_current_games.return_value = ["game-dict"]

        sync_current_games()

        mock_client_cls.assert_called_once_with(username="ChessHandle")
        mock_upsert.assert_called_once_with(user, ["game-dict"])

    def test_skips_user_without_linked_username(
        self, mock_client_cls, mock_upsert, django_user_model
    ):
        # No chessdotcom_username set: chess_username would fall back to the
        # login username, but this user is intentionally not synced.
        django_user_model.objects.create_user(username="login_name", password="pw12345!")

        sync_current_games()

        mock_client_cls.assert_not_called()
        mock_upsert.assert_not_called()

    def test_skips_user_with_blank_linked_username(
        self, mock_client_cls, mock_upsert, django_user_model
    ):
        django_user_model.objects.create_user(
            username="login_name", password="pw12345!", chessdotcom_username=""
        )

        sync_current_games()

        mock_client_cls.assert_not_called()
        mock_upsert.assert_not_called()

    def test_skips_inactive_user(self, mock_client_cls, mock_upsert, django_user_model):
        django_user_model.objects.create_user(
            username="login_name",
            password="pw12345!",
            chessdotcom_username="ChessHandle",
            is_active=False,
        )

        sync_current_games()

        mock_client_cls.assert_not_called()
        mock_upsert.assert_not_called()

    def test_one_users_failure_does_not_block_the_rest(
        self, mock_client_cls, mock_upsert, django_user_model
    ):
        django_user_model.objects.create_user(
            username="bad_login", password="pw12345!", chessdotcom_username="Bad"
        )
        good_user = django_user_model.objects.create_user(
            username="good_login", password="pw12345!", chessdotcom_username="Good"
        )

        def _client_for(username):
            client = MagicMock()
            if username == "Bad":
                client.my_current_games.side_effect = Exception("boom")
            else:
                client.my_current_games.return_value = ["ok"]
            return client

        mock_client_cls.side_effect = _client_for

        sync_current_games()  # must not raise

        mock_upsert.assert_called_once_with(good_user, ["ok"])


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.scheduler.enqueue_game_analysis")
@patch("chessdotcom_ai_coach.services.scheduler.game_store.set_result")
@patch("chessdotcom_ai_coach.services.scheduler.Client")
class TestBackfillResults:
    def _linked_user(self, django_user_model):
        return django_user_model.objects.create_user(
            username="login_name",
            password="pw12345!",
            chessdotcom_username="MyUser",
        )

    def test_resolves_unresolved_finished_game(
        self, mock_client_cls, mock_set_result, mock_enqueue, django_user_model
    ):
        user = self._linked_user(django_user_model)
        _game(user, is_active=False)  # finished, result still UNKNOWN
        mock_client_cls.return_value.finished_game_results.return_value = {
            "944768131": {
                "result": "win",
                "detail": "resignation",
                "pgn": "1. e4 e5 1-0",
            }
        }

        resolved = backfill_results()

        assert resolved == 1
        mock_client_cls.assert_called_once_with(username="MyUser")
        # The archive's final PGN is written alongside the result, replacing a
        # snapshot that may have stopped short of the closing moves.
        mock_set_result.assert_called_once_with(
            user, "944768131", "win", "resignation", "1. e4 e5 1-0"
        )

    def test_backfills_every_user_move_of_the_resolved_game(
        self, mock_client_cls, mock_set_result, mock_enqueue, django_user_model
    ):
        """The live path only analyses positions a 5s poll happens to catch, so the
        moves it missed are filled in once the game ends and the full PGN is known."""
        user = self._linked_user(django_user_model)
        _game(user, is_active=False)
        mock_client_cls.return_value.finished_game_results.return_value = {
            "944768131": {"result": "win", "detail": "", "pgn": "1. e4 e5 1-0"}
        }

        backfill_results()

        mock_enqueue.assert_called_once_with(user, "944768131")

    def test_does_not_backfill_moves_of_an_unresolved_game(
        self, mock_client_cls, mock_set_result, mock_enqueue, django_user_model
    ):
        user = self._linked_user(django_user_model)
        _game(user, is_active=False)
        mock_client_cls.return_value.finished_game_results.return_value = {}

        backfill_results()

        mock_enqueue.assert_not_called()

    def test_skips_when_no_unresolved_games(
        self, mock_client_cls, mock_set_result, mock_enqueue, django_user_model
    ):
        user = self._linked_user(django_user_model)
        _game(user, is_active=True)  # still live → not backfilled

        resolved = backfill_results()

        assert resolved == 0
        mock_client_cls.assert_not_called()
        mock_set_result.assert_not_called()

    def test_leaves_unmatched_games_unresolved(
        self, mock_client_cls, mock_set_result, mock_enqueue, django_user_model
    ):
        user = self._linked_user(django_user_model)
        _game(user, is_active=False)
        # Archive has no entry for this game id (both months empty).
        mock_client_cls.return_value.finished_game_results.return_value = {}

        resolved = backfill_results()

        assert resolved == 0
        mock_set_result.assert_not_called()

    def test_falls_back_to_previous_month_when_not_in_current(
        self, mock_client_cls, mock_set_result, mock_enqueue, django_user_model
    ):
        user = self._linked_user(django_user_model)
        _game(user, is_active=False)
        # First call (current month) misses; second call (previous month) hits.
        mock_client_cls.return_value.finished_game_results.side_effect = [
            {},
            {"944768131": {"result": "draw", "detail": ""}},
        ]

        resolved = backfill_results()

        assert resolved == 1
        assert mock_client_cls.return_value.finished_game_results.call_count == 2
        mock_set_result.assert_called_once_with(user, "944768131", "draw", "", "")

    def test_one_users_failure_does_not_block_the_rest(
        self, mock_client_cls, mock_set_result, mock_enqueue, django_user_model
    ):
        bad = django_user_model.objects.create_user(
            username="bad_login", password="pw12345!", chessdotcom_username="Bad"
        )
        good = django_user_model.objects.create_user(
            username="good_login", password="pw12345!", chessdotcom_username="Good"
        )
        _game(bad, game_id="bad-game", is_active=False)
        _game(good, game_id="good-game", is_active=False)

        def _client_for(username):
            client = MagicMock()
            if username == "Bad":
                client.finished_game_results.side_effect = Exception("boom")
            else:
                client.finished_game_results.return_value = {
                    "good-game": {"result": "win", "detail": ""}
                }
            return client

        mock_client_cls.side_effect = _client_for

        resolved = backfill_results()  # must not raise

        assert resolved == 1
        mock_set_result.assert_called_once_with(good, "good-game", "win", "", "")


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.scheduler.analyze_game_task")
class TestRequeueStaleAnalyses:
    """A `CoachSuggestion` row is the in-flight lock, so a task that dies with its
    worker would otherwise leave the position PENDING — and skipped by every later
    `get_or_create` — for ever. These cover the expiry that breaks that deadlock."""

    def _pending(self, user, age, **kwargs):
        """A PENDING row whose `updated_at` is forced back by ``age``.

        `updated_at` is `auto_now`, so it can't be set on create — it has to be
        rewritten with a queryset update, which doesn't re-trigger the field.
        """
        row = CoachSuggestion.objects.create(
            user=user,
            game_id="944768131",
            fen=WHITE_TO_MOVE,
            move_no=1,
            status=CoachSuggestion.Status.PENDING,
            eval_text="",
            analysis="",
            **kwargs,
        )
        CoachSuggestion.objects.filter(pk=row.pk).update(
            updated_at=timezone.now() - age
        )
        row.refresh_from_db()
        return row

    def test_requeues_a_row_stuck_past_the_threshold(self, mock_task, user):
        _game(user)
        row = self._pending(user, STALE_PENDING_AFTER + timedelta(minutes=1), attempts=1)

        assert requeue_stale_analyses() == 1

        mock_task.delay.assert_called_once()
        row.refresh_from_db()
        assert row.status == CoachSuggestion.Status.PENDING
        assert row.attempts == 2

    def test_passes_the_games_pgn_to_the_task(self, mock_task, user):
        _game(user, pgn="1. e4 e5")
        self._pending(user, STALE_PENDING_AFTER + timedelta(minutes=1), attempts=1)

        requeue_stale_analyses()

        mock_task.delay.assert_called_once_with(
            user.id, "944768131", WHITE_TO_MOVE, "1. e4 e5"
        )

    def test_leaves_a_recently_enqueued_row_alone(self, mock_task, user):
        _game(user)
        self._pending(user, timedelta(minutes=1), attempts=1)

        assert requeue_stale_analyses() == 0
        mock_task.delay.assert_not_called()

    def test_ignores_completed_rows(self, mock_task, user):
        _game(user)
        row = self._pending(user, STALE_PENDING_AFTER + timedelta(minutes=1))
        CoachSuggestion.objects.filter(pk=row.pk).update(
            status=CoachSuggestion.Status.DONE
        )

        assert requeue_stale_analyses() == 0
        mock_task.delay.assert_not_called()

    def test_gives_up_after_the_attempt_cap(self, mock_task, user):
        _game(user)
        row = self._pending(
            user,
            STALE_PENDING_AFTER + timedelta(minutes=1),
            attempts=MAX_ANALYSIS_ATTEMPTS,
        )

        assert requeue_stale_analyses() == 0

        mock_task.delay.assert_not_called()
        row.refresh_from_db()
        # Closed as DONE (not a new status) so the card stops spinning, carrying the
        # same "unavailable" shape `coach.get_best_move` produces on engine failure.
        assert row.status == CoachSuggestion.Status.DONE
        assert row.eval_text == "Analysis unavailable."
        assert "did not complete" in row.analysis

    def test_survives_a_row_whose_game_is_gone(self, mock_task, user):
        # No `Game` row: the suggestion is decoupled from Game by design.
        self._pending(user, STALE_PENDING_AFTER + timedelta(minutes=1), attempts=1)

        assert requeue_stale_analyses() == 1
        mock_task.delay.assert_called_once_with(
            user.id, "944768131", WHITE_TO_MOVE, None
        )
