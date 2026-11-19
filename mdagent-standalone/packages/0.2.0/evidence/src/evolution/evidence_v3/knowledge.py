"""Knowledge publication validation; all writes belong to apply_proposal.

TaskStore's generation projection serializes controlled publication and task
acceptance. This module neither calls a model nor maintains a knowledge store.
"""
import copy
import hashlib
import os
import uuid
from pathlib import Path

from .contracts import V3Error, canonical_hash, read_json, validate_id
from . import policy

CLASS_NAME = "evidence_v3_knowledge"
PACK_NAME = "evidence-v3-reviewed"


def _require(value, code):
    if not value:
        raise V3Error(code)


def publish(proposal, approver, data_root):
    """Use the registered existing application channel, never a local writer."""
    try:
        from ..kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.publish_knowledge(proposal, approver, data_root)
    import apply_proposal
    return apply_proposal.apply_knowledge_proposal(proposal, approver=approver,
                                                  data_root=data_root)


def prepare_publication(proposal, approver, directory):
    """Called only under the application channel's generation lock.

    Return a complete replacement package plus its prior state. Every existing
    row, including revoked rows, reserves its ID. Missing/empty runtime storage
    is a legitimate cold start; malformed JSON is a facility error.
    """
    _require(isinstance(proposal, dict), "invalid-knowledge-proposal")
    proposal = copy.deepcopy(proposal)
    canonical_hash(proposal)
    _require(isinstance(approver, str) and bool(approver.strip()), "knowledge-human-approval-required")
    _require(proposal.get("class") == CLASS_NAME and proposal.get("tier") == "human",
             "knowledge-registered-human-class-required")
    _require(proposal.get("op") in {"publish", "promote", "revoke", "supersede"}, "invalid-knowledge-operation")
    validate_id(proposal.get("id"))
    validation = policy.validate_promotion(proposal)
    _require(all(source.get("source_kind") in {"human", "publication", "measurement", "case", "experience", "estimate"}
                 for source in proposal["sources"]), "knowledge-source-kind-required")
    _require(proposal["review"]["actor_id"] == approver, "knowledge-approver-mismatch")
    # No arbitrary checker code or command can be registered by publication.
    method = proposal.get("verification_method")
    _require(isinstance(method, dict) and method.get("kind") in {"deterministic", "measurement", "semantic"},
             "knowledge-verification-method-required")
    if method["kind"] == "deterministic":
        _require(isinstance(method.get("checker_id"), str) and bool(method["checker_id"]),
                 "knowledge-checker-reference-required")
    _require(not ({"code", "command", "python", "shell"} & set(method)), "knowledge-executable-content-forbidden")
    content = proposal.get("content")
    _require(isinstance(content, dict) and isinstance(content.get("question_or_claim"), str) and
             bool(content["question_or_claim"].strip()), "knowledge-content-required")
    _require(proposal.get("owner_rule") in {"E1", "E2", "E3", "E4", "E5"}, "knowledge-owner-rule-required")
    directory = Path(directory)
    target = directory / "evidence_v3.json"
    rows, pack, occurrences = [], None, {}
    if directory.exists():
        _require(directory.is_dir(), "knowledge-directory-invalid")
        for path in sorted(directory.glob("*.json")):
            _require(not path.is_symlink(), "knowledge-unsafe-path")
            doc = read_json(path)
            _require(isinstance(doc, dict) and isinstance(doc.get("entries", []), list), "knowledge-package-corrupt")
            for row in doc.get("entries", []):
                _require(isinstance(row, dict) and isinstance(row.get("id"), str), "knowledge-entry-corrupt")
                occurrences.setdefault(row["id"], []).append(path)
            if path == target:
                _require(doc.get("schema_version") == 3 and doc.get("pack") == PACK_NAME and
                         doc.get("consumer") == "evidence", "knowledge-package-identity")
                pack = doc
                rows = copy.deepcopy(doc["entries"])
    # Reserve shipped IDs too; publication cannot shadow a candidate package.
    seeds = Path(__file__).resolve().parents[2] / "mcp" / "review_experience"
    if seeds.resolve() != directory.resolve() and seeds.is_dir():
        for path in sorted(seeds.glob("*.json")):
            doc = read_json(path)
            _require(isinstance(doc, dict), "knowledge-seed-package-corrupt")
            for row in doc.get("entries", []):
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    if row["id"] == proposal["id"] or row["id"] in occurrences:
                        raise V3Error("knowledge-duplicate-id")
    _require(not any(len(paths) > 1 for paths in occurrences.values()), "knowledge-duplicate-id")
    pack = copy.deepcopy(pack) if pack else {"schema_version": 3, "pack": PACK_NAME,
                                           "consumer": "evidence", "version": 0, "entries": []}
    old = next((x for x in rows if x["id"] == proposal["id"]), None)
    op = proposal["op"]
    if op == "publish":
        _require(proposal["id"] not in occurrences, "knowledge-duplicate-id")
        status = proposal.get("status")
        _require(status in {"canary", "active"}, "knowledge-invalid-status")
        version = 1
    else:
        _require(old is not None, "knowledge-entry-not-found")
        _require(type(proposal.get("expected_version")) is int and
                 proposal["expected_version"] == old.get("version"), "knowledge-version-conflict")
        _require(old.get("status") in {"canary", "active"}, "knowledge-entry-retired")
        # A status change must not smuggle new knowledge under a reused ID.
        _require(canonical_hash(proposal["content"]) == canonical_hash(old["content"]) and
                 canonical_hash(proposal["conditions"]) == canonical_hash(old["conditions"]) and
                 sorted(validation["independent_sources"]) == sorted(old["independent_sources"]) and
                 proposal["owner_rule"] == old["owner_rule"] and
                 canonical_hash(method) == canonical_hash(old["verification_method"]) and
                 proposal["kind"] == old["kind"] and
                 canonical_hash(proposal["sources"]) == canonical_hash(old["sources"]) and
                 sorted(set(proposal["source_refs"])) == sorted(set(old["source_refs"])),
                 "knowledge-transition-content-changed")
        _require(op != "promote" or old["status"] == "canary", "knowledge-already-active")
        status = {"promote": "active", "revoke": "revoked", "supersede": "superseded"}[op]
        if op == "supersede":
            replacement = proposal.get("superseded_by")
            _require(isinstance(replacement, str) and replacement != proposal["id"] and
                     replacement in {x["id"] for x in rows if x.get("status") == "active"},
                     "knowledge-replacement-required")
        version = old["version"] + 1
    entry = {"id": proposal["id"], "version": version, "kind": proposal["kind"],
             "title": content.get("title", content["question_or_claim"]),
             "question_or_claim": content["question_or_claim"], "content": content,
             "status": status, "lifecycle": status, "conditions": proposal["conditions"],
             "owner_rule": proposal["owner_rule"], "verification_method": method,
             "source_refs": validation["source_refs"], "sources": proposal["sources"],
             "independent_sources": validation["independent_sources"],
             "approval": copy.deepcopy(proposal["review"]),
             "provenance": {"source": "apply_proposal", "content_hash": validation["content_hash"]},
             "confirmations": proposal.get("confirmations", []),
             "limitations": validation["limitations"], "superseded_by": proposal.get("superseded_by"),
             "history": ([] if old is None else old.get("history", []) + [{
                 "version": old["version"], "status": old["status"], "approval": old["approval"],
                 "content_hash": canonical_hash(old)}])}
    rows = [entry if x["id"] == entry["id"] else x for x in rows]
    if old is None:
        rows.append(entry)
    pack["entries"] = rows
    pack["version"] += 1
    return {"path": target, "pack": pack, "entry": entry, "previous_entry": old,
            "proposal_hash": canonical_hash(proposal), "operation": op,
            "limitations": validation["limitations"]}


