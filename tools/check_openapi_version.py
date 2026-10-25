"""Fails when api/openapi.json changed since the last deploy while info.version didn't.

    python -m tools.check_openapi_version [--base REF]

The baseline is the latest deploy-* tag (as for the migration check). With no tag, nothing
is deployed yet: the check compares with the root commit, which has no spec, and passes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from tools.check_migrations import DEPLOY_TAG, default_base_ref, resolve_base

SPEC = "api/openapi.json"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def check_openapi_version(repo: Path, base: str | None = None) -> tuple[bool, str]:
    """(ok, message)."""
    base = base or default_base_ref(repo)
    commit = resolve_base(repo, base)
    tagged = bool(DEPLOY_TAG.match(base))
    where = base if tagged else f"{commit[:12]} (no deploy-* tag: nothing deployed yet)"
    deployed = _git(repo, "show", f"{commit}:{SPEC}")
    if deployed.returncode != 0:
        return True, f"check_openapi_version: no {SPEC} at {where}, nothing to compare"
    current = (repo / SPEC).read_text(encoding="utf-8")
    if deployed.stdout == current:
        return True, f"check_openapi_version: {SPEC} unchanged since {where}"
    old, new = json.loads(deployed.stdout)["info"]["version"], json.loads(current)["info"]["version"]
    if old == new:
        return False, f"check_openapi_version: {SPEC} changed since {where} but info.version is still {new}: bump it"
    return True, f"check_openapi_version: {SPEC} changed since {where}, info.version {old} -> {new}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", help="git ref of the last deploy (default: latest deploy-* tag)")
    args = parser.parse_args()
    repo = Path(_git(Path.cwd(), "rev-parse", "--show-toplevel").stdout.strip())
    try:
        ok, message = check_openapi_version(repo, args.base)
    except ValueError as e:
        parser.error(str(e))
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
