"""Auto-analysis of active games.

One "tick" of the background worker: scan the users who have active games,
re-fetch their current games from Chess.com, and start the coach analysis for
every position where it is the user's turn and no analysis exists yet.

Kept as a plain, synchronous service (no Celery/queue — the project has none)
so it is trivially testable and driven by the ``analyze_active_games``
management command loop. The analysis itself (``get_best_move``) is async, so
each call is run to completion via ``asyncio.run``; positions are processed
sequentially to stay resource-aware (Stockfish + a local LLM on a small node).
"""

from __future__ import annotations

import asyncio
import logging

from django.contrib.auth import get_user_model
from django.db import IntegrityError

from ..models import CoachSuggestion
from . import board as board_utils
from . import game_store
from .chess_client import Client
from .coach import get_best_move

logger = logging.getLogger(__name__)


def _active_game_users():
    """Users with at least one active game — the only ones worth polling."""
    User = get_user_model()
    return User.objects.filter(games__is_active=True).distinct()


def _already_analyzed(user, game_id: str, fen: str) -> bool:
    """True when this exact position already has a stored coach analysis.

    The ``(user, game_id, fen)`` uniqueness of ``CoachSuggestion`` is what makes
    auto-analysis "start at most once" per position — we never overwrite here
    (the manual "Re-analyze" button stays the only way to refresh a position).
    """
    return CoachSuggestion.objects.filter(
        user=user, game_id=game_id, fen=fen
    ).exists()


def _analyze_game(user, game: dict) -> bool:
    """Analyze one eligible game position and persist it. Returns True if started.

    Skips games that are not the user's turn, have no FEN, or are already
    analyzed. Any failure is logged and swallowed so a single bad game never
    aborts the surrounding tick.
    """
    game_id = game.get("game_id")
    fen = game.get("fen")
    if not game_id or not fen:
        return False
    if not game.get("is_my_turn"):
        return False
    if _already_analyzed(user, game_id, fen):
        return False

    try:
        suggestion = asyncio.run(get_best_move(fen, game.get("pgn")))
        # create() (not update_or_create): auto-analysis only ever adds. Under a
        # scaled-out worker two ticks could race here; the DB unique constraint
        # turns the loser into an IntegrityError, which we treat as "already done".
        CoachSuggestion.objects.create(
            user=user,
            game_id=game_id,
            fen=fen,
            move_no=board_utils.fullmove_number(fen),
            eval_text=suggestion["eval_text"],
            eval_cp=suggestion["eval_cp"],
            best_move_san=suggestion["best_move_san"],
            best_move_uci=suggestion["best_move_uci"],
            analysis=suggestion["analysis"],
        )
    except IntegrityError:
        logger.info(
            "auto-analyze: position already analyzed concurrently "
            "(user=%s game=%s)",
            user.pk,
            game_id,
        )
        return False
    except Exception:
        logger.exception(
            "auto-analyze: analysis failed (user=%s game=%s)", user.pk, game_id
        )
        return False

    logger.info(
        "auto-analyze: started analysis (user=%s game=%s move=%s best=%s)",
        user.pk,
        game_id,
        board_utils.fullmove_number(fen),
        suggestion["best_move_san"],
    )
    return True


def run_once(max_per_tick: int | None = None) -> int:
    """Run a single scan/analysis pass. Returns the number of analyses started.

    ``max_per_tick`` caps how many analyses may be started in one pass to keep
    the worker resource-aware; ``None`` means no cap.
    """
    started = 0
    for user in _active_game_users():
        if max_per_tick is not None and started >= max_per_tick:
            break
        try:
            games = Client(username=user.chess_username).my_current_games()
        except Exception:
            # Transient Chess.com error: skip this user for this tick.
            logger.exception("auto-analyze: fetch failed (user=%s)", user.pk)
            continue

        # Keep the game-history snapshot fresh, exactly like the home polling.
        try:
            game_store.upsert_current_games(user, games)
        except Exception:
            logger.exception("auto-analyze: upsert failed (user=%s)", user.pk)

        for game in games:
            if max_per_tick is not None and started >= max_per_tick:
                break
            if _analyze_game(user, game):
                started += 1

    if started:
        logger.info("auto-analyze: tick started %d analysis(es)", started)
    return started
