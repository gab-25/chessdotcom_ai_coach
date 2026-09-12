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
| `web` | built from [`Dockerfile`](../Dockerfile) | Gunicorn, started by [`entrypoint.sh`](../entrypoint.sh). Exposes `8000`. Stateless — **scale it freely**. |
| `worker` | same image | `celery -A chessdotcom_ai_coach worker -l info`. Runs Stockfish, calls the LLM, and imports Chess.com archives. |
| `redis` | `redis:7-alpine` | Celery broker and result backend, with append-only persistence on the `redis-data` volume (see [Volumes](#volumes)). Health-checked with `redis-cli ping`. |
| `postgres` | `postgres:18-alpine` | Health-checked with `pg_isready`. |
| `ollama` | `ollama/ollama:latest` | OpenAI-compatible endpoint on `11434/v1`. Needs a one-off model pull, see below. |

`web` and `worker` both wait for `postgres` and `redis` to be **healthy**, but
only for `ollama` to have **started** — the coach degrades gracefully to
Stockfish-only text when the LLM isn't answering yet, so blocking on it would be
pointless.

### Restarting the worker

Safe to do at any time, including mid-analysis. Tasks are acknowledged after they
run (`CELERY_TASK_ACKS_LATE`), so anything in flight when the container goes down
**restarts from the beginning** rather than disappearing:

```bash
docker compose restart worker
```

This is a graceful stop, so Celery hands its un-acked messages back on the way
out and you'll see the same tasks re-delivered in the new worker's log within
seconds.

A **hard** kill (OOM, `docker kill`) is different: nothing gets to hand anything
back, and Redis only re-delivers those messages after kombu's visibility timeout,
an hour by default. You'll see it as an `unacked` count stuck above the worker's
concurrency. The recovery there is app-side and takes 10 minutes:
`sync.requeue_stale_analyses` returns any row left `RUNNING` past
`ANALYSIS_TIMEOUT` to the queue. It runs from the detail page's **Refresh**
button, so asking after the affected analysis is what triggers it.

So a redeploy costs at most the mid-flight analyses, redone — never a gap in the
history — but budget minutes, not seconds, when the worker died badly.

### After the first start: pull the model

Ollama ships no model. Until you run it, analyses complete on the Stockfish-only
fallback:

```bash
docker compose exec ollama ollama pull llama3.2:3b
```

Once only, per `ollama-data` volume. Details and how to switch model in
[configuration.md](configuration.md#pulling-the-model).

### Volumes

- **`postgres-data`** — the database.
- **`ollama-data`** — the model store. Keep it, or you have to re-pull ~2GB.
- **`redis-data`** — the task queue, with `--appendonly yes`. Without it a
  restart of the `redis` container empties the queue, and every analysis waiting
  in it is orphaned: the `CoachSuggestion` row still reads `PENDING`, so the
  reconciliation passes skip it as already queued and nothing ever runs it.
  `sync.requeue_orphaned_analyses` does recover that state when you open the
  position, but only once the queue is fully drained — keeping the volume avoids
  the situation.

## The container entrypoint

[`entrypoint.sh`](../entrypoint.sh) is the `web` container's command:

```sh
python manage.py migrate --noinput
python manage.py collectstatic --noinput
exec gunicorn chessdotcom_ai_coach.wsgi:application --bind 0.0.0.0:8000 --timeout 180
```

**`--timeout 180`** rather than Gunicorn's 30s default. A synchronous analysis
path costs ~2s of Stockfish plus 20–30s of CPU inference; the default worker
timeout would kill the request. Analysis now runs in Celery, but the generous
timeout stays as a safety margin.

Note what is *not* here: no background process. The archive import is claimed
from the request path (`services.sync.request_sync`) and executed by the worker,
so `web` holds no state of its own and `docker compose up --scale web=3` is
safe — the claim is a conditional `UPDATE` on `User.last_synced_at`, so three
replicas racing on the same user still produce one import.

**`worker` is the one to keep at a single replica**, and not because of the
import: Ollama serves one request at a time, so parallel analyses queue behind it
until they exceed the coach's 150s timeout. See the `--concurrency` note in
[`docker-compose.yaml`](../docker-compose.yaml).

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
- Give the LLM host enough RAM: ~2GB while the 3B model is loaded. Thanks to
  `OLLAMA_KEEP_ALIVE=30s` that is a *peak* during and just after an analysis, not
  a permanent floor — Ollama unloads the weights once the window closes. See
  [configuration.md](configuration.md#keep-alive--why-ollama-and-not-llama-server).
