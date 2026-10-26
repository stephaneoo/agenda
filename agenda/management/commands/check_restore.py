"""Sanity checks of a restored database (spec, Restore step 2; tools/restore.py runs it
against the temporary instance before anything is swapped):

- no applied migration the deployed code doesn't know (a backup newer than the code). Its
  migrations not applied yet are only a note: a backup older than the last deploy lacks them,
  and agenda-migrate applies them when the app starts (expand-only between deploys);
- the canonical tables (and the communes) are non-empty;
- the latest claim is no newer than the target (now, without --target), give or take the
  5 minutes a claim's seen_at may be ahead.

    manage.py check_restore [--target <RFC 3339>]
Exits non-zero, naming every failed check.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Max
from django.utils import timezone

from agenda.models import Event, Occurrence
from claims.models import Claim
from geo.models import Commune

SEEN_AT_SLACK = timedelta(minutes=5)


class Command(BaseCommand):
    help = "Sanity checks of a restored database."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--target", help="the restore's target time, RFC 3339 with offset")

    def handle(self, *args, target: str | None = None, **options) -> None:
        if target:
            try:
                when = datetime.fromisoformat(target)
            except ValueError:
                raise CommandError(f"--target: not an RFC 3339 date-time: {target!r}") from None
            if when.tzinfo is None:
                raise CommandError("--target needs a UTC offset")
        else:
            when = timezone.now()

        problems: list[str] = []
        executor = MigrationExecutor(connection)
        unapplied = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if unapplied:
            self.stdout.write(f"note: {len(unapplied)} migration(s) of the deployed code not applied yet; "
                              "agenda-migrate applies them when the app starts")
        unknown = sorted(set(executor.loader.applied_migrations) - set(executor.loader.disk_migrations))
        if unknown:
            problems.append("applied migrations the deployed code doesn't have: "
                            + ", ".join(f"{a}.{n}" for a, n in unknown[:10]))

        counts = {m.__name__: m.objects.count() for m in (Event, Occurrence, Commune)}
        problems += [f"{name} is empty" for name, n in counts.items() if n == 0]

        latest = Claim.objects.aggregate(latest=Max("seen_at"))["latest"]
        if latest is None:
            problems.append("Claim is empty")
        elif latest > when + SEEN_AT_SLACK:
            problems.append(f"the latest claim ({latest.isoformat()}) is newer than the target ({when.isoformat()})")

        summary = ", ".join(f"{n} {name}" for name, n in counts.items())
        if problems:
            raise CommandError("restore sanity checks failed: " + "; ".join(problems) + f" ({summary})")
        self.stdout.write(f"sanity checks passed: {summary}, latest claim {latest.isoformat()}, "
                          f"no migration unknown to the deployed code")
