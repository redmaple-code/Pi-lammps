"""Generate PACKAGE-MANIFEST.json for this handoff package.

Usage (from the package root):
    python tools/package_manifest.py
    python tools/package_manifest.py --upstream v3=../../MDagent-evidence-v3 \
                                     --upstream dual-gate=../../MDagent-src

Without --upstream the manifest still records every file hash; with it, files
whose bytes match an upstream path are tagged with their origin (and the
upstream's current commit when it is a git work tree).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
MANIFEST = PACKAGE / "PACKAGE-MANIFEST.json"
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".git"}
EXCLUDE_NAMES = {"PACKAGE-MANIFEST.json"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".bak")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for name in sorted(filenames):
            if name in EXCLUDE_NAMES or name.endswith(EXCLUDE_SUFFIX):
                continue
            path = Path(dirpath) / name
            yield path.relative_to(root).as_posix(), path


def upstream_commit(root: Path):
    try:
        proc = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=15)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", action="append", default=[],
                    metavar="LABEL=PATH", help="label an upstream work tree for provenance mapping")
    ap.add_argument("--docs-root", help="governance docs directory used for docs/ provenance")
    args = ap.parse_args()

    upstreams = []
    for spec in args.upstream:
        label, _, raw = spec.partition("=")
        if not raw:
            raise SystemExit("bad --upstream value: " + spec)
        root = Path(raw).resolve()
        upstreams.append((label, root, upstream_commit(root)))

    files = {}
    by_origin = {}
    for rel, path in walk(PACKAGE):
        digest = sha256(path)
        row = {"sha256": digest, "size": path.stat().st_size, "source": "handoff-package"}
        for label, root, commit in upstreams:
            candidate = root / rel[4:] if rel.startswith("src/") else root / rel
            if candidate.is_file() and sha256(candidate) == digest:
                row["source"] = label
                row["source_path"] = candidate.relative_to(root).as_posix()
                if commit:
                    row["source_commit"] = commit
                break
        if row["source"] == "handoff-package" and args.docs_root and rel.startswith("docs/"):
            candidate = Path(args.docs_root) / Path(rel).name
            if candidate.is_file() and sha256(candidate) == digest:
                row["source"] = "workspace-governance"
                row["source_path"] = ".project-to-act/docs/" + candidate.name
        files[rel] = row
        by_origin[row["source"]] = by_origin.get(row["source"], 0) + 1

    version = (PACKAGE / "VERSION").read_text(encoding="utf-8").splitlines()[0].strip()
    manifest = {
        "package": PACKAGE.name,
        "version": version,
        "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hash_algorithm": "sha256",
        "file_count": len(files),
        "total_bytes": sum(row["size"] for row in files.values()),
        "origins": by_origin,
        "upstreams": [{"label": label, "path": str(root), "commit": commit}
                      for label, root, commit in upstreams],
        "files": files,
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                        encoding="utf-8")
    print("wrote %s: %d files, %.2f MB, origins=%s" % (
        MANIFEST.name, len(files), manifest["total_bytes"] / 1024 / 1024, by_origin))
    return 0


if __name__ == "__main__":
    sys.exit(main())
