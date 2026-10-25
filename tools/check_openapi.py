"""Fails when the generated OpenAPI description differs from the committed api/openapi.json.

    python -m tools.check_openapi           compare
    python -m tools.check_openapi --write   regenerate api/openapi.json

Needs only the settings (no database).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

COMMITTED = Path(__file__).resolve().parent.parent / "api" / "openapi.json"


def generate() -> str:
    """The OpenAPI description, as stable text (sorted keys, two-space indent)."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agenda_site.settings")
    import django

    django.setup()
    from api.v1 import api

    schema = api.get_openapi_schema(path_prefix="/api/v1/")
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def check_openapi() -> bool:
    return COMMITTED.is_file() and COMMITTED.read_text(encoding="utf-8") == generate()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="regenerate api/openapi.json")
    if parser.parse_args().write:
        COMMITTED.write_text(generate(), encoding="utf-8")
        print(f"check_openapi: wrote {COMMITTED.name}")
        return 0
    ok = check_openapi()
    print("check_openapi: " + ("ok" if ok else "api/openapi.json differs from the generated spec; run `python -m tools.check_openapi --write`"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
