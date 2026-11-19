"""Single schema-3 task authority; adapters supply reasoning and execution.

planning/policy implement semantic contracts. This service computes identities,
CAS, receipt chains and execution ownership; no caller-supplied verdict is a gate.
All controlled mutations share generation->task locking. Direct external writes
remain outside this protocol. Legacy snapshots are deliberately read-only.
"""
import contextlib
import importlib
import time

from .contracts import STAGES, V3Error, canonical_hash, validate_id
from . import execution
from .store import TaskStore, clone

_RECEIPT_KINDS = {"PLAN": "PLAN_REVIEW", "BUILD": "BUILD_CHECK", "CONFIG": "INPUT_REVIEW",
                  "EXEC": "EXEC_REVIEW", "ANALYZE": "RESULT_REVIEW"}
_SUBJECT_KEYS = ("intent", "plan", "evidence", "decisions", "dependencies")


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _call(module, name, *args):
    try:
        function = getattr(importlib.import_module("." + module, __package__), name)
        return function(*args)
    except V3Error:
        raise
    except Exception as exc:
        raise V3Error("INFRA_ERROR", module + "." + name) from exc


def _items(plan):
    if not isinstance(plan, dict):
        return []
    return plan.get("items", plan.get("obligations", []))


class TaskService:
    def __init__(self, data_root, *, action_adapter=None, result_verifier=None, knowledge_reviewer=None,
                 human_verifier=None, atom_writer=None):
        self.store = TaskStore(data_root)
        self.action_adapter = action_adapter
        self.result_verifier = result_verifier
        self.knowledge_reviewer = knowledge_reviewer
        self.human_verifier = human_verifier
        self.atom_writer = atom_writer

    @contextlib.contextmanager
    def _knowledge_guard(self):
        try:
            from ..kb import adapter
        except ImportError:
            from kb import adapter
        if adapter.enabled():
            # Rollback-journal readers serialize approval commits with permit consumption.
            with adapter._knowledge_read(self.store.root):
                yield
        else:
            yield

    @contextlib.contextmanager
    def _transaction(self, task_id, *, knowledge_guard=True):
        with self.store.transaction(task_id) as generation:
            if knowledge_guard:
                with self._knowledge_guard():
                    yield generation
            else:
                yield generation

    def _request_replay(self, state, request_id, action, request):
        if request_id is None:
            return None, None
        validate_id(request_id)
        digest = canonical_hash({"action": action, "request": request})
        prior = state.setdefault("requests", {}).get(request_id)
        if prior is not None:
            if prior.get("hash") != digest or prior.get("action") != action:
                raise V3Error("REQUEST_CONFLICT")
            return None, clone(prior["response"])
        return {"id": request_id, "hash": digest, "action": action}, None

    def _save_request_response(self, state, request, response):
        snapshot = clone(response)
        if request is not None:
            if isinstance(snapshot, dict) and "requests" in snapshot:
                snapshot["requests"] = {}
            state.setdefault("requests", {})[request["id"]] = {
                "hash": request["hash"], "action": request["action"], "response": snapshot}
        self.store.commit(state["task_id"], state)
        return snapshot

    def _subject(self, state):
        return canonical_hash({k: state.get(k) for k in _SUBJECT_KEYS})

    def _run_knowledge_ok(self, state, run):
        if run.get("needs_revalidation"):
            raise V3Error("STALE_RUN_KNOWLEDGE")
        if run.get("knowledge_snapshot_hash") != (state.get("knowledge_snapshot") or {}).get("hash"):
            raise V3Error("STALE_RUN_KNOWLEDGE")

    def _execution_hash(self, state):
        # New result evidence is legitimate after launch; changed execution
        # dependencies cannot borrow a run from the previous design.
        decisions = {k: v for k, v in state["decisions"].items() if k != "selected_run_id"}
        # Analysis-only obligations can acquire run/measurement bindings after
        # execution. Earlier obligations and all intent/input decisions stay fixed.
        execution_items = [item for item in _items(state["plan"]) if item.get("due_stage") != "ANALYZE"]
        return canonical_hash({"intent": state["intent"], "execution_items": execution_items,
                               "decisions": decisions, "dependencies": state["dependencies"]})

    def _cas(self, state, expected_revision, expected_state_seq=None):
        if type(expected_revision) is not int or state["revision"] != expected_revision:
            raise V3Error("REVISION_CONFLICT", str(state["revision"]))
        if expected_state_seq is not None and (type(expected_state_seq) is not int or state["state_seq"] != expected_state_seq):
            raise V3Error("STATE_SEQ_CONFLICT", str(state["state_seq"]))
        if state.get("status") == "completed":
            raise V3Error("TASK_COMPLETED")
        if state.get("status") == "archived_failure":
            raise V3Error("TASK_ARCHIVED")

    def _commit(self, state, substantive=False, response=None):
        if substantive:
            state["revision"] += 1
            state["subject_hash"] = self._subject(state)
            state["active_receipts"] = {}
            state["status"] = "in_progress"
        state["state_seq"] += 1
        return self.store.commit(state["task_id"], state)

    def _artifact(self, state, category, record):
        self.store.artifact(state["task_id"], category, record)
        state[category].append(record)
        return record

    def _generation_ok(self, state, generation):
        if generation["status"] != "ready":
            raise V3Error("GENERATION_PENDING", "reconcile external publication")
        if state.get("generation") != generation["generation"]:
            raise V3Error("REVALIDATION_REQUIRED", "knowledge/authority generation changed")
        try:
            from ..kb import adapter
        except ImportError:
            from kb import adapter
        if adapter.enabled():
            from .knowledge import snapshot
            previous = state.get("knowledge_snapshot")
            if previous is None or previous.get("hash") != snapshot(self.store.root)["hash"]:
                raise V3Error("REVALIDATION_REQUIRED", "UKB knowledge changed; revise and review task")

    def _bind_knowledge(self, state, generation):
        if state.get("generation") != generation["generation"] or generation["status"] != "ready":
            return
        from .knowledge import snapshot
        current = snapshot(self.store.root)
        ids = set()
        explicit = state["dependencies"].get("knowledge", [])
        if isinstance(explicit, dict):
            explicit = list(explicit.values())
        if not isinstance(explicit, list):
            raise V3Error("KNOWLEDGE_DEPENDENCY_INVALID")
        for dependency in explicit:
            identity = dependency if isinstance(dependency, str) else dependency.get("id")
            if identity not in current["entries"]:
                raise V3Error("KNOWLEDGE_DEPENDENCY_MISSING", str(identity))
            if isinstance(dependency, dict):
                for key in ("version", "entry_hash", "source", "conditions", "source_refs"):
                    if key in dependency and canonical_hash(dependency[key]) != canonical_hash(current["entries"][identity][key]):
                        raise V3Error("KNOWLEDGE_DEPENDENCY_MISMATCH", key)
            ids.add(identity)
        for item in _items(state["plan"]) + state["evidence"]:
            for ref in item.get("origin_refs", []) + item.get("source_refs", []):
                identity = ref.removeprefix("knowledge:") if isinstance(ref, str) else None
                if identity in current["entries"]:
                    ids.add(identity)
                elif isinstance(ref, str) and ref.startswith("knowledge:"):
                    raise V3Error("KNOWLEDGE_DEPENDENCY_MISSING", ref)
                else:
                    ids.update(k for k, v in current["entries"].items() if ref == v["source"] or
                               (isinstance(ref, str) and ref.startswith(v["source"] + "#")))
        state["knowledge_snapshot"] = current
        state["knowledge_dependencies"] = {k: current["entries"][k] for k in sorted(ids)}

    def _record_generation_valid(self, state, record):
        number = record.get("generation")
        if number == state["generation"]:
            return True
        for proof in state.get("knowledge_refreshes", []):
            if (proof.get("reused") is True and proof.get("from_generation") == number and
                    proof.get("subject_hash") == state["subject_hash"] and proof.get("revision") == state["revision"]):
                self.store.verify_artifact(state["task_id"], "knowledge_refreshes", proof)
                number = proof["to_generation"]
        return number == state["generation"]

    def refresh_knowledge(self, task_id, expected_revision, expected_state_seq=None, request_id=None):
        """Reconcile controlled changes. Semantic exclusion comes from a trusted adapter.

        No review/status is accepted from the client. Related dependencies,
        unknown events and new applicable obligations always invalidate receipts.
        """
        from .knowledge import snapshot
        if request_id is not None:
            validate_id(request_id)
        legacy_fingerprint = canonical_hash({"action": "refresh_knowledge", "expected_revision": expected_revision,
                                             "expected_state_seq": expected_state_seq})
        with self._transaction(task_id) as generation:
            state = self.store.load(task_id)
            prior = state.get("requests", {}).get(request_id) if request_id is not None else None
            if prior is not None and "action" not in prior:
                if prior.get("hash") != legacy_fingerprint:
                    raise V3Error("REQUEST_CONFLICT")
                return clone(prior["response"])
            request_record, replay = self._request_replay(state, request_id, "refresh_knowledge",
                                                          {"expected_revision": expected_revision,
                                                           "expected_state_seq": expected_state_seq})
            if replay is not None:
                return replay
            self._cas(state, expected_revision, expected_state_seq)
            if generation["status"] != "ready":
                raise V3Error("GENERATION_PENDING")
            previous = state.get("knowledge_snapshot")
            current = snapshot(self.store.root)
            start, end = state["generation"], generation["generation"]
            if start == end:
                if previous and previous != current:
                    try:
                        from ..kb import adapter
                    except ImportError:
                        from kb import adapter
                    if not adapter.enabled():
                        raise V3Error("UNCONTROLLED_KNOWLEDGE_CHANGE")
                    state["knowledge_snapshot"] = current
                    state["knowledge_dependencies"] = {}
                    state["revision"] += 1
                    state["state_seq"] += 1
                    state["active_receipts"] = {}
                    state["needs_revalidation"] = True
                    for review_record in state["reviews"]:
                        review_record["stale"] = True
                    for run in state["runs"]:
                        if run.get("status") in {"STARTING", "START_UNKNOWN", "RUNNING", "FINISHED"}:
                            run["needs_revalidation"] = True
                    proof = {"id": execution.new_id("knowledge-refresh"), "task_id": task_id,
                             "from_generation": start, "to_generation": end, "reused": False,
                             "reasons": ["UKB knowledge changed; full task revalidation required"],
                             "created_at": _now()}
                    self.store.artifact(task_id, "knowledge_refreshes", proof)
                    state.setdefault("knowledge_refreshes", []).append(proof)
                    return self._save_request_response(state, request_record, state)
                if request_record is not None:
                    self._save_request_response(state, request_record, state)
                return clone(state)
            events = [e for e in generation.get("events", []) if start < e.get("generation", -1) <= end]
            try:
                from ..kb import adapter
            except ImportError:
                from kb import adapter
            reusable = not adapter.enabled()
            reasons = [] if reusable else ["UKB knowledge requires full task revalidation"]
            if [e.get("generation") for e in events] != list(range(start + 1, end + 1)) or previous is None:
                reusable, reasons = False, ["incomplete-change-history"]
            reconstructed = clone(previous["entries"]) if previous else {}
            changed = set()
            semantic = {}
            for event in events:
                if event.get("kind") != "knowledge":
                    reusable = False
                    reasons.append("authority-or-unknown-change")
                    continue
                changes = event.get("changes", [])
                if sorted(event.get("changed_ids", [])) != sorted(c.get("id") for c in changes):
                    reusable = False
                    reasons.append("invalid-change-manifest")
                for change in changes:
                    identity, before, after = change["id"], change.get("before"), change.get("after")
                    changed.add(identity)
                    prior = reconstructed.get(identity)
                    if ((prior is None) != (before is None) or
                            (before is not None and prior.get("entry_hash") != canonical_hash(before))):
                        reusable = False
                        reasons.append("change-history-mismatch")
                    if after is None:
                        reconstructed.pop(identity, None)
                    else:
                        reconstructed[identity] = {"id": identity, "version": after.get("version"),
                            "entry_hash": canonical_hash(after), "source": change["source"],
                            "conditions": after.get("conditions", {}), "source_refs": after.get("source_refs", []),
                            "status": after.get("status", after.get("lifecycle")), "record": after}
                    if any(row and row.get("status", row.get("lifecycle")) == "active" for row in (before, after)):
                        semantic[identity] = {"entry_id": identity, "before": previous["entries"].get(identity) if previous else None,
                                              "after": current["entries"].get(identity)}
            if reconstructed != current["entries"]:
                reusable = False
                reasons.append("uncontrolled-knowledge-change")
            bound = set(state.get("knowledge_dependencies", {}))
            if bound & changed:
                reusable = False
                reasons.append("related-knowledge-change")
            # Package bytes may change only through the listed controlled target.
            controlled = {c["source"] for e in events for c in e.get("changes", [])}
            old_packages = {p["source"]: p["sha256"] for p in (previous or {}).get("packages", [])}
            new_packages = {p["source"]: p["sha256"] for p in current["packages"]}
            if any(old_packages.get(k) != new_packages.get(k) for k in (old_packages.keys() | new_packages.keys()) - controlled):
                reusable = False
                reasons.append("uncontrolled-package-change")
            context = {"task_id": task_id, "revision": state["revision"], "subject_hash": state["subject_hash"],
                       "intent": state["intent"], "plan": state["plan"], "dependencies": state.get("knowledge_dependencies", {}),
                       "from_generation": start, "to_generation": end, "events": events,
                       "previous_snapshot_hash": (previous or {}).get("hash"), "current_snapshot_hash": current["hash"],
                       "changes": list(semantic.values())}
            context["context_hash"] = canonical_hash(context)
            review = None
            if reusable and semantic:
                if callable(self.knowledge_reviewer):
                    try:
                        review = self.knowledge_reviewer(clone(context))
                    except Exception as exc:
                        raise V3Error("KNOWLEDGE_REVIEW_INFRA_ERROR") from exc
                rows = review.get("items", []) if isinstance(review, dict) else []
                valid = (isinstance(review, dict) and review.get("status") == "success" and
                         review.get("context_hash") == context["context_hash"] and bool(review.get("source_refs")) and
                         isinstance(rows, list) and len(rows) == len(semantic) and
                         {r.get("entry_id") for r in rows} == set(semantic))
                for row in rows if isinstance(rows, list) else []:
                    change = semantic.get(row.get("entry_id"), {})
                    valid = valid and row.get("change_hash") == canonical_hash(change) and row.get("status") == "inapplicable" and bool(row.get("reason")) and bool(row.get("source_refs"))
                if not valid:
                    reusable = False
                    reasons.append("applicability-review-required")
            proof = {"id": execution.new_id("knowledge-refresh"), "task_id": task_id,
                     "revision": state["revision"], "subject_hash": state["subject_hash"],
                     "from_generation": start, "to_generation": end, "reused": reusable,
                     "reasons": reasons, "context": context, "review": review, "created_at": _now()}
            self.store.artifact(task_id, "knowledge_refreshes", proof)
            state.setdefault("knowledge_refreshes", []).append(proof)
            state["generation"] = end
            state["knowledge_snapshot"] = current
            if not reusable:
                state["revision"] += 1
                state["active_receipts"] = {}
                state["needs_revalidation"] = True
                for review_record in state["reviews"]:
                    review_record["stale"] = True
                for run in state["runs"]:
                    if run.get("status") in {"STARTING", "START_UNKNOWN", "RUNNING", "FINISHED"}:
                        run["needs_revalidation"] = True
                state["knowledge_dependencies"] = {k: current["entries"][k] for k in bound if k in current["entries"]}
            state["state_seq"] += 1
            return self._save_request_response(state, request_record, state)

    def read(self, task_id):
        with self._transaction(task_id):
            state = self.store.load(task_id, writable=False)
            if state.get("schema_version") == 3:
                if state.get("subject_hash") != self._subject(state):
                    raise V3Error("SUBJECT_MISMATCH")
                for receipt in state["receipts"]:
                    self.store.verify_artifact(task_id, "receipts", receipt)
                try:
                    state["inputs"] = execution.inspect_inputs(state["dependencies"].get("cwd"), state["dependencies"].get("inputs"))
                    state["input_dependencies_complete"] = True
                except V3Error as exc:
                    state["inputs"] = []
                    state["input_dependencies_complete"] = False
                    state["input_health"] = exc.as_dict()
            return clone(state)

    def create(self, task_id, intent, request_id=None):
        validate_id(task_id)
        if request_id is not None:
            validate_id(request_id)
        intent = _call("planning", "validate_intent", clone(intent))
        fingerprint = canonical_hash(intent)
        with self._transaction(task_id) as generation:
            existing = self.store.load(task_id, missing=True)
            if existing is not None:
                if request_id and existing.get("create_request") == {"id": request_id, "hash": fingerprint}:
                    return clone(existing)
                raise V3Error("TASK_EXISTS", task_id)
            if generation["status"] != "ready":
                raise V3Error("GENERATION_PENDING")
            state = {"schema_version": 3, "task_id": task_id, "revision": 1, "state_seq": 1,
                     "current_stage": "PLAN", "status": "in_progress", "intent": intent,
                     "plan": None, "decisions": {}, "dependencies": {}, "generation": generation["generation"],
                     "active_receipts": {}, "consumed_receipts": [], "requests": {},
                     "create_request": {"id": request_id, "hash": fingerprint}, "created_at": _now()}
            for name in ("evidence", "proofs", "rule_results", "reviews", "findings", "authorizations", "receipts", "permits", "runs"):
                state[name] = []
            state["subject_hash"] = self._subject(state)
            self._bind_knowledge(state, generation)
            return self.store.commit(task_id, state)

    def revise(self, task_id, patch, expected_revision, expected_state_seq=None, request_id=None):
        patch = clone(patch)
        allowed = {"intent", "decisions", "dependencies", "generation_review"}
        if not isinstance(patch, dict) or not patch or set(patch) - allowed:
            raise V3Error("INVALID_PATCH")
        if request_id is not None:
            validate_id(request_id)
        fingerprint = canonical_hash({"action": "revise", "patch": patch})
        with self._transaction(task_id) as generation:
            state = self.store.load(task_id)
            if request_id in state["requests"]:
                entry = state["requests"][request_id]
                if entry["hash"] != fingerprint:
                    raise V3Error("REQUEST_CONFLICT")
                return clone(entry["response"])
            self._cas(state, expected_revision, expected_state_seq)
            if "generation_review" in patch:
                # Compatibility request only. Refresh derives changes from the
                # journal and a backend reviewer; client success is no proof.
                patch.pop("generation_review")
                raise V3Error("GENERATION_REVIEW_REQUIRED", "use refresh_knowledge")
            for key, value in patch.items():
                if key == "intent":
                    value = _call("planning", "validate_intent", value)
                elif not isinstance(value, dict):
                    raise V3Error("INVALID_PATCH", key)
                state[key] = value
            self._bind_knowledge(state, generation)
            # An explicit revision also revalidates an unchanged dependency manifest.
            state["revision"] += 1
            state["subject_hash"] = self._subject(state)
            state["active_receipts"] = {}
            state["state_seq"] += 1
            if request_id:
                response = clone(state)
                state["requests"][request_id] = {"hash": fingerprint, "response": response}
            return self.store.commit(task_id, state)

    def set_plan(self, task_id, plan, expected_revision):
        with self._transaction(task_id) as generation:
            state = self.store.load(task_id)
            self._cas(state, expected_revision)
            proposal = clone(plan)
            bundle = proposal.pop("retrieval_bundle", None)
            if bundle is None:
                bundle = proposal.get("retrieval", {})
            if "proposal_items" in proposal:
                proposal["items"] = proposal["proposal_items"]
            prepared = _call("planning", "prepare_plan", state["intent"], bundle, proposal)
            if not isinstance(prepared, dict):
                raise V3Error("INVALID_PLAN")
            if state["plan"] is not None:
                if "reviewed_change" in proposal:
                    prepared["reviewed_change"] = proposal["reviewed_change"]
                    prepared["plan_hash"] = _call("planning", "hash_plan", prepared)
                prepared = _call("planning", "revise_plan", state["plan"], prepared)
            for raw in prepared.get("findings", []):
                if not any(f["id"] == raw["id"] for f in state["findings"]):
                    state["findings"].append(_call("policy", "classify_finding", raw))
            state["plan"] = prepared
            self._bind_knowledge(state, generation)
            return self._commit(state, substantive=True)

    def record_evidence(self, task_id, record, expected_revision):
        with self._transaction(task_id):
            state = self.store.load(task_id)
            self._cas(state, expected_revision)
            record = clone(record)
            validate_id(record.get("id"))
            if record.get("task_id", task_id) != task_id:
                raise V3Error("EVIDENCE_TASK_MISMATCH")
            if any(r["id"] == record["id"] for r in state["evidence"]):
                raise V3Error("IMMUTABLE_ARTIFACT", record["id"])
            record["task_id"] = task_id
            # Policy validates claim semantics; the store preserves even rejected material.
            self._artifact(state, "evidence", record)
            return self._commit(state, substantive=True)

    def record_measurement(self, task_id, run_id, item_id, recipe, evidence_id, expected_revision, conditions=None):
        """Recompute from the finished run's raw outputs, under the task lock."""
        from pathlib import Path
        from . import measurements
        validate_id(evidence_id)
        with self._transaction(task_id):
            state = self.store.load(task_id)
            self._cas(state, expected_revision)
            run = next((r for r in state["runs"] if r["id"] == run_id), None)
            if not run or run["status"] != "FINISHED":
                raise V3Error("MEASUREMENT_FINISHED_RUN_REQUIRED")
            self._run_knowledge_ok(state, run)
            item = next((i for i in _items(state["plan"]) if i["id"] == item_id), None)
            if not item or item.get("owner_rule") != "E2" or item.get("run_id", state["decisions"].get("selected_run_id")) != run_id:
                raise V3Error("MEASUREMENT_ITEM_MISMATCH")
            declared = item.get("measurement", {})
            claim_id = item.get("claim_id", item.get("claim"))
            if not claim_id or not declared.get("metric") or "tolerance" not in declared or recipe.get("unit") != declared.get("unit") or ("window" in declared and recipe.get("window") != declared["window"]):
                raise V3Error("MEASUREMENT_CONTRACT_REQUIRED")
            if any(e["id"] == evidence_id for e in state["evidence"]):
                raise V3Error("IMMUTABLE_ARTIFACT", evidence_id)
            receipt = next((r for r in state["receipts"] if r["id"] == run.get("receipt_id")), None)
            if not receipt or receipt.get("kind") != "RUN_RECEIPT" or receipt.get("run_id") != run_id:
                raise V3Error("RUN_RECEIPT_REQUIRED")
            self.store.verify_artifact(task_id, "receipts", receipt)
            original_outputs = receipt["result"]["outputs"]
            if recipe.get("source") not in original_outputs:
                raise V3Error("MEASUREMENT_RAW_SOURCE_REQUIRED")
            for output in original_outputs:
                if execution.file_hash(output["path"]) != output["sha256"]:
                    raise V3Error("OUTPUT_HASH_MISMATCH")
            permit = next(p for p in state["permits"] if p["id"] == run["permit_id"])
            execution.verify_frozen(self.store, task_id, permit)
            directory = Path(permit["frozen_spec"]["output_dir"])
            material_run = dict(clone(run), output_dir=str(directory), outputs=clone(original_outputs))
            product = measurements.materialize(material_run, clone(recipe), directory,
                evidence_id=evidence_id, claim_id=claim_id, metric=declared["metric"], tolerance=declared["tolerance"],
                conditions=clone(conditions or {}), provenance_refs=["run:" + run_id, "item:" + item_id])
            evidence, manifest = product["evidence"], product["artifact_manifest"]
            if not isinstance(manifest, dict) or not isinstance(manifest.get("path"), str):
                raise V3Error("MEASUREMENT_ARTIFACT_INVALID")
            path = Path(manifest["path"])
            if not path.is_absolute() or path.is_symlink() or execution.file_hash(path) != manifest.get("sha256"):
                raise V3Error("MEASUREMENT_ARTIFACT_INVALID")
            try:
                path.resolve().relative_to(directory.resolve())
            except ValueError as exc:
                raise V3Error("MEASUREMENT_OUTPUT_SCOPE") from exc
            if evidence.get("id") != evidence_id or evidence.get("task_id") != task_id or evidence.get("run_id") != run_id:
                raise V3Error("MEASUREMENT_BINDING_MISMATCH")
            chain = {"id": execution.new_id("derived-output"), "run_id": run_id,
                     "run_receipt_id": receipt["id"], "source_outputs": clone(original_outputs),
                     "artifact_manifest": clone(manifest), "evidence_id": evidence_id,
                     "recipe_hash": canonical_hash(recipe), "created_at": _now()}
            self.store.artifact(task_id, "derived_outputs", chain)
            run.setdefault("derived_outputs", []).append(chain)
            run["outputs"] = run.get("outputs", clone(original_outputs)) + [clone(manifest)]
            run["output_hash"] = canonical_hash(run["outputs"])
            self._artifact(state, "evidence", evidence)
            return self._commit(state, substantive=True)

    def record_proof(self, task_id, proof, expected_revision):
        """Immutable deterministic input, recomputed by policy; never a PASS flag."""
        with self._transaction(task_id):
            state = self.store.load(task_id)
            self._cas(state, expected_revision)
            proof = clone(proof)
            validate_id(proof.get("id"))
            for key, expected in (("task_id", task_id), ("revision", state["revision"]),
                                  ("subject_hash", state["subject_hash"]), ("plan_hash", self._plan_hash(state))):
                if proof.get(key) != expected:
                    raise V3Error("PROOF_BINDING_MISMATCH", key)
            if proof.get("item_id") not in {i["id"] for i in _items(state["plan"])}:
                raise V3Error("PROOF_ITEM_MISMATCH")
            self._artifact(state, "proofs", proof)
            state["active_receipts"] = {}
            return self._commit(state)

    def record_review(self, task_id, review, expected_revision):
        with self._transaction(task_id):
            state = self.store.load(task_id)
            if type(expected_revision) is not int or expected_revision > state["revision"] or expected_revision < 1:
                raise V3Error("REVISION_CONFLICT", str(state["revision"]))
            review = clone(review)
            if expected_revision != state["revision"]:
                validate_id(review.get("id"))
                if review.get("task_id", task_id) != task_id or not review.get("subject_hash"):
                    raise V3Error("INVALID_REVIEW")
                review.update(task_id=task_id, revision=expected_revision, stale=True)
                self._artifact(state, "reviews", review)
                return self._commit(state)
            self._cas(state, expected_revision)
            validate_id(review.get("id"))
            if review.get("task_id", task_id) != task_id:
                raise V3Error("REVIEW_TASK_MISMATCH")
            covered = review.get("covered_items")
            valid_ids = {item["id"] for item in _items(state["plan"])}
            if review.get("scope") == "validation" and review.get("question_id"):
                valid_ids.add(review["question_id"])
            if review.get("kind") in {"plan", "input"}:
                review.setdefault("scope", review["kind"])
            if review.get("status") == "success" and (not isinstance(covered, list) or not covered or not set(covered) <= valid_ids or not (review.get("source_refs") or review.get("citations"))):
                raise V3Error("INVALID_REVIEW", "coverage/source required")
            if review.get("status") == "success" and review.get("scope") != "validation" and set(covered) != valid_ids:
                raise V3Error("INVALID_REVIEW", "all dynamic items must be covered")
            if review.get("plan_hash", self._plan_hash(state)) != self._plan_hash(state):
                raise V3Error("INVALID_REVIEW", "plan hash")
            review["task_id"] = task_id
            review["stale"] = review.get("subject_hash") != state["subject_hash"] or review.get("revision", state["revision"]) != state["revision"]
            if not review.get("subject_hash"):
                raise V3Error("INVALID_REVIEW", "full subject_hash required")
            self._artifact(state, "reviews", review)
            if not review["stale"]:
                affected = "CONFIG" if review.get("scope") == "input" else "PLAN"
                if review.get("scope") != "validation":
                    for stage in STAGES[STAGES.index(affected):]:
                        state["active_receipts"].pop(stage, None)
                for raw in review.get("findings", []):
                    finding = _call("policy", "classify_finding", raw)
                    validate_id(finding.get("id"))
                    existing = next((f for f in state["findings"] if f["id"] == finding["id"]), None)
                    if existing is not None:
                        base = lambda row: {k: v for k, v in row.items() if k not in {"status", "resolution"}}
                        if base(existing) != base(finding):
                            raise V3Error("FINDING_EXISTS", "changed findings require a new id and supersedes")
                    else:
                        state["findings"].append(finding)
            # Findings affect subsequent policy evaluation, not the reviewed content's identity.
            # Their resolutions are substantive decisions and do change revision.
            state["subject_hash"] = self._subject(state)
            return self._commit(state)

    def _review(self, state, stage=None, item_id=None):
        scope = {"PLAN": "plan", "BUILD": "plan", "CONFIG": "input", "EXEC": "input", "ANALYZE": "input"}.get(stage)
        for review in reversed(state["reviews"]):
            if review.get("subject_hash") != state["subject_hash"] or review.get("stale") or review.get("scope") == "validation":
                continue
            if scope and review.get("scope") != scope:
                continue
            if stage is not None and review.get("stage", stage) != stage:
                continue
            if review.get("status") != "success":
                return None
            if item_id is None or item_id in review.get("covered_items", []):
                return review
        return None

    def _context(self, state, review=None):
        return {"task_id": state["task_id"], "revision": state["revision"],
                "subject_hash": state["subject_hash"], "plan_hash": self._plan_hash(state),
                "review": review, "authorizations": clone(state["authorizations"]),
                "findings": clone(state["findings"]), "runs": clone(state["runs"]),
                "proofs": clone(state.get("proofs", [])), "dependencies": clone(state["dependencies"]),
                "execution_hash": self._execution_hash(state),
                "knowledge_snapshot_hash": (state.get("knowledge_snapshot") or {}).get("hash")}

    def _plan_hash(self, state):
        plan = state["plan"]
        return plan.get("plan_hash", canonical_hash(plan)) if isinstance(plan, dict) else canonical_hash(None)

    def _decision_hash(self, state, stage):
        scope = "plan" if stage in {"PLAN", "BUILD"} else "input"
        return canonical_hash({"reviews": [r for r in state["reviews"] if r.get("scope") == scope and r.get("subject_hash") == state["subject_hash"] and not r.get("stale")],
                               "findings": state["findings"], "proofs": state.get("proofs", []),
                               "authorizations": [a for a in state["authorizations"] if a.get("scope") != "production"]})

    def _valid_receipt(self, state, stage, receipt_id=None):
        active_id = state["active_receipts"].get(stage)
        if receipt_id is not None and receipt_id != active_id:
            raise V3Error("INACTIVE_RECEIPT", stage)
        receipt_id = receipt_id or active_id
        receipt = next((r for r in state["receipts"] if r["id"] == receipt_id), None)
        if not receipt or receipt.get("stage") != stage or receipt.get("verdict") != "PASS":
            raise V3Error("RECEIPT_REQUIRED", stage)
        self.store.verify_artifact(state["task_id"], "receipts", receipt)
        if receipt["subject_hash"] != state["subject_hash"] or receipt["revision"] != state["revision"] or not self._record_generation_valid(state, receipt):
            raise V3Error("STALE_RECEIPT", stage)
        if receipt.get("decision_context_hash") != self._decision_hash(state, stage):
            raise V3Error("STALE_DECISION_CONTEXT", stage)
        if receipt.get("kind") != _RECEIPT_KINDS[stage]:
            raise V3Error("WRONG_RECEIPT_KIND", stage)
        for ancestor in STAGES[:STAGES.index(stage)]:
            upstream = self._valid_receipt(state, ancestor)
            if receipt.get("upstream", {}).get(ancestor) != upstream["id"]:
                raise V3Error("STALE_DEPENDENCY", ancestor)
        return receipt

    def _ancestors(self, state, stage):
        return {s: self._valid_receipt(state, s)["id"] for s in STAGES[:STAGES.index(stage)]}

    def evaluate(self, task_id, stage=None):
        with self._transaction(task_id) as generation:
            state = self.store.load(task_id)
            if state.get("status") == "completed":
                raise V3Error("TASK_COMPLETED")
            stage = stage or state["current_stage"]
            if stage not in STAGES or STAGES.index(stage) > STAGES.index(state["current_stage"]):
                raise V3Error("INVALID_STAGE")
            reasons, results, upstream = [], [], {}
            try:
                self._generation_ok(state, generation)
                upstream = self._ancestors(state, stage)
                if not _items(state["plan"]):
                    raise V3Error("EMPTY_PLAN")
                if stage == "PLAN":
                    review = self._review(state, "PLAN")
                    confirmed = any(a.get("scope") == "plan" and a.get("decision") in {"confirm", "allow"} and a.get("actor_kind") == "human" and a.get("revoked") is False and a.get("subject_hash") == state["subject_hash"] for a in state["authorizations"])
                    if not review or review.get("scope") != "plan" or not confirmed:
                        raise V3Error("PLAN_REVIEW_REQUIRED")
                if stage == "CONFIG":
                    review = self._review(state, "CONFIG")
                    if not review or review.get("scope") != "input" or not execution.manifest_equal(review.get("input_manifest"), state["dependencies"].get("inputs")) or not review.get("input_manifest"):
                        raise V3Error("INPUT_REVIEW_REQUIRED")
                    execution.inspect_inputs(state["dependencies"].get("cwd"), state["dependencies"].get("inputs"))
                for obligation in _items(state["plan"]):
                    context = self._context(state, self._review(state, stage, obligation["id"]))
                    context["run_id"] = obligation.get("run_id", state["decisions"].get("selected_run_id"))
                    result = _call("policy", "evaluate", obligation["owner_rule"], obligation,
                                   clone(state["evidence"]), context)
                    if not isinstance(result, dict) or result.get("item_id") != obligation["id"] or result.get("rule") != obligation["owner_rule"] or result.get("subject_hash") != state["subject_hash"] or result.get("plan_hash") != self._plan_hash(state) or result.get("verdict") not in {"PASS", "BLOCK", "ACCEPTED_ASSUMPTION", "NOT_APPLICABLE"}:
                        raise V3Error("INVALID_RULE_RESULT")
                    results.append(clone(result))
                coverage = _call("planning", "coverage", state["plan"], results, stage, state["subject_hash"])
                if not isinstance(coverage, dict) or coverage.get("ok") is not True:
                    reasons.extend(coverage.get("reasons", ["coverage incomplete"]) if isinstance(coverage, dict) else ["invalid coverage"])
                for finding in state["findings"]:
                    if finding.get("type", finding.get("kind")) != "advisory" and finding.get("status") not in {"resolved", "accepted", "accepted_assumption", "corrected", "not_applicable", "superseded"}:
                        reasons.append("unresolved-finding:" + finding["id"])
                required_rules = {"PLAN": ("E1", "E3"), "BUILD": ("E4",), "CONFIG": ("E4",)}.get(stage, ())
                for rule in required_rules:
                    matching = {item["id"] for item in _items(state["plan"]) if item["owner_rule"] == rule and STAGES.index(item["due_stage"]) <= STAGES.index(stage)}
                    if not matching or not any(r["item_id"] in matching and r["verdict"] in {"PASS", "ACCEPTED_ASSUMPTION", "NOT_APPLICABLE"} for r in results):
                        reasons.append("stage-owner-coverage:" + rule)
                if stage == "ANALYZE":
                    self._analysis_chain(state, results)
                if stage == "EXEC" and not state["dependencies"].get("inputs"):
                    reasons.append("final-inputs-unbound")
                if stage in {"EXEC", "ANALYZE"} and state["intent"]["action"] == "consult":
                    reasons.append("consultation-not-executable")
            except Exception as exc:
                coverage = {"ok": False, "reasons": [getattr(exc, "code", "INFRA_ERROR")]}
                reasons.append(getattr(exc, "code", "INFRA_ERROR"))
            receipt = {"id": execution.new_id("receipt"), "task_id": task_id, "revision": state["revision"],
                       "subject_hash": state["subject_hash"], "plan_hash": self._plan_hash(state),
                       "generation": state["generation"], "decision_context_hash": self._decision_hash(state, stage),
                       "stage": stage, "kind": _RECEIPT_KINDS[stage],
                       "verdict": "BLOCK" if reasons else "PASS", "reasons": reasons, "coverage": coverage,
                       "upstream": upstream, "rule_results": results, "created_at": _now(), "validator": "evidence-v3"}
            self._artifact(state, "receipts", receipt)
            state["rule_results"] = results
            state["active_receipts"][stage] = receipt["id"]
            self._commit(state)
            return clone(receipt)

    def advance(self, task_id, to_stage, expected_revision, receipt_id=None):
        with self._transaction(task_id) as generation:
            state = self.store.load(task_id)
            self._cas(state, expected_revision)
            self._generation_ok(state, generation)
            current = state["current_stage"]
            if to_stage not in STAGES or STAGES.index(to_stage) != STAGES.index(current) + 1:
                raise V3Error("INVALID_TRANSITION")
            receipt = self._valid_receipt(state, current, receipt_id)
            if receipt["id"] in state["consumed_receipts"]:
                raise V3Error("RECEIPT_CONSUMED")
            if current == "EXEC":
                self._production_run(state)
            state["consumed_receipts"].append(receipt["id"])
            state["current_stage"] = to_stage
            return self._commit(state)

    def resolve(self, task_id, finding_id, resolution, expected_revision):
        with self._transaction(task_id):
            state = self.store.load(task_id)
            self._cas(state, expected_revision)
            finding = next((f for f in state["findings"] if f["id"] == finding_id), None)
            if finding is None:
                raise V3Error("FINDING_NOT_FOUND")
            resolved = _call("policy", "resolve_finding", clone(finding), clone(resolution), self._context(state))
            if not isinstance(resolved, dict):
                raise V3Error("INVALID_RESOLUTION")
            # Preserve the original judgment and every resolution; no deletion escape.
            finding["status"] = resolved.get("status", "unresolved")
            finding["resolution"] = clone(resolution)
            resolution_record = {"id": execution.new_id("resolution"), "finding_id": finding_id,
                                 "original": clone(finding), "request": clone(resolution), "result": clone(resolved)}
            self.store.artifact(task_id, "resolutions", resolution_record)
            state["active_receipts"] = {}
            # A disposition binds this exact subject; it does not rewrite that subject.
            return self._commit(state)

    def record_authorization(self, task_id, authorization, expected_revision):
        """Bind an authorization request to a server-verified human event."""
        if not callable(self.human_verifier):
            raise V3Error("HUMAN_AUTHORITY_REQUIRED")
        if not isinstance(authorization, dict):
            raise V3Error("INVALID_AUTHORIZATION")
        allowed = {"id", "scope", "decision", "subject_hash", "spec_hash", "finding_id", "reason",
                   "actor_kind", "actor_id", "source_refs", "authorization_source", "revoked"}
        if set(authorization) - allowed:
            raise V3Error("INVALID_AUTHORIZATION")
        request = {key: clone(value) for key, value in authorization.items()
                   if key not in {"actor_kind", "actor_id", "source_refs", "authorization_source", "revoked"}}
        validate_id(request.get("id"))
        if (not isinstance(request.get("scope"), str) or not request["scope"] or
                not isinstance(request.get("decision"), str) or not request["decision"] or
                not isinstance(request.get("reason"), str) or not request["reason"].strip() or
                not isinstance(request.get("subject_hash"), str)):
            raise V3Error("INVALID_AUTHORIZATION")
        request_hash = canonical_hash(request)
        with self._transaction(task_id):
            state = self.store.load(task_id)
            self._cas(state, expected_revision)
            if request["subject_hash"] != state["subject_hash"]:
                raise V3Error("INVALID_AUTHORIZATION")
            expected = (state["revision"], state["state_seq"], state["subject_hash"])
            context = {key: clone(state[key]) for key in
                       ("task_id", "revision", "state_seq", "subject_hash", "current_stage", "status")}

        try:
            verified = self.human_verifier(task_id, clone(request), clone(context))
        except Exception as exc:
            raise V3Error("HUMAN_VERIFIER_INFRA_ERROR") from exc
        if not isinstance(verified, dict):
            raise V3Error("HUMAN_AUTHORITY_REQUIRED")
        source = verified.get("authorization_source")
        actor_id, refs = verified.get("actor_id"), verified.get("source_refs")
        if (not isinstance(actor_id, str) or not actor_id.strip() or not isinstance(refs, list) or not refs or
                any(not isinstance(ref, str) or not ref.strip() for ref in refs) or
                not isinstance(source, dict) or
                set(source) != {"kind", "source_id", "actor_id", "request_hash", "verified"} or
                source.get("kind") != "trusted_human_event" or
                not isinstance(source.get("source_id"), str) or not source["source_id"].strip() or
                source.get("actor_id") != actor_id or source.get("request_hash") != request_hash or
                source.get("verified") is not True):
            raise V3Error("HUMAN_AUTHORITY_REQUIRED")
        record = dict(request, actor_kind="human", actor_id=actor_id,
                      source_refs=clone(refs), authorization_source=clone(source), revoked=False)

        with self._transaction(task_id):
            state = self.store.load(task_id)
            if (state["revision"], state["state_seq"], state["subject_hash"]) != expected:
                raise V3Error("STATE_SEQ_CONFLICT", str(state["state_seq"]))
            if record["subject_hash"] != state["subject_hash"]:
                raise V3Error("INVALID_AUTHORIZATION")
            if any(row.get("id") == record["id"] for row in state["authorizations"]):
                raise V3Error("IMMUTABLE_ARTIFACT", record["id"])
            self._artifact(state, "authorizations", record)
            if record["scope"] != "production":
                state["active_receipts"] = {}
            return self._commit(state)

    def revoke_authorization(self, task_id, authorization_id, expected_revision, reason, source_refs=None):
        """Append a withdrawal event; retain original authorization and run facts."""
        if not isinstance(reason, str) or not reason.strip() or not isinstance(source_refs, list) or not source_refs:
            raise V3Error("REVOCATION_REASON_REQUIRED")
        with self._transaction(task_id):
            state = self.store.load(task_id)
            self._cas(state, expected_revision)
            record = next((a for a in state["authorizations"] if a["id"] == authorization_id), None)
            if record is None:
                raise V3Error("AUTHORIZATION_NOT_FOUND")
            event = {"id": execution.new_id("withdrawal"), "authorization_id": authorization_id,
                     "reason": reason, "source_refs": clone(source_refs), "created_at": _now()}
            self.store.artifact(task_id, "withdrawals", event)
            state.setdefault("withdrawals", []).append(event)
            record["revoked"] = True
            state["active_receipts"] = {}
            state["needs_revalidation"] = True
            return self._commit(state)

    def _probe_review(self, state, spec):
        review = next((r for r in state["reviews"] if r["id"] == spec.get("review_id")), None)
        if not review or review.get("status") != "success" or review.get("stale") or review.get("subject_hash") != state["subject_hash"] or review.get("spec_hash") != canonical_hash(spec) or review.get("question_id") != spec.get("question_id") or review.get("scope") != "validation":
            raise V3Error("PROBE_REVIEW_REQUIRED")
        conclusion = review.get("conclusion")
        if not isinstance(conclusion, dict) or conclusion.get("verdict") not in {"PASS", "BLOCK"} or (conclusion["verdict"] == "BLOCK" and not review.get("findings")):
            raise V3Error("PROBE_CONCLUSION_REQUIRED")
        if not execution.manifest_equal(review.get("input_manifest"), spec["inputs"]):
            raise V3Error("PROBE_INPUT_REVIEW_REQUIRED")
        for raw in review.get("findings", []):
            finding = next((f for f in state["findings"] if f["id"] == raw.get("id")), None)
            if not finding:
                raise V3Error("PROBE_FINDINGS_UNRESOLVED")
            if finding.get("type") == "advisory":
                continue
            resolution = finding.get("resolution")
            if not resolution:
                raise V3Error("PROBE_FINDINGS_UNRESOLVED")
            resolved = _call("policy", "resolve_finding", finding, resolution, self._context(state))
            if resolved.get("status") not in {"accepted_assumption", "corrected", "not_applicable"}:
                raise V3Error("PROBE_FINDINGS_UNRESOLVED")
        # Approval is an explicit input to the review, not manufactured by this service.
        refs = review.get("authorization_refs", [])
        authorized = any(a["id"] in refs and a.get("scope") in ("validation", spec["question_id"]) and a.get("decision") == "allow" and a.get("actor_kind") == "human" and a.get("revoked") is False and a.get("subject_hash") == state["subject_hash"] and a.get("spec_hash") == canonical_hash(spec) for a in state["authorizations"])
        if review.get("authorized") is not True or not authorized:
            raise V3Error("PROBE_AUTHORIZATION_REQUIRED")

    def _production_authorization(self, state, spec):
        authorization = next((a for a in state["authorizations"] if a["id"] == spec.get("authorization_id")), None)
        if not authorization or authorization.get("revoked") is not False or authorization.get("decision") != "allow" or authorization.get("scope") != "production" or authorization.get("subject_hash") != state["subject_hash"] or authorization.get("spec_hash") != canonical_hash(spec):
            raise V3Error("PRODUCTION_AUTHORIZATION_REQUIRED")
        return authorization

    def _registration_hash(self, attestation):
        value = attestation.get("registration_hash") if isinstance(attestation, dict) else None
        if (not isinstance(value, str) or len(value) != 64 or
                any(char not in "0123456789abcdef" for char in value)):
            raise V3Error("UNSUPPORTED", "stable adapter registration identity required")
        return value

    def _validate_start(self, state, generation, permit_id, spec):
        self._generation_ok(state, generation)
        permit = next((p for p in state["permits"] if p["id"] == permit_id), None)
        if not permit:
            raise V3Error("PERMIT_NOT_FOUND")
        self.store.verify_artifact(state["task_id"], "permits", permit)
        if any(r["permit_id"] == permit_id for r in state["runs"]):
            raise V3Error("PERMIT_CONSUMED")
        if (permit["subject_hash"] != state["subject_hash"] or
                permit["revision"] != state["revision"] or
                permit.get("generation") != state["generation"] or
                permit.get("generation") != generation["generation"]):
            raise V3Error("STALE_PERMIT")
        if permit["spec_hash"] != canonical_hash(spec):
            raise V3Error("SPEC_MISMATCH")
        if permit["kind"] == "production":
            if state["current_stage"] != "EXEC":
                raise V3Error("EXEC_STAGE_REQUIRED")
            self._production_authorization(state, spec)
            expected = {"EXEC": self._valid_receipt(state, "EXEC")["id"], **self._ancestors(state, "EXEC")}
            if expected != permit["upstream"]:
                raise V3Error("STALE_DEPENDENCY")
        elif permit["kind"] == "validation":
            self._probe_review(state, spec)
        elif (state["current_stage"] != {"build": "BUILD", "analyze": "ANALYZE"}[permit["kind"]] or
              self._ancestors(state, state["current_stage"]) != permit["upstream"]):
            raise V3Error("STALE_DEPENDENCY")
        return permit

    def authorize(self, task_id, spec, expected_revision):
        spec = execution.normalize_spec(spec)
        with self._transaction(task_id) as generation:
            state = self.store.load(task_id)
            self._cas(state, expected_revision)
            self._generation_ok(state, generation)
            kind, stage = spec["kind"], state["current_stage"]
            if kind == "production":
                if stage != "EXEC":
                    raise V3Error("EXEC_STAGE_REQUIRED")
                if state["intent"]["action"] not in {"production", "validation"}:
                    raise V3Error("ACTION_MISMATCH")
                receipt = self._valid_receipt(state, "EXEC")
                self._production_authorization(state, spec)
                if state["dependencies"].get("cwd") != spec["cwd"]:
                    raise V3Error("INPUT_CWD_MISMATCH")
                if state["dependencies"].get("inputs") != spec["inputs"]:
                    raise V3Error("INPUT_MANIFEST_MISMATCH")
            elif kind == "validation":
                self._probe_review(state, spec)
            else:
                required_stage = {"build": "BUILD", "analyze": "ANALYZE"}[kind]
                if stage != required_stage:
                    raise V3Error("ACTION_STAGE_MISMATCH")
                if kind == "analyze":
                    self._production_run(state)
            expected = (state["revision"], state["state_seq"], state["subject_hash"],
                        state["generation"], generation["generation"])
            context = self._context(state)

        attestation = execution.check_adapter(self.action_adapter, spec, context)
        self._registration_hash(attestation)

        with self._transaction(task_id) as generation:
            state = self.store.load(task_id)
            actual = (state["revision"], state["state_seq"], state["subject_hash"],
                      state["generation"], generation["generation"])
            if actual != expected:
                raise V3Error("STATE_SEQ_CONFLICT", str(state["state_seq"]))
            self._generation_ok(state, generation)
            kind = spec["kind"]
            if kind == "production":
                dependencies = {"EXEC": self._valid_receipt(state, "EXEC")["id"],
                                **self._ancestors(state, "EXEC")}
            elif kind == "validation":
                dependencies = {"review": spec["review_id"]}
            else:
                dependencies = self._ancestors(state, state["current_stage"])
            permit_id = execution.new_id("permit")
            frozen = execution.freeze(self.store, task_id, permit_id, spec)
            permit = {"id": permit_id, "task_id": task_id, "kind": kind, "revision": state["revision"],
                      "subject_hash": state["subject_hash"], "generation": state["generation"],
                      "spec": spec, "spec_hash": canonical_hash(spec), "frozen_spec": frozen,
                      "execution_hash": self._execution_hash(state), "upstream": dependencies,
                      "adapter": attestation, "created_at": _now()}
            self._artifact(state, "permits", permit)
            self._commit(state)
            return clone(permit)

    def start(self, task_id, permit_id, spec, request_id, launcher):
        validate_id(request_id)
        spec = execution.normalize_spec(spec, require_live_cwd=False)
        fingerprint = canonical_hash({"permit_id": permit_id, "spec": spec})
        if not callable(launcher):
            raise V3Error("LAUNCHER_REQUIRED")

        with self._transaction(task_id) as generation:
            state = self.store.load(task_id)
            prior = next((run for run in state["runs"] if run["request_id"] == request_id), None)
            if prior:
                if prior["request_hash"] != fingerprint:
                    raise V3Error("REQUEST_CONFLICT")
                if prior["status"] == "STARTING":
                    prior["status"] = "START_UNKNOWN"
                    prior["launch_error"] = "handoff-not-reconciled"
                    self._commit(state)
                return clone(prior)
            permit = self._validate_start(state, generation, permit_id, spec)
            expected_seq = state["state_seq"]
            expected_revision = state["revision"]
            expected_subject = state["subject_hash"]
            context = self._context(state)
            frozen_spec = clone(permit["frozen_spec"])
            permit_registration = self._registration_hash(permit["adapter"])

        try:
            attestation = execution.check_adapter(self.action_adapter, frozen_spec, context)
            current_registration = self._registration_hash(attestation)
            if current_registration != permit_registration:
                raise V3Error("REGISTRATION_MISMATCH")
        except V3Error as exc:
            if exc.code in {"UNSUPPORTED", "REGISTRATION_MISMATCH"}:
                raise V3Error("INFRA_ERROR", "bound adapter registration unavailable") from exc
            raise

        with self._transaction(task_id, knowledge_guard=False) as generation:
            with self._knowledge_guard():
                state = self.store.load(task_id)
                prior = next((run for run in state["runs"] if run["request_id"] == request_id), None)
                if prior:
                    if prior["request_hash"] != fingerprint:
                        raise V3Error("REQUEST_CONFLICT")
                    return clone(prior)
                if state["state_seq"] != expected_seq or state["revision"] != expected_revision or state["subject_hash"] != expected_subject:
                    raise V3Error("STATE_SEQ_CONFLICT", str(state["state_seq"]))
                permit = self._validate_start(state, generation, permit_id, spec)
                if self._registration_hash(permit["adapter"]) != current_registration:
                    raise V3Error("REGISTRATION_MISMATCH")
                execution.verify_frozen(self.store, task_id, permit)
                run = {"id": execution.new_id("run"), "task_id": task_id, "permit_id": permit_id,
                       "kind": permit["kind"], "revision": state["revision"], "subject_hash": state["subject_hash"],
                       "request_id": request_id, "request_hash": fingerprint, "status": "STARTING",
                       "spec_hash": permit["spec_hash"], "execution_hash": permit["execution_hash"],
                       "inputs": [{"path": str(self.store.task_dir(task_id) / "frozen" / permit_id / entry["path"]),
                                   "sha256": entry["sha256"]} for entry in permit["spec"]["inputs"]],
                       "knowledge_snapshot_hash": (state.get("knowledge_snapshot") or {}).get("hash"),
                       "started_at": _now()}
                run["input_hash"] = canonical_hash(run["inputs"])
                state["runs"].append(run)
                self._commit(state)
            try:
                handoff = launcher(clone(permit["frozen_spec"]))
                canonical_hash(handoff)
                if not isinstance(handoff, dict):
                    raise V3Error("INVALID_HANDOFF_RECEIPT")
                if handoff.get("registration_hash") != permit_registration:
                    raise V3Error("REGISTRATION_MISMATCH")
                if handoff.get("started") is True and isinstance(handoff.get("launch_id"), str) and handoff["launch_id"].strip():
                    run["launch"] = clone(handoff)
                    run["launch_id"] = handoff["launch_id"]
                    run["registration_hash"] = permit_registration
                    run["status"] = "RUNNING"
                    if handoff.get("execution_complete") is True:
                        if type(handoff.get("returncode")) is not int or not isinstance(handoff.get("outputs"), list):
                            raise V3Error("INVALID_HANDOFF_RECEIPT")
                elif handoff.get("started") is False and handoff.get("definitely_not_started") is True:
                    run["launch"] = clone(handoff)
                    run["status"] = "START_FAILED"
                else:
                    run["status"] = "START_UNKNOWN"
                    run["launch_error"] = "handoff-identity-unconfirmed"
            except Exception as exc:
                run["status"] = "START_UNKNOWN"
                run["launch_error"] = getattr(exc, "code", type(exc).__name__)
            try:
                with self._knowledge_guard():
                    self._generation_ok(state, generation)
            except Exception:
                run["needs_revalidation"] = True
                state["needs_revalidation"] = True
            if state["revision"] != run["revision"] or state["subject_hash"] != run["subject_hash"]:
                run["needs_revalidation"] = True
                state["needs_revalidation"] = True
            self._commit(state)
            return clone(run)

    def reconcile_start(self, task_id, run_id, receipt, *, reconciler):
        """Read the trusted adapter journal without launching or accepting paths."""
        if not callable(reconciler):
            raise V3Error("RECONCILER_REQUIRED")
        if receipt is None:
            receipt = {}
        if not isinstance(receipt, dict) or set(receipt) - {"launch_id"}:
            raise V3Error("INVALID_RECONCILIATION_RECEIPT")
        with self._transaction(task_id):
            state = self.store.load(task_id)
            run = next((r for r in state["runs"] if r["id"] == run_id), None)
            if not run or run["status"] not in {"STARTING", "START_UNKNOWN", "RUNNING"}:
                raise V3Error("RUN_NOT_UNCERTAIN")
            permit = next((p for p in state["permits"] if p["id"] == run["permit_id"]), None)
            if permit is None:
                raise V3Error("PERMIT_NOT_FOUND")
            self.store.verify_artifact(task_id, "permits", permit)
            server_run = clone(run)
            server_run["frozen_spec"] = clone(permit["frozen_spec"])
            expected_seq = state["state_seq"]
            expected_hash = self._registration_hash(permit["adapter"])
            prior_launch = run.get("launch_id")

        result = reconciler(server_run, clone(receipt))
        try:
            canonical_hash(result)
        except V3Error as exc:
            raise V3Error("INVALID_RECONCILIATION") from exc
        if not isinstance(result, dict) or result.get("run_id") != run_id:
            raise V3Error("INVALID_RECONCILIATION")
        if result.get("started") is not True:
            if result.get("started") is False and result.get("definitely_not_started") is True:
                with self._transaction(task_id):
                    state = self.store.load(task_id)
                    run = next((r for r in state["runs"] if r["id"] == run_id), None)
                    if run is None:
                        raise V3Error("RUN_NOT_FOUND")
                    run["status"] = "START_FAILED"
                    run["reconciliation"] = clone(result)
                    self._commit(state)
                    return clone(run)
            raise V3Error("START_UNKNOWN")
        launch_id = result.get("launch_id")
        if not isinstance(launch_id, str) or not launch_id.strip() or result.get("registration_hash") != expected_hash:
            raise V3Error("INVALID_RECONCILIATION")
        if receipt.get("launch_id") is not None and receipt["launch_id"] != launch_id:
            raise V3Error("INVALID_RECONCILIATION", "launch_id mismatch")
        if prior_launch is not None and prior_launch != launch_id:
            raise V3Error("INVALID_RECONCILIATION", "launch identity changed")
        complete = result.get("execution_complete", False)
        if complete is True:
            if type(result.get("returncode")) is not int or not isinstance(result.get("outputs"), list):
                raise V3Error("INVALID_RECONCILIATION")
        elif complete is not False:
            raise V3Error("INVALID_RECONCILIATION")

        with self._transaction(task_id):
            state = self.store.load(task_id)
            run = next((r for r in state["runs"] if r["id"] == run_id), None)
            if run is None:
                raise V3Error("RUN_NOT_FOUND")
            if run.get("result") is not None:
                return clone(run)
            if state["state_seq"] != expected_seq:
                if run.get("reconciliation") and canonical_hash(run["reconciliation"]) == canonical_hash(result):
                    return clone(run)
                raise V3Error("STATE_SEQ_CONFLICT", str(state["state_seq"]))
            if run["status"] not in {"STARTING", "START_UNKNOWN", "RUNNING"}:
                raise V3Error("RUN_NOT_UNCERTAIN")
            run["launch_id"] = launch_id
            run["registration_hash"] = expected_hash
            run["reconciliation"] = clone(result)
            run["status"] = "RUNNING"
            self._commit(state)
            return clone(run)

    def finish(self, task_id, run_id, result=None):
        with self._transaction(task_id, knowledge_guard=False) as generation:
            state = self.store.load(task_id)
            run = next((r for r in state["runs"] if r["id"] == run_id), None)
            if not run:
                raise V3Error("RUN_NOT_FOUND")
            trusted = run.get("reconciliation", run.get("launch", {}))
            if result is None and trusted.get("execution_complete") is True:
                result = {"run_id": run_id, "returncode": trusted.get("returncode"),
                          "outputs": clone(trusted.get("outputs"))}
            result = clone(result)
            if run.get("result") is not None:
                if canonical_hash(run["result"]) != canonical_hash(result):
                    raise V3Error("RUN_RESULT_IMMUTABLE")
                return clone(run)
            if run["status"] != "RUNNING":
                raise V3Error("RUN_NOT_STARTED")
            if not isinstance(result, dict) or result.get("run_id") != run_id or type(result.get("returncode")) is not int:
                raise V3Error("INVALID_RUN_RESULT")
            launch = run.get("reconciliation", run.get("launch", {}))
            if launch.get("execution_complete") is True and type(launch.get("returncode")) is int:
                if (launch["returncode"] != result["returncode"] or
                        canonical_hash(launch.get("outputs")) != canonical_hash(result.get("outputs"))):
                    raise V3Error("RUN_RESULT_MISMATCH")
            else:
                if not callable(self.result_verifier):
                    raise V3Error("RUN_RESULT_VERIFIER_REQUIRED")
                verified = self.result_verifier(clone(run), clone(result))
                if (not isinstance(verified, dict) or verified.get("run_id") != run_id or
                        verified.get("execution_complete") is not True or
                        type(verified.get("returncode")) is not int or
                        verified["returncode"] != result["returncode"]):
                    raise V3Error("RUN_RESULT_NOT_VERIFIED")
                run["completion_verification"] = clone(verified)
            outputs = result.get("outputs")
            if not isinstance(outputs, list):
                raise V3Error("OUTPUTS_REQUIRED")
            permit = next(p for p in state["permits"] if p["id"] == run["permit_id"])
            for output in outputs:
                if not isinstance(output, dict) or not isinstance(output.get("path"), str) or not isinstance(output.get("sha256"), str):
                    raise V3Error("INVALID_OUTPUT")
                from pathlib import Path
                path = Path(output["path"])
                if not path.is_absolute() or not path.is_file() or path.is_symlink() or execution.file_hash(path) != output["sha256"]:
                    raise V3Error("OUTPUT_HASH_MISMATCH")
                try:
                    path.resolve().relative_to(Path(permit["frozen_spec"]["output_dir"]))
                except ValueError as exc:
                    raise V3Error("OUTPUT_SCOPE_MISMATCH") from exc
                current = path
                boundary = Path(permit["frozen_spec"]["output_dir"])
                while current != boundary:
                    if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                        raise V3Error("UNSAFE_OUTPUT")
                    current = current.parent
            execution.verify_frozen(self.store, task_id, permit)
            receipt = {"id": execution.new_id("run-receipt"), "kind": "RUN_RECEIPT", "task_id": task_id,
                       "run_id": run_id, "run_kind": run["kind"], "permit_id": run["permit_id"],
                       "revision": run["revision"], "subject_hash": run["subject_hash"],
                       "result": result, "created_at": _now()}
            self._artifact(state, "receipts", receipt)
            run["result"] = result
            run["outputs"] = clone(outputs)
            run["output_hash"] = canonical_hash(outputs)
            run["receipt_id"] = receipt["id"]
            run["status"] = "FINISHED" if result["returncode"] == 0 else "FAILED"
            with self._knowledge_guard():
                try:
                    self._generation_ok(state, generation)
                except V3Error as exc:
                    if exc.code != "REVALIDATION_REQUIRED":
                        raise
                    run["needs_revalidation"] = True
                    state["needs_revalidation"] = True
                self._commit(state)
            return clone(run)

    def _production_run(self, state):
        selected = state["decisions"].get("selected_run_id")
        runs = [r for r in state["runs"] if r["kind"] == "production" and r["status"] == "FINISHED" and (selected is None or r["id"] == selected)]
        if len(runs) != 1:
            raise V3Error("PRODUCTION_RUN_RECEIPT_REQUIRED", "select one successful production run")
        run = runs[0]
        self._run_knowledge_ok(state, run)
        if run.get("execution_hash") != self._execution_hash(state):
            raise V3Error("STALE_RUN_DEPENDENCIES")
        if not run.get("result", {}).get("outputs"):
            raise V3Error("RUN_OUTPUTS_REQUIRED")
        receipt = next((r for r in state["receipts"] if r["id"] == run.get("receipt_id")), None)
        if not receipt or receipt.get("kind") != "RUN_RECEIPT" or receipt.get("run_id") != run["id"]:
            raise V3Error("RUN_RECEIPT_REQUIRED")
        self.store.verify_artifact(state["task_id"], "receipts", receipt)
        for output in run["result"]["outputs"]:
            if execution.file_hash(output["path"]) != output["sha256"]:
                raise V3Error("OUTPUT_HASH_MISMATCH")
        return run

    def _analysis_chain(self, state, results):
        selected_run = self._production_run(state)
        goals = {goal["id"] for goal in state["intent"]["goals"]}
        passing = {r["item_id"] for r in results if r.get("verdict") == "PASS"}
        items = _items(state["plan"])
        # A production conclusion needs actual measurements, result judgment
        # and goal acceptance even when a caller omitted them from its plan.
        for rule in ("E2", "E3", "E5"):
            covered = set()
            for item in items:
                if item["owner_rule"] == rule and item.get("due_stage") == "ANALYZE" and item["id"] in passing:
                    covered.update(item["target_refs"])
            if not goals <= covered:
                raise V3Error("RESULT_CHAIN_REQUIRED", rule)
        for goal in goals:
            claims = {}
            for rule in ("E2", "E3", "E5"):
                claims[rule] = {item.get("claim_id", item["claim"]) for item in items
                                if item["owner_rule"] == rule and item.get("due_stage") == "ANALYZE"
                                and item["id"] in passing and goal in item["target_refs"]
                                and item.get("run_id", state["decisions"].get("selected_run_id")) == selected_run["id"]}
            if not (claims["E2"] & claims["E3"] & claims["E5"]):
                raise V3Error("RESULT_CLAIM_MAPPING_REQUIRED", goal)

    def complete(self, task_id, expected_revision):
        with self._transaction(task_id) as generation:
            state = self.store.load(task_id)
            self._cas(state, expected_revision)
            self._generation_ok(state, generation)
            if state["current_stage"] != "ANALYZE":
                raise V3Error("ANALYZE_STAGE_REQUIRED")
            receipt = self._valid_receipt(state, "ANALYZE")
            self._analysis_chain(state, receipt["rule_results"])
            state["status"] = "completed"
            return self._commit(state)
