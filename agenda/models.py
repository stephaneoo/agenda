"""Canonical tables: the precomputed projection of the claims (decision 0005).

Only agenda.projection writes them, inside projection_writer(). Every other write (save,
delete, queryset update/delete, bulk operations) raises CanonicalWriteError.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from django.db import models
from django.db.models.signals import pre_delete, pre_save

from agenda.fields import STATUSES as _STATUSES
from agenda.fields import VISIBILITIES as _VISIBILITIES

_writing: ContextVar[bool] = ContextVar("projection_writing", default=False)


class CanonicalWriteError(RuntimeError):
    """A write to a canonical table from outside the projection."""


@contextmanager
def projection_writer() -> Iterator[None]:
    token = _writing.set(True)
    try:
        yield
    finally:
        _writing.reset(token)


def _guard() -> None:
    if not _writing.get():
        raise CanonicalWriteError("canonical tables are written only by agenda.projection")


class CanonicalQuerySet(models.QuerySet):
    def update(self, **kwargs: Any) -> int:
        _guard()
        return super().update(**kwargs)

    def delete(self) -> tuple[int, dict[str, int]]:
        _guard()
        return super().delete()

    def bulk_create(self, *args: Any, **kwargs: Any) -> list:
        _guard()
        return super().bulk_create(*args, **kwargs)

    def bulk_update(self, *args: Any, **kwargs: Any) -> int:
        _guard()
        return super().bulk_update(*args, **kwargs)


class CanonicalModel(models.Model):
    objects = CanonicalQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        _guard()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        _guard()
        return super().delete(*args, **kwargs)


VISIBILITIES = list(_VISIBILITIES)
STATUSES = list(_STATUSES)


class Event(CanonicalModel):
    # Byte order ("C"): lists sort by id the same way in every locale, and in JS.
    id = models.CharField(primary_key=True, max_length=20, db_collation="C")  # evt_<hash of key>
    key = models.CharField(max_length=250, unique=True)  # the claims' event key
    # "<name>-<url_key>": the url_key part never changes, so a renamed event keeps its pages.
    slug = models.CharField(max_length=120)
    url_key = models.CharField(max_length=8, db_index=True)
    name = models.TextField()
    description = models.TextField(null=True, blank=True)
    category = models.CharField(max_length=30)
    commune = models.CharField(max_length=5, db_index=True, blank=True)  # INSEE code, from location
    # The API's location object: name, street, postal_code, commune, commune_name, lat, lon.
    location = models.JSONField(default=dict)
    organizer = models.JSONField(null=True, blank=True)
    offers = models.JSONField(null=True, blank=True)
    visibility = models.CharField(max_length=10)
    # Published, complete and cited: what the read side may show.
    shown = models.BooleanField(default=False)
    sources = models.JSONField(default=list)
    updated_at = models.DateTimeField()

    class Meta:
        ordering = ["key"]
        verbose_name = "événement"
        # Related-object access and cascades go through the guarded manager too.
        base_manager_name = "objects"

    def __str__(self) -> str:
        return self.name or self.key


class Occurrence(CanonicalModel):
    id = models.CharField(primary_key=True, max_length=20, db_collation="C")  # occ_<hash of key>
    key = models.CharField(max_length=262, unique=True)  # "<event key>/<local_date>"
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="occurrences")
    local_date = models.DateField()
    start = models.CharField(max_length=25)  # local date-time, or a date
    end = models.CharField(max_length=25, null=True, blank=True)
    status = models.CharField(max_length=12)
    previous_start = models.CharField(max_length=25, null=True, blank=True)
    moved_to_id = models.CharField(max_length=20, null=True, blank=True)
    # Some source lists it (a source dropping it after it ended doesn't count).
    listed = models.BooleanField()
    visibility = models.CharField(max_length=10)
    # listed, published, not moved, and its event shown.
    shown = models.BooleanField()
    start_utc = models.DateTimeField()
    end_utc_inclusive = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        ordering = ["start_utc", "id"]
        verbose_name = "occurrence"
        base_manager_name = "objects"
        indexes = [
            models.Index(fields=["start_utc", "id"], name="occurrence_shown_start", condition=models.Q(shown=True)),
            # The list's range test starts here, so its cost doesn't grow with past occurrences.
            models.Index(fields=["end_utc_inclusive"], name="occurrence_shown_end", condition=models.Q(shown=True)),
            models.Index(fields=["event", "local_date"], name="occurrence_event_date"),
        ]

    def __str__(self) -> str:
        return f"{self.event} — {self.local_date}"

    def get_absolute_url(self) -> str:
        return f"/evenements/{self.event.slug}-{self.local_date.isoformat()}/"


def _signal_guard(sender: type, **kwargs: Any) -> None:
    """save_base() and loaddata (raw saves) skip save(); the signals still fire."""
    _guard()


for _model in (Event, Occurrence):
    pre_save.connect(_signal_guard, sender=_model, dispatch_uid=f"canonical-pre-save-{_model.__name__}")
    pre_delete.connect(_signal_guard, sender=_model, dispatch_uid=f"canonical-pre-delete-{_model.__name__}")
