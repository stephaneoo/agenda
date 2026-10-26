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


# --- Batch D review: system checks feed "needs attention" -----------------------------------------

def test_a_system_check_needing_attention_is_listed(tmp_path) -> None:
    t = NOW - timedelta(hours=1)
    jobs = {n: job("ok", t, t) for n in statusfile.EXPECTED}
    calm = statusfile.render(statusfile.read(write(tmp_path, jobs)), now=NOW,
                             system=[statusfile.Check("disk /: 10% used", None)])
    assert "all jobs ok" in calm
    worried = statusfile.render(statusfile.read(write(tmp_path, jobs)), now=NOW,
                                system=[statusfile.Check("disk /: 91% used", "disk / over 80%")])
    assert "needs attention: disk / over 80%" in worried and "all jobs ok" not in worried


@pytest.mark.parametrize(("used", "problem"), [(79.9, False), (80.1, True)])
def test_disk_over_80_percent(used, problem) -> None:
    assert (statusfile.disk_problem("/", used) is not None) is problem


@pytest.mark.parametrize(("health", "cap", "problem"), [("ONLINE", "10%", False), ("DEGRADED", "10%", True),
                                                         ("ONLINE", "85%", True), ("FAULTED", "1%", True)])
def test_pool_not_online_or_full(health, cap, problem) -> None:
    assert (statusfile.pool_problem("tank", health, cap) is not None) is problem


def archiver(**kw) -> dict:
    base = {"current_wal": "00000001000000000000000B", "last_archived_wal": "00000001000000000000000A",
            "last_archived_time": NOW - timedelta(minutes=1), "last_failed_time": None}
    return {**base, **kw}


def test_archiver_healthy_and_idle_are_fine() -> None:
    assert statusfile.archiver_problem(archiver(), NOW) is None
    # Idle database: nothing new to archive, however old the last archive is.
    assert statusfile.archiver_problem(archiver(last_archived_time=NOW - timedelta(days=2)), NOW) is None
    assert statusfile.archiver_problem(archiver(current_wal="00000001000000000000000A",
                                                last_archived_time=NOW - timedelta(days=2)), NOW) is None


def test_archiver_failing_since_its_last_success() -> None:
    p = statusfile.archiver_problem(archiver(last_failed_time=NOW - timedelta(seconds=30)), NOW)
    assert p and "failing" in p
    # A failure before the last success is history.
    assert statusfile.archiver_problem(archiver(last_failed_time=NOW - timedelta(hours=1)), NOW) is None


def test_archiver_behind_for_15_minutes() -> None:
    behind = archiver(current_wal="00000001000000000000000D", last_archived_time=NOW - timedelta(minutes=16))
    assert "behind" in statusfile.archiver_problem(behind, NOW)
    recent = archiver(current_wal="00000001000000000000000D", last_archived_time=NOW - timedelta(minutes=5))
    assert statusfile.archiver_problem(recent, NOW) is None
    never = archiver(current_wal="000000010000000000000003", last_archived_wal=None, last_archived_time=None)
    assert "behind" in statusfile.archiver_problem(never, NOW)


@pytest.mark.parametrize(("left", "problem"), [(timedelta(days=4), False), (timedelta(days=2, hours=23), True),
                                               (-timedelta(hours=1), True)])
def test_certificate_expiring_within_3_days(left, problem) -> None:
    assert (statusfile.cert_problem("51.15.79.107", NOW + left, NOW) is not None) is problem


def test_record_if_silent_only_writes_when_the_job_did_not(tmp_path) -> None:
    path = tmp_path / "s" / "status.json"
    start = datetime.now(UTC) - timedelta(minutes=5)
    statusfile.record(path, "pgbackrest-backup", "failed", "S3 unreachable")
    assert statusfile.main(["--file", str(path), "record-if-silent", "--since", str(int(start.timestamp())),
                            "pgbackrest-backup", "failed", "unit stopped: timeout"]) == 0
    assert json.loads(path.read_text())["jobs"]["pgbackrest-backup"]["summary"] == "S3 unreachable"
    later = datetime.now(UTC) + timedelta(minutes=1)
    assert statusfile.main(["--file", str(path), "record-if-silent", "--since", str(int(later.timestamp())),
                            "pgbackrest-backup", "failed", "unit stopped: timeout"]) == 0
    assert json.loads(path.read_text())["jobs"]["pgbackrest-backup"]["summary"] == "unit stopped: timeout"
