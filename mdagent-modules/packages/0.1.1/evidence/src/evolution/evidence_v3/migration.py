"""Read-only legacy discovery and explicit, restartable task migration.

Legacy files are opaque history, never current evidence, runs or permissions.
The caller must select one exact source directory and supply a reviewed intent.
All new artifacts live below the destination TaskStore.tasks directory.
"""
import hashlib
import json
import os
from pathlib import Path
import uuid

from .contracts import V3Error, atomic_json, canonical_hash, read_json, validate_id
from .planning import validate_intent
from .store import TaskStore


_RUNNING = {"RUNNING", "STARTING", "START_UNKNOWN", "LEGACY_RUNNING", "PENDING", "QUEUED"}
_COMPLETED = {"COMPLETED", "COMPLETE", "DONE", "FINISHED"}


def _need(condition, code, detail=""):
    if not condition:
        raise V3Error(code, detail)


def _safe_source(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        _need(not part.is_symlink() and not (hasattr(part, "is_junction") and part.is_junction()),
              "MIGRATION_UNSAFE_SOURCE", str(part))
    return path.resolve()


def _files(directory):
    paths = []

    def fail(exc):
        raise V3Error("MIGRATION_SOURCE_UNREADABLE", str(exc.filename)) from exc

    for base, directories, filenames in os.walk(directory, followlinks=False, onerror=fail):
        for name in directories + filenames:
            path = Path(base) / name
            _safe_source(path)
        for name in filenames:
            path = Path(base) / name
            _need(path.is_file(), "MIGRATION_UNSAFE_SOURCE", str(path))
            paths.append(path)
    return sorted(paths, key=lambda path: path.relative_to(directory).as_posix())


def _json_bytes(data, path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _need(key not in result, "MIGRATION_INVALID_JSON", str(path))
            result[key] = value
        return result
    try:
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique)
        canonical_hash(value)
        return value
    except (ValueError, UnicodeError) as exc:
        raise V3Error("MIGRATION_INVALID_JSON", str(path)) from exc


def _snapshot(directory):
    _need(directory.is_dir(), "MIGRATION_SOURCE_NOT_DIRECTORY", str(directory))
    payload, manifest = {}, []
    try:
        for path in _files(directory):
            relative = path.relative_to(directory).as_posix()
            data = path.read_bytes()
            payload[relative] = data
            manifest.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(),
                             "bytes": len(data)})
    except OSError as exc:
        raise V3Error("MIGRATION_SOURCE_UNREADABLE", str(directory)) from exc
    _need(manifest, "MIGRATION_EMPTY_SOURCE")
    return payload, manifest


def _summarize(payload):
    documents = {}
    for name in ("state.json", "intent.json"):
        if name in payload:
            documents[name] = _json_bytes(payload[name], name)
            _need(isinstance(documents[name], dict), "MIGRATION_INVALID_DOCUMENT", name)
    _need(documents, "MIGRATION_SOURCE_SELECTION_REQUIRED")
    state = documents.get("state.json", {})
    if "state.json" in documents:
        _need(state.get("schema_version") == 1, "MIGRATION_NOT_LEGACY_SCHEMA")
    aliases = set()
    for document in documents.values():
        for key in ("task_id", "case_id", "project_id", "id", "name"):
            value = document.get(key)
            if isinstance(value, str) and value.strip():
                aliases.add(value)
    runs = []

    def visit(value, locator):
        if isinstance(value, dict):
            status = value.get("status", value.get("state"))
            if isinstance(status, str) and status.upper() in _RUNNING:
                runs.append({"locator": locator, "id": value.get("run_id", value.get("id")),
                             "original_status": status, "status": "LEGACY_RUNNING"})
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    visit(child, locator + "/" + str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, locator + "/" + str(index))

    for name, data in payload.items():
        if name.endswith(".json"):
            # Corrupt run/decision history is visible, never quietly omitted.
            value = documents.get(name) if name in documents else _json_bytes(data, name)
            visit(value, name)
        elif name.endswith(".jsonl"):
            for index, line in enumerate(data.splitlines()):
                if line.strip():
                    visit(_json_bytes(line, name), name + ":" + str(index + 1))
    status = state.get("status", state.get("state"))
    return {"schema_version": state.get("schema_version"), "aliases": sorted(aliases),
            "original_status": status,
            "legacy_completed": isinstance(status, str) and status.upper() in _COMPLETED,
            "running": runs,
            "run_status": "LEGACY_RUNNING" if runs else "NO_RUNNING_RECORD_FOUND",
            "historical_decisions_only": True, "revalidation_required": True}


