"""Copy the legacy atoms file from the MDagent server into this data root (read-only on the server).

  python tools/kb/pull_server_atoms.py --host <ssh-host> [--user root] [--remote /opt/mdagent/data]
  python tools/kb/pull_server_atoms.py --from-file D:/tmp/atoms.jsonl

Uses rsync over ssh (falls back to scp). The local copy lands at <data_root>/memory/atoms.jsonl, which is where
import_legacy.py looks by default. Nothing on the server is modified. Verify the file hash afterwards.
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "mcp"))

from evolution.kb import adapter  # noqa: E402


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root")
    ap.add_argument("--host", help="ssh host or alias")
    ap.add_argument("--user", default="root")
    ap.add_argument("--remote", default="/opt/mdagent/data")
    ap.add_argument("--file", default="memory/atoms.jsonl", help="path under --remote")
    ap.add_argument("--from-file", help="already copied local file")
    a = ap.parse_args()
    root = adapter.data_root(a.data_root)
    dest_dir = os.path.join(root, "memory")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "atoms.jsonl")
    if a.from_file:
        shutil.copyfile(a.from_file, dest)
    else:
        if not a.host:
            ap.error("give --host or --from-file")
        src = "%s@%s:%s/%s" % (a.user, a.host, a.remote.rstrip("/"), a.file)
        cmd = ["rsync", "-avz", src, dest]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr.decode("utf-8", "replace")[-800:])
            cmd = ["scp", src, dest]
            proc = subprocess.run(cmd, capture_output=True)
            if proc.returncode != 0:
                sys.stderr.write(proc.stderr.decode("utf-8", "replace")[-800:])
                return 1
    with open(dest, "rb") as f:
        lines = sum(1 for _ in f)
    print("atoms copied", {"path": dest, "lines": lines, "sha256": _sha(dest)})
    print("next: python tools/kb/import_legacy.py --dry-run   then import without --dry-run, then review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
