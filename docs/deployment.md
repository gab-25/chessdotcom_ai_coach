# Deployment

## Docker Compose

```bash
docker compose up --build
```

The app is served on http://localhost:8000. Create a user through the admin (see
[development.md](development.md#first-run)).

### Services

| Service | Image / build | Role |
| --- | --- | --- |
| `web` | built from [`Dockerfile`](../Dockerfile) | Gunicorn **plus** the APScheduler process, started by [`entrypoint.sh`](../entrypoint.sh). Exposes `8000`. |
| `worker` | same image | `celery -A chessdotcom_ai_coach worker -l info`. Runs Stockfish and calls the LLM. |
| `redis` | `redis:7-alpine` | Celery broker and result backend. Health-checked with `redis-cli ping`. |
| `postgres` | `postgres:18-alpine` | Health-checked with `pg_isready`. |
| `llm` | `ghcr.io/ggml-org/llama.cpp:server` | OpenAI-compatible endpoint on `8080`. |

`web` and `worker` both wait for `postgres` and `redis` to be **healthy**, but
only for `llm` to have **started** — the model download takes minutes and the
coach degrades gracefully to Stockfish-only text meanwhile, so blocking on it
would be pointless.

### Volumes

- **`postgres-data`** — the database.
- **`llm-models`** — the GGUF cache. Keep it, or every restart re-downloads ~2GB.

## The container entrypoint

[`entrypoint.sh`](../entrypoint.sh) is the `web` container's command:

```sh
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py run_scheduler &
exec gunicorn chessdotcom_ai_coach.wsgi:application --bind 0.0.0.0:8000 --timeout 180
```

Two decisions worth preserving:

**The scheduler is backgrounded here, once per container** — not started
in-process by Django. Gunicorn forks workers; an in-process scheduler would start
once per worker and enqueue duplicate analyses. Backgrounding it from the
entrypoint means exactly one instance exists, and it runs after `migrate`, so the
tables it polls already exist.

**`--timeout 180`** rather than Gunicorn's 30s default. A synchronous analysis
path costs ~2s of Stockfish plus 20–30s of CPU inference; the default worker
timeout would kill the request. Analysis now runs in Celery, but the generous
timeout stays as a safety margin.

If you scale the `web` service to more than one replica, **the scheduler will run
in each of them.** Duplicate ticks are mostly harmless — the `get_or_create` lock
on `CoachSuggestion` deduplicates the enqueues (see
[data-model.md](data-model.md#the-row-is-the-lock)) — but you'd be making
redundant Chess.com calls. To scale out properly, build a separate service that
runs `python manage.py run_scheduler` alone and drop the background line from the
entrypoint.

## The image

[`Dockerfile`](../Dockerfile) is a two-stage build:

1. **`engine`** (`debian:bookworm-slim`) downloads the official Stockfish `sf_18`
   release tarball and extracts the binary. Nothing is compiled: the releases are
   dynamically linked x86-64 binaries with the NNUE network embedded. The variant
   is a build arg — `--build-arg SF_VARIANT=stockfish-ubuntu-x86-64-sse41-popcnt`
   for older CPUs or VMs that raise "Illegal instruction".
2. **`python:3.13-slim`** installs `libstdc++6` (needed by that binary and not
   present in the slim base), copies the engine to `/usr/local/bin/stockfish`,
   installs the project with `pip install .`, then copies the app.

Note that the image installs from `pyproject.toml` with pip, **not** from
`uv.lock` — the lockfile pins the development and CI environment, while the image
resolves within the declared version ranges.

## CI/CD

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml), triggered on **push to
`main` only** — deliberately not on pull requests.

**Job `test`:** checkout → `astral-sh/setup-uv` with caching → `uv python install
3.13` → `uv sync --group dev` → `uv run pytest`. No services needed, because the
suite swaps in SQLite and mocks the engine and LLM (see [testing.md](testing.md)).

**Job `build-app`** (needs `test`): Buildx → log in to `ghcr.io` with
`GITHUB_TOKEN` → `docker/metadata-action` tags the image `latest` **and** a short
SHA → build and push to `ghcr.io/<owner>/<repo>`, with GitHub Actions layer
caching (`type=gha`, `mode=max`).

So `main` is the release branch: every push that passes tests publishes an image.
Pin the short-SHA tag in production if you want deploys to be explicit rather
than following `latest`.

## Production checklist

- `DEBUG=false`.
- A real `SECRET_KEY` — the default in `settings.py` is a placeholder.
- `ALLOWED_HOSTS` set to your actual hostnames, not `*`.
- `CSRF_TRUSTED_ORIGINS` set to the public origin **with scheme** if TLS is
  terminated by a proxy. `SECURE_PROXY_SSL_HEADER` is already configured for
  `X-Forwarded-Proto`; make sure the proxy sets it. See
  [configuration.md](configuration.md#behind-a-reverse-proxy).
- Change the Postgres credentials — `docker-compose.yaml` hardcodes
  `postgres`/`password` for local convenience.
- Neither Redis nor Postgres is authenticated or firewalled in the Compose file,
  and both publish their ports to the host. Fine locally; not fine on a public
  machine.
- Give the LLM host enough RAM: ~2GB for the loaded 3B model plus the KV cache
  bounded by `-c 4096`.
