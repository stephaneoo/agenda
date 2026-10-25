"""Conformance of occurrence objects (spec, Conformance): the JSON Schema in
schemas/occurrence.schema.json, plus the two rules JSON Schema can't state: every local
date-time carries the offset in force in Europe/Paris at that instant, and end is not
before start."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "schemas" / "occurrence.schema.json").read_text(encoding="utf-8"))
Draft202012Validator.check_schema(SCHEMA)
_validator = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
PARIS = ZoneInfo("Europe/Paris")


def _parse(value: str) -> date | datetime:
    return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)


def _offset_problem(where: str, value: Any) -> str | None:
    if not isinstance(value, str) or len(value) == 10:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None  # the schema reports it
    if parsed.tzinfo is None or parsed.utcoffset() != parsed.astimezone(PARIS).utcoffset():
        return f"{where}: offset doesn't match Europe/Paris at that instant ({value})"
    return None


def _not_before(end: str, start: str) -> bool:
    e, s = _parse(end), _parse(start)
    if isinstance(e, datetime) and isinstance(s, datetime):
        return e >= s
    return (e.date() if isinstance(e, datetime) else e) >= (s.date() if isinstance(s, datetime) else s)


def occurrence_problems(obj: Any) -> list[str]:
    """Every rule the object breaks; empty when it conforms."""
    problems = [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in _validator.iter_errors(obj)]
    if problems or not isinstance(obj, dict):
        return problems
    dated = [("start", obj.get("start")), ("end", obj.get("end")), ("previous_start", obj.get("previous_start")),
             ("updated_at", obj.get("updated_at"))]
    dated += [(f"sources/{i}/updated_at", s.get("updated_at")) for i, s in enumerate(obj["sources"])]
    problems += [p for where, v in dated if (p := _offset_problem(where, v))]
    if obj.get("end") is not None and not _not_before(obj["end"], obj["start"]):
        problems.append("end: before start")
    return problems
