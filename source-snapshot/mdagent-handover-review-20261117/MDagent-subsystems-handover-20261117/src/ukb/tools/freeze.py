"""Freeze the UKB package: write RELEASE.json (version, schema version, per-file sha256) deterministically.

  python tools/freeze.py [--check] [--version 1.0.0]

--check   verify the manifest still matches the tree (exit 1 on drift), do not rewrite.
Files are listed relative to the package root; __pycache__, *.pyc and RELEASE.json itself are excluded.
"""
import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache"}
SKIP_NAMES = {"RELEASE.json"}
SKIP_EXT = {".pyc", ".pyo"}


def _files():
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            if name in SKIP_NAMES or os.path.splitext(name)[1] in SKIP_EXT:
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            yield rel, path


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def build(version):
    files = {rel: _sha(path) for rel, path in _files()}
    schema = None
    schema_path = os.path.join(ROOT, "ukb", "core", "schema.sql")
    with open(schema_path, encoding="utf-8") as f:
        for line in f:
            if "schema_version" in line and "INSERT" in line:
                schema = line.split("'")[-2]
    return {
        "name": "ukb",
        "version": version,
        "status": "frozen",
        "frozen_for": "embedding into MDagent (local, temporary replacement of the legacy knowledge base)",
        "schema_version": schema,
        "python": ">=3.9",
        "dependencies": {"runtime": ["stdlib"], "optional": ["pymupdf or pypdf for PDF text", "numpy via text extractor only"]},
        "counts": {"files": len(files), "python": sum(1 for r in files if r.endswith(".py")),
                   "seed_types": len([r for r in files if r.startswith("data/seed/types/")]),
                   "seed_files": len([r for r in files if r.startswith("data/seed/") and not r.startswith("data/seed/types/")]),
                   "domain_packs": sorted({r.split("/")[2] for r in files if r.startswith("data/domain/")})},
        "entry_points": {"cli": "python -m ukb", "mcp": "python -m ukb.mcp_server --root <data> --agent <name>",
                         "review_console": "python -m ukb --root <data> serve", "api": "ukb.api.UKB"},
        "files": files,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    path = os.path.join(ROOT, "RELEASE.json")
    if a.check:
        if not os.path.exists(path):
            print("no RELEASE.json", file=sys.stderr)
            return 2
        old = json.load(open(path, encoding="utf-8"))
        new = build(old["version"])
        diff = {rel for rel in set(old["files"]) | set(new["files"]) if old["files"].get(rel) != new["files"].get(rel)}
        print(json.dumps({"ok": not diff, "changed": sorted(diff), "version": old["version"]}, ensure_ascii=False))
        return 0 if not diff else 1
    version = a.version
    if not version:
        if os.path.exists(path):
            version = json.load(open(path, encoding="utf-8"))["version"]
        else:
            version = "1.0.0"
    with open(os.path.join(ROOT, "VERSION"), "w", encoding="utf-8", newline="\n") as f:
        f.write(version + "\n")
    doc = build(version)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
    print(json.dumps({"ok": True, "version": version, "files": doc["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
