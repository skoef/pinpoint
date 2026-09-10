"""Database configuration.

Kept out of ``settings.py`` so the Postgres branch can be unit-tested without
re-importing Django settings.

Postgres is used as soon as a host is configured; otherwise SQLite, so local
development and the test suite need no configuration at all.

Both naming conventions are accepted for the connection details: the explicit
``DB_*`` variables, and the ``RDS_*`` ones that Elastic Beanstalk injects when an
RDS instance is attached to the environment. ``DB_*`` wins when both are present.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

SQLITE_ENGINE = "django.db.backends.sqlite3"
POSTGRES_ENGINE = "django.db.backends.postgresql"

# Setting name -> the environment variables consulted for it, in priority order.
ENV_ALIASES = {
    "NAME": ("DB_NAME", "RDS_DB_NAME"),
    "USER": ("DB_USER", "RDS_USERNAME"),
    "PASSWORD": ("DB_PASSWORD", "RDS_PASSWORD"),
    "HOST": ("DB_HOST", "RDS_HOSTNAME"),
    "PORT": ("DB_PORT", "RDS_PORT"),
}


def _lookup(env, key):
    for name in ENV_ALIASES[key]:
        value = env.get(name, "")
        if value and value.strip():
            return value.strip()
    return ""


def database_config(env=None, base_dir=None):
    """Return the dict for Django's ``DATABASES["default"]``."""
    env = os.environ if env is None else env
    base_dir = Path(base_dir) if base_dir else Path(".")

    host = _lookup(env, "HOST")
    if not host:
        return {"ENGINE": SQLITE_ENGINE, "NAME": base_dir / "db.sqlite3"}

    # Fail loudly rather than half-connect with a blank user or database.
    missing = [
        ENV_ALIASES[key][0]
        for key in ("NAME", "USER", "PASSWORD")
        if not _lookup(env, key)
    ]
    if missing:
        raise ImproperlyConfigured(
            "A database host is configured, so Postgres is in use, but "
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} missing. "
            "Set them (or the matching RDS_* variables) alongside the host."
        )

    return {
        "ENGINE": POSTGRES_ENGINE,
        "NAME": _lookup(env, "NAME"),
        "USER": _lookup(env, "USER"),
        "PASSWORD": _lookup(env, "PASSWORD"),
        "HOST": host,
        "PORT": _lookup(env, "PORT") or "5432",
        # RDS is a network hop, so reconnecting per request is wasteful. The
        # health check stops a pooled-but-dead connection surfacing as a 500
        # after RDS closes it (failover, idle timeout, reboot).
        "CONN_MAX_AGE": int(env.get("DB_CONN_MAX_AGE", "600")),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            # RDS terminates TLS, so require it -- credentials and player data
            # should never cross the VPC in the clear. Use "verify-full" plus
            # sslrootcert if you ship the RDS CA bundle.
            "sslmode": env.get("DB_SSLMODE", "require"),
            "connect_timeout": int(env.get("DB_CONNECT_TIMEOUT", "5")),
        },
    }
