"""manage.py sync_datatourisme: the nightly DATAtourisme sync (systemd timer, 06:00 Paris).

Exits non-zero when the run failed (a page could not be fetched, or the run passed its
deadline): the unit shows as failed, and the status file says so. A partial run (withdrawals
skipped, or rows refused) exits 0; the status file records it as partial.

--allow-mass-withdrawal=N applies withdrawals past the threshold for that one run. N must be
the count the last partial run skipped (its summary: "N withdrawals skipped"), and the run
applies them only if it would withdraw exactly N again: use it only once upstream is known to
be right.
"""

from django.core.management.base import BaseCommand, CommandError

from ingest import sync


class Command(BaseCommand):
    help = "Fetch DATAtourisme events for the covered area and write them as claims."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--allow-mass-withdrawal", type=int, metavar="N", default=None,
            help="Apply the N withdrawals the last partial run skipped (past the 20%% threshold), "
                 "for this run only, after checking upstream.",
        )

    def handle(self, *args, **options) -> None:
        allowed = options["allow_mass_withdrawal"]
        if allowed is not None:
            skipped = sync.skipped_last_partial()
            if skipped is None:
                raise CommandError("--allow-mass-withdrawal: the last partial run skipped no withdrawals")
            if allowed != skipped:
                raise CommandError(f"--allow-mass-withdrawal={allowed}: the last partial run skipped {skipped}")
        result = sync.run(allow_mass_withdrawal=allowed)
        self.stdout.write(result.summary())
        if result.status == "failed":
            raise CommandError("sync failed: no withdrawals applied")
