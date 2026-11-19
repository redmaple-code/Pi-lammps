"""Out-of-band human authority for evidence v3.

The MCP surface may *ask* for an authorization but can never grant one. Requests
are parked as data files; the owner answers them out of band through
``tools/human_approve.py``. Only a matching, unexpired, task-bound approval is
accepted; the service's immutable authorization record makes it single-use.

Trust boundary: on a single-user machine the agent can write the same file
system, so this is a confirmation gate, not a security boundary. Set
``MDSIM_HUMAN_INBOX_SECRET`` to require an HMAC-SHA256 signature whose secret
lives only in the owner's shell; for real isolation use separate OS users or an
external approval service.
"""
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path

MAX_AGE_SEC = 900
_ACTOR_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
_DECISION_KEYS = {"schema", "kind", "request_hash", "task_id", "decision",
                  "actor_id", "reason", "issued_at"}


def max_age_sec():
    raw = os.environ.get("MDSIM_HUMAN_INBOX_MAX_AGE", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return MAX_AGE_SEC
    return value if value > 0 else MAX_AGE_SEC


def inbox_root():
    from paths import data_root
    base = os.environ.get("MDSIM_HUMAN_INBOX") or os.path.join(data_root(), "human_inbox")
    return Path(base)


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
    os.replace(tmp, path)


def _read_json(path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def secret():
    value = os.environ.get("MDSIM_HUMAN_INBOX_SECRET", "")
    return value.encode("utf-8") if value else None


def sign(decision):
    key = secret()
    if key is None:
        return None
    body = json.dumps({k: v for k, v in decision.items() if k != "signature"},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def park(task_id, request, context):
    """Park a pending owner decision. Parking is not authority."""
    from evidence_v3.contracts import canonical_hash
    request_hash = canonical_hash(request)
    path = inbox_root() / "requests" / (request_hash + ".json")
    if path.exists():
        return path
    _atomic_json(path, {"schema": 1, "kind": "mdagent-authorization-request",
                        "request_hash": request_hash, "task_id": task_id,
                        "request": request, "context": context,
                        "parked_at": int(time.time())})
    return path


def pending(task_id=None):
    rows = []
    for path in sorted((inbox_root() / "requests").glob("*.json")):
        try:
            row = _read_json(path)
        except (OSError, ValueError):
            continue
        if task_id and row.get("task_id") != task_id:
            continue
        rows.append(row)
    rows.sort(key=lambda row: row.get("parked_at", 0), reverse=True)
    return rows


def verify(task_id, request, context):
    """Return a trusted-human-event attestation, or None. Never invent authority."""
    from evidence_v3.contracts import canonical_hash
    request_hash = canonical_hash(request)
    path = inbox_root() / "decisions" / (request_hash + ".json")
    if not path.is_file():
        park(task_id, request, context)
        return None
    try:
        decision = _read_json(path)
    except (OSError, ValueError):
        return None
    expected = set(_DECISION_KEYS) | ({"signature"} if secret() else set())
    if set(decision) != expected:
        return None
    if (decision.get("kind") != "mdagent-authorization-decision" or
            decision.get("request_hash") != request_hash or
            decision.get("task_id") != task_id or
            decision.get("decision") != "approve"):
        return None
    actor = decision.get("actor_id")
    if not isinstance(actor, str) or not _ACTOR_RE.match(actor):
        return None
    issued = decision.get("issued_at")
    if type(issued) is not int or abs(time.time() - issued) > max_age_sec():
        return None
    if not isinstance(decision.get("reason"), str) or not decision["reason"].strip():
        return None
    if secret() and not hmac.compare_digest(str(decision.get("signature")), str(sign(decision))):
        return None
    source_id = "human-inbox:" + request_hash
    return {"actor_id": actor, "source_refs": [source_id],
            "authorization_source": {"kind": "trusted_human_event",
                                     "source_id": source_id, "actor_id": actor,
                                     "request_hash": request_hash, "verified": True}}
