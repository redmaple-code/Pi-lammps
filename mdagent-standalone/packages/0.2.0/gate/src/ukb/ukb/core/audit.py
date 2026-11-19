"""Append-only, hash-chained audit log."""
import json

from .canon import canonical_json, now, sha256_text

GENESIS = "0" * 64


def append(conn, actor, action, object_kind, object_id, detail=None):
    """Must be called inside a write transaction."""
    row = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else GENESIS
    rec = {
        "ts": now(), "actor": actor, "action": action,
        "object_kind": object_kind, "object_id": object_id, "detail": detail or {},
    }
    h = sha256_text(prev + canonical_json(rec))
    conn.execute(
        "INSERT INTO audit_log(ts,actor,action,object_kind,object_id,detail_json,prev_hash,hash) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (rec["ts"], actor, action, object_kind, object_id, canonical_json(rec["detail"]), prev, h))
    return h


def verify(conn):
    """Returns {"ok": bool, "count": n, "bad_seq": seq|None}."""
    prev = GENESIS
    n = 0
    for r in conn.execute("SELECT * FROM audit_log ORDER BY seq"):
        n += 1
        rec = {
            "ts": r["ts"], "actor": r["actor"], "action": r["action"],
            "object_kind": r["object_kind"], "object_id": r["object_id"],
            "detail": json.loads(r["detail_json"]),
        }
        if r["prev_hash"] != prev or sha256_text(prev + canonical_json(rec)) != r["hash"]:
            return {"ok": False, "count": n, "bad_seq": r["seq"]}
        prev = r["hash"]
    return {"ok": True, "count": n, "bad_seq": None}
