"""Settings under a controlled environment, each import in a fresh interpreter."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STRONG_KEY = "k" + "".join(chr(ord("a") + i % 26) for i in range(59))  # 60 chars, 26 distinct
KEYS = [
    "DEBUG", "SECRET_KEY", "ALLOWED_HOSTS", "SECURE_PROXY_SSL_HEADER", "SESSION_COOKIE_SECURE",
    "CSRF_COOKIE_SECURE", "CSRF_TRUSTED_ORIGINS", "SECURE_HSTS_SECONDS", "SILENCED_SYSTEM_CHECKS",
]
PROBE = (
    "import json, agenda_site.settings as s; "
    f"print(json.dumps({{k: getattr(s, k, None) for k in {KEYS!r}}}))"
)


def load(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # Only PATH is inherited: nothing from the dev shell (AGENDA_DEV, DATABASE_URL) leaks in.
    return subprocess.run(
        [sys.executable, "-c", PROBE], cwd=ROOT, capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], **env},
    )


def settings_for(env: dict[str, str]) -> dict:
    proc = load(env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def refused(env: dict[str, str], match: str) -> str:
    proc = load(env)
    assert proc.returncode != 0
    assert "ImproperlyConfigured" in proc.stderr and match in proc.stderr, proc.stderr
    return proc.stderr


PROD = {"DJANGO_SECRET_KEY": STRONG_KEY, "DJANGO_ALLOWED_HOSTS": "agenda.example.org,51.15.0.1"}


def test_dev_mode_debug_and_dev_key() -> None:
    s = settings_for({"AGENDA_DEV": "1"})
    assert s["DEBUG"] is True
    assert s["SECRET_KEY"]
    assert not s["SESSION_COOKIE_SECURE"]


@pytest.mark.parametrize("extra", [{"AGENDA_ENV": "production"}, {"INVOCATION_ID": "0123abcd"}])
def test_dev_mode_refused_in_production_or_under_systemd(extra: dict[str, str]) -> None:
    refused({"AGENDA_DEV": "1", **PROD, **extra}, "AGENDA_DEV")


def test_production_https_behind_caddy() -> None:
    s = settings_for(PROD)
    assert s["DEBUG"] is False
    assert s["SECURE_PROXY_SSL_HEADER"] == ["HTTP_X_FORWARDED_PROTO", "https"]
    assert s["SESSION_COOKIE_SECURE"] is True
    assert s["CSRF_COOKIE_SECURE"] is True
    assert s["CSRF_TRUSTED_ORIGINS"] == ["https://agenda.example.org", "https://51.15.0.1"]
    # No HSTS while the site is served on an IP.
    assert not s["SECURE_HSTS_SECONDS"]
    assert set(s["SILENCED_SYSTEM_CHECKS"]) == {"security.W004", "security.W008"}


def test_production_wildcard_subdomain_trusted_origin() -> None:
    s = settings_for({**PROD, "DJANGO_ALLOWED_HOSTS": ".example.org"})
    assert s["CSRF_TRUSTED_ORIGINS"] == ["https://*.example.org"]


def test_production_requires_secret_key() -> None:
    refused({"DJANGO_ALLOWED_HOSTS": "x.example.org"}, "secret key")


@pytest.mark.parametrize(
    "key",
    [
        "short-but-varied-key-0123456789",  # < 50 characters
        "ab" * 30,  # 60 characters, 2 distinct
        "django-insecure-" + STRONG_KEY,  # Django's generated dev keys
    ],
)
def test_production_rejects_weak_secret_key_without_echoing_it(key: str) -> None:
    stderr = refused({**PROD, "DJANGO_SECRET_KEY": key}, "secret key")
    assert key not in stderr


def test_secret_key_file_takes_precedence(tmp_path: Path) -> None:
    other = STRONG_KEY[::-1] + "Z"
    (tmp_path / "key").write_text(other + "\n")
    s = settings_for({**PROD, "DJANGO_SECRET_KEY_FILE": str(tmp_path / "key")})
    assert s["SECRET_KEY"] == other


def test_systemd_credential_takes_precedence(tmp_path: Path) -> None:
    cred = "C" + STRONG_KEY
    (tmp_path / "django-secret-key").write_text(cred)
    (tmp_path / "key").write_text("F" + STRONG_KEY)
    s = settings_for({**PROD, "DJANGO_SECRET_KEY_FILE": str(tmp_path / "key"), "CREDENTIALS_DIRECTORY": str(tmp_path)})
    assert s["SECRET_KEY"] == cred


def test_missing_secret_key_file_is_an_error(tmp_path: Path) -> None:
    refused({**PROD, "DJANGO_SECRET_KEY_FILE": str(tmp_path / "nope")}, "DJANGO_SECRET_KEY_FILE")
