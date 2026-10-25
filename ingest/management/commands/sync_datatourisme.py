"""manage.py sync_datatourisme: the nightly DATAtourisme sync (systemd timer, 06:00 Paris).

Exits non-zero when the run failed (a page could not be fetched): the unit shows as failed,
and the status file says so. A partial run (withdrawals skipped) exits 0; the status file
records it as partial.
"""

from django.core.management.base import BaseCommand, CommandError

from ingest import sync


class Command(BaseCommand):
    help = "Fetch DATAtourisme events for the covered area and write them as claims."

    def handle(self, *args, **options) -> None:
        result = sync.run()
        self.stdout.write(result.summary())
        if result.status == "failed":
            raise CommandError("sync failed: no withdrawals applied")
