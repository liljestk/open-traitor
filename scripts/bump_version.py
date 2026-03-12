#!/usr/bin/env python3
"""
Bump the semantic version in VERSION file.

Usage:
    python scripts/bump_version.py patch   # 1.0.0 → 1.0.1
    python scripts/bump_version.py minor   # 1.0.1 → 1.1.0
    python scripts/bump_version.py major   # 1.1.0 → 2.0.0

The VERSION file is the single source of truth. src/__init__.py, server.py,
and package.json all read from it at runtime or build time.
"""

from __future__ import annotations

import json
import os
import re
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(REPO_ROOT, "VERSION")
PACKAGE_JSON = os.path.join(REPO_ROOT, "dashboard", "frontend", "package.json")

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def read_version() -> tuple[int, int, int]:
    with open(VERSION_FILE) as f:
        text = f.read().strip()
    m = VERSION_RE.match(text)
    if not m:
        print(f"ERROR: VERSION file contains invalid version: {text!r}", file=sys.stderr)
        sys.exit(1)
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def write_version(major: int, minor: int, patch: int) -> str:
    version = f"{major}.{minor}.{patch}"
    with open(VERSION_FILE, "w", newline="\n") as f:
        f.write(version + "\n")
    # Also sync package.json if it exists
    if os.path.isfile(PACKAGE_JSON):
        with open(PACKAGE_JSON) as f:
            pkg = json.load(f)
        pkg["version"] = version
        with open(PACKAGE_JSON, "w", newline="\n") as f:
            json.dump(pkg, f, indent=2)
            f.write("\n")
    return version


def bump(part: str) -> str:
    major, minor, patch = read_version()
    if part == "major":
        major += 1
        minor = 0
        patch = 0
    elif part == "minor":
        minor += 1
        patch = 0
    elif part == "patch":
        patch += 1
    else:
        print(f"ERROR: unknown bump part {part!r} — use major/minor/patch", file=sys.stderr)
        sys.exit(1)
    return write_version(major, minor, patch)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("major", "minor", "patch"):
        print("Usage: bump_version.py [major|minor|patch]", file=sys.stderr)
        sys.exit(1)
    new = bump(sys.argv[1])
    print(new)
