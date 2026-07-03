"""Validated backend environment knobs exposed to agents via spec."""

from __future__ import annotations

import re
from typing import Any

# Keys agents may set under ``backend.env``. Framework-injected vars (PORT,
# DB_*) are merged separately and cannot be overridden here.
ALLOWED_BACKEND_ENV_KEYS: frozenset[str] = frozenset(
    {
        "DB_POOL_SIZE",
        "DB_POOL_OVERFLOW",
        "PG_POOL_MAX",
        "SQLALCHEMY_POOL_RECYCLE",
    }
)

_POSITIVE_INT_RE = re.compile(r"^\d+$")


def parse_backend_env(raw: Any) -> tuple[dict[str, str], list[str]]:
    """Return ``(env, errors)`` for agent-supplied ``backend.env``."""
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, ["backend.env must be a mapping of string keys to string values"]
    errors: list[str] = []
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if not name:
            continue
        if name in {
            "PORT",
            "DB_HOST",
            "DB_PORT",
            "DB_USER",
            "DB_PASSWORD",
            "DB_NAME",
            "DB_READ_HOST",
            "DB_READ_PORT",
            "REDIS_URL",
            "DB_REDIS_URL",
        }:
            errors.append(
                f"backend.env.{name}: reserved — set via spec fields, not env"
            )
            continue
        if name not in ALLOWED_BACKEND_ENV_KEYS:
            allowed = ", ".join(sorted(ALLOWED_BACKEND_ENV_KEYS))
            errors.append(
                f"backend.env.{name}: not in the allowed set ({allowed})"
            )
            continue
        text = str(value).strip()
        if not text:
            errors.append(f"backend.env.{name}: value must be non-empty")
            continue
        if name in {
            "DB_POOL_SIZE",
            "DB_POOL_OVERFLOW",
            "PG_POOL_MAX",
            "SQLALCHEMY_POOL_RECYCLE",
        } and not _POSITIVE_INT_RE.match(text):
            errors.append(f"backend.env.{name}: must be a positive integer")
            continue
        out[name] = text
    return out, errors
