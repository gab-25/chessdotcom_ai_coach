"""
Django settings for the chessdotcom_ai_coach project.
"""

import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Core ------------------------------------------------------------------
SECRET_KEY = os.getenv("SECRET_KEY", "a-very-secret-key")
DEBUG = os.getenv("DEBUG", "true").lower() in ("1", "true", "yes")
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "*").split(",")
# Trusted origins for Django's CSRF check (comma-separated, scheme included).
# Needed when running behind a reverse proxy that terminates TLS (e.g. Traefik).
CSRF_TRUSTED_ORIGINS = [o for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o]
# The proxy terminates TLS and forwards plain HTTP; trust its forwarded-proto
# header so Django knows the original request was HTTPS.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# App version, read once from pyproject.toml (replaces FastAPI's app.state.version).
try:
    with open(BASE_DIR / "pyproject.toml", "rb") as fh:
        APP_VERSION = tomllib.load(fh).get("project", {}).get("version", "0.1.0")
except FileNotFoundError:
    APP_VERSION = "0.1.0"

# --- Applications ----------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django_htmx",
    "theme",
    "chessdotcom_ai_coach.apps.ChessdotcomAiCoach",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves static files directly from gunicorn (Django's dev server
    # only serves them under runserver). Must sit right after SecurityMiddleware.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "chessdotcom_ai_coach.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "chessdotcom_ai_coach.context_processors.app_version",
            ],
        },
    },
]

WSGI_APPLICATION = "chessdotcom_ai_coach.wsgi.application"
ASGI_APPLICATION = "chessdotcom_ai_coach.asgi.application"

# --- Database (PostgreSQL, reusing the existing POSTGRES_* env vars) --------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "postgres"),
        "USER": os.getenv("POSTGRES_USER", "postgres"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "password"),
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
    }
}

# --- Auth ------------------------------------------------------------------
AUTH_USER_MODEL = "chessdotcom_ai_coach.User"
LOGIN_URL = "/login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- I18N ------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# --- Static files ----------------------------------------------------------
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# Let WhiteNoise compress static files. We use the non-manifest backend because
# the bundled Font Awesome all.min.css references webfonts (fa-brands-400,
# fa-regular-400, fa-solid-900.ttf, ...) that aren't shipped; the manifest
# backend parses those url() refs and would fail collectstatic on the missing
# files.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Integrations ----------------------------------------------------------
# Base URL of the OpenAI-compatible LLM endpoint (Ollama's /v1).
LLM_BASE_URL = os.getenv("LLM_BASE_URL")

# --- Celery ----------------------------------------------------------------
# Redis is the broker and result backend. Both of the app's background jobs are
# enqueued from the request path — `analyze_game_task` when the user asks for an
# analysis, `sync_user_task` when a page load claims the user's archive sync —
# and a dedicated worker executes them out of it. Nothing is scheduled.
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_ALWAYS_EAGER = False

# Acknowledge a task after it ran, not when it was delivered, so an analysis in
# flight when the worker goes down is not simply lost. On a graceful stop
# (`docker compose restart/stop`) Celery hands its un-acked messages straight
# back and the analysis starts over immediately.
#
# A *hard* kill is slower to recover, not faster: the messages sit in Redis'
# `unacked` set, and kombu only re-delivers them after its visibility timeout
# (an hour by default). The guarantee that actually holds in that case is
# app-side — `sync.requeue_stale_analyses` returns any row left RUNNING for
# `ANALYSIS_TIMEOUT` to the queue, swept from the coach card's own poll. Don't
# rely on the broker for it.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
# One task reserved at a time: an analysis takes seconds to minutes, so
# prefetching a batch would hide those tasks from an idle worker and, with
# `acks_late`, put the whole batch back on the queue when one worker dies.
#
# This bounds what a worker *reserves*, not what it *runs*: concurrency is a
# separate knob, and Celery defaults it to one process per CPU core. That
# default is wrong here — Ollama serves one request at a time, so parallel
# analyses queue behind it until they exceed the coach's 150s timeout. The cap
# lives with the worker command in `docker-compose.yaml` (`--concurrency=2`),
# since it depends on the machine and the LLM runtime rather than on the app.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# How long a user's archive sync claim holds, in seconds. Requests to Chess.com
# are now paid only while somebody is using the app, so this is a per-active-user
# rate rather than the old scheduler's per-linked-user-forever one: raise it to be
# gentler on Chess.com, lower it to see finished games sooner.
SYNC_COOLDOWN_SECONDS = int(os.getenv("SYNC_COOLDOWN_SECONDS", "300"))
