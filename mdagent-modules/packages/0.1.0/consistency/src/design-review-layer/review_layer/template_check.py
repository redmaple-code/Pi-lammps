"""Enforce the frozen design-review page template (v1.0).

Fails closed: hash mismatch or missing structural markers => non-zero exit.
Run before serving/releasing, and at server startup.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

LAYER = Path(__file__).resolve().parents[1]
MANIFEST = LAYER / "templates" / "manifest.json"


def check() -> tuple[bool, str]:
    if not MANIFEST.exists():
        return False, "template manifest missing: %s" % MANIFEST
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    p = LAYER / m["template_path"]
    if not p.exists():
        return False, "template file missing: %s" % p
    data = p.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if sha != m["sha256"]:
        return False, "template hash mismatch: expected %s, got %s" % (m["sha256"][:16], sha[:16])
    text = data.decode("utf-8", errors="replace")
    missing = [x for x in m.get("required_markers", []) if x not in text]
    if missing:
        return False, "missing required markers: %s" % ", ".join(missing)
    return True, "template v%s OK (sha256 %s)" % (m.get("version"), sha[:16])


if __name__ == "__main__":
    ok, msg = check()
    print(("PASS: " if ok else "FAIL: ") + msg)
    sys.exit(0 if ok else 1)
