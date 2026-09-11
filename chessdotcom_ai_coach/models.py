from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    Application user. Extends Django's built-in user (username, password hashing,
    sessions, admin) with the linked Chess.com account name.
    """

    chessdotcom_username = models.CharField(max_length=255, blank=True, null=True)

    # When an archive sync was last *claimed* — not when one completed. There is
    # no scheduler: a sync is started by the request the Refresh button makes, and
    # this column is the claim that keeps every web replica from starting the same
    # one.
    # `services.sync.request_sync` stamps it with a conditional UPDATE (atomic in
    # Postgres and in the SQLite the tests use), and the import loop re-stamps it
    # after each month so a long backfill holds the claim for its whole run
    # instead of letting a second one start on top of it.
    last_synced_at = models.DateTimeField(null=True, blank=True)

    @property
    def chess_username(self) -> str:
        """The Chess.com username to query, falling back to the app username."""
        return self.chessdotcom_username or self.username


class Game(models.Model):
    """Persisted snapshot of a Chess.com game; the PGN is the source of the moves.

    Every row is a finished game, mirrored from the **monthly archives** with its
    final PGN and result. Games in progress are not tracked at all: they cannot be
    reviewed, and the archive carries the finished game complete, so there is
    nothing a snapshot would add.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="games"
    )
    game_id = models.CharField(max_length=64)  # last segment of the Chess.com URL
    url = models.URLField(blank=True)
    white_name = models.CharField(max_length=255, blank=True)
    black_name = models.CharField(max_length=255, blank=True)
    white_rating = models.CharField(max_length=16, blank=True)
    black_rating = models.CharField(max_length=16, blank=True)
    time_class = models.CharField(max_length=32, blank=True)
    pgn = models.TextField(blank=True)  # the source of the move history
    fen = models.CharField(max_length=100, blank=True)  # final position
    # When the game ended, from the archive. Indexed because it orders the home
    # page: an imported archive arrives in bulk, so `updated_at` says when we
    # fetched a game, never when it was played. Null for a game we have only ever
    # seen as "current" (still in progress) and for rows predating the import.
    end_time = models.DateTimeField(null=True, blank=True, db_index=True)
    # Vestigial: every imported game is written False, because the archive only
    # holds finished games. It survives for the rows left True by older versions,
    # which snapshotted games while they were still being played — those close
    # themselves out the next time the archive covers them. The read paths
    # (`game_store.past_games`, `views._reviewable_game`) still honour it, so such
    # a row is never shown half-finished in the meantime.
    is_active = models.BooleanField(default=True)

    class Result(models.TextChoices):
        WIN = "win", "Win"
        LOSS = "loss", "Loss"
        DRAW = "draw", "Draw"
        UNKNOWN = "unknown", "Unknown"

    # Outcome relative to this row's user, straight from the archive. Stays
    # UNKNOWN only for a row that never came from there — one left by an older
    # version of the app — and resolves itself the next time the archive covers
    # that game.
    result = models.CharField(
        max_length=8, choices=Result.choices, default=Result.UNKNOWN
    )
    result_detail = models.CharField(
        max_length=32, blank=True
    )  # how it ended: "checkmate", "resignation", "timeout", "agreement", …
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Newest game first. `-updated_at` breaks the tie so rows without an
        # end_time (games in progress, pre-import rows) stay in a stable order
        # instead of drifting between queries.
        ordering = ["-end_time", "-updated_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "game_id"], name="uniq_user_game")
        ]

    @property
    def has_result(self) -> bool:
        """True once the outcome has been resolved from the archives."""
        return self.result != self.Result.UNKNOWN

    @property
    def result_label(self) -> str:
        """Human label for the outcome ("Win"/"Loss"/"Draw"), "" when unknown."""
        return self.get_result_display() if self.has_result else ""

    def __str__(self) -> str:
        return f"{self.white_name} vs {self.black_name} ({self.game_id})"


