from pathlib import Path

import pytest
from django.core.management import call_command

GEO_FIXTURES = Path(__file__).parent / "geo" / "fixtures"


@pytest.fixture
def small_geo(db: None) -> None:
    """The small geo fixture: Nyons, Mirabel (10 min), Vinsobres (12), Valréas (30) covered."""
    call_command(
        "load_geo",
        matrix=str(GEO_FIXTURES / "travel-times.small.json"),
        communes=str(GEO_FIXTURES / "communes.small.geojson"),
    )
