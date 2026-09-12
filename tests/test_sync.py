"""Unit tests for `services.sync`: the request-driven replacement for the old
scheduler.

* `sync_user` — the monthly archive -> DB, and where the app's games come from.
* `request_sync` — the per-user claim that keeps N web replicas from starting the
  same import, and hands it to the worker.
* `requeue_stale_analyses` / `requeue_orphaned_analyses` — reviving analyses whose
  worker, or whose broker message, never came back, and `recover_stuck_analyses`
  which throttles them behind the detail page's Refresh button.

**Nothing here enqueues analysis** — that is on demand, from the detail page.
`TestSyncUser.test_never_enqueues_analysis` pins it down.

The Celery tasks and the Chess.com `Client` are mocked, so no broker, worker or
network is needed.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from chessdotcom_ai_coach.models import (
    MAX_ANALYSIS_ATTEMPTS,
    ArchiveImport,
    CoachSuggestion,
    Game,
    User,
)
from chessdotcom_ai_coach.services import sync as sync_module
from chessdotcom_ai_coach.services.sync import (
    ANALYSIS_TIMEOUT,
    REQUEUE_BATCH_SIZE,
    SYNC_COOLDOWN,
    import_all_archives,
    recover_stuck_analyses,
    request_sync,
    requeue_orphaned_analyses,
    requeue_stale_analyses,
    sync_user,
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


def _archive_game(game_id="944768131", **overrides):
    """One normalised entry as `Client.finished_games` returns it."""
    game = {
        "game_id": game_id,
        "url": f"https://www.chess.com/game/live/{game_id}",
        "pgn": PGN,
        "fen": WHITE_TO_MOVE,
        "time_class": "blitz",
        "end_time": timezone.now(),
        "white": {"username": "MyUser", "rating": "1500"},
        "black": {"username": "Opponent", "rating": "1600"},
        "result": "win",
        "result_detail": "resignation",
    }
    game.update(overrides)
    return game


def _archive_client(mock_client_cls, months, games_by_month=None):
    """Point the mocked Client at a fixed archive: months + games per month."""
    games_by_month = games_by_month or {}
    client = mock_client_cls.return_value
    client.archive_months.return_value = months
    client.finished_games.side_effect = lambda y, m: games_by_month.get((y, m), [])
    return client


@pytest.fixture(autouse=True)
def _reset_recovery_throttle():
    """`recover_stuck_analyses` throttles on a module global that outlives a test."""
    sync_module._last_recovery = None
    yield
    sync_module._last_recovery = None


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.sync.Client")
class TestSyncUser:
    """The archive import: where the app's games actually come from.

    Which months get read is a set difference — everything with no
    `ArchiveImport` row, plus the current one — so a first sync reads the whole
    history, a later one reads only the current month, and an interrupted first
    sync picks up where it stopped.
    """

    @pytest.fixture(autouse=True)
    def _linked(self, django_user_model):
        self.user = django_user_model.objects.create_user(
            username="login_name", password="pw12345!", chessdotcom_username="MyUser"
        )

    def _now_month(self):
        now = timezone.now()
        return (now.year, now.month)

    def test_imports_the_current_month(self, mock_client_cls):
        current = self._now_month()
        _archive_client(mock_client_cls, [current], {current: [_archive_game()]})

        assert sync_user(self.user) == 1

        game = Game.objects.get(user=self.user, game_id="944768131")
        assert game.time_class == "blitz"  # a live game: never in "current games"
        assert game.result == "win"
        assert game.is_active is False

    def test_records_the_month_it_read(self, mock_client_cls):
        current = self._now_month()
        _archive_client(mock_client_cls, [current], {current: [_archive_game()]})

        sync_user(self.user)

        row = ArchiveImport.objects.get(user=self.user)
        assert (row.year, row.month, row.game_count) == (*current, 1)

    def test_first_sync_reads_the_whole_history(self, mock_client_cls):
        """No pacing any more: the user gets their games, not a month of them."""
        current = self._now_month()
        months = [(2023, 1), (2023, 2), current]
        client = _archive_client(
            mock_client_cls,
            months,
            {
                (2023, 1): [_archive_game("old1")],
                (2023, 2): [_archive_game("old2")],
                current: [_archive_game("new")],
            },
        )

        assert sync_user(self.user) == 3
        assert {row.game_id for row in Game.objects.all()} == {"old1", "old2", "new"}
        # Newest first, so recent games land before years of history scroll past.
        assert [call.args for call in client.finished_games.call_args_list] == [
            current,
            (2023, 2),
            (2023, 1),
        ]

    def test_a_later_sync_reads_only_the_current_month(self, mock_client_cls):
        """It keeps growing as the user plays, so a finished game is in it and
        nowhere else. Everything else has a row and is left alone."""
        current = self._now_month()
        client = _archive_client(mock_client_cls, [(2023, 1), current])

        sync_user(self.user)
        client.finished_games.reset_mock()
        sync_user(self.user)

        assert [call.args for call in client.finished_games.call_args_list] == [current]

    def test_is_idempotent(self, mock_client_cls):
        current = self._now_month()
        _archive_client(mock_client_cls, [current], {current: [_archive_game()]})

        assert sync_user(self.user) == 1
        assert sync_user(self.user) == 0  # updated in place, not added
        assert Game.objects.count() == 1

    def test_an_interrupted_backfill_resumes_the_months_it_missed(
        self, mock_client_cls
    ):
        """A first sync cut short by a restart or a rate limit leaves rows for the
        months it managed. Treating "has any row" as "imported" would strand the
        rest of the history for good; the set difference reads exactly the gaps."""
        current = self._now_month()
        months = [(2023, 1), (2023, 2), (2023, 3), current]
        client = _archive_client(mock_client_cls, months)
        # A previous run got through the current month and 2023-03 before dying.
        for year, month in (current, (2023, 3)):
            ArchiveImport.objects.create(user=self.user, year=year, month=month)

        sync_user(self.user)

        read = [call.args for call in client.finished_games.call_args_list]
        assert read == [current, (2023, 2), (2023, 1)]

    def test_heartbeats_the_sync_claim_as_it_goes(self, mock_client_cls):
        """A first backfill can outlast SYNC_COOLDOWN. Without this the claim would
        expire mid-run and a second sync would start on top of it."""
        current = self._now_month()
        _archive_client(mock_client_cls, [(2023, 1), current])
        self.user.last_synced_at = timezone.now() - SYNC_COOLDOWN * 2
        self.user.save()

        sync_user(self.user)

        self.user.refresh_from_db()
        assert timezone.now() - self.user.last_synced_at < SYNC_COOLDOWN

    def test_leaves_a_legacy_in_progress_row_alone(self, mock_client_cls):
        """Older versions snapshotted games while they were being played. Such a
        row is not in the archive, so the import must not touch it — it closes
        itself out once that game is covered."""
        current = self._now_month()
        Game.objects.create(user=self.user, game_id="running", is_active=True, pgn=PGN)
        _archive_client(mock_client_cls, [current], {current: [_archive_game()]})

        sync_user(self.user)

        assert Game.objects.get(game_id="running").is_active is True

    def test_closes_out_a_legacy_row_once_the_archive_covers_it(self, mock_client_cls):
        """The same row, updated in place: finished, with the archive's full PGN."""
        current = self._now_month()
        Game.objects.create(
            user=self.user,
            game_id="944768131",
            is_active=True,
            pgn='[Event "T"]\n\n1. e4 *',
        )
        _archive_client(mock_client_cls, [current], {current: [_archive_game()]})

        sync_user(self.user)

        game = Game.objects.get(game_id="944768131")
        assert Game.objects.count() == 1  # updated, not duplicated
        assert game.is_active is False
        assert game.pgn == PGN
        assert game.result == "win"

    @patch("chessdotcom_ai_coach.services.analysis.analyze_game_task")
    def test_never_enqueues_analysis(self, mock_task, mock_client_cls):
        """Analysis is on demand: importing a game must not queue any work."""
        current = self._now_month()
        _archive_client(mock_client_cls, [current], {current: [_archive_game()]})

        sync_user(self.user)

        mock_task.delay.assert_not_called()
        assert CoachSuggestion.objects.count() == 0

    def test_does_nothing_for_an_account_with_no_archive(self, mock_client_cls):
        _archive_client(mock_client_cls, [])

        assert sync_user(self.user) == 0
        assert ArchiveImport.objects.count() == 0

    def test_a_failure_propagates_to_the_task(self, mock_client_cls):
        """`sync_user` works for one user, so there is nothing to protect the rest
        of a batch from: the task lets it surface in the worker log instead."""
        mock_client_cls.return_value.archive_months.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError):
            sync_user(self.user)


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.tasks.sync_user_task")
class TestRequestSync:
    """The claim that replaces the scheduler process.

    One conditional UPDATE, so N web replicas racing on the same user's Sync press
    produce exactly one import.
    """

    @pytest.fixture(autouse=True)
    def _users(self, django_user_model):
        self.user = django_user_model.objects.create_user(
            username="login_name", password="pw12345!", chessdotcom_username="MyUser"
        )
        self.unlinked = django_user_model.objects.create_user(
            username="no_account", password="pw12345!"
        )

    def test_claims_and_enqueues(self, mock_task):
        assert request_sync(self.user) is True

        mock_task.apply_async.assert_called_once()
        assert mock_task.apply_async.call_args.kwargs["args"] == [self.user.pk]
        self.user.refresh_from_db()
        assert self.user.last_synced_at is not None

    def test_a_second_call_within_the_cooldown_enqueues_nothing(self, mock_task):
        assert request_sync(self.user) is True
        mock_task.apply_async.reset_mock()

        assert request_sync(self.user) is False
        mock_task.apply_async.assert_not_called()

    def test_enqueues_again_once_the_cooldown_lapses(self, mock_task):
        request_sync(self.user)
        User.objects.filter(pk=self.user.pk).update(
            last_synced_at=timezone.now() - SYNC_COOLDOWN - timedelta(seconds=1)
        )
        mock_task.apply_async.reset_mock()

        assert request_sync(self.user) is True
        mock_task.apply_async.assert_called_once()

    def test_never_publishes_with_retries(self, mock_task):
        """`task_publish_retry` is on by default, and this runs in a request: a
        broker that is down must not hold the home page for seconds of backoff."""
        request_sync(self.user)

        assert mock_task.apply_async.call_args.kwargs["retry"] is False

    def test_an_unlinked_user_is_left_alone(self, mock_task):
        """The `chess_username` fallback orients the board; it is not a claim that
        the login name is a real Chess.com account."""
        assert request_sync(self.unlinked) is False

        mock_task.apply_async.assert_not_called()
        self.unlinked.refresh_from_db()
        assert self.unlinked.last_synced_at is None

    def test_a_broker_failure_does_not_raise_and_keeps_the_claim(self, mock_task):
        """One publish attempt per cooldown while Redis is down, not one per page
        load — and the home page still renders."""
        mock_task.apply_async.side_effect = RuntimeError("broker down")

        assert request_sync(self.user) is False

        self.user.refresh_from_db()
        assert self.user.last_synced_at is not None
        mock_task.apply_async.reset_mock()
        assert request_sync(self.user) is False
        mock_task.apply_async.assert_not_called()


