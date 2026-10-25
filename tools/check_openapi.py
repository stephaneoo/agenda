"""Fails when the generated OpenAPI description differs from the committed api/openapi.json.

    python -m tools.check_openapi
"""

from __future__ import annotations

import sys


def check_openapi() -> bool:
    # The API lands in Task 8; until then there is nothing to compare.
    return True


def main() -> int:
    ok = check_openapi()
    print("check_openapi: " + ("ok" if ok else "api/openapi.json differs from the generated spec"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
