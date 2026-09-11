"""Persistence for the game history.

One writer, ``upsert_finished_games``: every game comes from the monthly
archives, already finished. ``past_games`` is what the app shows, as a queryset,
because a fully imported archive is thousands of rows.

Kept separate from the Chess.com ``Client`` (which does pure IO) and from the
views (which stay thin).
"""

from __future__ import annotations

from typing import List

from django.db.models import QuerySet

from ..models import Game


def past_games(user, time_class: str = "") -> QuerySet[Game]:
    """Finished games for the user, newest first — the only list the app shows.

    Returns a **queryset, not a list**: an imported archive runs to thousands of
    rows, so the caller pages and counts in the database rather than in Python.
    ``time_class`` narrows to one Chess.com time control ("bullet", "blitz",
    "rapid", "daily"); empty means all of them.
    """
    games = Game.objects.filter(user=user, is_active=False)
    if time_class:
        games = games.filter(time_class=time_class)
    return games


def time_classes(user) -> List[str]:
    """The time controls this user actually has games for, for the home filter.

    Read from the data rather than hard-coded, so the filter never offers a
    choice that would come back empty.
    """
    return sorted(
        tc
        for tc in Game.objects.filter(user=user, is_active=False)
        .values_list("time_class", flat=True)
        .distinct()
        if tc
    )


def upsert_finished_games(user, games: List[dict]) -> int:
    """Store finished games from the monthly archive. Returns how many were new.

    Each entry is one of ``Client.finished_games``' normalised dicts. Two
    invariants matter here:

    * **``is_active`` is always False.** An archived game is finished by
      definition. Writing the flag explicitly is what closes out rows left
      ``True`` by older versions of the app, which used to snapshot games while
      they were still being played.
    * **an empty PGN never overwrites a stored one.** The archive is normally the
      better copy (ours stops at the last sync before the game left "current
      games"), but a blank one carries no moves and would destroy the only record
      we have.
    """
    created_count = 0
    for game in games:
        game_id = game.get("game_id")
        if not game_id:
            continue
        white = game.get("white") or {}
        black = game.get("black") or {}
        fields = {
            "url": game.get("url", ""),
            "white_name": white.get("username", ""),
            "black_name": black.get("username", ""),
            "white_rating": str(white.get("rating", "")),
            "black_rating": str(black.get("rating", "")),
            "time_class": game.get("time_class", ""),
            "fen": game.get("fen", ""),
            "end_time": game.get("end_time"),
            "result": game.get("result", Game.Result.UNKNOWN),
            "result_detail": game.get("result_detail", ""),
            "is_active": False,
        }
        pgn = game.get("pgn", "")
        if pgn:
            fields["pgn"] = pgn
        _row, created = Game.objects.update_or_create(
            user=user, game_id=game_id, defaults=fields
        )
        if created:
            created_count += 1
    return created_count


def stored_game(user, game_id: str) -> Game | None:
    """The stored game for an id, or ``None`` when we never saw it."""
    return Game.objects.filter(user=user, game_id=game_id).first()