@pytest.mark.django_db
class TestRecoverStuckAnalyses:
    """The throttle in front of the two sweeps, called from the Refresh button."""

    @pytest.fixture(autouse=True)
    def _user(self, django_user_model):
        self.user = django_user_model.objects.create_user(
            username="login_name", password="pw12345!"
        )

    @patch("chessdotcom_ai_coach.services.sync.requeue_orphaned_analyses")
    @patch("chessdotcom_ai_coach.services.sync.requeue_stale_analyses")
    def test_runs_both_sweeps_scoped_to_the_user(self, mock_stale, mock_orphaned):
        mock_stale.return_value = 1
        mock_orphaned.return_value = 2

        assert recover_stuck_analyses(self.user) == 3

        mock_stale.assert_called_once_with(user=self.user)
        mock_orphaned.assert_called_once_with(user=self.user)

    @patch("chessdotcom_ai_coach.services.sync.requeue_stale_analyses")
    def test_a_second_call_inside_the_interval_is_a_no_op(self, mock_stale):
        """A waiting user can press Refresh as fast as they like; each sweep
        reads the broker's queue depth, so they must not follow every press."""
        mock_stale.return_value = 0
        recover_stuck_analyses(self.user)
        mock_stale.reset_mock()

        assert recover_stuck_analyses(self.user) == 0
        mock_stale.assert_not_called()

    @patch("chessdotcom_ai_coach.services.sync.requeue_stale_analyses")
    def test_a_sweep_failure_never_reaches_the_page(self, mock_stale):
        """This sits in front of a fragment render: a broker outage must cost a
        page that shows what the database holds, not a 500."""
        mock_stale.side_effect = RuntimeError("broker down")

        with patch(
            "chessdotcom_ai_coach.services.sync._queued_task_count", return_value=None
        ):
            assert recover_stuck_analyses(self.user) == 0


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.sync.Client")
class TestImportAllArchives:
    """The bulk catch-up behind `manage.py import_archives`."""

    @pytest.fixture(autouse=True)
    def _linked(self, django_user_model):
        self.user = django_user_model.objects.create_user(
            username="login_name", password="pw12345!", chessdotcom_username="MyUser"
        )

    def test_reads_every_month(self, mock_client_cls):
        _archive_client(
            mock_client_cls,
            [(2023, 1), (2023, 2)],
            {(2023, 1): [_archive_game("a")], (2023, 2): [_archive_game("b")]},
        )

        assert import_all_archives(self.user) == 2
        assert ArchiveImport.objects.count() == 2

    def test_months_caps_to_the_most_recent(self, mock_client_cls):
        client = _archive_client(
            mock_client_cls, [(2023, 1), (2023, 2), (2023, 3)]
        )

        import_all_archives(self.user, months=2)

        read = {call.args for call in client.finished_games.call_args_list}
        assert read == {(2023, 2), (2023, 3)}

    def test_is_idempotent(self, mock_client_cls):
        _archive_client(
            mock_client_cls, [(2023, 1)], {(2023, 1): [_archive_game("a")]}
        )

        assert import_all_archives(self.user) == 1
        assert import_all_archives(self.user) == 0  # updated in place, not added
        assert Game.objects.count() == 1


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.sync.analyze_game_task")
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

    def test_scopes_to_one_user(self, mock_task, user, django_user_model):
        """The request path passes the user making the request: a page load should
        unstick the card they are looking at, not do the deployment's housekeeping."""
        other = django_user_model.objects.create_user(
            username="someone_else", password="pw12345!"
        )
        _game(user)
        mine = self._running(user, ANALYSIS_TIMEOUT + timedelta(minutes=1))
        theirs = self._running(other, ANALYSIS_TIMEOUT + timedelta(minutes=1))

        assert requeue_stale_analyses(user=user) == 1

        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert mine.status == CoachSuggestion.Status.PENDING
        assert theirs.status == CoachSuggestion.Status.RUNNING

    def test_survives_a_row_whose_game_is_gone(self, mock_task, user):
        # No `Game` row: the suggestion is decoupled from Game by design.
        self._running(user, ANALYSIS_TIMEOUT + timedelta(minutes=1), attempts=1)

        assert requeue_stale_analyses() == 1
        mock_task.delay.assert_called_once_with(
            user.id, "944768131", WHITE_TO_MOVE, None
        )


@pytest.mark.django_db
@patch("chessdotcom_ai_coach.services.sync._queued_task_count")
@patch("chessdotcom_ai_coach.services.sync.analyze_game_task")
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

    def test_scopes_the_rows_but_not_the_preconditions(
        self, mock_task, mock_depth, user, django_user_model
    ):
        """Which rows are revived is per-user; "is anything running?" and "is the
        queue empty?" are facts about the whole deployment, so they stay global."""
        other = django_user_model.objects.create_user(
            username="someone_else", password="pw12345!"
        )
        _game(user)
        mine = self._pending(user)
        theirs = self._pending(other)
        mock_depth.return_value = 0

        assert requeue_orphaned_analyses(user=user) == 1

        assert mock_task.delay.call_args[0][0] == user.id
        before = theirs.updated_at
        theirs.refresh_from_db()
        assert theirs.updated_at == before  # untouched
        assert mine.pk is not None

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
