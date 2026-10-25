from django.contrib import admin

from ingest.models import SyncRun


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    """The record of sync runs, read-only."""

    list_display = ("started_at", "source", "status", "added", "updated", "withdrawn", "skipped_withdrawals",
                    "unresolved_rows", "failed_rows")
    list_filter = ("status", "source")

    def has_add_permission(self, request, obj=None) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
