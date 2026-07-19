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
OLLAMA_HOST = os.getenv("OLLAMA_HOST")
OLLAMA_PORT = os.getenv("OLLAMA_PORT")
