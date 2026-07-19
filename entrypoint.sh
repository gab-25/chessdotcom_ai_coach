#!/bin/sh
set -e

# Apply database migrations, then serve.
python manage.py migrate --noinput
python manage.py collectstatic --noinput

# --timeout 180: Stockfish analysis (~2s) plus llama3.2:3b CPU inference (~20-30s)
# can exceed gunicorn's default 30s worker timeout, which would kill the request.
exec gunicorn chessdotcom_ai_coach.wsgi:application --bind 0.0.0.0:8000 --timeout 180
