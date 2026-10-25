import json
from datetime import UTC, datetime

import pytest

from agenda_site import status


@pytest.fixture
def path(settings, tmp_path):
    settings.STATUS_FILE = str(tmp_path / "status.json")
    return tmp_path / "status.json"


def test_record_rewrites_one_job_and_keeps_the_others(path) -> None:
    t1 = datetime(2026, 10, 26, 5, 0, tzinfo=UTC)
    status.record("sync-datatourisme", "ok", "clean: 3 added", now=t1)
    status.record("pgbackrest-backup", "failed", "boom\nsecond line", now=t1)
    t2 = datetime(2026, 10, 27, 5, 0, tzinfo=UTC)
    status.record("sync-datatourisme", "failed", "fetch error", now=t2)
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert data["jobs"]["sync-datatourisme"] == {
        "outcome": "failed", "at": t2.isoformat(), "summary": "fetch error", "last_success": t1.isoformat(),
    }
    backup = data["jobs"]["pgbackrest-backup"]
    assert backup["summary"] == "boom second line" and backup["last_success"] is None
    assert sorted(p.name for p in path.parent.iterdir()) == ["status.json", "status.json.lock"]  # no temp file left


def test_record_replaces_a_corrupt_file_and_refuses_bad_outcomes(path) -> None:
    path.write_text("{not json")
    status.record("restore-test", "partial", "x" * 1000)
    job = json.loads(path.read_text())["jobs"]["restore-test"]
    assert job["outcome"] == "partial" and len(job["summary"]) <= status.MAX_SUMMARY
    assert job["last_success"] == job["at"]  # a partial run counts as a success
    with pytest.raises(ValueError):
        status.record("restore-test", "fine", "")
    with pytest.raises(ValueError):
        status.record("../etc", "ok", "")


# --- Batch C security review ---------------------------------------------------------------------

def test_a_deeply_nested_file_reads_as_corrupt(path) -> None:
    path.write_text("[" * 100_000 + "]" * 100_000)
    assert status.read() == {"version": 1, "jobs": {}}
    status.record("sync-datatourisme", "ok", "fine")
    assert json.loads(path.read_text())["jobs"]["sync-datatourisme"]["outcome"] == "ok"


def test_the_lock_is_never_followed_through_a_symlink(path, tmp_path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    (tmp_path / "status.json.lock").symlink_to(victim)
    with pytest.raises(OSError):
        status.record("sync-datatourisme", "ok", "fine")
    assert victim.read_text() == "keep me" and not path.exists()


def test_a_world_writable_directory_is_refused(settings, tmp_path, caplog) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    settings.STATUS_FILE = str(shared / "status.json")
    status.record("sync-datatourisme", "ok", "fine")
    assert not (shared / "status.json").exists() and not (shared / "status.json.lock").exists()
    assert "world-writable" in caplog.text


def test_the_directory_is_created_0750_and_the_lock_0600(settings, tmp_path) -> None:
    import os
    import stat

    old = os.umask(0o022)
    try:
        settings.STATUS_FILE = str(tmp_path / "state" / "status.json")
        status.record("sync-datatourisme", "ok", "fine")
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o750
    assert stat.S_IMODE((tmp_path / "state" / "status.json.lock").stat().st_mode) == 0o600


def test_the_summary_has_no_c1_or_bidi_controls(path) -> None:
    status.record("sync-datatourisme", "ok", "ok\x85 \x9b31m‮evil‬ ⁦x⁩ ‎y‏؜")
    summary = json.loads(path.read_text())["jobs"]["sync-datatourisme"]["summary"]
    assert summary == "ok 31m evil x y"
