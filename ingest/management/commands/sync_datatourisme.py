"""manage.py sync_datatourisme: the nightly DATAtourisme sync (systemd timer, 06:00 Paris).

Exits non-zero when the run failed (a page could not be fetched): the unit shows as failed,
and the status file says so. A partial run (withdrawals skipped, or rows refused) exits 0; the
status file records it as partial. --allow-mass-withdrawal applies withdrawals past the
threshold for that one run: use it only once upstream is known to be right.
"""

from django.core.management.base import BaseCommand, CommandError

from ingest import sync


class Command(BaseCommand):
    help = "Fetch DATAtourisme events for the covered area and write them as claims."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--allow-mass-withdrawal", action="store_true",
            help="Apply withdrawals even past the 20%% threshold, for this run only (after checking upstream).",
        )

    def handle(self, *args, **options) -> None:
        result = sync.run(allow_mass_withdrawal=options["allow_mass_withdrawal"])
        self.stdout.write(result.summary())
        if result.status == "failed":
            raise CommandError("sync failed: no withdrawals applied")
