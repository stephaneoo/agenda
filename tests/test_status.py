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
