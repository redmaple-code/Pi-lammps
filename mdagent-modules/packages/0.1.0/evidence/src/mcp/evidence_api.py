"""MCP adaptation for the task evidence service."""
import os
import sys
from pathlib import Path

EVO = str(Path(__file__).resolve().parent.parent / "evolution")
if EVO not in sys.path:
    sys.path.insert(0, EVO)

from evidence_v3.contracts import V3Error
from evidence_v3 import planning, retrieval


_DEFAULT_PROFILE = Path(__file__).resolve().parent / "data" / "evidence_execution_profiles" / "lammps_static_v1.json"
DEFAULT_REVIEW_BUDGET = {"max_bytes": 96000, "max_tokens": 96000, "max_attempts": 2,
                         "max_batches": 64, "max_calls": 96, "deadline_seconds": 180}


def reviewer_identity(transport=None):
    from evidence_v3.contracts import canonical_hash
    if transport is not None:
        return canonical_hash({"injected_transport": getattr(transport, "__module__", ""),
                              "name": getattr(transport, "__qualname__", type(transport).__qualname__)})
    import model_research
    providers = [(name, endpoint, model) for name, endpoint, model, _secret in model_research._providers()]
    return canonical_hash({"providers": providers, "evidence-review-protocol": "v3-review-2"})


def _load_profile(path):
    from evidence_v3.contracts import read_json
    profile_path = Path(path).resolve()
    if profile_path != _DEFAULT_PROFILE.resolve():
        raise V3Error("UNSUPPORTED", "execution profile is not an approved server data file")
    return read_json(profile_path)


def task_service(data_root, action_adapter=None, result_verifier=None, knowledge_reviewer=None,
                 human_verifier=None, atom_writer=None):
    from evidence_v3.service import TaskService
    return TaskService(data_root, action_adapter=action_adapter, result_verifier=result_verifier,
                       knowledge_reviewer=knowledge_reviewer, human_verifier=human_verifier,
                       atom_writer=atom_writer)


