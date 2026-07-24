from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render

from .models import CoachSuggestion
from .services import board as board_utils
from .services import game_store
from .tasks import analyze_game_task

_START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def _decorate_games(games, username):
    """Attach board cells, move number, side-to-move and turn ownership to `Game` rows.

    Used for both the current and past-games sections of the home page (both
    plain DB reads). `turn` reproduces the side-to-move from the stored FEN, and
    `is_user_turn` highlights games awaiting the logged-in user's move.
    """
    for game in games:
        game.cells = board_utils.fen_to_cells(game.fen)
        game.move_no = board_utils.fullmove_number(game.fen)
        game.turn = board_utils.active_color(game.fen)
        orientation = "white" if game.white_name.lower() == username.lower() else "black"
        game.is_user_turn = game.turn == orientation
    return games


def _uci_to_squares(uci):
    """Turn a UCI move like ``"d4f5"`` into its from/to square names."""
    if uci and len(uci) >= 4:
        return [uci[0:2], uci[2:4]]
    return []


def _eval_fill(eval_cp):
    """Map a White-POV centipawn eval to the eval bar's white fill percentage."""
    if eval_cp is None:
        return 50
    pct = 50 + eval_cp * 7
    return max(7, min(93, round(pct)))


def _sq_center(sq, flipped):
    """Square centre as an ``(x%, y%)`` string pair for the SVG arrow overlay."""
    col = ord(sq[0]) - 97
    row = 8 - int(sq[1])
    if flipped:
        col, row = 7 - col, 7 - row
    return f"{(col + 0.5) / 8 * 100:.2f}", f"{(row + 0.5) / 8 * 100:.2f}"


def _arrow(from_sq, to_sq, color, marker, flipped):
    x1, y1 = _sq_center(from_sq, flipped)
    x2, y2 = _sq_center(to_sq, flipped)
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "color": color, "marker": marker}


def _suggestion_fields(row):
    """The coach's move/eval/prose/arrow squares from a DONE suggestion row."""
    rec = _uci_to_squares(row.best_move_uci)
    return {
        "rec_san": row.best_move_san or "",
        "rec_eval": row.eval_text or "",
        "prose": row.analysis or "",
        "rec_from": rec[0] if rec else "",
        "rec_to": rec[1] if rec else "",
        "fill": _eval_fill(row.eval_cp),
    }


