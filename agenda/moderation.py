"""Moderation: what the admin does to events and occurrences, as `moderation` claims.

Every write goes through claims.services.moderation_write / moderation_delete, which accept
the `moderation` source only: nothing here can forge another source's claim. Moderation
outranks every source (priority 100), so a hide, an edit or a move survives the next sync.
Each function raises ValueError for bad input (an empty reason, an unknown field or status, a
value write_claims refuses); the admin shows it on the form.

- hide / restore: a visibility claim {"value": "hidden", "reason": …}; restore deletes it,
  so it removes the override without pinning anything: an occurrence its source has
  withdrawn stays withdrawn.
- edit_field / revert_field: a moderation claim on one event field, or its removal.
- set_status: postponed or cancelled on one occurrence; scheduled removes the override.
- move: the new date gets dates, listed, status "rescheduled" and previous_start; the old
  occurrence gets moved_to the new key. The projection follows chains of moves.

`user` is who did it, for the log (the admin also records a LogEntry).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

from agenda.fields import MAX_TEXT
from agenda.models import Event, Occurrence
from agenda.periods import OccurrenceDates
from agenda.timeutil import PARIS, parse
from claims.keys import occurrence_key
from claims.models import Claim
from claims.services import MODERATION, NewClaim, moderation_delete, moderation_write

logger = logging.getLogger(__name__)

EDITABLE_FIELDS = ("name", "description", "category", "location", "organizer", "offers")
SETTABLE_STATUSES = ("postponed", "cancelled", "scheduled")


def _target(target: Event | Occurrence) -> tuple[str, str, str]:
    """(target type, target key, event key)."""
    if isinstance(target, Event):
        return "event", target.key, target.key
    if isinstance(target, Occurrence):
        return "occurrence", target.key, target.event.key
    raise ValueError(f"not an event or occurrence: {target!r}")


def _reason(reason: Any) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a reason is required")
    reason = " ".join(reason.split())
    if len(reason) > MAX_TEXT:
        raise ValueError(f"the reason is longer than {MAX_TEXT} characters")
    return reason


def _log(action: str, key: str, user: Any, detail: str = "") -> None:
    # %r: keys and reasons are untrusted text.
    logger.info("moderation: %s %r by %r %r", action, key, getattr(user, "username", None), detail)


def hide(target: Event | Occurrence, reason: str, user: Any) -> None:
    target_type, key, event_key = _target(target)
    reason = _reason(reason)
    moderation_write(event_key, [NewClaim(target_type, key, "visibility", {"value": "hidden", "reason": reason}, MODERATION)])
    _log("hide", key, user, reason)


def restore(target: Event | Occurrence, user: Any, reason: str) -> None:
    """Remove the moderator's visibility override. The spec asks a reason for it too; it goes
    to the log and the admin history (the claim it removes is archived in ClaimHistory)."""
    target_type, key, _ = _target(target)
    reason = _reason(reason)
    moderation_delete(target_type, key, "visibility")
    _log("restore", key, user, reason)


def edit_field(event: Event, field: str, value: Any, user: Any) -> None:
    if field not in EDITABLE_FIELDS:
        raise ValueError(f"{field!r} can't be edited (one of {', '.join(EDITABLE_FIELDS)})")
    _, key, event_key = _target(event)
    moderation_write(event_key, [NewClaim("event", key, field, value, MODERATION)])
    _log(f"edit {field}", key, user)


def revert_field(event: Event, field: str, user: Any) -> None:
    """Drop the moderator's edit of `field`: the sources' value shows again."""
    if field not in EDITABLE_FIELDS:
        raise ValueError(f"{field!r} can't be edited (one of {', '.join(EDITABLE_FIELDS)})")
    _, key, _ = _target(event)
    moderation_delete("event", key, field)
    _log(f"revert {field}", key, user)


def set_status(occurrence: Occurrence, status: str, user: Any) -> None:
    """postponed or cancelled; scheduled removes the moderator's status (rescheduled comes
    from move())."""
    if status not in SETTABLE_STATUSES:
        raise ValueError(f"status must be one of {', '.join(SETTABLE_STATUSES)}")
    _, key, event_key = _target(occurrence)
    if status == "scheduled":
        moderation_delete("occurrence", key, "status")
    else:
        moderation_write(event_key, [NewClaim("occurrence", key, "status", status, MODERATION)])
    _log(f"status {status}", key, user)


def _wall(value: str) -> tuple[date, time | None]:
    v = parse(value)
    if isinstance(v, datetime):
        local = v.astimezone(PARIS)
        return local.date(), local.time().replace(tzinfo=None)
    return v, None


def move(occurrence: Occurrence, new_date: date, new_time: time | None, user: Any) -> None:
    """Move an occurrence to another date (and time: None keeps its time). Its end moves
    with it. The new date may or may not already be an occurrence."""
    if not isinstance(new_date, date) or isinstance(new_date, datetime):
        raise ValueError("new_date must be a date")
    _, old_key, event_key = _target(occurrence)
    new_key = occurrence_key(event_key, new_date)
    if new_key == old_key:
        raise ValueError("the occurrence is already on that date")
    start_date, start_time = _wall(occurrence.start)
    shift = new_date - start_date
    end_date = end_time = None
    if occurrence.end:
        end_date, end_time = _wall(occurrence.end)
        end_date += shift
    dates = OccurrenceDates(new_date, new_time or start_time, end_date, end_time)
    # The original date, if this occurrence was itself moved there.
    previous = occurrence.previous_start or occurrence.start
    try:
        value = dates.to_claim()
    except (ValueError, OverflowError) as e:
        raise ValueError(f"bad date: {e}") from None
    # Moving back to a date that was itself moved away: that move no longer holds (else
    # the two would form a cycle, which the projection ignores).
    if Claim.objects.filter(target_type="occurrence", target_key=new_key, field="moved_to", source_id=MODERATION).exists():
        moderation_delete("occurrence", new_key, "moved_to")
    moderation_write(event_key, [
        NewClaim("occurrence", new_key, "dates", value, MODERATION),
        NewClaim("occurrence", new_key, "listed", True, MODERATION),
        NewClaim("occurrence", new_key, "status", "rescheduled", MODERATION),
        NewClaim("occurrence", new_key, "previous_start", previous, MODERATION),
        NewClaim("occurrence", old_key, "moved_to", new_key, MODERATION),
    ])
    _log("move", old_key, user, new_key)
