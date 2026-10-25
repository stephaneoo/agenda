"""Per-source claims (decision 0005).

Every source, moderation included, states values as claims on a target (an event, or one
of its occurrences). The canonical tables in `agenda` are a projection of these claims.
Write claims only through `claims.services`, which keeps the projection in step: Claim and
ClaimHistory refuse every write (save, delete, queryset update/delete, bulk operations,
related managers) outside claims_writer(), which only write_claims and delete_claim enter.
Raw SQL and QuerySet._raw_delete() bypass the guard: never use them on these tables.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from django.db import models
from django.db.models.signals import pre_delete, pre_save

TARGET_TYPES = [("event", "événement"), ("occurrence", "occurrence")]

_writing: ContextVar[bool] = ContextVar("claims_writing", default=False)


class ClaimWriteError(RuntimeError):
    """A write to Claim or ClaimHistory from outside claims.services."""


@contextmanager
def claims_writer() -> Iterator[None]:
    """Only claims.services enters this (tests too, to plant rows that predate validation)."""
    token = _writing.set(True)
    try:
        yield
    finally:
        _writing.reset(token)


def _guard() -> None:
    if not _writing.get():
        raise ClaimWriteError("claims are written only by claims.services.write_claims and delete_claim")


class GuardedQuerySet(models.QuerySet):
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


class GuardedModel(models.Model):
    objects = GuardedQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args: Any, **kwargs: Any) -> None:
        _guard()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        _guard()
        return super().delete(*args, **kwargs)


class Source(models.Model):
    name = models.CharField(primary_key=True, max_length=40)
    # Shown in attributions ("DATAtourisme").
    label = models.CharField(max_length=100)
    priority = models.IntegerField()
    licence = models.CharField(max_length=100, null=True, blank=True)
    # External sources are cited in `sources`; our own (moderation, rules) never are.
    external = models.BooleanField(default=True)
    # Written by the source's sync job (Task 10), read by agenda.read.sync_status().
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_outcome = models.CharField(max_length=10, null=True, blank=True)  # ok | partial | failed
    last_success_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-priority", "name"]

    def __str__(self) -> str:
        return self.label


class SourceRecord(models.Model):
    """The raw copy of what a source sent for one of its records."""

    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    external_id = models.CharField(max_length=200)
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    fetched_at = models.DateTimeField()
    # The record's own last update date at the source (DATAtourisme: Date_de_mise_a_jour).
    updated_at_source = models.DateTimeField(null=True, blank=True)
    # The sync run that last saw this record.
    last_seen_sync = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "external_id"], name="source_record_unique")]


class Claim(GuardedModel):
    target_type = models.CharField(max_length=10, choices=TARGET_TYPES)
    # An event key ("datatourisme:<id>"), or an occurrence key "<event key>/<YYYY-MM-DD>".
    target_key = models.CharField(max_length=250)
    # The event the target belongs to (claims.keys.event_key_of): the projection selects an
    # event's claims by equality on it, never by a LIKE prefix on target_key.
    event_key = models.CharField(max_length=250, db_default="", db_index=True)
    field = models.CharField(max_length=40)
    # SQL NULL stands for a JSON null: "the source says there is none".
    value = models.JSONField(null=True)
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    seen_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["target_type", "target_key", "field", "source"], name="claim_unique"),
            # The projection selects an event's claims by event_key: an empty one is never projected.
            models.CheckConstraint(condition=~models.Q(event_key=""), name="claim_event_key_not_empty"),
        ]
        indexes = [models.Index(fields=["target_key"], name="claim_target_key")]
        # Related-object access and cascades go through the guarded manager too.
        base_manager_name = "objects"

    def __str__(self) -> str:
        return f"{self.target_key} {self.field} ({self.source_id})"

    # The ClaimLike protocol of claims.resolve.
    @property
    def source_name(self) -> str:
        return self.source_id

    @property
    def priority(self) -> int:
        return self.source.priority


class ClaimHistory(GuardedModel):
    """A value a claim held before it was replaced or deleted."""

    target_type = models.CharField(max_length=10, choices=TARGET_TYPES)
    target_key = models.CharField(max_length=250)
    field = models.CharField(max_length=40)
    value = models.JSONField(null=True)
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    seen_at = models.DateTimeField()
    replaced_at = models.DateTimeField()
    action = models.CharField(max_length=10, choices=[("replaced", "remplacée"), ("deleted", "supprimée")])

    class Meta:
        indexes = [models.Index(fields=["target_key"], name="claim_history_target_key")]
        base_manager_name = "objects"


def _signal_guard(sender: type, **kwargs: Any) -> None:
    """save_base() and loaddata (raw saves) skip save(); the signals still fire."""
    _guard()


for _model in (Claim, ClaimHistory):
    pre_save.connect(_signal_guard, sender=_model, dispatch_uid=f"claims-pre-save-{_model.__name__}")
    pre_delete.connect(_signal_guard, sender=_model, dispatch_uid=f"claims-pre-delete-{_model.__name__}")
