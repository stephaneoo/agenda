"""Fails unless every package in uv.lock comes from PyPI.

    python -m tools.check_lock_sources [uv.lock]

Every sdist, wheel or direct URL must start with https://files.pythonhosted.org/, the index
must be https://pypi.org/simple, and there may be no git or path sources besides the
project itself.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = "https://files.pythonhosted.org/"
INDEX = "https://pypi.org/simple"


def check_lock_sources(path: Path) -> list[str]:
    lock = tomllib.loads(path.read_text(encoding="utf-8"))
    problems = []
    for pkg in lock.get("package", []):
        label = f"{pkg.get('name')} {pkg.get('version')}"
        source = pkg.get("source", {})
        if source == {"virtual": "."} or source == {"editable": "."}:
            pass
        elif set(source) == {"registry"}:
            if source["registry"] != INDEX:
                problems.append(f"{label}: index {source['registry']}")
        elif "url" in source:
            if not source["url"].startswith(FILES):
                problems.append(f"{label}: direct URL {source['url']}")
        else:
            problems.append(f"{label}: source {source}")
        urls = [pkg["sdist"]["url"]] if "url" in pkg.get("sdist", {}) else []
        urls += [w["url"] for w in pkg.get("wheels", []) if "url" in w]
        problems += [f"{label}: {url}" for url in urls if not url.startswith(FILES)]
    return problems


def main(argv: list[str]) -> int:
    path = Path(argv[0]) if argv else ROOT / "uv.lock"
    problems = check_lock_sources(path)
    for p in problems:
        print(p, file=sys.stderr)
    print(f"check_lock_sources: {len(problems)} problem(s) in {path.name}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
