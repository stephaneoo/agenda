import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.conf import settings
from django.test import override_settings

from tools.check_migrations import Violation, check_migrations

FIXTURES = Path(__file__).parent / "fixtures" / "migrations"
BASE_TAG = "deploy-20261025T000000Z"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.migrations = root / "shop" / "migrations"

    def add(self, *names: str) -> None:
        for name in names:
            shutil.copy(FIXTURES / f"{name}.py", self.migrations / f"{name}.py")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "add " + " ".join(names))

    def deploy(self) -> None:
        git(self.root, "tag", BASE_TAG)


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Repo]:
    root = tmp_path / "repo"
    (root / "shop" / "migrations").mkdir(parents=True)
    (root / "shop" / "__init__.py").touch()
    (root / "shop" / "migrations" / "__init__.py").touch()
    git(root, "init", "-q")
    sys.path.insert(0, str(root))
    try:
        with override_settings(INSTALLED_APPS=[*settings.INSTALLED_APPS, "shop"]):
            yield Repo(root)
    finally:
        sys.path.remove(str(root))
        for mod in [m for m in sys.modules if m == "shop" or m.startswith("shop.")]:
            del sys.modules[mod]


def migrations_of(violations: list[Violation]) -> set[str]:
    return {v.migration for v in violations}


@pytest.mark.django_db
def test_remove_field_without_contract_fails(repo: Repo) -> None:
    repo.add("0001_initial")
    repo.deploy()
    repo.add("0002_remove_price_plain")
    assert migrations_of(check_migrations(BASE_TAG, repo.root)) == {"shop.0002_remove_price_plain"}


@pytest.mark.django_db
def test_remove_field_with_contract_of_existing_expand_passes(repo: Repo) -> None:
    repo.add("0001_initial", "0002_price_nullable")
    repo.deploy()
    repo.add("0003_remove_price")
    assert check_migrations(BASE_TAG, repo.root) == []


@pytest.mark.django_db
def test_contract_of_expand_not_yet_deployed_fails(repo: Repo) -> None:
    repo.add("0001_initial")
    repo.deploy()
    repo.add("0002_price_nullable", "0003_remove_price")
    violations = check_migrations(BASE_TAG, repo.root)
    assert migrations_of(violations) == {"shop.0003_remove_price"}
    assert "shop.0002_price_nullable" in violations[0].message


@pytest.mark.django_db
def test_add_nullable_field_passes(repo: Repo) -> None:
    repo.add("0001_initial")
    repo.deploy()
    repo.add("0002_add_note_nullable")
    assert check_migrations(BASE_TAG, repo.root) == []


@pytest.mark.django_db
def test_add_not_null_without_default_fails(repo: Repo) -> None:
    repo.add("0001_initial")
    repo.deploy()
    repo.add("0002_add_sku_not_null")
    assert migrations_of(check_migrations(BASE_TAG, repo.root)) == {"shop.0002_add_sku_not_null"}