class ArchiveImport(models.Model):
    """One month of a user's Chess.com archive, already read.

    A row here means "this month has been read". That is the whole of the import
    schedule: `services.sync._due_months` asks for every month the archive offers
    that has no row, so a user's first sync reads the lot and later ones read
    nothing — and a backfill cut short by a restart or a rate limit resumes at
    exactly the months it never got to, rather than starting over or, worse,
    treating the partial history as complete.

    The current month is the exception: it keeps growing as the user plays, so it
    is re-read on every sync and this row is refreshed rather than treated as
    done. `game_count` is what was seen on the last read, kept for the log and
    for telling "no games that month" apart from "never looked".
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="archive_imports"
    )
    year = models.PositiveSmallIntegerField()
    month = models.PositiveSmallIntegerField()
    game_count = models.PositiveIntegerField(default=0)
    imported_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-year", "-month"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "year", "month"], name="uniq_user_archive_month"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id} {self.year}-{self.month:02d} ({self.game_count} games)"


# How many times a worker may start on the same position before it is retired as
# FAILED. Lives here rather than in `services.sync` because both the worker
# (`tasks.analyze_game_task`, which counts the attempts) and the recovery sweep
# (which retries the stuck ones) need it, and the worker cannot import `sync`.
MAX_ANALYSIS_ATTEMPTS = 3


class CoachSuggestion(models.Model):
    """The coach's analysis for one position: at most ONE per (user, game_id, fen).

    The FEN identifies the analysed position — the one the player faced *before*
    the move being reviewed — so re-analysing the same position overwrites the
    existing row, and every move keeps a single, latest analysis rather than an
    ever-growing pile of duplicates.

    Rows are only ever created for moves the user actually played, and only when
    someone asks (`services.analysis.enqueue_game_analysis`, driven by the
    "Analyse this game" button) — nothing creates them in the background. Older
    rows written for a position that was never played still exist; they are
    simply never rendered, because the templates join suggestions onto the plies
    in the PGN.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="coach_suggestions",
    )
    game_id = models.CharField(max_length=64)  # decoupled: no FK to Game required
    fen = models.CharField(max_length=100)  # analysed position = the move's join key
    move_no = models.PositiveIntegerField(null=True, blank=True)

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"  # queued on the broker, no worker yet
        RUNNING = "running", "Running"  # a worker picked it up and is analysing
        DONE = "done", "Done"  # analysis computed and persisted
        FAILED = "failed", "Failed"  # gave up after MAX_ANALYSIS_ATTEMPTS

    # The row doubles as the in-flight lock: the enqueue path creates it PENDING
    # (via get_or_create on the unique key) and only enqueues when it was just
    # created, so a position under analysis is not re-enqueued on the next scan.
    #
    # PENDING and RUNNING are kept apart because they fail differently. A PENDING
    # row is just waiting its turn on the broker — a whole-game backfill can leave
    # it queued for far longer than an analysis takes — and is recovered by Celery
    # redelivering the message (`task_acks_late`). A RUNNING row has a worker on
    # it, so a single analysis bounds how long it may legitimately take, and
    # anything past `services.sync.ANALYSIS_TIMEOUT` is genuinely stuck.
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
    # How many times a worker has actually started analysing this position —
    # incremented by `tasks.analyze_game_task` as it claims the row, not by
    # whoever enqueued it, so a task sitting in a long queue never spends an
    # attempt. Bounds the retrying so a position that fails every time (broken
    # FEN, engine that won't start) is retired instead of looping for ever.
    attempts = models.PositiveSmallIntegerField(default=0)
    eval_text = models.CharField(max_length=255, blank=True)
    eval_cp = models.FloatField(null=True, blank=True)
    best_move_san = models.CharField(max_length=16, blank=True, null=True)
    best_move_uci = models.CharField(max_length=10, blank=True, null=True)
    analysis = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)  # first analysis
    updated_at = models.DateTimeField(auto_now=True)  # last re-analysis (overwrite)

    class Meta:
        ordering = ["move_no", "-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "game_id", "fen"], name="uniq_user_game_fen"
            )
        ]

    def __str__(self) -> str:
        return f"{self.game_id} @ move {self.move_no}: {self.best_move_san or '—'}"
