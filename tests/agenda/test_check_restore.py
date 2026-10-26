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


def test_an_unapplied_migration_is_a_note_not_a_failure(small_geo, monkeypatch, capsys) -> None:
    """A backup taken before the last deploy lacks its migrations: agenda-migrate applies them
    when the app starts on the restored data (expand-only between deploys)."""
    import_event()
    from django.db.migrations.executor import MigrationExecutor

    monkeypatch.setattr(MigrationExecutor, "migration_plan", lambda self, targets, clean_start=False: [("x", False)])
    call_command("check_restore")
    assert "1 migration(s) of the deployed code not applied yet" in capsys.readouterr().out


def test_an_applied_migration_the_code_does_not_know_fails(small_geo, monkeypatch) -> None:
    import_event()
    from django.db.migrations.loader import MigrationLoader

    real = MigrationLoader.build_graph

    def build_graph(self):
        real(self)
        self.applied_migrations = {**self.applied_migrations, ("agenda", "9999_from_the_future"): None}

    monkeypatch.setattr(MigrationLoader, "build_graph", build_graph)
    with pytest.raises(CommandError, match="9999_from_the_future"):
        call_command("check_restore")


def test_a_target_without_offset_is_refused(small_geo) -> None:
    with pytest.raises(CommandError, match="offset"):
        call_command("check_restore", "--target", "2026-11-02T21:00:00")