def dispatch(args, data_root, action_adapter=None, transport=None, *, result_verifier=None,
             knowledge_reviewer=None, human_verifier=None, atom_writer=None, review_budget=None,
             reviewer_id=None, discovery_budget=None):
    """Only a successful service result can change a task or issue a receipt."""
    if not isinstance(args, dict):
        return V3Error("invalid-arguments").as_dict()
    try:
        action = args.get("action", "status")
        task_id = args.get("task_id") or args.get("case_id")
        if action == "recognize":
            import evidence_review
            request = args.get("request")
            if isinstance(request, str):
                request = {"request": request}
            return evidence_review.recognize_intent(request, transport=transport)
        if action == "retrieve":
            intent = planning.validate_intent(args.get("intent"))
            return retrieval.search(intent, data_root,
                                    knowledge_dirs=args.get("knowledge_dirs"),
                                    limit=args.get("limit", 20))
        if action == "propose":
            import evidence_review
            return evidence_review.propose_checks(args.get("intent"), args.get("bundle"),
                                                   transport=transport)
        if action == "discover":
            import evidence_discovery
            options = dict(discovery_budget or {})
            return evidence_discovery.discover(args.get("request"), data_root, transport=transport,
                                               **options)
        svc = task_service(data_root, action_adapter, result_verifier, knowledge_reviewer,
                           human_verifier, atom_writer)
        if action == "inventory":
            from evidence_v3 import migration
            return migration.inventory(data_root, project_roots=args.get("project_roots"))
        if action == "migrate":
            from evidence_v3 import migration
            return migration.migrate(args.get("source_path"), svc, task_id,
                                     args.get("intent"), args.get("request_id"))
        if action == "init":
            return svc.create(task_id, args.get("intent"), request_id=args.get("request_id"))
        if action == "status":
            return svc.read(task_id)
        revision = args.get("expected_revision")
        if action == "revise":
            return svc.revise(task_id, args.get("patch"), revision,
                              expected_state_seq=args.get("expected_state_seq"),
                              request_id=args.get("request_id"))
        if action == "plan":
            snapshot = svc.read(task_id)
            plan = planning.prepare_plan(snapshot["intent"], args.get("bundle"),
                                         args.get("proposal"))
            return svc.set_plan(task_id, plan, revision)
        if action == "proof":
            return svc.record_proof(task_id, args.get("record"), revision)
        if action == "evidence":
            return svc.record_evidence(task_id, args.get("record"), revision)
        if action == "revoke_authorization":
            return svc.revoke_authorization(task_id, args.get("authorization_id"), revision,
                                             args.get("reason"), source_refs=args.get("source_refs"))
        if action == "authorization":
            return svc.record_authorization(task_id, args.get("authorization"), revision)
        if action == "review":
            import evidence_review
            snapshot = svc.read(task_id)
            kind = args.get("kind", "plan")
            if kind == "validation":
                snapshot["validation_spec"] = args.get("spec")
            if kind in ("input", "validation"):
                snapshot = attach_inputs(snapshot, args.get("spec"))
                if kind == "validation":
                    from evidence_v3.execution import normalize_spec
                    checked = normalize_spec(args.get("spec"))
                    attestation = action_adapter(checked, snapshot) if callable(action_adapter) else {}
                    snapshot["input_dependencies_complete"] = attestation.get("dependencies_complete") is True
            effective_budget = review_budget
            effective_reviewer = reviewer_id
            if effective_budget is None:
                effective_budget = DEFAULT_REVIEW_BUDGET
            if effective_reviewer is None:
                effective_reviewer = reviewer_identity(transport)
            review = evidence_review.run_review(snapshot, transport=transport, kind=kind,
                                                budget=effective_budget,
                                                artifact_root=data_root,
                                                reviewer_id=effective_reviewer)
            if review.get("status") != "success":
                return review
            return svc.record_review(task_id, review, revision)
        if action == "evaluate":
            return svc.evaluate(task_id, stage=args.get("stage"))
        if action == "advance":
            return svc.advance(task_id, args.get("to_stage"), revision,
                               receipt_id=args.get("receipt_id"))
        if action == "resolve_review":
            import evidence_review
            return evidence_review.review_resolution(svc.read(task_id), args.get("finding_id"),
                                                       args.get("resolution"), transport=transport)
        if action == "resolve":
            return svc.resolve(task_id, args.get("finding_id"), args.get("resolution"), revision)
        if action == "authorize":
            return svc.authorize(task_id, args.get("spec"), revision)
        if action == "finish":
            return svc.finish(task_id, args.get("run_id"), args.get("result"))
        if action == "submit_evidence":
            from evolution.kb import adapter
            if not adapter.enabled():
                raise V3Error("UKB_REQUIRED_FOR_EVIDENCE_SUBMISSION")
            record = args.get("record")
            task_id = args.get("task_id") or (record.get("task_id") if isinstance(record, dict) else None)
            if not isinstance(record, dict) or not task_id:
                raise V3Error("EVIDENCE_RECORD_REQUIRED")
            return adapter.submit_evidence_record(record, task_id, data_root)
        if action == "publish":
            from evidence_v3 import knowledge
            return knowledge.publish(args.get("proposal"), args.get("approver"), data_root)
        if action == "refresh_knowledge":
            return svc.refresh_knowledge(task_id, revision,
                                         expected_state_seq=args.get("expected_state_seq"),
                                         request_id=args.get("request_id"))
        if action == "inspect_publication":
            from evidence_v3 import knowledge
            return knowledge.inspect_publication(data_root, args.get("event_id"))
        if action == "recover_publication":
            from evidence_v3 import knowledge
            return knowledge.recover_publication(data_root, args.get("event_id"),
                                                  args.get("decision"), approver=args.get("approver"),
                                                  reason=args.get("reason"))
        if action == "measure":
            return svc.record_measurement(task_id, args.get("run_id"), args.get("item_id"),
                                           args.get("recipe"), args.get("evidence_id"), revision,
                                           conditions=args.get("conditions"))
        if action == "complete":
            return svc.complete(task_id, revision)
        raise V3Error("unknown-action", str(action))
    except V3Error as exc:
        return exc.as_dict()
    except Exception as exc:
        return {"error": "evidence-internal-error", "detail": type(exc).__name__,
                "fail_closed": True}


def attach_inputs(snapshot, spec=None):
    """Read complete declared text inputs without substituting truncated previews."""
    from evidence_v3.contracts import file_hash
    from evidence_v3.execution import normalize_spec
    from evidence_v3.store import clone
    result = clone(snapshot)
    dependencies = result.get("dependencies") or {}
    if spec is not None:
        normalized = normalize_spec(spec)
        cwd = normalized["cwd"]
        manifest = normalized["inputs"]
    else:
        cwd = dependencies.get("cwd")
        manifest = dependencies.get("inputs")
    if not isinstance(cwd, str) or not Path(cwd).is_dir() or not isinstance(manifest, list) or not manifest:
        raise V3Error("input-review-material-required")
    root = Path(cwd).resolve()
    inputs = []
    for item in manifest:
        relative = item.get("path")
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise V3Error("invalid-input-path")
        source = root / relative
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise V3Error("unsafe-input-path")
        if file_hash(source) != item.get("sha256"):
            raise V3Error("input-changed", relative)
        try:
            payload = source.read_bytes()
            # Semantic review of unsupported binary formats needs an explicit adapter.
            text = payload.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise V3Error("unsupported-input-review", relative) from exc
        import hashlib
        if hashlib.sha256(payload).hexdigest() != item["sha256"]:
            raise V3Error("input-changed", relative)
        inputs.append({"path": relative, "content": text, "sha256": item["sha256"]})
    result["inputs"] = inputs
    result["input_dependencies_complete"] = snapshot.get("input_dependencies_complete") is True
    if spec is not None:
        result["input_dependencies_complete"] = False
    return result


