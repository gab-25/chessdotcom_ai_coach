"""Chess.com IO: pure HTTP + shape normalisation, no DB and no Django models.

Everything comes from the monthly archives:

* ``/player/{u}/games/archives`` (``archive_months``) — one URL per month the
  player was active, which is how far back an account goes.
* ``/player/{u}/games/{yyyy}/{mm}`` (``finished_games``) — every finished game of
  that month, live and daily alike, with its final PGN and result.

Notably *not* used: ``/player/{u}/games``, the endpoint most Chess.com
integrations start from. It serves "Daily Chess games that a player is currently
playing" — no live games, and nothing finished — so it can neither list a
player's games nor complete one. The archives can do both.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from chessdotcom import ChessDotComClient

# Chess.com per-side `result` strings that mean the game was drawn. Anything that
# isn't one of these and isn't "win" is treated as a loss for that side.
_DRAW_RESULTS = {
    "stalemate",
    "agreed",
    "repetition",
    "insufficient",
    "50move",
    "timevsinsufficient",
}

# Map Chess.com's raw per-side `result` codes to a short, human-friendly reason.
_RESULT_DETAIL = {
    "win": "",
    "checkmated": "checkmate",
    "resigned": "resignation",
    "timeout": "timeout",
    "abandoned": "abandonment",
    "stalemate": "stalemate",
    "agreed": "agreement",
    "repetition": "repetition",
    "insufficient": "insufficient material",
    "50move": "50-move rule",
    "timevsinsufficient": "timeout vs insufficient",
}

# Chess.com variants share the archive with standard games. The coach evaluates
# with Stockfish on a standard `chess.Board` and `board.moves_from_pgn` replays
# the movetext on one too, so a crazyhouse PGN (which carries drops, "@") would
# stop at its first illegal ply and a Chess960 game would start from the wrong
# position. Importing them would mean truncated games and wrong evaluations, not
# extra coverage — so only `rules == "chess"` is kept.
STANDARD_RULES = "chess"


def _outcome(mine: dict, theirs: dict) -> Tuple[str, str]:
    """Map a pair of archive player entries to ``(result, detail)`` for `mine`.

    Chess.com states the outcome per side: the winner simply reads "win", so the
    *reason* a game ended always lives on the losing side.
    """
    my_result = str(mine.get("result", ""))
    if not my_result:
        return "", ""
    if my_result == "win":
        return "win", _RESULT_DETAIL.get(str(theirs.get("result", "")), "")
    if my_result in _DRAW_RESULTS:
        return "draw", ""
    return "loss", _RESULT_DETAIL.get(my_result, "")


def _archive_player(side: dict) -> dict:
    """The ``{username, rating}`` shape the `Game` rows are written from."""
    return {
        "username": str(side.get("username", "")),
        "rating": str(side.get("rating", "")),
    }


def _game_id(url: str) -> str:
    """The Chess.com game id: the last segment of the game URL."""
    return url.split("/")[-1] if url else ""


def _end_time(value) -> Optional[datetime]:
    """The archive's unix `end_time` as an aware datetime, or None."""
    try:
        return datetime.fromtimestamp(int(value), timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


class Client:
    """
    Client for interacting with the Chess.com API.
    """

    def __init__(self, username: str) -> None:
        self._chessdotcomclient = ChessDotComClient(
            user_agent="Chessdotcom-AI-Coach (Contact: gabrielesorci.25@gmail.com)"
        )
        self.username = username

    def archive_months(self) -> List[Tuple[int, int]]:
        """Every month the player has an archive for, oldest first.

        Chess.com publishes one archive URL per month the player was active
        (``.../games/2024/06``), in ascending chronological order, and the list
        is the only way to know how far back an account goes. Months with no
        games simply are not listed, so walking this is exact rather than a
        guess at a start date.
        """
        response = self._chessdotcomclient.get_player_game_archives(self.username)  # pyright: ignore[reportAttributeAccessIssue]
        data = response.json
        urls = data.get("archives", []) if isinstance(data, dict) else []

        months: List[Tuple[int, int]] = []
        for url in urls:
            parts = str(url).rstrip("/").split("/")
            if len(parts) < 2:
                continue
            try:
                months.append((int(parts[-2]), int(parts[-1])))
            except ValueError:
                continue  # not a .../yyyy/mm archive URL
        return months

    def finished_games(self, year: int, month: int) -> List[Dict]:
        """The user's finished games for one month, ready to be stored.

        The app's only source of games. Each entry is normalised into the shape a
        ``Game`` row is written from: ``game_id``, ``url``, ``pgn`` (the *final*
        movetext), ``fen`` (the final position), ``time_class``, ``end_time``,
        ``white``/``black`` as ``{username, rating}`` dicts, plus ``result`` and
        ``result_detail`` from this user's point of view.

        Two kinds of entry are skipped: games under ``rules`` other than
        ``chess`` (see `STANDARD_RULES`), and games where neither side matches
        the username — the archive can hold games played under an alias we have
        no way to attribute.
        """
        response = self._chessdotcomclient.get_player_games_by_month(  # pyright: ignore[reportAttributeAccessIssue]
            self.username, year, month
        )
        data = response.json
        games = data.get("games", []) if isinstance(data, dict) else []

        me = self.username.lower()
        finished: List[Dict] = []
        for game in games:
            if str(game.get("rules", STANDARD_RULES)) != STANDARD_RULES:
                continue

            white = game.get("white") or {}
            black = game.get("black") or {}
            if not isinstance(white, dict) or not isinstance(black, dict):
                continue  # not the archive shape (defensive)

            if str(white.get("username", "")).lower() == me:
                mine, theirs = white, black
            elif str(black.get("username", "")).lower() == me:
                mine, theirs = black, white
            else:
                continue

            result, detail = _outcome(mine, theirs)
            if not result:
                continue  # no per-side result: nothing to record

            url = str(game.get("url", ""))
            game_id = _game_id(url)
            if not game_id:
                continue

            finished.append(
                {
                    "game_id": game_id,
                    "url": url,
                    "pgn": game.get("pgn", "") or "",
                    "fen": game.get("fen", "") or "",
                    "time_class": game.get("time_class", "") or "",
                    "end_time": _end_time(game.get("end_time")),
                    "white": _archive_player(white),
                    "black": _archive_player(black),
                    "result": result,
                    "result_detail": detail,
                }
            )

        return finished
