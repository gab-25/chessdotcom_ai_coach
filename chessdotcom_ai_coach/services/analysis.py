"""Enqueue coach analysis for a whole game.

Analysis is keyed on the moves the user actually played, read from the stored
PGN, so a game can be reviewed with the coach's take on *every* one of them. A
position the user never played is not analysed — there is nothing in the PGN to
analyse it against.

It is written to be run repeatedly rather than once: enqueuing is idempotent, so
the same call reconciles a game towards "every user move analysed" no matter how
much of it is already done. That is what lets ``views.analyze_game`` sit behind a
button nobody has to press carefully — a second press queues nothing — and what
makes the ``analyze_game`` management command safe to re-run. Reads the stored
``Game`` row only, never Chess.com.

Idempotence is also what makes a *re-analysis* need saying explicitly: ``force``
queues every move again and overwrites the analyses already on record, which is
the only way to get a second opinion out of a game the coach has finished with.
"""

from __future__ import annotations

from ..models import CoachSuggestion
from ..tasks import analyze_game_task
from . import board as board_utils
from . import game_store


def _user_orientation(user, game) -> str:
    return "white" if (game.white_name or "").lower() == user.chess_username.lower() else "black"


def _rows_by_ply(user, game_id: str) -> dict[tuple[int, str], CoachSuggestion]:
    """The plies already carrying a row, keyed by ``(move_no, side to move)``.

    The unique key is the raw FEN, but the same ply can be stored under two
    spellings: this module saves python-chess's ``board.fen()``, while rows left
    over from the app's earlier live path hold Chess.com's spelling of the same
    position, and the two can differ in the halfmove clock or the en-passant
    field. Matching on ``(move_no, side to move)`` — the ply identity
    ``board_utils.annotate_moves`` already joins on — stops a second request
    re-analysing a move that already has a row, and gives a forced re-analysis
    the row to reset instead of a duplicate to create.

    Built in one query for the whole game rather than one per move.
    """
    by_ply: dict[tuple[int, str], CoachSuggestion] = {}
    rows = CoachSuggestion.objects.filter(user=user, game_id=game_id)
    for row in rows:
        move_no = row.move_no or board_utils.fullmove_number(row.fen)
        if move_no is None:
            continue  # no ply identity to match on — the FEN key still guards it
        by_ply[(move_no, board_utils.active_color(row.fen))] = row
    return by_ply


def reset_for_reanalysis(row: CoachSuggestion) -> CoachSuggestion:
    """Put a suggestion row back to PENDING, clearing the analysis it carries.

    Also resets ``attempts``, for two reasons: an explicit request must not be
    spent by earlier failures, and it is the signal to break an in-flight lock
    that ``services.sync.ANALYSIS_TIMEOUT`` has not expired yet. The cost of the
    latter is bounded — a worker still running on the old row finishes and writes
    a result the new run then overwrites.

    Shared with ``views.analyze_position``, whose per-move retry resets exactly
    the same fields.
    """
    row.status = CoachSuggestion.Status.PENDING
    row.attempts = 0
    row.eval_text = ""
    row.eval_cp = None
    row.best_move_san = None
    row.best_move_uci = None
    row.analysis = ""
    row.save()
    return row


def enqueue_game_analysis(user, game_id: str, force: bool = False):
    """Queue analysis for the moves the user played in ``game_id``.

    One Celery task per move, keyed by the position the user was about to play
    (``fen_before``). By default only un-analysed moves are queued, on two levels:
    ``_rows_by_ply`` skips a move already analysed under a different FEN spelling,
    and ``get_or_create`` on ``(user, game_id, fen)`` leaves an existing row
    alone. That is what makes the button and the management command safe to press
    twice.

    ``force=True`` is the "Re-analyse this game" path: every move is queued again,
    existing rows reset in place by ``reset_for_reanalysis``. They are reset and
    re-queued under **their own FEN**, not ``fen_before``, because a row written
    by the app's earlier live path may hold Chess.com's spelling of the position;
    creating a second row for the same ply would leave two rows competing for one
    slot in ``board_utils.annotate_moves``, which joins on ``(move_no, colour)``.

    Returns ``{"enqueued", "total", "game"}`` — the counts the caller shows as
    progress — or ``None`` when the game isn't stored for the user.
    """
    game = game_store.stored_game(user, game_id)
    if game is None:
        return None

    orientation = _user_orientation(user, game)
    user_moves = [m for m in board_utils.moves_from_pgn(game.pgn) if m["color"] == orientation]
    existing_rows = _rows_by_ply(user, game_id)

    enqueued = 0
    for move in user_moves:
        fen = move["fen_before"]
        move_no = board_utils.fullmove_number(fen)
        row = existing_rows.get((move_no, board_utils.active_color(fen)))

        if row is not None:
            if not force:
                continue
            reset_for_reanalysis(row)
            fen = row.fen  # the spelling the row is keyed on, so the worker claims it
        else:
            _row, created = CoachSuggestion.objects.get_or_create(
                user=user,
                game_id=game_id,
                fen=fen,
                defaults={
                    "status": CoachSuggestion.Status.PENDING,
                    "move_no": move_no,
                    "eval_text": "",
                    "analysis": "",
                },
            )
            if not created:
                continue  # raced with another request; it owns the enqueue

        analyze_game_task.delay(user.id, game_id, fen, game.pgn or None)
        enqueued += 1

    return {"enqueued": enqueued, "total": len(user_moves), "game": game}
