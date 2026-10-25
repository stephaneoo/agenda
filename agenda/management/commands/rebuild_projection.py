"""Rebuild the canonical Event and Occurrence tables from the claims.

    manage.py rebuild_projection

Safe to run at any time: unchanged rows are left as they are.
"""

from django.core.management.base import BaseCommand

from agenda.models import Event, Occurrence
from agenda.projection import rebuild_all


class Command(BaseCommand):
    help = "Rebuild the canonical tables from the claims."

    def handle(self, *args: object, **options: object) -> None:
        count = rebuild_all()
        self.stdout.write(
            f"rebuild_projection: {count} event key(s); "
            f"{Event.objects.count()} events, {Occurrence.objects.count()} occurrences"
        )