def inventory(data_root, project_roots=None):
    """Read known task/case roots and explicit project roots without writing.

    Return candidates, ambiguities and errors. Every state/intent parent is a
    separate candidate; duplicate names/aliases are reported, never selected by
    time. schema-3 states are reported separately and are never migration inputs.
    Missing default roots are normal; missing explicit roots are errors.
    """
    root = Path(data_root).absolute()
    roots = [root / "memory" / "tasks", root / "memory" / "cases", root / "cases", root / "projects"]
    if (root / "state.json").exists() or (root / "intent.json").exists():
        roots.append(root)
    _need(project_roots is None or isinstance(project_roots, (list, tuple)), "MIGRATION_INVALID_ROOTS")
    explicit = [Path(path).absolute() for path in (project_roots or [])]
    roots.extend(explicit)
    candidates, current, errors, directories = [], [], [], set()
    for search_root in roots:
        try:
            search_root = _safe_source(search_root)
            if not search_root.exists():
                if search_root in explicit:
                    errors.append({"path": str(search_root), "error": "MIGRATION_ROOT_MISSING"})
                continue
            _need(search_root.is_dir(), "MIGRATION_ROOT_NOT_DIRECTORY", str(search_root))
            for path in _files(search_root):
                if path.name in {"state.json", "intent.json"}:
                    # Artifacts of a schema-3 task include preserved state files;
                    # only the task's own root is a live discovery candidate.
                    relative = path.relative_to(search_root)
                    if search_root == root / "memory" / "tasks" and len(relative.parts) != 2:
                        continue
                    directories.add(path.parent)
        except (V3Error, OSError) as exc:
            errors.append({"path": str(search_root), "error": getattr(exc, "code", "MIGRATION_READ_ERROR")})
    for directory in sorted(directories):
        try:
            state_path = directory / "state.json"
            if state_path.exists():
                state = read_json(state_path)
                if isinstance(state, dict) and state.get("schema_version") == 3:
                    current.append({"source_path": str(directory), "schema_version": 3})
                    continue
            payload, manifest = _snapshot(directory)
            summary = _summarize(payload)
            candidates.append(dict(summary, source_path=str(directory), name=directory.name,
                                   manifest=manifest, manifest_hash=canonical_hash(manifest)))
        except (V3Error, OSError) as exc:
            errors.append({"path": str(directory), "error": getattr(exc, "code", "MIGRATION_READ_ERROR")})
    aliases = {}
    for candidate in candidates:
        for alias in set([candidate["name"], *candidate["aliases"]]):
            aliases.setdefault(alias.casefold(), set()).add(candidate["source_path"])
    ambiguities = [{"alias": alias, "candidates": sorted(paths), "selection_required": True}
                   for alias, paths in sorted(aliases.items()) if len(paths) > 1]
    return {"status": "error" if errors else "ambiguous" if ambiguities else "ok",
            "candidates": candidates, "current": current, "ambiguities": ambiguities,
            "errors": errors, "read_only": True,
            "limitations": ["External legacy writers must be quiescent for a stable snapshot.",
                            "Only recorded statuses are reported; no process is contacted or restarted."]}


def _exclusive_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _verify_backup(store, task_id, journal):
    directory = store.task_dir(task_id) / journal["backup_directory"]
    store._safe(directory)
    expected = journal["manifest"]
    _, actual = _snapshot(directory)
    _need(actual == expected, "MIGRATION_BACKUP_MISMATCH")
    return directory


def _pristine(state, intent, create_request):
    _need(state.get("create_request") == {"id": create_request, "hash": canonical_hash(intent)},
          "MIGRATION_TASK_EXISTS")
    _need(state.get("schema_version") == 3 and state.get("current_stage") == "PLAN" and
          state.get("revision") == 1 and state.get("state_seq") == 1 and state.get("plan") is None and
          not state.get("decisions") and not state.get("dependencies") and
          all(not state.get(key) for key in ("evidence", "rule_results", "reviews", "findings", "authorizations",
                                            "receipts", "permits", "runs", "proofs", "active_receipts", "requests")),
          "MIGRATION_TASK_CHANGED")


