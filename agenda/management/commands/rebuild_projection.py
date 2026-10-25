"""Rebuild the canonical Event and Occurrence tables from the claims.

    manage.py rebuild_projection

Safe to run at any time: unchanged rows are left as they are. An event that fails is
logged and skipped; the command then exits non-zero and names the failed keys.
"""

from django.core.management.base import BaseCommand, CommandError

from agenda.models import Event, Occurrence
from agenda.projection import rebuild_all


class Command(BaseCommand):
    help = "Rebuild the canonical tables from the claims."

    def handle(self, *args: object, **options: object) -> None:
        result = rebuild_all()
        self.stdout.write(
            f"rebuild_projection: {result.count} event key(s); "
            f"{Event.objects.count()} events, {Occurrence.objects.count()} occurrences"
        )
        if result.failed:
            # repr(): one line, whatever the keys hold.
            raise CommandError(f"{len(result.failed)} event(s) failed: {', '.join(map(repr, result.failed))}")
