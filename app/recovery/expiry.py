"""One predicate for "has this approval request expired?", used wherever expiry is PRESENTED (counts, summaries, lifecycle
state, whether a decision control is offered).

The stored status is only changed to ``expired`` lazily, when somebody tries to decide on a request. Until then a request
whose time has passed is still stored as ``pending``. This module derives the effective status for display; it never writes.
Server-side enforcement (``ApprovalService`` at decision time, ``ApprovalService.check`` at execution) is separate and unchanged.

Domain contract (the same one the Human Review route has always used):
* only a request stored as ``pending`` can be effectively expired;
* ``expires_at`` is an ISO-8601 timestamp; a trailing ``Z`` means UTC and a timestamp without a zone is read as UTC;
* expired means ``expires_at <= now`` (the boundary instant counts as expired);
* a missing or unparseable ``expires_at`` is NOT reinterpreted: the request is not shown as expired (it keeps its stored status).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def parse_expiry(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def is_expired(approval: Mapping[str, Any] | None, now: datetime) -> bool:
    """True only for a request stored as pending whose parsed expiry is at or before ``now``."""
    if not approval or approval.get("status") != "pending":
        return False
    expiry = parse_expiry(approval.get("expires_at"))
    return expiry is not None and expiry <= _aware(now)


def effective_status(approval: Mapping[str, Any] | None, now: datetime) -> str | None:
    """The stored status, except that an overdue pending request reads as ``expired``."""
    if not approval:
        return None
    return "expired" if is_expired(approval, now) else approval.get("status")