def _position_context(user, game, sel):
    """Everything the position fragment needs to render one ply of a game.

    Reads entirely from the stored ``Game`` snapshot (kept fresh by the
    scheduler) and the persisted ``CoachSuggestion`` rows — no Chess.com call —
    so navigation, the live poll and the review of a finished game are all cheap
    DB reads. ``sel`` is the 0-based ply cursor (0 = starting position, ``head``
    = the latest/current position).
    """
    username = user.chess_username
    orientation = "white" if (game.white_name or "").lower() == username.lower() else "black"
    flipped = orientation == "black"

    pgn = game.pgn
    moves = board_utils.moves_from_pgn(pgn)
    positions = board_utils.positions_from_pgn(pgn) or [game.fen or _START_FEN]
    history = list(CoachSuggestion.objects.filter(user=user, game_id=game.game_id))
    board_utils.annotate_moves(moves, history)
    by_fen = {row.fen: row for row in history}

    head = len(moves)
    sel = max(0, min(head, sel))
    is_live = game.is_active
    ply = moves[sel - 1] if sel > 0 else None

    # Board + last-move highlight for the selected ply.
    board_fen = positions[sel] if sel < len(positions) else (game.fen or _START_FEN)
    highlight = _uci_to_squares(ply["uci"]) if ply else []
    cells = board_utils.fen_to_cells(board_fen, highlight=highlight, flipped=flipped)

    # Eval bar: carry the last analysed value forward across un-analysed plies.
    eval_fill = 50
    for i in range(1, sel + 1):
        m = moves[i - 1]
        s = m["suggestion"]
        if m["color"] == orientation and s is not None and s.status == CoachSuggestion.Status.DONE:
            eval_fill = _eval_fill(s.eval_cp)

    at_live_head = is_live and sel == head
    head_row = by_fen.get(game.fen)
    user_to_move = board_utils.active_color(game.fen) == orientation if game.fen else False

    coach = {"mode": "start"}
    arrows = []

    if at_live_head:
        # The position you're about to play (or waiting on the opponent).
        if not user_to_move:
            coach = {"mode": "live_waiting"}
        elif head_row is None:
            coach = {"mode": "live_request", "fen": game.fen}
        elif head_row.status == CoachSuggestion.Status.PENDING:
            coach = {"mode": "live_pending"}
        else:
            fields = _suggestion_fields(head_row)
            coach = {"mode": "live_analyzed", **fields}
            eval_fill = fields["fill"]
            if fields["rec_from"] and fields["rec_to"]:
                arrows.append(_arrow(fields["rec_from"], fields["rec_to"], "#b78e54", "url(#gr-ah-brass)", flipped))
    elif ply is None:
        coach = {"mode": "start"}
    elif ply["color"] != orientation:
        coach = {"mode": "opponent", "san": ply["san"]}
    else:
        s = ply["suggestion"]
        if s is None:
            coach = {"mode": "unanalyzed", "san": ply["san"], "fen": ply["fen_before"]}
        elif s.status == CoachSuggestion.Status.PENDING:
            coach = {"mode": "pending", "san": ply["san"]}
        else:
            fields = _suggestion_fields(s)
            followed = ply["followed"]
            coach = {
                "mode": "analyzed",
                "played_san": ply["san"],
                "played_eval": fields["rec_eval"] if followed else "",
                "followed": followed,
                **fields,
            }
            played_from = highlight[0] if highlight else ""
            played_to = highlight[1] if len(highlight) > 1 else ""
            rec_from = fields["rec_from"] or played_from
            rec_to = fields["rec_to"] or played_to
            if not followed and played_from and played_to:
                arrows.append(_arrow(played_from, played_to, "#4a7a52", "url(#gr-ah-green)", flipped))
            if rec_from and rec_to:
                arrows.append(_arrow(rec_from, rec_to, "#b78e54", "url(#gr-ah-brass)", flipped))

    # Moves grid.
    moves_view = []
    for i, m in enumerate(moves, start=1):
        s = m["suggestion"]
        done = m["color"] == orientation and s is not None and s.status == CoachSuggestion.Status.DONE
        pending = m["color"] == orientation and s is not None and s.status == CoachSuggestion.Status.PENDING
        moves_view.append(
            {
                "sel": i,
                "no": m["move_no"],
                "color": m["color"],
                "san": m["san"],
                "selected": i == sel,
                "analyzed": done,
                "pending": pending,
                "followed": done and m["followed"],
                "rec_san": (s.best_move_san if done else "") or "",
            }
        )

    # Analysis-history timeline (analysed user moves, in order).
    history_view = []
    for i, m in enumerate(moves, start=1):
        s = m["suggestion"]
        if m["color"] != orientation or s is None or s.status != CoachSuggestion.Status.DONE:
            continue
        history_view.append(
            {
                "sel": i,
                "no": m["move_no"],
                "rec_san": s.best_move_san or "",
                "rec_eval": s.eval_text or "",
                "prose": s.analysis or "",
                "followed": m["followed"],
                "selected": i == sel,
            }
        )

    last_move = None
    sel_text = "Starting position"
    if at_live_head:
        sel_text = "Live · your move" if user_to_move else "Live · opponent to move"
    elif ply is not None:
        ref = f"{ply['move_no']}{'. ' if ply['color'] == 'white' else '… '}{ply['san']}"
        last_move = ref
        sel_text = f"Reviewing: {ref}"

    if ply is not None:
        move_label = f"Move {ply['move_no']} · {'White' if ply['color'] == 'white' else 'Black'}"
    elif at_live_head:
        move_label = "Live position"
    else:
        move_label = ""

    return {
        "id": game.game_id,
        "is_live": is_live,
        "sel": sel,
        "head": head,
        "behind": head - sel,
        "prev_sel": max(0, sel - 1),
        "next_sel": min(head, sel + 1),
        "orientation": orientation,
        "flipped": flipped,
        "white_name": game.white_name or "White",
        "black_name": game.black_name or "Black",
        "white_rating": game.white_rating or None,
        "black_rating": game.black_rating or None,
        "result": game.result,
        "result_label": game.result_label,
        "result_detail": game.result_detail,
        "time_class": game.time_class,
        "cells": cells,
        "eval_fill": eval_fill,
        "arrows": arrows,
        "coach": coach,
        "moves": moves_view,
        "history": history_view,
        "history_count": len(history_view),
        "last_move": last_move,
        "sel_text": sel_text,
        "move_label": move_label,
        "at_live_head": at_live_head,
    }


