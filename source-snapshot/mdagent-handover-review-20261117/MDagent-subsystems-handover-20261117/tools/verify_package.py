"""Verify this handoff package against PACKAGE-MANIFEST.json.

Usage (from the package root):
    python tools/verify_package.py [--strict-extra]

Checks:
  * every manifest file exists and its sha256 matches;
  * optionally, no unexpected files exist (cache/backup files are ignored).
Exit code 0 = intact, 1 = mismatch/missing, 2 = manifest missing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
MANIFEST = PACKAGE / "PACKAGE-MANIFEST.json"
IGNORE_DIRS = {"__pycache__", ".pytest_cache", ".git"}
IGNORE_SUFFIX = (".pyc", ".pyo", ".bak")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict-extra", action="store_true",
                    help="fail when files exist outside the manifest")
    args = ap.parse_args()
    if not MANIFEST.is_file():
        print("manifest missing:", MANIFEST, file=sys.stderr)
        return 2
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = manifest["files"]
    problems = []

    for rel, row in sorted(expected.items()):
        path = PACKAGE / rel
        if not path.is_file():
            problems.append("MISSING %s" % rel)
            continue
        actual = sha256(path)
        if actual != row["sha256"]:
            problems.append("CHANGED %s (expected %s, got %s)" % (rel, row["sha256"][:12], actual[:12]))

    if args.strict_extra:
        for dirpath, dirnames, filenames in os.walk(PACKAGE):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
            for name in filenames:
                if name == MANIFEST.name or name.endswith(IGNORE_SUFFIX):
                    continue
                rel = (Path(dirpath) / name).relative_to(PACKAGE).as_posix()
                if rel not in expected:
                    problems.append("EXTRA %s" % rel)

    if problems:
        for row in problems:
            print(row)
        print("\n%d problem(s)" % len(problems))
        return 1
    print("OK: %d files match %s (version %s)" % (
        len(expected), MANIFEST.name, manifest.get("version", "?")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
