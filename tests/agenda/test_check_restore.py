"""`manage.py check_restore`: the restore's sanity checks (tools/restore.py, step 2)."""

from datetime import timedelta

import pytest
from django.core.management import CommandError, call_command

from tests.helpers import T0, import_event

pytestmark = pytest.mark.django_db


def test_an_empty_database_fails(small_geo) -> None:
    with pytest.raises(CommandError, match="Event.*empty"):
        call_command("check_restore")


def test_a_restored_database_with_data_passes(small_geo, capsys) -> None:
    import_event()
    call_command("check_restore", "--target", (T0 + timedelta(hours=1)).isoformat())
    out = capsys.readouterr().out
    assert "sanity checks passed" in out and "Event" in out


def test_a_claim_newer_than_the_target_fails(small_geo) -> None:
    import_event()
    with pytest.raises(CommandError, match="newer than the target"):
        call_command("check_restore", "--target", (T0 - timedelta(hours=1)).isoformat())


def test_an_unapplied_migration_fails(small_geo, monkeypatch) -> None:
    import_event()
    from django.db.migrations.executor import MigrationExecutor

    monkeypatch.setattr(MigrationExecutor, "migration_plan", lambda self, targets, clean_start=False: [("x", False)])
    with pytest.raises(CommandError, match="not applied"):
        call_command("check_restore")


def test_a_target_without_offset_is_refused(small_geo) -> None:
    with pytest.raises(CommandError, match="offset"):
        call_command("check_restore", "--target", "2026-11-02T21:00:00")
