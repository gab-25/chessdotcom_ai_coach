from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    Application user. Extends Django's built-in user (username, password hashing,
    sessions, admin) with the linked Chess.com account name.
    """

    chessdotcom_username = models.CharField(max_length=255, blank=True, null=True)

    @property
    def chess_username(self) -> str:
        """The Chess.com username to query, falling back to the app username."""
        return self.chessdotcom_username or self.username


class Game(models.Model):
    """Persisted snapshot of a Chess.com game; the PGN is the source of the moves.

    Chess.com only exposes games that are still "current", so once a game ends it
    disappears from the API. Snapshotting it here (from the home polling) keeps the
    game — and its move list — browsable afterwards.
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
    pgn = models.TextField(blank=True)  # snapshot: the source of the move history
    fen = models.CharField(max_length=100, blank=True)
    is_active = models.BooleanField(default=True)  # seen in the latest "current" fetch

    class Result(models.TextChoices):
        WIN = "win", "Win"
        LOSS = "loss", "Loss"
        DRAW = "draw", "Draw"
        UNKNOWN = "unknown", "Unknown"

    # Outcome relative to this row's user. Snapshots taken while the game is still
    # "current" carry a PGN with Result "*", so the result is not known from the
    # snapshot itself — the scheduler backfills it from the Chess.com monthly
    # archives once the game ends (see services.scheduler.backfill_results).
    result = models.CharField(
        max_length=8, choices=Result.choices, default=Result.UNKNOWN
    )
    result_detail = models.CharField(
        max_length=32, blank=True
    )  # how it ended: "checkmate", "resignation", "timeout", "agreement", …
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
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


class CoachSuggestion(models.Model):
    """The coach's analysis for one position: at most ONE per (user, game_id, fen).

    The FEN identifies the analysed position (the move-to-play), so re-analysing the
    same position overwrites the existing row — every move keeps a single, latest
    analysis rather than an ever-growing pile of duplicates.
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
        PENDING = "pending", "Pending"  # enqueued, analysis in flight
        DONE = "done", "Done"  # analysis computed and persisted

    # The row doubles as the in-flight lock: the scheduler creates it PENDING (via
    # get_or_create on the unique key) and only enqueues when it was just created,
    # so a position under analysis is not re-enqueued on every 1s poll tick.
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )
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
