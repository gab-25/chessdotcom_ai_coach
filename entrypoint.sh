#!/bin/sh
set -e

# Apply database migrations, then serve.
python manage.py migrate --noinput
python manage.py collectstatic --noinput

exec gunicorn chessdotcom_ai_coach.wsgi:application --bind 0.0.0.0:8000
