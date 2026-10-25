import json
from pathlib import Path

import pytest

from tests.conformance import occurrence_problems

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "api"
VALID = sorted((FIXTURES / "valid").glob("*.json"))
INVALID = sorted((FIXTURES / "invalid").glob("*.json"))
# The rule each invalid example of the spec breaks, as the checker words it.
BROKEN_RULE = {
    "end-before-start": "end: before start",
    "status-tentative": "status: 'tentative' is not one of",
    "rescheduled-without-previous-start": "previous_start: None is not of type 'string'",
    "timezone-utc": "timezone: 'Europe/Paris' was expected",
    "start-offset-not-paris": "start:",
    "sources-empty": "sources: [] should be non-empty",
    "location-missing": "'location' is a required property",
}


def test_every_invalid_example_of_the_spec_has_a_fixture() -> None:
    assert {p.stem for p in INVALID} == set(BROKEN_RULE)
    assert {p.stem for p in VALID} == {"minimal", "full"}


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_fixtures_pass_schema(path: Path) -> None:
    assert occurrence_problems(json.loads(path.read_text(encoding="utf-8"))) == []


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_fixtures_fail_schema(path: Path) -> None:
    problems = occurrence_problems(json.loads(path.read_text(encoding="utf-8")))
    assert problems
    assert any(BROKEN_RULE[path.stem] in p for p in problems), problems


def test_offset_must_match_paris_at_that_instant() -> None:
    obj = json.loads((FIXTURES / "valid" / "full.json").read_text(encoding="utf-8"))
    obj["start"] = "2026-11-14T21:00:00+02:00"  # summer offset in November
    assert any("offset" in p for p in occurrence_problems(obj))