@login_required
def home(request):
    """Home page: lists the user's current games plus the past-games history."""
    username = request.user.chess_username
    games = _decorate_games(game_store.current_games(request.user), username)
    past = _decorate_games(game_store.past_games(request.user), username)
    return render(request, "home.html", {"games": games, "past_games": past})


@login_required
def game_list(request):
    """HTMX endpoint: current games + past-games history fragment for polling."""
    username = request.user.chess_username
    games = _decorate_games(game_store.current_games(request.user), username)
    past = _decorate_games(game_store.past_games(request.user), username)
    return render(
        request, "partials/game_list.html", {"games": games, "past_games": past}
    )


@login_required
def game_detail(request, id):
    """The one detail page — move-by-move over a game, live or finished.

    The full page renders the shell plus the initial position fragment (the live
    head for a game in progress, the opening for a finished one). Navigation, the
    live poll and analysis are all htmx fragment swaps from here on.
    """
    game = game_store.stored_game(request.user, id)
    if game is None:
        return render(request, "error.html", {"message": "Game not found."}, status=404)

    moves = board_utils.moves_from_pgn(game.pgn)
    sel = len(moves) if game.is_active else 0
    context = _position_context(request.user, game, sel)
    return render(request, "game_detail.html", context)


@login_required
def game_position(request, id):
    """HTMX fragment: the position view for a given ply (nav / move-click)."""
    game = game_store.stored_game(request.user, id)
    if game is None:
        return HttpResponse(status=404)
    sel = _int(request.GET.get("sel"), 0)
    return render(request, "partials/position.html", _position_context(request.user, game, sel))


@login_required
def game_live(request, id):
    """HTMX poll: swap in new moves for a live game, or 204 when nothing changed.

    The client sends its current ``sel`` and the ``head`` it already knows. When
    a new move has appeared we re-render the position — following the live head if
    the user was sitting on it, otherwise leaving them on the move they're
    reviewing (with the moves grid refreshed and a jump-to-live button shown).
    """
    game = game_store.stored_game(request.user, id)
    if game is None:
        return HttpResponse(status=204)

    sel = _int(request.GET.get("sel"), 0)
    known_head = _int(request.GET.get("head"), 0)
    head = len(board_utils.moves_from_pgn(game.pgn))
    if head == known_head and game.is_active:
        return HttpResponse(status=204)  # no new move — keep polling

    following = sel >= known_head
    render_sel = head if following else sel
    return render(request, "partials/position.html", _position_context(request.user, game, render_sel))


@login_required
def analyze_position(request, id):
    """HTMX endpoint: the coach card for a ply, requesting analysis on demand.

    ``POST`` enqueues background analysis for the ply's position (idempotent),
    reusing the same Celery task; ``GET`` is the pending self-poll. Both return
    the coach-card fragment for the current state. Analysis is read/enqueued from
    the stored snapshot, so a finished game never triggers a Chess.com call.
    """
    game = game_store.stored_game(request.user, id)
    if game is None:
        return HttpResponse(status=404)
    sel = _int(request.GET.get("sel") or request.POST.get("sel"), 0)
    context = _position_context(request.user, game, sel)

    if request.method == "POST":
        fen = context["coach"].get("fen")
        if fen:
            row = CoachSuggestion.objects.filter(
                user=request.user, game_id=id, fen=fen
            ).first()
            if row is None:
                CoachSuggestion.objects.create(
                    user=request.user,
                    game_id=id,
                    fen=fen,
                    status=CoachSuggestion.Status.PENDING,
                    move_no=board_utils.fullmove_number(fen),
                    eval_text="",
                    analysis="",
                )
                analyze_game_task.delay(request.user.id, id, fen, game.pgn or None)
            elif row.status != CoachSuggestion.Status.PENDING:
                row.status = CoachSuggestion.Status.PENDING
                row.eval_text = ""
                row.eval_cp = None
                row.best_move_san = None
                row.best_move_uci = None
                row.analysis = ""
                row.save()
                analyze_game_task.delay(request.user.id, id, fen, game.pgn or None)
            context = _position_context(request.user, game, sel)

    return render(request, "partials/coach_card.html", context)


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def logout_view(request):
    """Clears the session and redirects to login (keeps the GET /logout link)."""
    logout(request)
    return redirect("login")
