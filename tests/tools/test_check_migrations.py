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

    def write(self, name: str, source: str, *, commit: bool = True) -> None:
        (self.migrations / f"{name}.py").write_text(source)
        if commit:
            git(self.root, "add", "-A")
            git(self.root, "commit", "-qm", f"write {name}")

    def remove(self, name: str) -> None:
        git(self.root, "rm", "-q", f"shop/migrations/{name}.py")
        git(self.root, "commit", "-qm", f"remove {name}")

    def deploy(self) -> None:
        git(self.root, "tag", BASE_TAG)


def remove_price(ref: str, dependency: str = "0002_price_nullable") -> str:
    return f"""from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("shop", "{dependency}")]
    operations = [
        # contract-of: {ref}
        migrations.RemoveField(model_name="product", name="price"),
    ]
"""


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


@pytest.mark.django_db
def test_contract_of_unrelated_migration_fails(repo: Repo) -> None:
    # The initial migration created the field but is no expand step for its removal.
    repo.add("0001_initial", "0002_price_nullable")
    repo.deploy()
    repo.write("0003_remove_price", remove_price("shop.0001_initial"))
    assert migrations_of(check_migrations(BASE_TAG, repo.root)) == {"shop.0003_remove_price"}


@pytest.mark.django_db
def test_contract_of_expand_of_another_field_fails(repo: Repo) -> None:
    repo.add("0001_initial", "0002_add_note_nullable")
    repo.deploy()
    repo.write("0003_remove_price", remove_price("shop.0002_add_note_nullable", "0002_add_note_nullable"))
    assert migrations_of(check_migrations(BASE_TAG, repo.root)) == {"shop.0003_remove_price"}


@pytest.mark.django_db
def test_contract_of_other_app_fails(repo: Repo) -> None:
    repo.add("0001_initial", "0002_price_nullable")
    repo.deploy()
    repo.write("0003_remove_price", remove_price("auth.0002_price_nullable"))
    assert migrations_of(check_migrations(BASE_TAG, repo.root)) == {"shop.0003_remove_price"}


@pytest.mark.django_db
def test_contract_of_expand_leaving_field_not_null_fails(repo: Repo) -> None:
    repo.add("0001_initial", "0002_price_default")
    repo.deploy()
    repo.write("0003_remove_price", remove_price("shop.0002_price_default", "0002_price_default"))
    violations = check_migrations(BASE_TAG, repo.root)
    assert migrations_of(violations) == {"shop.0003_remove_price"}
    assert "nullable" in violations[0].message


@pytest.mark.django_db
def test_migration_file_without_number_is_linted(repo: Repo) -> None:
    repo.add("0001_initial")
    repo.deploy()
    shutil.copy(FIXTURES / "0002_remove_price_plain.py", repo.migrations / "remove_price.py")
    git(repo.root, "add", "-A")
    git(repo.root, "commit", "-qm", "unnumbered")
    assert migrations_of(check_migrations(BASE_TAG, repo.root)) == {"shop.remove_price"}


@pytest.mark.django_db
def test_contract_comment_on_other_operation_does_not_cover_breaking_one(repo: Repo) -> None:
    repo.add("0001_initial", "0002_price_nullable")
    repo.deploy()
    repo.write("0003_two_ops", """from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("shop", "0002_price_nullable")]
    operations = [
        # contract-of: shop.0002_price_nullable
        migrations.AddField(model_name="product", name="note", field=models.TextField(null=True)),
        migrations.RemoveField(model_name="product", name="price"),
    ]
""")
    violations = check_migrations(BASE_TAG, repo.root)
    assert any(v.migration == "shop.0003_two_ops" and v.operation == 1 for v in violations)


@pytest.mark.django_db
def test_editing_deployed_migration_fails(repo: Repo) -> None:
    repo.add("0001_initial")
    repo.deploy()
    path = repo.migrations / "0001_initial.py"
    path.write_text(path.read_text() + "# edited\n")
    git(repo.root, "commit", "-qam", "edit")
    assert migrations_of(check_migrations(BASE_TAG, repo.root)) == {"shop.0001_initial"}


@pytest.mark.django_db
def test_untracked_breaking_migration_fails(repo: Repo) -> None:
    repo.add("0001_initial")
    repo.deploy()
    shutil.copy(FIXTURES / "0002_remove_price_plain.py", repo.migrations / "0002_remove_price_plain.py")
    assert migrations_of(check_migrations(BASE_TAG, repo.root)) == {"shop.0002_remove_price_plain"}


@pytest.mark.django_db
def test_deleting_deployed_migration_fails(repo: Repo) -> None:
    repo.add("0001_initial", "0002_add_note_nullable")
    repo.deploy()
    repo.remove("0002_add_note_nullable")
    assert migrations_of(check_migrations(BASE_TAG, repo.root)) == {"shop.0002_add_note_nullable"}
