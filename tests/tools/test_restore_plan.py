"""The scripted restore (tools/restore.py, spec: Restore is scripted)."""

import re
import shlex

import pytest

from tools.restore import Args, execute, parse_args, plan

NOW = "20261103T120000Z"


def steps_for(**kw):
    return plan(Args(**kw), stamp=NOW)


def test_args_parse_target_set_and_test_only() -> None:
    a = parse_args(["--target-time", "2026-11-02T21:00:00+01:00", "--test-only"])
    assert a.target_time == "2026-11-02T21:00:00+01:00" and a.test_only and a.set is None
    assert parse_args(["--set", "20261101-010001F"]).set == "20261101-010001F"
    with pytest.raises(SystemExit):
        parse_args(["--target-time", "yesterday"])
    with pytest.raises(SystemExit):
        parse_args(["--set", "x; rm -rf /"])


@pytest.mark.parametrize("kw", [{}, {"target_time": "2026-11-02T21:00:00+01:00"}, {"set": "20261101-010001F"},
                                {"test_only": True}])
def test_restore_plan_never_targets_live_dataset(kw) -> None:
    steps = steps_for(**kw)
    names = [s.name for s in steps]
    swap = names.index("swap datasets") if "swap datasets" in names else len(steps)
    for s in steps[:swap]:
        # Before the swap, nothing writes the live dataset or its mountpoint.
        assert not re.search(r"tank/pg(?![-\w])(?!@)", s.run.replace("tank/pg@pre-restore", "")), s.name
        assert "/var/lib/postgresql" not in s.run or s.name == "snapshot the live dataset", s.name
    for s in steps:
        assert "zfs destroy" not in s.run or "tank/pg-restore" in s.run, s.name
        assert not re.search(r"zfs destroy[^\n]*tank/pg(?!-restore)", s.run), s.name
    restore = next(s for s in steps if s.name == "restore into the scratch dataset")
    assert "--pg1-path=/var/lib/pg-restore/16" in restore.run and "--archive-mode=off" in restore.run


def test_restore_plan_test_only_stops_after_sanity() -> None:
    steps = steps_for(test_only=True)
    names = [s.name for s in steps]
    assert names.index("sanity checks") == len(names) - 1 - sum(1 for s in steps if s.cleanup)
    assert all(s.cleanup for s in steps[names.index("sanity checks") + 1:])
    assert not any(w in s.run for s in steps for w in ("systemctl stop", "zfs rename", "zfs snapshot"))


def test_restore_plan_targets() -> None:
    restore = lambda **kw: next(s for s in steps_for(**kw) if s.name == "restore into the scratch dataset").run
    assert "--type=time" in restore(target_time="2026-11-02T21:00:00+01:00")
    assert "--target=2026-11-02T21:00:00+01:00" in shlex.split(restore(target_time="2026-11-02T21:00:00+01:00"))
    assert "--set=20261101-010001F" in restore(set="20261101-010001F") and "--type=immediate" in restore(set="20261101-010001F")
    assert "--type" not in restore()  # the latest: to the end of the WAL
    sanity = lambda **kw: next(s for s in steps_for(**kw) if s.name == "sanity checks").run
    assert shlex.split(sanity(target_time="2026-11-02T21:00:00+01:00"))[-2:] == ["--target", "2026-11-02T21:00:00+01:00"]


def test_restore_plan_includes_swap_back_on_failure() -> None:
    steps = steps_for()
    names = [s.name for s in steps]
    assert names[names.index("snapshot the live dataset"):] == [
        "snapshot the live dataset", "stop the app", "stop postgresql", "swap datasets",
        "start postgresql", "start the app", "check /healthz", "full backup on the new timeline",
    ]
    swap = steps[names.index("swap datasets")]
    assert "zfs rename tank/pg tank/pg-prev-" + NOW in swap.run
    assert "zfs rename tank/pg-prev-" + NOW + " tank/pg" in swap.undo

    ran: list[str] = []

    def runner(cmd: str) -> int:
        ran.append(cmd)
        return 1 if "healthz" in cmd else 0

    ok, log = execute(steps, runner)
    assert not ok
    cleanups = {s.run for s in steps if s.cleanup}
    undo = [c for c in ran[ran.index(next(c for c in ran if "healthz" in c)) + 1:] if c not in cleanups]
    # Undone in reverse: the app stopped, postgres stopped, datasets swapped back, both started.
    assert undo == [s.undo for s in reversed(steps[: names.index("check /healthz")]) if s.undo]
    assert any("tank/pg-prev-" + NOW + " tank/pg" in c for c in undo)
    assert "kept" in log[-1] or any("previous dataset" in l for l in log)


def test_a_failure_before_the_swap_only_cleans_up() -> None:
    steps = steps_for()
    ran: list[str] = []

    def runner(cmd: str) -> int:
        ran.append(cmd)
        return 1 if "check_restore" in cmd else 0

    ok, _ = execute(steps, runner)
    assert not ok
    assert not any("zfs rename" in c or "systemctl stop" in c for c in ran)


def test_the_backup_after_the_swap_never_swaps_back() -> None:
    steps = steps_for()
    ran: list[str] = []

    def runner(cmd: str) -> int:
        ran.append(cmd)
        return 1 if "agenda-backup" in cmd else 0

    ok, log = execute(steps, runner)
    assert ok  # restored and healthy; the backup failure is reported, not undone
    assert not any("tank/pg-prev-" + NOW + " tank/pg" in c for c in ran)
    assert any("backup" in l and "FAILED" in l for l in log)
