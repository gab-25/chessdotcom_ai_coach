#!/bin/sh
set -e

# Apply database migrations, then serve.
python manage.py migrate --noinput
python manage.py collectstatic --noinput

# APScheduler runs inside the backend container, started once here (not per
# gunicorn worker): migrations above have already created the tables it polls.
# It's a background process, independent of gunicorn's worker forks, so exactly
# one scheduler instance exists per container. It enqueues the 1s analysis poll.
python manage.py run_scheduler &

# --timeout 180: Stockfish analysis (~2s) plus llama3.2:3b CPU inference (~20-30s)
# can exceed gunicorn's default 30s worker timeout, which would kill the request.
exec gunicorn chessdotcom_ai_coach.wsgi:application --bind 0.0.0.0:8000 --timeout 180
