#!/usr/bin/env python3
"""Validate compatibility-facade retirement policy."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from architecture.compatibility import (  # noqa: E402
    PROJECT_ROOT,
    audit_repository,
    facade_summary,
    render_summary,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    issues = audit_repository(root)
    print(render_summary(facade_summary(root)))
    if issues:
        print("\nCompatibility facade violations:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
