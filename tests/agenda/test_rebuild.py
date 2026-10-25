"""rebuild_all carries on past a failing event (security review H1c), and event keys are
escaped in log lines (L8)."""

from __future__ import annotations

import logging

import pytest
from django.core.management import CommandError, call_command

from agenda import projection
from agenda.models import Event
from agenda.projection import rebuild_all
from claims.models import Claim, claims_writer
from tests.helpers import T0, import_event

pytestmark = pytest.mark.django_db

BAD = "datatourisme:bad\nINFO forged line"


@pytest.fixture
def three_events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Three events; reprojecting the second one fails."""
    import_event("datatourisme:a")
    import_event("datatourisme:c")
    with claims_writer():
        Claim.objects.create(target_type="event", target_key=BAD, event_key=BAD, field="name", value="x",
                             source_id="datatourisme", seen_at=T0)
    real = projection.project_event

    def flaky(key: str) -> None:
        if key == BAD:
            raise OverflowError("date value out of range")
        real(key)

    monkeypatch.setattr(projection, "project_event", flaky)
    with projection.projection_writer():
        Event.objects.all().delete()
    return ["datatourisme:a", BAD, "datatourisme:c"]


def test_rebuild_all_logs_each_failure_and_carries_on(three_events: list[str], caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="agenda.projection"):
        result = rebuild_all()
    assert result.failed == [BAD]
    assert result.count == 3
    assert set(Event.objects.values_list("key", flat=True)) == {"datatourisme:a", "datatourisme:c"}  # past the failure
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    message = record.getMessage()
    assert repr(BAD) in message
    assert "\n" not in message  # L8: one log line, whatever the key holds


def test_rebuild_command_exits_non_zero_with_the_failed_keys(three_events: list[str]) -> None:
    with pytest.raises(CommandError) as caught:
        call_command("rebuild_projection")
    assert caught.value.returncode != 0
    assert repr(BAD) in str(caught.value)
    assert "\n" not in str(caught.value)


def test_rebuild_command_succeeds_when_every_event_projects(capsys: pytest.CaptureFixture) -> None:
    import_event("datatourisme:a")
    call_command("rebuild_projection")
    assert "1 event key(s)" in capsys.readouterr().out


def test_moved_to_cycle_warning_escapes_the_key(caplog: pytest.LogCaptureFixture) -> None:
    key = "datatourisme:x\nERROR forged"
    a, b = f"{key}/2026-11-05", f"{key}/2026-11-12"
    with caplog.at_level(logging.WARNING, logger="agenda.projection"):
        projection._final_targets(key, {a: b, b: a}, {a, b})
    (record,) = caplog.records
    assert "\n" not in record.getMessage()
    assert repr(key) in record.getMessage()
