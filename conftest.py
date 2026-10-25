import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    if not os.environ.get("DATABASE_URL"):
        pytest.exit(
            "DATABASE_URL is not set. Run the tests through `nix develop -c pytest`, "
            "which starts an ephemeral PostGIS, or wrap them in postgis-fixture.",
            returncode=4,
        )
