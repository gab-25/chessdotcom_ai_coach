"""Force a re-read of a user's Chess.com game archive.

Ordinary imports need no command: pressing Sync claims a sync and the worker
reads whatever months are still missing. This is the override for when that is
not enough — a history imported by an older version, or `ArchiveImport` rows that
claim more than the database actually holds. It ignores those rows entirely and
re-reads every monthly archive in sequence, newest first.

    python manage.py import_archives [--user <username>] [--months N]

Without ``--user`` every linked user is imported. ``--months`` caps how many of
the most recent months are read, for a quick partial catch-up. Idempotent: a
game already stored is updated in place, so re-running adds nothing.
"""

from django.core.management.base import BaseCommand, CommandError

from ...services.sync import linked_users, import_all_archives


class Command(BaseCommand):
    help = "Import finished games from the Chess.com monthly archives."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            dest="username",
            help="App username to import for (default: every linked user).",
        )
        parser.add_argument(
            "--months",
            type=int,
            help="Only read this many of the most recent months (default: all).",
        )

    def handle(self, *args, **options):
        username = options.get("username")
        months = options.get("months")
        if months is not None and months < 1:
            raise CommandError("--months must be at least 1.")

        users = list(linked_users())
        if username:
            users = [u for u in users if u.get_username() == username]
            if not users:
                raise CommandError(
                    f"No active user named {username} with a linked Chess.com account."
                )
        if not users:
            raise CommandError("No active users with a linked Chess.com account.")

        total = 0
        for user in users:
            try:
                added = import_all_archives(user, months=months)
            except Exception as exc:  # one bad account must not stop the rest
                self.stderr.write(
                    self.style.WARNING(
                        f"Import failed for {user.chess_username}: {exc}"
                    )
                )
                continue
            total += added
            self.stdout.write(
                f"{user.get_username()}: {added} new game{'' if added == 1 else 's'}."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {total} new game{'' if total == 1 else 's'} "
                f"for {len(users)} user{'' if len(users) == 1 else 's'}."
            )
        )
