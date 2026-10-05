"""Single UTC clock helpers shared across runtime modules."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now() -> str:
    return utc_now().isoformat()
