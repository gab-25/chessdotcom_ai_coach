"""Enqueue coach analysis for a whole game.

Analysis is keyed on the moves the user actually played, read from the stored
PGN, so a game can be reviewed with the coach's take on *every* one of them. A
position the user never played is not analysed — there is nothing in the PGN to
analyse it against.

It is written to be run repeatedly rather than once: enqueuing is idempotent, so
the same call reconciles a game towards "every user move analysed" no matter how
much of it is already done. That is what
``scheduler.enqueue_finished_game_analyses`` uses it for on every finished game
each 10 minutes, alongside the manual ``analyze_game`` management command. Reads
the stored ``Game`` snapshot only — no Chess.com call.
"""

from __future__ import annotations

from ..models import CoachSuggestion
from ..tasks import analyze_game_task
from . import board as board_utils
from . import game_store


def _user_orientation(user, game) -> str:
    return "white" if (game.white_name or "").lower() == user.chess_username.lower() else "black"


def _covered_plies(user, game_id: str) -> set[tuple[int, str]]:
    """The plies already carrying a row, as ``{(move_no, side to move)}``.

    The unique key is the raw FEN, but the same ply can be stored under two
    spellings: the live scheduler saves Chess.com's FEN while this module saves
    python-chess's ``board.fen()``, and the two can differ in the halfmove clock
    or the en-passant field. Matching on ``(move_no, side to move)`` — the ply
    identity ``board_utils.annotate_moves`` already joins on — keeps a rescan
    from re-analysing every move the coach handled live.

    Built in one query for the whole game: this runs on every active game each
    5s tick and on every finished game every 10 minutes, so a per-move lookup
    would be the dominant cost of both.
    """
    covered: set[tuple[int, str]] = set()
    rows = CoachSuggestion.objects.filter(user=user, game_id=game_id).only(
        "fen", "move_no"
    )
    for row in rows:
        move_no = row.move_no or board_utils.fullmove_number(row.fen)
        if move_no is None:
            continue  # no ply identity to match on — the FEN key still guards it
        covered.add((move_no, board_utils.active_color(row.fen)))
    return covered


def enqueue_game_analysis(user, game_id: str, limit: int | None = None):
    """Queue analysis for every un-analysed move the user played in ``game_id``.

    For each of the user's moves we enqueue the same Celery task the live coach
    uses, keyed by the position the user was about to play (``fen_before``). It is
    idempotent on two levels: ``_covered_plies`` skips a move already analysed
    under a different FEN spelling, and ``get_or_create`` on
    ``(user, game_id, fen)`` leaves an existing row alone — so re-running is safe
    and cheap, which is what lets the scheduler use this as a reconciliation pass.
    ``limit`` caps how many tasks a single call may enqueue, so a caller sweeping
    many games can spread a large backlog over several runs. Returns
    ``{"enqueued", "total", "game"}`` or ``None`` when the game isn't stored for
    the user.
    """
    game = game_store.stored_game(user, game_id)
    if game is None:
        return None

    orientation = _user_orientation(user, game)
    user_moves = [m for m in board_utils.moves_from_pgn(game.pgn) if m["color"] == orientation]
    covered = _covered_plies(user, game_id)

    enqueued = 0
    for move in user_moves:
        if limit is not None and enqueued >= limit:
            break
        fen = move["fen_before"]
        move_no = board_utils.fullmove_number(fen)
        if (move_no, board_utils.active_color(fen)) in covered:
            continue
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
        if created:
            analyze_game_task.delay(user.id, game_id, fen, game.pgn or None)
            enqueued += 1

    return {"enqueued": enqueued, "total": len(user_moves), "game": game}
