"""Unit tests for the scheduler job bodies: `sync_current_games` (Chess.com ->
DB), `enqueue_due_analyses` (DB -> Celery, for the active games),
`backfill_results` (archives -> DB), `enqueue_finished_game_analyses` (the 10
minute reconciliation over finished games), `requeue_stale_analyses` (reviving
analyses whose worker never came back) and `requeue_orphaned_analyses` (reviving
those whose broker message went missing instead).

The Celery task and the Chess.com `Client` are mocked, so no broker, worker or
network is needed.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from chessdotcom_ai_coach.models import MAX_ANALYSIS_ATTEMPTS, CoachSuggestion, Game
from chessdotcom_ai_coach.services import analysis as analysis_module
from chessdotcom_ai_coach.services import board as board_utils
from chessdotcom_ai_coach.services.scheduler import (
    ANALYSIS_TIMEOUT,
    REQUEUE_BATCH_SIZE,
    backfill_results,
    enqueue_due_analyses,
    enqueue_finished_game_analyses,
    requeue_orphaned_analyses,
    requeue_stale_analyses,
    sync_current_games,
)

# White to move (FEN field 2 = "w") vs. black to move.
WHITE_TO_MOVE = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
BLACK_TO_MOVE = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"

# Two moves each; the user (White) played e4 and Nf3.
PGN = '[Event "Test"]\n\n1. e4 e5 2. Nf3 Nc6 *'


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

    @patch("chessdotcom_ai_coach.services.analysis.analyze_game_task")
    def test_also_enqueues_moves_the_poll_missed(self, mock_backfill_task, mock_task, user):
        """A 5s poll only sees the position it lands on, so in fast time controls
        most turns come and go unseen. The already-played moves in the PGN are
        reconciled on every tick instead of waiting for the game to end."""
        _game(user, fen=BLACK_TO_MOVE, pgn=PGN)  # opponent to move: no live enqueue

        enqueued = enqueue_due_analyses()

        assert enqueued == 2  # e4 and Nf3
        assert mock_backfill_task.delay.call_count == 2
        mock_task.delay.assert_not_called()
        white_fens = {
            m["fen_before"]
            for m in board_utils.moves_from_pgn(PGN)
            if m["color"] == "white"
        }
        rows = CoachSuggestion.objects.filter(user=user, game_id="944768131")
        assert set(rows.values_list("fen", flat=True)) == white_fens

    @patch("chessdotcom_ai_coach.services.analysis.analyze_game_task")
    def test_missed_moves_are_enqueued_once(self, mock_backfill_task, mock_task, user):
        _game(user, fen=BLACK_TO_MOVE, pgn=PGN)

        assert enqueue_due_analyses() == 2
        assert enqueue_due_analyses() == 0
        assert mock_backfill_task.delay.call_count == 2


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
        self, mock_client_cls, mock_set_result, django_user_model
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

    def test_skips_when_no_unresolved_games(
        self, mock_client_cls, mock_set_result, django_user_model
    ):
        user = self._linked_user(django_user_model)
        _game(user, is_active=True)  # still live → not backfilled

        resolved = backfill_results()

        assert resolved == 0
        mock_client_cls.assert_not_called()
        mock_set_result.assert_not_called()

    def test_leaves_unmatched_games_unresolved(
        self, mock_client_cls, mock_set_result, django_user_model
    ):
        user = self._linked_user(django_user_model)
        _game(user, is_active=False)
        # Archive has no entry for this game id (both months empty).
        mock_client_cls.return_value.finished_game_results.return_value = {}

        resolved = backfill_results()

        assert resolved == 0
        mock_set_result.assert_not_called()

    def test_falls_back_to_previous_month_when_not_in_current(
        self, mock_client_cls, mock_set_result, django_user_model
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
        self, mock_client_cls, mock_set_result, django_user_model
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
@patch("chessdotcom_ai_coach.services.analysis.analyze_game_task")
class TestEnqueueFinishedGameAnalyses:
    """The 10 minute scan: every finished game is compared against its rows and the
    difference is queued, so anything the live path missed is eventually covered."""

    def _linked_user(self, django_user_model):
        return django_user_model.objects.create_user(
            username="login_name", password="pw12345!", chessdotcom_username="MyUser"
        )

    def test_enqueues_the_moves_a_finished_game_is_missing(
        self, mock_task, django_user_model
    ):
        user = self._linked_user(django_user_model)
        _game(user, is_active=False, pgn=PGN)

        # White (the user) played two moves; neither has been analysed.
        assert enqueue_finished_game_analyses() == 2
        assert mock_task.delay.call_count == 2
        assert CoachSuggestion.objects.filter(user=user).count() == 2

    def test_does_not_re_enqueue_what_is_already_covered(
        self, mock_task, django_user_model
    ):
        user = self._linked_user(django_user_model)
        _game(user, is_active=False, pgn=PGN)
        enqueue_finished_game_analyses()
        mock_task.delay.reset_mock()

        # A second run finds nothing left to do — this is what makes it safe to run
        # every ten minutes for as long as the game is stored.
        assert enqueue_finished_game_analyses() == 0
        mock_task.delay.assert_not_called()

    def test_covers_a_game_whose_result_never_resolved(
        self, mock_task, django_user_model
    ):
        """The archive backfill gives up after `RESULT_BACKFILL_WINDOW`; the scan
        reads the stored PGN instead, so such a game is still analysed."""
        user = self._linked_user(django_user_model)
        game = _game(user, is_active=False, pgn=PGN, result=Game.Result.UNKNOWN)
        Game.objects.filter(pk=game.pk).update(
            updated_at=timezone.now() - timedelta(days=30)
        )

        assert enqueue_finished_game_analyses() == 2

    def test_skips_active_games(self, mock_task, django_user_model):
        # Those belong to the 5s tick, which also has the live position to enqueue.
        user = self._linked_user(django_user_model)
        _game(user, is_active=True, pgn=PGN)

        assert enqueue_finished_game_analyses() == 0
        mock_task.delay.assert_not_called()

    def test_skips_games_without_a_pgn(self, mock_task, django_user_model):
        user = self._linked_user(django_user_model)
        _game(user, is_active=False, pgn="")

        assert enqueue_finished_game_analyses() == 0
        mock_task.delay.assert_not_called()

    def test_stops_at_the_per_run_budget(self, mock_task, django_user_model):
        user = self._linked_user(django_user_model)
        _game(user, game_id="g1", is_active=False, pgn=PGN)
        _game(user, game_id="g2", is_active=False, pgn=PGN)

        with patch(
            "chessdotcom_ai_coach.services.scheduler.ENQUEUE_BUDGET_PER_RUN", 3
        ):
            assert enqueue_finished_game_analyses() == 3

        # The fourth analysis waits for the next run rather than piling onto the
        # worker now.
        assert mock_task.delay.call_count == 3

    def test_one_users_failure_does_not_block_the_rest(
        self, mock_task, django_user_model
    ):
        bad = self._linked_user(django_user_model)
        good = django_user_model.objects.create_user(
            username="good_login", password="pw12345!", chessdotcom_username="Good"
        )
        _game(bad, game_id="bad-game", is_active=False, pgn=PGN)
        _game(good, game_id="good-game", is_active=False, pgn=PGN, white_name="Good")

        real = analysis_module.enqueue_game_analysis

        def _explode(user, game_id, **kwargs):
            if game_id == "bad-game":
                raise RuntimeError("boom")
            return real(user, game_id, **kwargs)

        with patch(
            "chessdotcom_ai_coach.services.scheduler.enqueue_game_analysis", _explode
        ):
            assert enqueue_finished_game_analyses() == 2  # must not raise

        assert CoachSuggestion.objects.filter(user=good).count() == 2


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.scheduler.analyze_game_task")
class TestRequeueStaleAnalyses:
    """A `CoachSuggestion` row is the in-flight lock, so an analysis whose worker
    never came back would otherwise leave the position RUNNING — and skipped by
    every later `get_or_create` — for ever. These cover the expiry on that lock.

    Only RUNNING rows are swept: a PENDING row is queued, not stuck, and can
    legitimately wait far longer than `ANALYSIS_TIMEOUT` while a scan drains.
    """

    def _running(self, user, age, **kwargs):
        """A RUNNING row whose `updated_at` is forced back by ``age``.

        `updated_at` is `auto_now`, so it can't be set on create — it has to be
        rewritten with a queryset update, which doesn't re-trigger the field. For a
        RUNNING row it is the moment the worker started, which is what times out.
        """
        row = CoachSuggestion.objects.create(
            user=user,
            game_id="944768131",
            fen=WHITE_TO_MOVE,
            move_no=1,
            status=CoachSuggestion.Status.RUNNING,
            eval_text="",
            analysis="",
            **kwargs,
        )
        CoachSuggestion.objects.filter(pk=row.pk).update(
            updated_at=timezone.now() - age
        )
        row.refresh_from_db()
        return row

    def test_requeues_a_row_stuck_past_the_timeout(self, mock_task, user):
        _game(user)
        row = self._running(user, ANALYSIS_TIMEOUT + timedelta(minutes=1), attempts=1)

        assert requeue_stale_analyses() == 1

        mock_task.delay.assert_called_once()
        row.refresh_from_db()
        assert row.status == CoachSuggestion.Status.PENDING
        # The attempt is counted by the worker when it picks the row up, not here,
        # so a re-queue that turns out to be unnecessary costs nothing.
        assert row.attempts == 1

    def test_passes_the_games_pgn_to_the_task(self, mock_task, user):
        _game(user, pgn="1. e4 e5")
        self._running(user, ANALYSIS_TIMEOUT + timedelta(minutes=1), attempts=1)

        requeue_stale_analyses()

        mock_task.delay.assert_called_once_with(
            user.id, "944768131", WHITE_TO_MOVE, "1. e4 e5"
        )

    def test_leaves_an_analysis_within_the_timeout_alone(self, mock_task, user):
        _game(user)
        self._running(user, timedelta(minutes=1), attempts=1)

        assert requeue_stale_analyses() == 0
        mock_task.delay.assert_not_called()

    def test_ignores_queued_rows(self, mock_task, user):
        """A PENDING row has no worker on it: it is waiting its turn on the broker,
        which redelivers it if the worker dies (`task_acks_late`). Timing it out
        would re-queue healthy work and deepen the very backlog it reacts to."""
        _game(user)
        row = self._running(user, ANALYSIS_TIMEOUT + timedelta(minutes=1))
        CoachSuggestion.objects.filter(pk=row.pk).update(
            status=CoachSuggestion.Status.PENDING
        )

        assert requeue_stale_analyses() == 0
        mock_task.delay.assert_not_called()

    def test_ignores_completed_rows(self, mock_task, user):
        _game(user)
        row = self._running(user, ANALYSIS_TIMEOUT + timedelta(minutes=1))
        CoachSuggestion.objects.filter(pk=row.pk).update(
            status=CoachSuggestion.Status.DONE
        )

        assert requeue_stale_analyses() == 0
        mock_task.delay.assert_not_called()

    def test_gives_up_after_the_attempt_cap(self, mock_task, user):
        _game(user)
        row = self._running(
            user,
            ANALYSIS_TIMEOUT + timedelta(minutes=1),
            attempts=MAX_ANALYSIS_ATTEMPTS,
        )

        assert requeue_stale_analyses() == 0

        mock_task.delay.assert_not_called()
        row.refresh_from_db()
        # Retired explicitly, so the card can offer a retry instead of spinning.
        assert row.status == CoachSuggestion.Status.FAILED

    def test_survives_a_row_whose_game_is_gone(self, mock_task, user):
        # No `Game` row: the suggestion is decoupled from Game by design.
        self._running(user, ANALYSIS_TIMEOUT + timedelta(minutes=1), attempts=1)

        assert requeue_stale_analyses() == 1
        mock_task.delay.assert_called_once_with(
            user.id, "944768131", WHITE_TO_MOVE, None
        )


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.scheduler._queued_task_count")
@patch("chessdotcom_ai_coach.services.scheduler.analyze_game_task")
class TestRequeueOrphanedAnalyses:
    """The other way a position gets stuck: the row survives but its message
    doesn't (Redis losing the queue, a bad manual write). Nothing else recovers
    it — `requeue_stale_analyses` only looks at RUNNING, and every reconciliation
    pass sees the PENDING row and treats the position as already queued.

    Detection is by comparison, not by age: an empty queue with nothing running
    means no PENDING row can have a message, whatever its age.
    """

    def _pending(self, user, **overrides):
        defaults = {
            "game_id": "944768131",
            "fen": WHITE_TO_MOVE,
            "move_no": 1,
            "status": CoachSuggestion.Status.PENDING,
            "eval_text": "",
            "analysis": "",
        }
        defaults.update(overrides)
        return CoachSuggestion.objects.create(user=user, **defaults)

    def test_requeues_when_the_queue_is_empty(self, mock_task, mock_depth, user):
        _game(user, pgn="1. e4 e5")
        row = self._pending(user)
        mock_depth.return_value = 0

        assert requeue_orphaned_analyses() == 1

        mock_task.delay.assert_called_once_with(
            user.id, "944768131", WHITE_TO_MOVE, "1. e4 e5"
        )
        row.refresh_from_db()
        # Still PENDING, and no attempt spent: nothing was ever attempted.
        assert row.status == CoachSuggestion.Status.PENDING
        assert row.attempts == 0

    def test_does_nothing_while_work_is_queued(self, mock_task, mock_depth, user):
        """A deep queue is the normal state during a scan — those rows have
        messages, they just haven't been reached yet."""
        _game(user)
        self._pending(user)
        mock_depth.return_value = 5

        assert requeue_orphaned_analyses() == 0
        mock_task.delay.assert_not_called()

    def test_does_nothing_while_a_worker_is_running(self, mock_task, mock_depth, user):
        """The queue empties as the last messages are picked up; a RUNNING row
        means the worker is mid-drain, not that anything is orphaned."""
        _game(user)
        self._pending(user)
        CoachSuggestion.objects.create(
            user=user,
            game_id="944768131",
            fen=BLACK_TO_MOVE,
            move_no=1,
            status=CoachSuggestion.Status.RUNNING,
            eval_text="",
            analysis="",
        )
        mock_depth.return_value = 0

        assert requeue_orphaned_analyses() == 0
        mock_task.delay.assert_not_called()
        mock_depth.assert_not_called()  # cheap check first

    def test_does_nothing_when_the_broker_is_unreadable(
        self, mock_task, mock_depth, user
    ):
        """Unknown depth must never be read as an empty queue, or a broker hiccup
        would duplicate the whole backlog."""
        _game(user)
        self._pending(user)
        mock_depth.return_value = None

        assert requeue_orphaned_analyses() == 0
        mock_task.delay.assert_not_called()

    def test_leaves_settled_rows_alone(self, mock_task, mock_depth, user):
        _game(user)
        self._pending(user, status=CoachSuggestion.Status.DONE)
        self._pending(
            user, fen=BLACK_TO_MOVE, status=CoachSuggestion.Status.FAILED
        )
        mock_depth.return_value = 0

        assert requeue_orphaned_analyses() == 0
        mock_task.delay.assert_not_called()

    def test_drains_in_batches_oldest_first(self, mock_task, mock_depth, user):
        _game(user)
        for i in range(REQUEUE_BATCH_SIZE + 5):
            self._pending(user, fen=f"{WHITE_TO_MOVE} {i}")
        mock_depth.return_value = 0

        assert requeue_orphaned_analyses() == REQUEUE_BATCH_SIZE
        assert mock_task.delay.call_count == REQUEUE_BATCH_SIZE