class UnsupportedExecutionAdapter:
    reason = "no audited execution registration is configured"

    def __call__(self, spec, context):
        raise V3Error("UNSUPPORTED", self.reason)


def configured_action_adapter(config, *, bridge_exec=None, worker_launcher=None):
    """Build only registrations pinned by server-owned executable and profile data."""
    from evidence_execution import ControlledExecutionAdapter, build_registration
    if bridge_exec is None:
        raise V3Error("UNSUPPORTED", "audited execution bridge is unavailable")
    config = config if isinstance(config, dict) else {}
    raw = config.get("evidence_execution_registrations", [])
    if not isinstance(raw, list):
        raise V3Error("INVALID_EXECUTION_REGISTRATIONS")
    registrations = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"profile_id", "executable", "command", "kinds", "entrypoints"}:
            raise V3Error("INVALID_EXECUTION_REGISTRATION")
        if entry["profile_id"] != "local-lammps-static-v1":
            raise V3Error("UNSUPPORTED", "unknown execution profile")
        profile = _load_profile(_DEFAULT_PROFILE)
        profile["entrypoints"] = entry["entrypoints"]
        registrations.append(build_registration(profile, entry["executable"], entry["command"],
                                                kinds=entry["kinds"], registration_id=entry["profile_id"]))
    return ControlledExecutionAdapter(registrations, bridge_exec, worker_launcher=worker_launcher) if registrations else UnsupportedExecutionAdapter()


TOOL = {
    "name": "sim_evidence",
    "description": (
        "开放证据工作流：先recognize识别目标与未知，retrieve检索候选及反例，"
        "propose生成动态核查计划；init建档后plan/evidence/review/evaluate/advance。"
        "人工授权与事实分开；review内部故障不放行；authorize只签发当前输入的动作许可。"
        "状态变更需expected_revision；status返回当前版本。只读咨询不要求生产许可。"),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": [
                "recognize", "retrieve", "propose", "discover", "init", "status", "revise", "plan",
                "evidence", "proof", "authorization", "revoke_authorization", "review", "evaluate", "advance", "resolve_review", "resolve", "authorize", "finish", "submit_evidence", "publish", "inspect_publication", "recover_publication", "refresh_knowledge", "measure", "inventory", "migrate", "complete"]},
            "task_id": {"type": "string"}, "case_id": {"type": "string"},
            "request": {"type": "string"}, "intent": {"type": "object"},
            "bundle": {"type": "object"}, "proposal": {"type": "object"},
            "recipe": {"type": "object"}, "conditions": {"type": "object"},
            "evidence_id": {"type": "string"}, "item_id": {"type": "string"},
            "publication_id": {"type": "string"}, "event_id": {"type": "string"},
            "decision": {"type": "string", "enum": ["commit", "rollback"]},
            "record": {"type": "object"}, "authorization": {"type": "object"}, "patch": {"type": "object"},
            "resolution": {"type": "object"}, "spec": {"type": "object"},
            "expected_revision": {"type": "integer"},
            "expected_state_seq": {"type": "integer"},
            "request_id": {"type": "string"}, "receipt_id": {"type": "string"},
            "run_id": {"type": "string"}, "result": {"type": "object"},
            "approver": {"type": "string"}, "authorization_id": {"type": "string"},
            "reason": {"type": "string"}, "source_refs": {"type": "array", "items": {"type": "string"}},
            "source_path": {"type": "string"}, "project_roots": {"type": "array", "items": {"type": "string"}},
            "finding_id": {"type": "string"}, "stage": {"type": "string"},
            "to_stage": {"type": "string"}, "kind": {"type": "string", "enum": ["plan", "input", "validation"]},
            "limit": {"type": "integer", "minimum": 1},
            "knowledge_dirs": {"type": "array", "items": {"type": "string"}}
        }, "required": ["action"], "additionalProperties": False
    }
}
