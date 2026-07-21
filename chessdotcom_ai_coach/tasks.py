import logging
import os

from asgiref.sync import async_to_sync
from celery import shared_task
from django.db.models import Exists, OuterRef
from django.utils import timezone

from .models import CoachSuggestion, Game
from .services import board as board_utils
from .services.coach import get_best_move

logger = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, str(default))
    try:
        return max(1, int(value))
    except ValueError:
        return default


def _is_user_turn(game: Game) -> bool:
    orientation = (
        "white"
        if (game.white_name or "").lower() == game.user.chess_username.lower()
        else "black"
    )
    return board_utils.active_color(game.fen) == orientation


def _eligible_games(batch_size: int):
    analyzed = CoachSuggestion.objects.filter(
        user_id=OuterRef("user_id"),
        game_id=OuterRef("game_id"),
        fen=OuterRef("fen"),
    )
    return (
        Game.objects.select_related("user")
        .filter(is_active=True)
        .exclude(fen="")
        .annotate(has_analysis=Exists(analyzed))
        .filter(has_analysis=False)
        .order_by("-updated_at")[:batch_size]
    )


@shared_task
def schedule_active_game_analyses():
    batch_size = _int_env("ANALYSIS_SCHEDULER_BATCH_SIZE", 10)
    timeout_seconds = _int_env("ANALYSIS_TASK_TIMEOUT_SECONDS", 120)
    now = timezone.now()
    scanned = 0
    enqueued = 0
    failures = 0

    for game in _eligible_games(batch_size):
        scanned += 1
        if not _is_user_turn(game):
            continue

        claimed = (
            Game.objects.filter(
                pk=game.pk,
                is_active=True,
                fen=game.fen,
            )
            .exclude(analysis_enqueued_fen=game.fen)
            .update(
                analysis_enqueued_fen=game.fen,
                analysis_enqueued_at=now,
            )
        )
        if claimed != 1:
            continue

        try:
            analyze_game_position.apply_async(
                kwargs={"game_pk": game.pk, "fen": game.fen},
                expires=timeout_seconds,
            )
            enqueued += 1
            logger.info(
                "analysis_enqueued",
                extra={
                    "game_pk": game.pk,
                    "user_id": game.user_id,
                    "game_id": game.game_id,
                    "fen": game.fen,
                },
            )
        except Exception:
            failures += 1
            Game.objects.filter(pk=game.pk, analysis_enqueued_fen=game.fen).update(
                analysis_enqueued_fen=""
            )
            logger.exception(
                "analysis_enqueue_failed",
                extra={
                    "game_pk": game.pk,
                    "user_id": game.user_id,
                    "game_id": game.game_id,
                },
            )

    logger.info(
        "analysis_scheduler_tick",
        extra={"jobs_run": 1, "games_scanned": scanned, "analyses_started": enqueued, "failures": failures},
    )
    return {"jobs_run": 1, "games_scanned": scanned, "analyses_started": enqueued, "failures": failures}


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
    acks_late=True,
)
def analyze_game_position(self, game_pk: int, fen: str):
    game = Game.objects.select_related("user").get(pk=game_pk)

    if not game.is_active or not fen or game.fen != fen or game.analysis_enqueued_fen != fen:
        return "skipped"

    if CoachSuggestion.objects.filter(user=game.user, game_id=game.game_id, fen=fen).exists():
        return "already_analyzed"

    started_at = timezone.now()
    updated = Game.objects.filter(pk=game.pk, analysis_enqueued_fen=fen).update(
        analysis_started_at=started_at
    )
    if updated != 1:
        return "superseded"

    logger.info(
        "analysis_started",
        extra={"game_pk": game.pk, "user_id": game.user_id, "game_id": game.game_id, "fen": fen},
    )

    suggestion = async_to_sync(get_best_move)(fen, game.pgn)

    CoachSuggestion.objects.update_or_create(
        user=game.user,
        game_id=game.game_id,
        fen=fen,
        defaults={
            "move_no": board_utils.fullmove_number(fen),
            "eval_text": suggestion["eval_text"],
            "eval_cp": suggestion["eval_cp"],
            "best_move_san": suggestion["best_move_san"],
            "best_move_uci": suggestion["best_move_uci"],
            "analysis": suggestion["analysis"],
        },
    )
    logger.info(
        "analysis_completed",
        extra={"game_pk": game.pk, "user_id": game.user_id, "game_id": game.game_id, "fen": fen},
    )
    return "ok"
