"""One row per sync run, for the record (the status file and Source carry the latest)."""

from django.db import models

STATUSES = [("clean", "complète"), ("partial", "partielle"), ("failed", "échouée")]


class SyncRun(models.Model):
    source = models.CharField(max_length=40, default="datatourisme")
    started_at = models.DateTimeField()  # the fetch time: every claim of the run is seen then
    finished_at = models.DateTimeField(null=True, blank=True)
    # clean: every page fetched, withdrawals applied; partial: withdrawals skipped (over the
    # threshold) or rows refused by write_claims; failed: a page could not be fetched (or the
    # run could not start), no withdrawals.
    status = models.CharField(max_length=10, choices=STATUSES)
    fetched_rows = models.PositiveIntegerField(default=0)
    added = models.PositiveIntegerField(default=0)  # records, new
    updated = models.PositiveIntegerField(default=0)  # records, changed
    unchanged = models.PositiveIntegerField(default=0)  # records, same payload: no claims written
    withdrawn = models.PositiveIntegerField(default=0)  # future occurrences
    skipped_withdrawals = models.PositiveIntegerField(default=0)  # future occurrences
    unresolved_rows = models.PositiveIntegerField(default=0)  # commune not found: skipped, not guessed
    out_of_area = models.PositiveIntegerField(default=0)  # in a commune outside the covered area
    failed_rows = models.PositiveIntegerField(default=0)  # unusable: no id, name or dates
    # Refused by write_claims: the run is partial, the record's dates are kept as they were.
    refused_rows = models.PositiveIntegerField(default=0, db_default=0)
    # Refused for expanding to more than parse.MAX_OCCURRENCES dates: partial, dates kept.
    too_many_dates = models.PositiveIntegerField(default=0, db_default=0)
    error = models.TextField(blank=True, default="")
    # Something an operator should see although the run is clean (e.g. many withdrawals).
    warning = models.CharField(max_length=200, blank=True, default="", db_default="")

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "synchronisation"

    def __str__(self) -> str:
        return f"{self.source} {self.started_at:%Y-%m-%d %H:%M} {self.status}"

    def summary(self) -> str:
        text = (
            f"{self.status}: {self.fetched_rows} rows, {self.added} added, {self.updated} updated, "
            f"{self.unchanged} unchanged, {self.withdrawn} withdrawn, {self.skipped_withdrawals} withdrawals skipped, "
            f"{self.unresolved_rows} unresolved, {self.out_of_area} out of area, {self.failed_rows} failed, "
            f"{self.refused_rows} refused"
        )
        if self.too_many_dates:
            text = f"{text}, {self.too_many_dates} refused for too many dates"
        if self.warning:
            text = f"{text}; {self.warning}"
        return f"{text}; {self.error}" if self.error else text
