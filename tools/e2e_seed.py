"""Seeds the e2e database: the small area and the shared filter cases' occurrences.

Run as `python -m tools.e2e_seed` by tools/e2e_offline.py, with the app's venv, against the postgis-fixture database.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agenda_site.settings")
django.setup()

from django.core.management import call_command  # noqa: E402

from claims.models import Source  # noqa: E402
from tests.helpers import import_event  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests/fixtures/filter-cases.json").read_text(encoding="utf-8"))
SYNCED = datetime(2026, 10, 31, 5, 2, tzinfo=UTC)

call_command("migrate", verbosity=0)
call_command(
    "load_geo",
    matrix=str(ROOT / "tests/geo/fixtures/travel-times.small.json"),
    communes=str(ROOT / "tests/geo/fixtures/communes.small.geojson"),
)
for o in FIXTURE["occurrences"]:
    import_event(o["event_key"], dates=[o["dates"]], category=o["category"], loc=o["location"], name=o["name"])
Source.objects.filter(name="datatourisme").update(last_run_at=SYNCED, last_success_at=SYNCED, last_outcome="ok")
print(f"e2e seed: {len(FIXTURE['occurrences'])} occurrences")
