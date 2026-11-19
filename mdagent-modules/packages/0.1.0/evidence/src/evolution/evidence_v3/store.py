"""Task snapshots, immutable artifacts and portable, ordered file locks.

Only state.json commits an artifact. Unreferenced files after a failed commit
are deliberately inert. The generation file is a synchronization projection,
not a knowledge store; external writers must join the same protocol.
"""
import contextlib
import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path

from .contracts import V3Error, atomic_json, canonical_hash, read_json, validate_id

try:
    import fcntl
except ImportError:
    fcntl = None
    import msvcrt

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()
_HELD = threading.local()


def clone(value):
    canonical_hash(value)
    return copy.deepcopy(value)


def _thread_lock(path):
    key = os.path.normcase(str(path.resolve()))
    with _LOCKS_GUARD:
        return key, _LOCKS.setdefault(key, threading.RLock())


@contextlib.contextmanager
def exclusive_lock(path):
    """RLock then flock/msvcrt; recursion never acquires a second OS lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key, lock = _thread_lock(path)
    with lock:
        held = getattr(_HELD, "paths", set())
        if key in held:
            raise V3Error("NESTED_LOCK", str(path))
        _HELD.paths = held
        with open(path, "a+b") as stream:
            if path.is_symlink():
                raise V3Error("UNSAFE_PATH", str(path))
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            else:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                while True:
                    try:
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.02)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                else:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def exclusive_json(path, value):
    """O_EXCL is mandatory for immutable artifacts and backups."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    try:
        with open(path, "xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise V3Error("IMMUTABLE_ARTIFACT", str(path)) from exc


@contextlib.contextmanager
def generation_lock(data_root):
    """Shared knowledge/authority lock. Acquire before any task lock."""
    store = TaskStore(data_root)
    with store.generation_lock():
        yield


def read_generation(data_root):
    """Read while holding generation_lock for a consistent publication boundary."""
    return TaskStore(data_root).generation()["generation"]


def _require_generation_lock(store):
    path = store.tasks / ".evidence-v3-generation.lock"
    key = os.path.normcase(str(path.resolve()))
    if key not in getattr(_HELD, "paths", set()):
        raise V3Error("GENERATION_LOCK_REQUIRED")


def bump_generation(data_root, *, transaction_id=None, decision=None):
    store = TaskStore(data_root)
    _require_generation_lock(store)
    doc = store.generation()
    if doc.get("pending", {}).get("transaction_id"):
        # A controlled publication can only finish its named transaction after
        # the complete journal and exact package have been verified.
        if transaction_id != doc["pending"]["transaction_id"] or decision not in {"commit", "rollback"}:
            raise V3Error("PUBLICATION_RECOVERY_REQUIRED")
        doc["pending"]["decision"] = {"decision": decision, "journal_hash": doc["pending"].get("journal_hash")}
        store.write_generation(doc)
        from .knowledge import finalized_generation
        doc = finalized_generation(store, doc, decision=decision)
    else:
        if doc.get("status") != "ready":
            raise V3Error("GENERATION_RECOVERY_REQUIRED")
        doc["generation"] += 1
        doc["status"] = "ready"
        doc.setdefault("events", []).append({"id": "opaque-" + uuid.uuid4().hex,
            "kind": "opaque", "generation": doc["generation"]})
    store.write_generation(doc)
    return doc["generation"]


def mark_generation_pending(data_root):
    """Compatibility API cannot create an unjournaled pending generation."""
    store = TaskStore(data_root)
    _require_generation_lock(store)
    doc = store.generation()
    if doc.get("status") != "ready" or doc.get("pending"):
        raise V3Error("GENERATION_PENDING")
    raise V3Error("PUBLICATION_JOURNAL_REQUIRED")


class TaskStore:
    def __init__(self, data_root):
        if not data_root:
            raise V3Error("DATA_ROOT_REQUIRED")
        self.root = Path(data_root).absolute().resolve()
        self.tasks = self.root / "memory" / "tasks"
        self._safe(self.tasks)

    def _safe(self, path):
        path = Path(path)
        try:
            path.absolute().relative_to(self.root)
            path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise V3Error("UNSAFE_PATH", str(path)) from exc
        current = path
        while current != self.root:
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise V3Error("UNSAFE_PATH", str(current))
            current = current.parent
        return path

    def task_dir(self, task_id):
        validate_id(task_id)
        path = self._safe(self.tasks / task_id)
        # Case-insensitive filesystems must never alias two stable identities.
        if self.tasks.exists():
            for sibling in self.tasks.iterdir():
                if sibling.name.casefold() == task_id.casefold() and sibling.name != task_id:
                    raise V3Error("TASK_ID_COLLISION", task_id)
        return path

    @contextlib.contextmanager
    def generation_lock(self):
        path = self._safe(self.tasks / ".evidence-v3-generation.lock")
        with exclusive_lock(path):
            yield self.generation()

    @contextlib.contextmanager
    def transaction(self, task_id):
        # Every controlled task operation observes the same lock order.
        with self.generation_lock() as generation:
            directory = self.task_dir(task_id)
            with exclusive_lock(self._safe(directory / "state.lock")):
                yield generation

    def generation(self):
        path = self._safe(self.tasks / ".evidence-v3-generation.json")
        if not path.exists():
            return {"generation": 0, "status": "ready", "events": []}
        doc = read_json(path)
        if not isinstance(doc, dict) or type(doc.get("generation")) is not int or doc.get("status") not in {"ready", "pending"}:
            raise V3Error("GENERATION_CORRUPT")
        return doc

    def write_generation(self, value):
        atomic_json(self._safe(self.tasks / ".evidence-v3-generation.json"), value)

    def load(self, task_id, *, writable=True, missing=False):
        path = self._safe(self.task_dir(task_id) / "state.json")
        if not path.exists():
            if missing:
                return None
            raise V3Error("TASK_NOT_FOUND", task_id)
        doc = read_json(path)
        if not isinstance(doc, dict):
            raise V3Error("STATE_CORRUPT", task_id)
        if doc.get("schema_version") != 3:
            if writable:
                raise V3Error("REVALIDATION_REQUIRED", task_id)
            return dict(doc, read_only=True, revalidation_required=True)
        if doc.get("task_id") != task_id:
            raise V3Error("TASK_ID_MISMATCH", task_id)
        if any(type(doc.get(k)) is not int or doc[k] < 1 for k in ("revision", "state_seq")):
            raise V3Error("STATE_CORRUPT", task_id)
        return doc

    def artifact(self, task_id, category, value):
        validate_id(category)
        artifact_id = validate_id(value["id"])
        path = self._safe(self.task_dir(task_id) / "artifacts" / category / (artifact_id + ".json"))
        exclusive_json(path, value)
        return path

    def verify_artifact(self, task_id, category, value):
        path = self._safe(self.task_dir(task_id) / "artifacts" / category / (validate_id(value["id"]) + ".json"))
        if canonical_hash(read_json(path)) != canonical_hash(value):
            raise V3Error("ARTIFACT_MISMATCH", value["id"])

    def commit(self, task_id, state):
        path = self._safe(self.task_dir(task_id) / "state.json")
        canonical_hash(state)
        if path.exists():
            backup = self._safe(path.parent / "backups" / ("state-" + uuid.uuid4().hex + ".json"))
            backup.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "rb") as source, open(backup, "xb") as target:
                target.write(source.read())
                target.flush()
                os.fsync(target.fileno())
        atomic_json(path, state)
        return clone(state)