def migrate(source_path, service, task_id, intent, request_id):
    """Create a fresh blocked PLAN task, retaining a byte-exact legacy backup.

    Source is an exact state/intent file or its directory, never an alias. Caller
    intent is mandatory. A durable migration journal precedes service.create;
    retries recover the same reserved creation only and never reset a progressed
    task. Immutable backups use O_EXCL, journals/state use atomic replacement.
    """
    validate_id(task_id)
    validate_id(request_id)
    _need(isinstance(intent, dict), "MIGRATION_INTENT_REQUIRED")
    intent = validate_intent(intent)
    _need(isinstance(getattr(service, "store", None), TaskStore), "MIGRATION_TASK_SERVICE_REQUIRED")
    store = service.store
    source = _safe_source(source_path)
    if source.is_file():
        _need(source.name in {"state.json", "intent.json"}, "MIGRATION_SOURCE_SELECTION_REQUIRED")
        source = source.parent
    _need(source.is_dir(), "MIGRATION_SOURCE_NOT_DIRECTORY", str(source))
    destination = store.task_dir(task_id)
    _need(not destination.is_relative_to(source) and not source.is_relative_to(destination),
          "MIGRATION_SOURCE_DESTINATION_OVERLAP")
    payload, manifest = _snapshot(source)
    summary = _summarize(payload)
    fingerprint = canonical_hash({"source_path": str(source), "manifest": manifest,
                                  "task_id": task_id, "intent": intent, "request_id": request_id})
    create_request = "migration-" + fingerprint
    journal_path = destination / "migration" / "journal.json"
    store._safe(journal_path)
    _need(journal_path.exists() or not (destination / "state.json").exists(), "MIGRATION_TASK_EXISTS")
    with store.transaction(task_id) as generation:
        _need(generation["status"] == "ready", "GENERATION_PENDING")
        journal = read_json(journal_path) if journal_path.exists() else None
        state = store.load(task_id, missing=True)
        if journal is not None:
            _need(journal.get("fingerprint") == fingerprint, "MIGRATION_REQUEST_CONFLICT")
            _verify_backup(store, task_id, journal)
            if state is not None and state.get("migration", {}).get("fingerprint") == fingerprint:
                # A replay must not reset new reviews, runs, or the task stage.
                return state
            if state is not None:
                _pristine(state, intent, create_request)
        else:
            _need(state is None, "MIGRATION_TASK_EXISTS")
            attempt = "migration/backups/" + uuid.uuid4().hex
            backup_directory = destination / attempt
            store._safe(backup_directory)
            for relative, data in payload.items():
                _exclusive_bytes(backup_directory / relative, data)
            # Snapshot all files again, not just the selected state document.
            _, fresh_manifest = _snapshot(source)
            _need(fresh_manifest == manifest, "MIGRATION_SOURCE_CHANGED")
            journal = {"schema_version": 3, "fingerprint": fingerprint, "source_path": str(source),
                       "task_id": task_id, "request_id": request_id, "create_request": create_request,
                       "manifest": manifest, "manifest_hash": canonical_hash(manifest),
                       "backup_directory": attempt, "summary": summary, "status": "PREPARED"}
            _verify_backup(store, task_id, journal)
            atomic_json(journal_path, journal)
    # create owns its own transaction; never nest the service's OS locks.
    service.create(task_id, intent, request_id=create_request)
    with store.transaction(task_id):
        state = store.load(task_id)
        journal = read_json(journal_path)
        _need(journal.get("fingerprint") == fingerprint, "MIGRATION_REQUEST_CONFLICT")
        if state.get("migration", {}).get("fingerprint") == fingerprint:
            return state
        _pristine(state, intent, create_request)
        _verify_backup(store, task_id, journal)
        _, fresh_manifest = _snapshot(source)
        _need(fresh_manifest == manifest, "MIGRATION_SOURCE_CHANGED")
        state["migration"] = {key: value for key, value in journal.items() if key != "status"}
        state["migration"]["status"] = "COMPLETED"
        state["migration"]["historical_only"] = True
        state["migration"]["revalidation_status"] = "REVALIDATION_REQUIRED"
        state["status"] = "REVALIDATION_REQUIRED"
        state["revalidation_required"] = True
        state["state_seq"] += 1
        # History is outside subject content and grants no receipt/permit/run.
        return store.commit(task_id, state)
