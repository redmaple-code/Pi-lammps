"""Shared serialization and error contracts for task evidence."""
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

STAGES = ("PLAN", "BUILD", "CONFIG", "EXEC", "ANALYZE")
RULES = ("E1", "E2", "E3", "E4", "E5")
SCHEMA_VERSION = 3


class V3Error(ValueError):
    def __init__(self, code, detail=""):
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(self.code + (": " + self.detail if self.detail else ""))

    def as_dict(self):
        return {"error": self.code, "detail": self.detail, "fail_closed": True}


def _reject_constant(value):
    raise V3Error("invalid-json", value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise V3Error("duplicate-json-key", key)
        result[key] = value
    return result


def canonical_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise V3Error("invalid-json-value", str(exc)) from exc


def canonical_hash(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def validate_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise V3Error("invalid-id", str(value))
    if value.endswith((".", " ")) or value.split(".")[0].upper() in {
            "CON", "PRN", "AUX", "NUL", *("COM%d" % i for i in range(1, 10)),
            *("LPT%d" % i for i in range(1, 10))}:
        raise V3Error("invalid-id", value)
    return value


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream, parse_constant=_reject_constant,
                             object_pairs_hook=_unique_object)
    except V3Error:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise V3Error("unreadable-json", str(path)) from exc


def atomic_json(path, doc):
    payload = canonical_bytes(doc) + b"\n"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp." + uuid.uuid4().hex)
    try:
        with open(temporary, "xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def file_hash(path):
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise V3Error("source-unavailable", str(path)) from exc
    return digest.hexdigest()
