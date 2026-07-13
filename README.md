# chessdotcom_ai_coach

Chess Coach AI — a **Django** web app that lists your live Chess.com games,
renders the board, and asks an Ollama LLM to analyze the position like a
grandmaster coach.

## Stack

- **Django 5** — ORM, templates, admin, session auth (custom `User` model)
- **PostgreSQL** — via `psycopg2`
- **Ollama** — the AI coach (`coach/services/coach.py`)
- **Chess.com API** — via `chess-com` (`coach/services/chess_client.py`)
- **HTMX + Alpine.js** — board rendering and on-demand coach analysis
- Custom hand-written CSS theme (no Tailwind) in `static/css/styles.css`

## Run with Docker

```bash
docker compose up --build
```

The app runs migrations and `collectstatic` on start, then serves on
http://localhost:8000. Create a user via the admin (see below).

## Run locally

Requires Python 3.13+ and a running PostgreSQL (the `postgres` service in
`compose.yaml` works). Configure `.env` (see the `POSTGRES_*`, `SECRET_KEY`,
`OLLAMA_HOST`, `OLLAMA_MODEL` keys).

```bash
poetry install          # or: pip install .
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Then open http://localhost:8000, sign in, and set your **Chess.com username**
on the user via the admin at http://localhost:8000/admin/ (field
`chessdotcom_username`; it falls back to the login username if left blank).

The AI coach requires the Ollama service with the configured model pulled, e.g.:

```bash
docker compose exec ollama ollama pull llama3:8b
```
