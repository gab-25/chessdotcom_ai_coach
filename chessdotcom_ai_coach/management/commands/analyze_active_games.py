from django.core.management.base import BaseCommand

from chessdotcom_ai_coach.tasks import schedule_active_game_analyses


class Command(BaseCommand):
    help = "Run a single scheduler tick for active-game auto-analysis."

    def handle(self, *args, **options):
        result = schedule_active_game_analyses()
        self.stdout.write(self.style.SUCCESS(str(result)))
