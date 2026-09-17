#!/bin/sh
set -e

# Apply database migrations, then serve.
python manage.py migrate --noinput
python manage.py collectstatic --noinput

# --timeout 180: generous headroom over gunicorn's 30s default. No view calls
# Stockfish or the LLM — analysis runs in the Celery worker — so this only has to
# cover a slow database read on a page that renders a whole game.
exec gunicorn chessdotcom_ai_coach.wsgi:application --bind 0.0.0.0:8000 --timeout 180