def _package_hash(path):
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_bytes(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp." + uuid.uuid4().hex)
    try:
        with open(temporary, "xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _journal_path(store, event_id):
    return store._safe(store.tasks / ".evidence-v3-publications" / (validate_id(event_id) + ".json"))


def _load_journal(store, event_id):
    from .contracts import file_hash
    journal = read_json(_journal_path(store, event_id))
    digest = journal.pop("journal_hash", None)
    _require(digest == canonical_hash(journal) and journal.get("id") == event_id,
             "PUBLICATION_JOURNAL_CORRUPT")
    journal["journal_hash"] = digest
    target = store._safe(store.root / journal["target"])
    _require(target == store.root / "memory" / "review_experience" / "evidence_v3.json",
             "PUBLICATION_TARGET_INVALID")
    payloads = {}
    for name in ("before", "after"):
        record = journal[name]
        if record["exists"]:
            path = store._safe(store.root / record["path"])
            _require(file_hash(path) == record["sha256"], "PUBLICATION_BACKUP_CORRUPT")
            payloads[name] = path.read_bytes()
        else:
            payloads[name] = None
    return journal, target, payloads


def _decision(store, event_id):
    path = _journal_path(store, event_id).with_suffix(".decision.json")
    return read_json(path) if path.exists() else None


def _terminal(generation, event_id):
    return next((e for e in generation.get("events", []) if e.get("transaction_id") == event_id), None)


def finalized_generation(store, generation, *, decision):
    """Called under the shared lock only by the explicit recovery operation."""
    event_id = generation.get("pending", {}).get("transaction_id")
    journal, target, _ = _load_journal(store, event_id)
    _require(generation["pending"].get("journal_hash") == journal["journal_hash"], "PUBLICATION_JOURNAL_CORRUPT")
    _require(decision in {"commit", "rollback"}, "PUBLICATION_DECISION_REQUIRED")
    pending_decision = generation["pending"].get("decision")
    _require(not pending_decision or (pending_decision.get("decision") == decision and
             pending_decision.get("journal_hash") == journal["journal_hash"]), "PUBLICATION_DECISION_CONFLICT")
    desired = journal["after" if decision == "commit" else "before"]
    _require(_package_hash(target) == desired["sha256"], "PUBLICATION_PACKAGE_MISMATCH")
    _require(generation["generation"] == journal["base_generation"], "PUBLICATION_GENERATION_CONFLICT")
    event = copy.deepcopy(journal["event"])
    event.update(transaction_id=event_id, generation=generation["generation"] + 1,
                 outcome=decision, recovery=pending_decision,
                 effective_changed_ids=(event["changed_ids"] if decision == "commit" else []))
    return {"generation": event["generation"], "status": "ready",
            "events": generation.get("events", []) + [event]}


def apply_prepared(store, prepared, approver):
    """Application-channel implementation. Journal and backups precede pending."""
    from .contracts import canonical_bytes
    from .store import _require_generation_lock, exclusive_json
    from . import store as storage
    _require_generation_lock(store)
    generation = store.generation()
    _require(generation["status"] == "ready", "GENERATION_PENDING")
    event_id = "knowledge-" + uuid.uuid4().hex
    target = store._safe(prepared["path"])
    old_bytes = target.read_bytes() if target.exists() else None
    new_bytes = canonical_bytes(prepared["pack"]) + b"\n"
    backup_dir = store._safe(store.root / "memory" / "backups" / "evidence_v3")
    staged = _journal_path(store, event_id).with_suffix(".package")
    staged.parent.mkdir(parents=True, exist_ok=True)
    with open(staged, "xb") as stream:
        stream.write(new_bytes)
        stream.flush()
        os.fsync(stream.fileno())
    before = {"exists": old_bytes is not None, "sha256": None, "path": None}
    if old_bytes is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = store._safe(backup_dir / (event_id + ".json"))
        with open(backup, "xb") as stream:
            stream.write(old_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        before.update(path=str(backup.relative_to(store.root)), sha256=hashlib.sha256(old_bytes).hexdigest())
    after = {"exists": True, "path": str(staged.relative_to(store.root)),
             "sha256": hashlib.sha256(new_bytes).hexdigest()}
    event = {"id": event_id, "kind": "knowledge", "entry_id": prepared["entry"]["id"],
             "operation": prepared["operation"], "approver": approver,
             "proposal_hash": prepared["proposal_hash"], "package_hash": canonical_hash(prepared["pack"]),
             "changed_ids": [prepared["entry"]["id"]],
             "changes": [{"id": prepared["entry"]["id"], "before": prepared.get("previous_entry"),
                           "after": prepared["entry"], "source": str(target)}]}
    journal = {"id": event_id, "base_generation": generation["generation"],
               "target": str(target.relative_to(store.root)), "before": before, "after": after,
               "event": event, "response": {"status": "applied", "entry": prepared["entry"],
               "event_id": event_id, "generation": generation["generation"] + 1, "path": str(target),
               "limitations": prepared["limitations"]}}
    journal["journal_hash"] = canonical_hash(journal)
    exclusive_json(_journal_path(store, event_id), journal)
    generation.update(status="pending", pending={"transaction_id": event_id, "journal_hash": journal["journal_hash"]})
    store.write_generation(generation)
    _atomic_bytes(target, new_bytes)
    # A ready-generation write is the irreversible commit decision. If the write
    # is interrupted before replacement the transaction remains pending and an
    # operator must inspect/recover it; no generic bump clears it.
    generation = store.generation()
    generation["pending"]["decision"] = {"decision": "commit", "approver": approver,
        "reason": "approved publication", "journal_hash": journal["journal_hash"]}
    store.write_generation(generation)
    storage.bump_generation(store.root, transaction_id=event_id, decision="commit")
    return copy.deepcopy(journal["response"])


def inspect_publication(data_root, event_id=None):
    from .store import TaskStore
    store = TaskStore(data_root)
    with store.generation_lock():
        generation = store.generation()
        paths = ([_journal_path(store, event_id)] if event_id else
                 sorted((store.tasks / ".evidence-v3-publications").glob("knowledge-*.json")))
        transactions = []
        for path in paths:
            if path.name.endswith(".decision.json"):
                continue
            journal, target, _ = _load_journal(store, path.stem)
            transactions.append({"id": journal["id"], "journal": journal,
                "current_package_hash": _package_hash(target), "decision": _decision(store, journal["id"]),
                "terminal": _terminal(generation, journal["id"])})
        return {"generation": generation, "transactions": transactions}


def recover_publication(data_root, event_id, decision, *, approver, reason):
    """Resume the same transaction; no replayed approval, clearing, or new publication."""
    from .store import TaskStore, exclusive_json
    from . import store as storage
    _require(decision in {"commit", "rollback"} and isinstance(approver, str) and approver.strip() and
             isinstance(reason, str) and reason.strip(), "PUBLICATION_RECOVERY_REASON_REQUIRED")
    store = TaskStore(data_root)
    with store.generation_lock():
        generation = store.generation()
        journal, target, payloads = _load_journal(store, event_id)
        terminal = _terminal(generation, event_id)
        if terminal:
            _require(terminal.get("outcome") == decision, "PUBLICATION_ALREADY_FINALIZED")
            return {"status": "applied" if decision == "commit" else "rolled_back",
                    "event_id": event_id, "generation": terminal["generation"]}
        _require(generation["generation"] == journal["base_generation"], "PUBLICATION_GENERATION_CONFLICT")
        pending = generation.get("pending", {})
        _require(generation["status"] == "ready" or pending.get("transaction_id") == event_id,
                 "PUBLICATION_TRANSACTION_CONFLICT")
        _require(_package_hash(target) in {journal["before"]["sha256"], journal["after"]["sha256"]},
                 "PUBLICATION_PACKAGE_MISMATCH")
        prior = pending.get("decision") if pending.get("transaction_id") == event_id else None
        if prior:
            _require(prior["decision"] == decision and prior.get("journal_hash") == journal["journal_hash"],
                     "PUBLICATION_DECISION_CONFLICT")
        else:
            pending_decision = {"decision": decision, "approver": approver, "reason": reason,
                                "journal_hash": journal["journal_hash"]}
            generation.update(status="pending", pending={"transaction_id": event_id,
                              "journal_hash": journal["journal_hash"], "decision": pending_decision})
            store.write_generation(generation)
        payload = payloads["after" if decision == "commit" else "before"]
        if payload is None:
            if target.exists():
                target.unlink()
        elif _package_hash(target) != (journal["after" if decision == "commit" else "before"]["sha256"]):
            _atomic_bytes(target, payload)
        number = storage.bump_generation(store.root, transaction_id=event_id, decision=decision)
        return {"status": "applied" if decision == "commit" else "rolled_back", "event_id": event_id,
                "generation": number}


def snapshot(data_root):
    """Complete knowledge projection, including retired rows, not a second store."""
    try:
        from ..kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.knowledge_snapshot(data_root)
    from .store import TaskStore
    store = TaskStore(data_root)
    directories = [Path(__file__).resolve().parents[2] / "mcp" / "review_experience",
                   store._safe(store.root / "memory" / "review_experience")]
    entries, packages = {}, []
    for directory in directories:
        for path in sorted(directory.glob("*.json")):
            _require(not path.is_symlink(), "KNOWLEDGE_SNAPSHOT_UNSAFE")
            doc = read_json(path)
            _require(isinstance(doc, dict), "KNOWLEDGE_SNAPSHOT_CORRUPT")
            packages.append({"source": str(path), "sha256": _package_hash(path)})
            rows = doc.get("entries", [])
            _require(isinstance(rows, list), "KNOWLEDGE_SNAPSHOT_CORRUPT")
            for row in rows:
                _require(isinstance(row, dict) and isinstance(row.get("id"), str), "KNOWLEDGE_SNAPSHOT_CORRUPT")
                _require(row["id"] not in entries, "KNOWLEDGE_SNAPSHOT_DUPLICATE")
                entries[row["id"]] = {"id": row["id"], "version": row.get("version", doc.get("version")),
                    "entry_hash": canonical_hash(row), "source": str(path), "conditions": row.get("conditions", {}),
                    "source_refs": row.get("source_refs", row.get("refs", [])),
                    "status": row.get("status", row.get("lifecycle")), "record": copy.deepcopy(row)}
    result = {"entries": entries, "packages": packages}
    result["hash"] = canonical_hash(result)
    return result
