"""Canonical forms used for hashing, IDs and timestamps."""
import datetime
import hashlib
import json
import secrets
import time
import unicodedata


def nfc(s):
    return unicodedata.normalize("NFC", s)


def normalize(obj):
    """NFC + trim for every string; keeps structure. Only makes hashes stable, never rewrites meaning."""
    if isinstance(obj, str):
        return nfc(obj).strip()
    if isinstance(obj, dict):
        return {nfc(str(k)).strip(): normalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [normalize(v) for v in obj]
    return obj


def canonical_json(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def hash_obj(obj):
    return sha256_text(canonical_json(obj))


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix):
    return "%s%011x%s" % (prefix, int(time.time() * 1000), secrets.token_hex(3))


def alias_norm(s):
    return " ".join(nfc(s).casefold().split())
