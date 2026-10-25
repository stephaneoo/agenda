"""Django settings for the agenda. Environment variables configure deployment."""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# AGENDA_DEV=1 is set by the dev shell and the Nix checks, never on the host.
DEV = os.environ.get("AGENDA_DEV") == "1"
# The host sets AGENDA_ENV=production, and systemd sets INVOCATION_ID in every unit it starts:
# either one means this is not a dev shell, whatever AGENDA_DEV says.
if DEV and (os.environ.get("AGENDA_ENV") == "production" or os.environ.get("INVOCATION_ID")):
    raise ImproperlyConfigured("AGENDA_DEV=1 is refused in production and under systemd")
DEBUG = DEV


def _secret_key() -> str:
    """The systemd credential, else DJANGO_SECRET_KEY_FILE, else DJANGO_SECRET_KEY."""
    credentials = os.environ.get("CREDENTIALS_DIRECTORY")
    if credentials and (path := Path(credentials) / "django-secret-key").is_file():
        return path.read_text(encoding="utf-8").strip()
    if key_file := os.environ.get("DJANGO_SECRET_KEY_FILE"):
        try:
            return Path(key_file).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ImproperlyConfigured(f"DJANGO_SECRET_KEY_FILE: cannot read {key_file}: {e.strerror}") from None
    return os.environ.get("DJANGO_SECRET_KEY", "")


SECRET_KEY = _secret_key()
if DEV:
    SECRET_KEY = SECRET_KEY or "dev-only-insecure-key"
# The messages never include the key.
elif not SECRET_KEY:
    raise ImproperlyConfigured(
        "no secret key: set the django-secret-key credential, DJANGO_SECRET_KEY_FILE or DJANGO_SECRET_KEY"
    )
elif len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5 or SECRET_KEY.startswith("django-insecure-"):
    raise ImproperlyConfigured("the secret key is too weak: it needs 50+ characters, 5+ of them distinct")

ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",") if h]
if DEV:
    ALLOWED_HOSTS += ["localhost", "127.0.0.1", "testserver"]
else:
    # gunicorn listens on a Unix socket that only Caddy can reach, and Caddy sets
    # X-Forwarded-Proto itself, so the header can't come from a client.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    # A leading dot in ALLOWED_HOSTS matches subdomains; CSRF origins spell that "*.".
    CSRF_TRUSTED_ORIGINS = [
        "https://" + ("*" + h if h.startswith(".") else h) for h in ALLOWED_HOSTS if h != "*"
    ]
    # No HSTS for now: browsers ignore it for IP literals. Set SECURE_HSTS_SECONDS once a
    # domain exists. Caddy does the HTTP -> HTTPS redirect, not Django.
    SILENCED_SYSTEM_CHECKS = ["security.W004", "security.W008"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.gis",
    "geo",
    "claims",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "agenda_site.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "agenda_site.wsgi.application"

# DATABASE_URL, e.g. postgis://user@%2Frun%2Fpostgresql/agenda for a Unix socket.
# Left empty when unset, so that conftest.py can say what is missing.
DATABASES = {"default": dj_database_url.config(env="DATABASE_URL")} if os.environ.get("DATABASE_URL") else {}

# GeoDjango finds GDAL and GEOS through these when they are not on the default path (Nix).
GDAL_LIBRARY_PATH = os.environ.get("GDAL_LIBRARY_PATH")
GEOS_LIBRARY_PATH = os.environ.get("GEOS_LIBRARY_PATH")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "fr-fr"
TIME_ZONE = "Europe/Paris"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = os.environ.get("DJANGO_STATIC_ROOT", str(BASE_DIR / "staticfiles"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
