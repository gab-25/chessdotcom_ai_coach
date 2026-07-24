"""Enqueue coach analysis for every one of a user's moves in a single game.

The scheduler only analyses the current-turn position; this backfills the rest so
a game can be reviewed with the coach's take on each of the user's moves. Reads
the stored snapshot and enqueues the same Celery tasks the app uses — no
Chess.com call. Idempotent: already-analysed (or already-queued) moves are
skipped, so it's safe to re-run.

    python manage.py analyze_game <game_id> [--user <username>]

``--user`` is required only when the same game id is stored for more than one
user (game ids are unique per user, not globally).
"""

from django.core.management.base import BaseCommand, CommandError

from ...models import Game
from ...services.analysis import enqueue_game_analysis


class Command(BaseCommand):
    help = "Enqueue coach analysis for every one of the user's moves in a game."

    def add_arguments(self, parser):
        parser.add_argument("game_id", help="The game id (last segment of the Chess.com URL).")
        parser.add_argument(
            "--user",
            dest="username",
            help="App username owning the game (needed only if the id is not unique).",
        )

    def handle(self, *args, **options):
        game_id = options["game_id"]
        username = options.get("username")

        games = Game.objects.filter(game_id=game_id)
        if username:
            games = games.filter(user__username=username)

        owners = {game.user_id: game.user for game in games}
        if not owners:
            raise CommandError(
                f"No stored game with id {game_id}"
                + (f" for user {username}." if username else ".")
            )
        if len(owners) > 1:
            raise CommandError(
                f"Game id {game_id} is stored for multiple users — pass --user to choose one."
            )

        user = next(iter(owners.values()))
        result = enqueue_game_analysis(user, game_id)
        # `owners` was built from the same id, so the game exists for this user.
        self.stdout.write(
            self.style.SUCCESS(
                f"Queued {result['enqueued']} new analyses "
                f"({result['total']} of {user.get_username()}'s moves) for game {game_id}."
            )
        )
