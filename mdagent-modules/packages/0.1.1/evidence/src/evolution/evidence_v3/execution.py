"""Execution boundary: freeze declared files, never execute a command here.

An injected action_adapter(spec, context) is trusted server configuration. It
must return supported=True, dependencies_complete=True, constraints_enforced=True
and kind equal to spec.kind after checking command effects, dependency closure,
resource limits, environment and read-only input enforcement. A client cannot
supply these attestations in spec. A launcher receives ONLY the frozen spec and
must hand off quickly, enforce that contract and return a durable launch_id.
"""
import hashlib
import os
import re
import uuid
from pathlib import Path, PureWindowsPath

from .contracts import V3Error, canonical_hash
from .store import clone

KINDS = {"production", "validation", "build", "analyze"}


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_spec(spec, *, require_live_cwd=True):
    if not isinstance(spec, dict):
        raise V3Error("INVALID_SPEC")
    allowed = {"kind", "command", "inputs", "cwd", "limits", "question_id", "environment",
               "review_id", "authorization_id"}
    if set(spec) - allowed:
        raise V3Error("INVALID_SPEC", "unknown fields")
    result = clone(spec)
    if result.get("kind") not in KINDS:
        raise V3Error("UNSUPPORTED", "action kind")
    command = result.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(s, str) or not s or "\0" in s for s in command):
        raise V3Error("INVALID_COMMAND")
    cwd = result.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or (require_live_cwd and not Path(cwd).is_dir()):
        raise V3Error("INVALID_CWD")
    result["cwd"] = str(Path(cwd).resolve())
    if not isinstance(result.get("limits"), dict) or not result["limits"]:
        raise V3Error("LIMITS_REQUIRED")
    environment = result.setdefault("environment", {})
    if not isinstance(environment, dict) or any(not isinstance(k, str) or not isinstance(v, str) or "\0" in k + v for k, v in environment.items()):
        raise V3Error("INVALID_ENVIRONMENT")
    inputs = result.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise V3Error("INPUTS_REQUIRED")
    seen = set()
    for entry in inputs:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise V3Error("INVALID_INPUT")
        raw = entry["path"]
        if not isinstance(raw, str) or not raw or "\\" in raw or ":" in raw or "\0" in raw:
            raise V3Error("INVALID_INPUT_PATH")
        path = Path(raw)
        if path.is_absolute() or PureWindowsPath(raw).is_absolute() or any(p in {"", ".", ".."} for p in raw.split("/")):
            raise V3Error("INVALID_INPUT_PATH", raw)
        if raw.casefold() in seen:
            raise V3Error("DUPLICATE_INPUT", raw)
        seen.add(raw.casefold())
        if not isinstance(entry["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", entry["sha256"]):
            raise V3Error("INVALID_INPUT_HASH")
    if result["kind"] == "validation" and not result.get("question_id"):
        raise V3Error("QUESTION_REQUIRED")
    canonical_hash(result)
    return result


def check_adapter(adapter, spec, context):
    if not callable(adapter):
        raise V3Error("UNSUPPORTED", "no trusted action adapter")
    try:
        result = adapter(clone(spec), clone(context))
    except V3Error:
        raise
    except Exception as exc:
        raise V3Error("INFRA_ERROR", "action adapter") from exc
    if not isinstance(result, dict) or result.get("kind") != spec["kind"] or any(
            result.get(key) is not True for key in ("supported", "dependencies_complete", "constraints_enforced")):
        raise V3Error("UNSUPPORTED", "adapter cannot enforce requested action")
    return clone(result)


def inspect_inputs(cwd, inputs):
    """Full UTF-8 input projection for the external review adapter, never authority."""
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not isinstance(inputs, list) or not inputs:
        raise V3Error("INPUTS_UNBOUND")
    root = Path(cwd).resolve()
    result, seen = [], set()
    for entry in inputs:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise V3Error("INVALID_INPUT")
        raw = entry["path"]
        if not raw or "\\" in raw or ":" in raw or Path(raw).is_absolute() or any(p in {"", ".", ".."} for p in raw.split("/")) or raw.casefold() in seen:
            raise V3Error("INVALID_INPUT_PATH", raw)
        seen.add(raw.casefold())
        source = root / raw
        current = source
        while current != root:
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise V3Error("UNSAFE_INPUT", raw)
            current = current.parent
        try:
            source.resolve().relative_to(root)
            payload = source.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if digest != entry.get("sha256"):
                raise V3Error("INPUT_CHANGED", raw)
            content = payload.decode("utf-8")
        except (ValueError, OSError) as exc:
            raise V3Error("INPUT_UNAVAILABLE", raw) from exc
        result.append({"path": raw, "sha256": digest, "content": content, "bytes": len(payload)})
    return result


def manifest_equal(left, right):
    if not isinstance(left, list) or not isinstance(right, list):
        return False
    try:
        a = {row["path"]: row["sha256"] for row in left}
        b = {row["path"]: row["sha256"] for row in right}
        return len(a) == len(left) and len(b) == len(right) and a == b
    except (KeyError, TypeError):
        return False


def freeze(store, task_id, permit_id, spec):
    """Copy bytes once and hash the copied bytes, closing source read races."""
    source_root = Path(spec["cwd"])
    destination = store._safe(store.task_dir(task_id) / "frozen" / permit_id)
    destination.mkdir(parents=True, exist_ok=False)
    for entry in spec["inputs"]:
        source = source_root / entry["path"]
        current = source
        while current != source_root:
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise V3Error("UNSAFE_INPUT", entry["path"])
            current = current.parent
        try:
            source.resolve().relative_to(source_root)
            if not source.is_file():
                raise V3Error("INPUT_UNAVAILABLE", entry["path"])
            payload = source.read_bytes()
        except (OSError, ValueError) as exc:
            raise V3Error("INPUT_UNAVAILABLE", entry["path"]) from exc
        if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
            raise V3Error("INPUT_CHANGED", entry["path"])
        target = store._safe(destination / entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        target.chmod(0o444)
    frozen = clone(spec)
    frozen["cwd"] = str(destination.resolve())
    output_dir = store._safe(store.task_dir(task_id) / "outputs" / permit_id)
    output_dir.mkdir(parents=True, exist_ok=False)
    frozen["output_dir"] = str(output_dir.resolve())
    frozen["inputs_read_only"] = True
    return frozen


def verify_frozen(store, task_id, permit):
    root = store._safe(store.task_dir(task_id) / "frozen" / permit["id"])
    spec = permit["frozen_spec"]
    if str(root.resolve()) != spec["cwd"]:
        raise V3Error("FROZEN_PATH_MISMATCH")
    expected = {entry["path"] for entry in spec["inputs"]}
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != expected:
        raise V3Error("FROZEN_INPUT_CHANGED")
    for entry in spec["inputs"]:
        path = store._safe(root / entry["path"])
        if not path.is_file() or file_hash(path) != entry["sha256"]:
            raise V3Error("FROZEN_INPUT_CHANGED", entry["path"])


def new_id(prefix):
    return prefix + "-" + uuid.uuid4().hex
