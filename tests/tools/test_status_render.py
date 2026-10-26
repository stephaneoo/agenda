"""The MOTD / `nix run .#status` report (agenda_site.statusfile), and the `agenda-status` CLI."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from agenda_site import statusfile

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def job(outcome: str, at: datetime, last_success: datetime | None, summary: str = "fine") -> dict:
    return {
        "outcome": outcome, "at": at.isoformat(), "summary": summary,
        "last_success": last_success.isoformat() if last_success else None,
    }


def write(tmp_path, jobs: dict) -> str:
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"version": 1, "jobs": jobs}))
    return str(path)


def line_for(report: str, name: str) -> str:
    [line] = [l for l in report.splitlines() if l.strip().startswith(name)]
    return line


def render(path: str, **kw) -> str:
    return statusfile.render(statusfile.read(path), now=NOW, system=kw.get("system", []))


def test_ok_job_shows_ok_and_its_summary(tmp_path) -> None:
    t = NOW - timedelta(hours=6)
    report = render(write(tmp_path, {"sync-datatourisme": job("ok", t, t, "clean: 3 added")}))
    line = line_for(report, "sync-datatourisme")
    assert " ok " in f" {line} " and "clean: 3 added" in line and "STALE" not in line


def test_partial_job_is_shown_as_partial_not_stale(tmp_path) -> None:
    t = NOW - timedelta(hours=6)
    report = render(write(tmp_path, {"sync-datatourisme": job("partial", t, t, "withdrawals skipped")}))
    line = line_for(report, "sync-datatourisme")
    assert "PARTIAL" in line and "STALE" not in line


def test_failed_job_shows_failed_and_last_success(tmp_path) -> None:
    ok, failed = NOW - timedelta(hours=20), NOW - timedelta(hours=1)
    report = render(write(tmp_path, {"pgbackrest-backup": job("failed", failed, ok, "S3 unreachable")}))
    line = line_for(report, "pgbackrest-backup")
    assert "FAILED" in line and "S3 unreachable" in line
    assert "STALE" not in line  # the last success is still within 26 h


@pytest.mark.parametrize(
    ("name", "age", "stale"),
    [
        ("sync-datatourisme", timedelta(hours=25), False),
        ("sync-datatourisme", timedelta(hours=27), True),
        ("pgbackrest-backup", timedelta(hours=27), True),
        ("restore-test", timedelta(days=31), False),
        ("restore-test", timedelta(days=33), True),
    ],
)
def test_stale_thresholds_from_the_spec(tmp_path, name, age, stale) -> None:
    t = NOW - age
    line = line_for(render(write(tmp_path, {name: job("ok", t, t)})), name)
    assert ("STALE" in line) is stale


def test_expected_job_never_run_is_stale(tmp_path) -> None:
    report = render(write(tmp_path, {}))
    for name in statusfile.EXPECTED:
        line = line_for(report, name)
        assert "STALE" in line and "never" in line


def test_missing_file_is_reported_and_every_job_stale(tmp_path) -> None:
    report = render(str(tmp_path / "absent.json"))
    assert "no status file" in report
    assert all("STALE" in line_for(report, name) for name in statusfile.EXPECTED)


def test_control_c1_and_bidi_characters_are_stripped(tmp_path) -> None:
    t = NOW - timedelta(hours=1)
    evil = "ok\x1b[2J‮gnp.exe\x9b31m⁦x\n"
    report = render(write(tmp_path, {"sync-datatourisme": job("ok", t, t, evil), "x‮y": job("ok", t, t)}))
    assert not any(c in report for c in "\x1b‮\x9b⁦")
    assert all(ord(c) >= 0x20 or c == "\n" for c in report)


def test_system_lines_are_appended(tmp_path) -> None:
    t = NOW - timedelta(hours=1)
    report = statusfile.render(
        statusfile.read(write(tmp_path, {})), now=NOW, system=["disk /: 10% used", "zpool tank: ONLINE\x1b"],
    )
    assert "disk /: 10% used" in report and "zpool tank: ONLINE" in report and "\x1b" not in report


def test_cli_record_then_show(tmp_path, capsys) -> None:
    path = tmp_path / "state" / "status.json"
    assert statusfile.main(["--file", str(path), "record", "pgbackrest-backup", "ok", "full backup 12 MB"]) == 0
    data = json.loads(path.read_text())
    assert data["jobs"]["pgbackrest-backup"]["outcome"] == "ok"
    assert statusfile.main(["--file", str(path), "show", "--no-system"]) == 0
    assert "full backup 12 MB" in capsys.readouterr().out


def test_cli_record_refuses_bad_outcome(tmp_path) -> None:
    assert statusfile.main(["--file", str(tmp_path / "s.json"), "record", "x", "fine", ""]) == 2


def test_django_status_record_writes_the_same_format(settings, tmp_path) -> None:
    from agenda_site import status

    settings.STATUS_FILE = str(tmp_path / "status.json")
    status.record("sync-datatourisme", "ok", "clean")
    line = line_for(statusfile.render(statusfile.read(settings.STATUS_FILE), now=datetime.now(UTC), system=[]),
                    "sync-datatourisme")
    assert "clean" in line and "STALE" not in line
