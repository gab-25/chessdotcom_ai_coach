from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import redirect, render

from .models import CoachSuggestion
from .services import analysis as analysis_service
from .services import board as board_utils
from .services import game_store
from .services import sync

_START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

# Games per page on the home grid. An imported archive runs to thousands of rows,
# so the list is paged rather than rendered whole.
GAMES_PER_PAGE = 24


def _decorate_games(games):
    """Attach the mini-board cells and the move number to `Game` rows.

    Used by the home page's game grid (a plain DB read). Both come from the stored
    FEN, which for an imported game is the final position from the archive.
    """
    for game in games:
        game.cells = board_utils.fen_to_cells(game.fen)
        game.move_no = board_utils.fullmove_number(game.fen)
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


def _in_flight(row):
    """True while the analysis is queued or running — both render as "pending".

    The two are worth distinguishing to the recovery sweeps (only a RUNNING row
    can time out) but not on the card: either way the answer isn't there yet, and
    the page says so in one word until the user presses Refresh.
    """
    return row.status in (
        CoachSuggestion.Status.PENDING,
        CoachSuggestion.Status.RUNNING,
    )


def _failed_coach(row, san=""):
    """Card state for a position the coach gave up on.

    Rendering a failure through the "analyzed" branch would show an empty
    recommendation and claim the coach preferred nothing, so it gets its own
    state, saying which move failed and why. The way back is the whole-game
    button: `analysis.enqueue_game_analysis` re-queues a FAILED row without
    `force`, so there is nothing for the card itself to offer.
    """
    return {
        "mode": "failed",
        "san": san,
        # A row retired by the recovery sweep carries no prose — it never got far
        # enough to produce any — so say why instead of showing a bare card.
        "reason": row.analysis
        or row.eval_text
        or "The background analysis did not complete.",
    }


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

    Reads entirely from the stored ``Game`` snapshot and the persisted
    ``CoachSuggestion`` rows — no Chess.com call — so navigation and review are
    cheap DB reads. ``sel`` is the 0-based ply cursor: 0 is the starting position
    and ``head`` (the number of plies in the PGN) is the last played ply, which is
    also the end of the timeline. There is deliberately no cursor past ``head``:
    the coach comments on moves that were played, so a position the player never
    reached has nothing to show.
    """
    username = user.chess_username
    orientation = "white" if (game.white_name or "").lower() == username.lower() else "black"
    flipped = orientation == "black"

    pgn = game.pgn
    moves = board_utils.moves_from_pgn(pgn)
    positions = board_utils.positions_from_pgn(pgn) or [game.fen or _START_FEN]
    history = list(CoachSuggestion.objects.filter(user=user, game_id=game.game_id))
    # Joins on (move_no, side to move) against the plies in the PGN, so a row left
    # over for a position that was never played has no ply to attach to and is not
    # rendered anywhere. That is what keeps such rows invisible without filtering.
    board_utils.annotate_moves(moves, history)

    head = len(moves)
    sel = max(0, min(head, sel))
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

    coach = {"mode": "start"}
    arrows = []

    if ply is None:
        coach = {"mode": "start"}
    elif ply["color"] != orientation:
        coach = {"mode": "opponent", "san": ply["san"]}
    else:
        s = ply["suggestion"]
        if s is None:
            coach = {"mode": "unanalyzed", "san": ply["san"]}
        elif _in_flight(s):
            coach = {"mode": "pending", "san": ply["san"]}
        elif s.status == CoachSuggestion.Status.FAILED:
            coach = _failed_coach(s, san=ply["san"])
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
        pending = m["color"] == orientation and s is not None and _in_flight(s)
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

    # Whole-game analysis state, for the controls in the coach column. Counted off
    # `moves_view` rather than re-queried: it already holds the per-ply state. The
    # page shows only which of the three states this is — running, complete, or
    # neither — never the counts: a number that moves only when you press Refresh
    # reads as a stalled number.
    analysis_total = sum(1 for m in moves if m["color"] == orientation)
    analysis_done = sum(1 for m in moves_view if m["analyzed"])
    analysis_pending = sum(1 for m in moves_view if m["pending"])

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
    move_label = ""
    if ply is not None:
        ref = f"{ply['move_no']}{'. ' if ply['color'] == 'white' else '… '}{ply['san']}"
        last_move = ref
        sel_text = f"Reviewing: {ref}"
        move_label = f"Move {ply['move_no']} · {'White' if ply['color'] == 'white' else 'Black'}"

    return {
        "id": game.game_id,
        "sel": sel,
        "head": head,
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
        "analysis_total": analysis_total,
        "analysis_done": analysis_done,
        "analysis_pending": analysis_pending,
        "analysis_complete": analysis_total > 0 and analysis_done >= analysis_total,
    }


_IN_PROGRESS_MESSAGE = (
    "This game is still in progress. It becomes available for review once it ends."
)


def _reviewable_game(user, game_id):
    """The stored game if it can be reviewed, else the reason it can't.

    Returns ``(game, None)`` or ``(None, message)``. A game still in progress is
    refused: the coach only comments on moves that were played, so nothing about
    a running game is shown — and since the home page no longer links one, this
    is what closes the hand-typed URL.
    """
    game = game_store.stored_game(user, game_id)
    if game is None:
        return None, "Game not found."
    if game.is_active:
        return None, _IN_PROGRESS_MESSAGE
    return game, None


def _games_page(request):
    """The home grid's context: one page of finished games, plus its controls.

    Paging and filtering are done by the database (`game_store.past_games`
    returns a queryset), because a fully imported archive is thousands of rows
    and rendering or counting them in Python would not survive it.
    """
    time_class = request.GET.get("time_class", "")
    games = game_store.past_games(request.user, time_class=time_class)
    paginator = Paginator(games, GAMES_PER_PAGE)
    # `get_page` clamps: a junk or out-of-range page number lands on a real page
    # instead of raising, which matters because the page number is in a URL.
    page = paginator.get_page(request.GET.get("page"))

    _decorate_games(page.object_list)
    return {
        "games": page.object_list,
        "page": page,
        "total": paginator.count,
        "time_class": time_class,
        "time_classes": game_store.time_classes(request.user),
    }


@login_required
def home(request):
    """Home page: the user's finished games, the ones there is something to review.

    A plain DB read. It starts nothing: the archive import is claimed by the
    Sync button (`game_list`), because that is the one control whose meaning is
    "fetch my games". Opening a page used to claim it too, which made every return
    to the home page a potential Chess.com fetch and left the empty state
    announcing an import that a cooled-down load had not actually queued.
    """
    return render(request, "home.html", _games_page(request))


@login_required
def game_list(request):
    """HTMX endpoint: the finished-games fragment — refresh, paging and filtering.

    Also the app's only sync trigger. `request_sync` rate-limits the claim and
    hands the work to the worker, so this stays a DB read; its return value is
    what puts the single delayed re-fetch in the fragment, six seconds later,
    by which time the import has had a moment to land.

    Paging and filtering arrive here too and will normally lose the claim. When
    one of them wins it — the cooldown has lapsed — it gets the same one-shot
    re-fetch, which is right rather than a leak: the re-fetch reproduces the very
    view the user is looking at.
    """
    # `after_sync` is that re-fetch identifying itself, and it must never claim.
    # Otherwise a deployment with SYNC_COOLDOWN_SECONDS under 6 would turn one
    # re-fetch into a 6-second poll *and* a 6-second re-import. The cooldown makes
    # that impossible today; this makes it impossible by construction, which is
    # what the old trigger got from sitting on a wrapper the swap never replaced.
    after_sync = bool(request.GET.get("after_sync"))
    context = _games_page(request)
    context["sync_started"] = False if after_sync else sync.request_sync(request.user)
    context["oob"] = True
    return render(request, "partials/game_list.html", context)


@login_required
def game_detail(request, id):
    """The detail page — move-by-move review over a finished game.

    The full page renders the shell plus the opening position; navigation and
    analysis are htmx fragment swaps from here on. A game still in progress is
    refused (see `_reviewable_game`).
    """
    game, message = _reviewable_game(request.user, id)
    if game is None:
        return render(request, "error.html", {"message": message}, status=404)

    return render(request, "game_detail.html", _position_context(request.user, game, 0))


@login_required
def game_position(request, id):
    """HTMX fragment: the position view for a ply — navigation, and Refresh.

    `refresh` is the Refresh button naming itself, the way `after_sync` does in
    `game_list`. The page carries no poll of any kind, so this is the one request
    whose meaning is "show me where the analysis got to" — which makes it the
    right place for the recovery sweeps, until now hung off the coach card's own
    poll. Navigation is left as a pure read: a stuck analysis is not what the
    arrow keys are about, and the sweeps read the broker's queue depth.

    `recover_stuck_analyses` throttles itself and never raises, so a broker that
    is down costs a page that stays where it was, not a 500.
    """
    game, _message = _reviewable_game(request.user, id)
    if game is None:
        return HttpResponse(status=404)

    if request.GET.get("refresh"):
        sync.recover_stuck_analyses(request.user)

    sel = _int(request.GET.get("sel"), 0)
    return render(request, "partials/position.html", _position_context(request.user, game, sel))


@login_required
def analyze_game(request, id):
    """HTMX endpoint: queue the coach on every move the user played in this game.

    Analysis is on demand — nothing queues it in the background — so this is the
    control that starts it. `analysis.enqueue_game_analysis` is idempotent, so pressing it
    twice queues nothing the second time and there is no need to guard against a
    double click. Returns the position fragment, which re-renders with the plies
    now showing as pending.

    `force` is the other half of that: it is what the "Re-analyse this game"
    button sends, and it asks for every move to be queued again, overwriting the
    analyses already on record. Without it a finished game would be a dead end.
    A move the coach *failed* on needs no `force` — the service re-queues it on
    the plain press, which is what keeps that move from being a dead end too.
    """
    game, _message = _reviewable_game(request.user, id)
    if game is None:
        return HttpResponse(status=404)

    if request.method == "POST":
        # htmx posts the button's parameters in the query string, so read `force`
        # the way `sel` is read below.
        force = bool(request.GET.get("force") or request.POST.get("force"))
        analysis_service.enqueue_game_analysis(request.user, id, force=force)

    sel = _int(request.GET.get("sel") or request.POST.get("sel"), 0)
    return render(
        request, "partials/position.html", _position_context(request.user, game, sel)
    )


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def logout_view(request):
    """Clears the session and redirects to login (keeps the GET /logout link)."""
    logout(request)
    return redirect("login")
