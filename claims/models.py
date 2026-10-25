"""Per-source claims (decision 0005).

Every source, moderation included, states values as claims on a target (an event, or one
of its occurrences). The canonical tables in `agenda` are a projection of these claims.
Write claims only through `claims.services`, which keeps the projection in step.
"""

from django.db import models

TARGET_TYPES = [("event", "événement"), ("occurrence", "occurrence")]


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


class Claim(models.Model):
    target_type = models.CharField(max_length=10, choices=TARGET_TYPES)
    # An event key ("datatourisme:<id>"), or an occurrence key "<event key>/<YYYY-MM-DD>".
    target_key = models.CharField(max_length=250)
    field = models.CharField(max_length=40)
    value = models.JSONField()
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    seen_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["target_type", "target_key", "field", "source"], name="claim_unique")
        ]
        indexes = [models.Index(fields=["target_key"], name="claim_target_key")]

    def __str__(self) -> str:
        return f"{self.target_key} {self.field} ({self.source_id})"

    # The ClaimLike protocol of claims.resolve.
    @property
    def source_name(self) -> str:
        return self.source_id

    @property
    def priority(self) -> int:
        return self.source.priority


class ClaimHistory(models.Model):
    """A value a claim held before it was replaced or deleted."""

    target_type = models.CharField(max_length=10, choices=TARGET_TYPES)
    target_key = models.CharField(max_length=250)
    field = models.CharField(max_length=40)
    value = models.JSONField()
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    seen_at = models.DateTimeField()
    replaced_at = models.DateTimeField()
    action = models.CharField(max_length=10, choices=[("replaced", "remplacée"), ("deleted", "supprimée")])

    class Meta:
        indexes = [models.Index(fields=["target_key"], name="claim_history_target_key")]
