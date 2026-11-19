"""Deterministic evidence policy. No source category is a truth ranking.

Semantic producers supply successful, content-bound review artifacts; this module
validates those artifacts, never performs semantic reasoning or launches work.
All public validators return detached JSON values or raise V3Error. evaluate
converts every validation/internal failure to a BLOCK RuleResult.
"""
import copy
import hashlib
import json
import math
from pathlib import Path

from .contracts import V3Error, canonical_hash


_RULES = {"E1", "E2", "E3", "E4", "E5"}
_SOURCES = {"human", "publication", "measurement", "case", "experience", "estimate"}
_TYPES = {"deterministic", "infra", "evidence_gap", "judgment", "advisory"}
_BINDINGS = ("task_id", "revision", "subject_hash", "plan_hash")
_NUMERIC = ("claim_id", "metric", "unit", "value", "window", "tolerance")


def _require(condition, code, detail=""):
    if not condition:
        raise V3Error(code, detail)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _number(value):
    return (type(value) in (int, float) and
            (type(value) is int or math.isfinite(value)))


def _same(left, right):
    return canonical_hash(left) == canonical_hash(right)


def _digest(value):
    return (isinstance(value, str) and len(value) == 64 and
            all(char in "0123456789abcdef" for char in value))


def _bound(record, context):
    for key in _BINDINGS:
        _require(key in record and key in context and
                 _same(record[key], context[key]), "stale-binding", key)


def _unique(rows, label):
    _require(isinstance(rows, list), "invalid-list", label)
    ids = [row.get("id") if isinstance(row, dict) else None for row in rows]
    _require(all(_text(value) for value in ids) and len(set(ids)) == len(ids),
             "duplicate-or-missing-id", label)


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate-source-key", key)
        result[key] = value
    return result


def _source(path, expected_hash):
    _require(_text(path) and _digest(expected_hash), "missing-source-binding")
    try:
        data = Path(path).read_bytes()
    except (OSError, ValueError) as exc:
        raise V3Error("source-unavailable", str(path)) from exc
    _require(hashlib.sha256(data).hexdigest() == expected_hash,
             "source-hash-mismatch", str(path))
    return data


def _source_json(path, expected_hash):
    try:
        value = json.loads(_source(path, expected_hash).decode("utf-8"),
                           object_pairs_hook=_strict_object)
        canonical_hash(value)
        return value
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, V3Error):
            raise
        raise V3Error("invalid-source-json", str(path)) from exc


def _operand(spec):
    _require(isinstance(spec, dict), "invalid-operand")
    if set(spec) == {"literal"}:
        value = spec["literal"]
    else:
        value = _source_json(spec.get("path"), spec.get("sha256"))
        pointer = spec.get("pointer")
        _require(isinstance(pointer, str) and (not pointer or pointer.startswith("/")),
                 "invalid-source-pointer")
        for part in pointer.split("/")[1:]:
            part = part.replace("~1", "/").replace("~0", "~")
            try:
                if isinstance(value, list):
                    _require(part.isdigit() and str(int(part)) == part,
                             "invalid-source-pointer")
                    value = value[int(part)]
                else:
                    _require(isinstance(value, dict), "invalid-source-pointer")
                    value = value[part]
            except (KeyError, IndexError) as exc:
                raise V3Error("invalid-source-pointer", pointer) from exc
    canonical_hash(value)
    return value


def _predicate(proof):
    operands = proof.get("operands")
    _require(isinstance(operands, list) and len(operands) == 2,
             "invalid-predicate-operands")
    left, right = [_operand(value) for value in operands]
    name = proof.get("predicate")
    if name == "eq":
        return _same(left, right)
    if name == "ne":
        return not _same(left, right)
    _require(_number(left) and _number(right), "invalid-predicate-number")
    if name == "lt":
        return left < right
    if name == "le":
        return left <= right
    if name == "gt":
        return left > right
    if name == "ge":
        return left >= right
    if name == "close":
        tolerance = proof.get("tolerance")
        _require(_number(tolerance) and tolerance >= 0, "invalid-tolerance")
        return abs(left - right) <= tolerance
    raise V3Error("unsupported-predicate", str(name))


def _proof(proof, context=None):
    """Recompute a small registered predicate, with explicit true premises.

    A proof carries task_id/revision/subject_hash/plan_hash and premises (a
    nonempty list of predicates). Operands are {literal: JSON} or a hash-bound
    local JSON {path, sha256, pointer}; no expressions or external code run.
    """
    _require(isinstance(proof, dict), "missing-proof")
    if context is not None:
        _bound(proof, context)
    else:
        for key in _BINDINGS:
            _require(key in proof, "missing-proof-binding", key)
    premises = proof.get("premises")
    _require(isinstance(premises, list) and bool(premises), "missing-premises")
    _require(all(isinstance(row, dict) and _predicate(row) for row in premises),
             "invalid-premises")
    return _predicate(proof)


def classify_finding(finding):
    """Normalize without trusting severity, AI 'hard', or a claimed proof."""
    _require(isinstance(finding, dict) and _text(finding.get("id")), "invalid-finding")
    canonical_hash(finding)
    result = copy.deepcopy(finding)
    kind = result.get("type", "judgment")
    _require(kind in _TYPES, "invalid-finding-type", str(kind))
    result["reported_type"] = result.get("reported_type", kind)
    if kind == "deterministic":
        try:
            _require(_proof(result.get("proof")) is False, "not-a-contradiction")
        except (V3Error, TypeError, KeyError, OverflowError) as exc:
            code = getattr(exc, "code", "invalid-proof")
            kind = "infra" if code in {
                "source-unavailable", "source-hash-mismatch", "invalid-source-json",
                "duplicate-source-key", "invalid-json-value", "invalid-source-pointer",
            } else "judgment"
            result["proof_error"] = code
    result["type"] = kind
    # A submitted status is not a resolution; evaluate revalidates resolution.
    result["status"] = "open"
    return result


def review_content_hash(obligation, evidence, context):
    """Hash the exact review input, excluding the review receipt itself.

    Review schema: status='success', method='semantic', the four context
    bindings, content_hash (or item_content_hashes), item_ids, evidence_refs,
    and conclusions containing
    {item_id, verdict, reason, supported_evidence_refs, applicability}.
    """
    return canonical_hash({
        "obligation": obligation, "evidence": evidence,
        "bindings": {key: context.get(key) for key in _BINDINGS},
        "run_id": context.get("run_id"),
        "findings": [{key: value for key, value in classify_finding(row).items()
                      if key not in {"status", "resolution"}}
                     for row in context.get("findings", [])],
        # Authority is validated separately on each disposition and cannot
        # change the factual review input or require the original AI to agree.
    })


def _review(obligation, evidence, context):
    review = context.get("review")
    _require(isinstance(review, dict), "review-required")
    _require(review.get("status") == "success" and
             review.get("method") == "semantic", "review-not-successful")
    _bound(review, context)
    item_id = obligation["id"]
    item_hashes = review.get("item_content_hashes", {})
    _require(isinstance(item_hashes, dict), "invalid-review-item-hashes")
    content_hash = item_hashes.get(item_id, review.get("content_hash"))
    _require(content_hash == review_content_hash(obligation, evidence, context),
             "review-content-mismatch")
    _require(isinstance(review.get("item_ids"), list) and item_id in review["item_ids"],
             "review-missing-item", item_id)
    refs = [row["id"] for row in evidence]
    _require(isinstance(review.get("evidence_refs"), list) and
             set(review["evidence_refs"]) == set(refs), "review-evidence-mismatch")
    conclusions = review.get("conclusions")
    _require(isinstance(conclusions, list), "review-missing-conclusions")
    matching = [row for row in conclusions if isinstance(row, dict) and
                row.get("item_id") == item_id]
    _require(len(matching) == 1, "review-ambiguous-conclusion")
    conclusion = copy.deepcopy(matching[0])
    if "reason" not in conclusion:
        reasons = conclusion.get("reasons")
        _require(isinstance(reasons, list) and bool(reasons) and all(_text(row) for row in reasons),
                 "review-invalid-conclusion")
        conclusion["reason"] = "; ".join(reasons)
    _require(conclusion.get("verdict") in {"PASS", "BLOCK", "NOT_APPLICABLE"} and
             _text(conclusion.get("reason")), "review-invalid-conclusion")
    supports = conclusion.get("supported_evidence_refs", conclusion.get("evidence_refs", []))
    conclusion["supported_evidence_refs"] = supports
    _require(isinstance(supports, list) and set(supports) <= set(refs),
             "review-unknown-evidence")
    return conclusion


def _authorization(authorization, item_id, context, finding_id=None):
    _require(isinstance(authorization, dict) and _text(authorization.get("id")),
             "invalid-authorization")
    _require(authorization.get("revoked") is False and
             authorization.get("decision") in {"accept", "accept_assumption", "accept_risk"} and
             _text(authorization.get("reason")), "invalid-authorization")
    _require(authorization.get("subject_hash") == context["subject_hash"] and
             authorization.get("scope") in (item_id, [item_id]),
             "authorization-scope-mismatch")
    _require(authorization.get("finding_id") == finding_id, "authorization-finding-mismatch")
    _require(authorization in context.get("authorizations", []), "authorization-not-current")
    return authorization


def _resolution_hash(finding):
    return canonical_hash({key: value for key, value in finding.items()
                           if key not in {"resolution", "status"}})


def resolve_finding(finding, resolution, context):
    """Return a resolution preserving the original finding, never mutate it.

    dispute remains open. accept requires a current exact-scope authorization.
    correct/not_applicable requires a successful semantic review bound to
    {finding_hash, resolution excluding review}; true contradictions and infra
    instead require a successful replacement proof and supersedes reference.
    """
    current = classify_finding(finding)
    _require(isinstance(resolution, dict), "invalid-resolution")
    canonical_hash(resolution)
    _require(resolution.get("finding_id") == current["id"] and
             resolution.get("finding_hash") == _resolution_hash(finding),
             "resolution-finding-mismatch")
    _bound(resolution, context)
    _require(_text(resolution.get("id")) and _text(resolution.get("reason")),
             "resolution-reason-required")
    action = resolution.get("action")
    _require(action in {"dispute", "accept", "correct", "not_applicable"},
             "invalid-resolution-action")
    result = copy.deepcopy(current)
    result["resolution"] = copy.deepcopy(resolution)
    if action == "dispute":
        result["status"] = "disputed"
        return result
    kind = current["type"]
    if action == "accept":
        _require(kind in {"judgment", "advisory", "evidence_gap"}, "finding-not-waivable")
        if kind == "evidence_gap":
            _require(current.get("claim_kind") == "design" and
                     current.get("allow_assumption") is True, "finding-not-waivable")
        authorization = resolution.get("authorization")
        _authorization(authorization, current.get("item_id"), context, current["id"])
        result["status"] = "accepted_assumption"
        return result
    _require(resolution.get("supersedes") == current["id"], "missing-supersedes")
    if kind == "deterministic":
        # A still-valid contradiction cannot disappear on the same subject.
        # Corrected inputs require a new subject and a bound replacement proof;
        # retain the old proof fingerprint so the correction stays traceable.
        old_proof = current["proof"]
        _require(old_proof.get("subject_hash") != context.get("subject_hash") and
                 type(context.get("revision")) is int and
                 context["revision"] > old_proof.get("revision", context["revision"]),
                 "deterministic-contradiction-persists")
        _require(resolution.get("supersedes_proof_hash") == canonical_hash(old_proof),
                 "missing-superseded-proof")
        _require(_proof(resolution.get("replacement_proof"), context) is True,
                 "correction-not-proven")
    if kind == "infra":
        recovery = resolution.get("recovery", {})
        _require(recovery.get("status") == "success", "infra-not-recovered")
        proof = recovery.get("proof")
        _require(isinstance(proof, dict) and any(
            isinstance(operand, dict) and "path" in operand
            for operand in proof.get("operands", [])), "infra-recovery-source-required")
        _require(_proof(proof, context) is True, "infra-recovery-not-proven")
    review = resolution.get("review")
    _require(isinstance(review, dict) and review.get("status") == "success" and
             review.get("method") == "semantic", "resolution-review-required")
    _bound(review, context)
    payload = {key: value for key, value in resolution.items() if key != "review"}
    _require(review.get("content_hash") == canonical_hash({
        "finding_hash": _resolution_hash(finding), "resolution": payload}),
        "resolution-review-content-mismatch")
    _require(review.get("decision") == action, "resolution-review-decision-mismatch")
    result["status"] = "corrected" if action == "correct" else "not_applicable"
    return result


def _findings(context, item_id):
    findings = context.get("findings", [])
    _unique(findings, "findings")
    limitations = []
    for original in findings:
        finding = classify_finding(original)
        # Unscoped findings conservatively affect every obligation.
        if finding.get("item_id") not in (None, item_id):
            continue
        if original.get("resolution") is not None:
            finding = resolve_finding(original, original["resolution"], context)
        elif finding["type"] == "deterministic":
            _bound(finding["proof"], context)
        if finding["type"] == "advisory" or finding["status"] in {
                "accepted_assumption", "corrected", "not_applicable"}:
            limitations.append({"finding_id": finding["id"], "status": finding["status"],
                                "reason": finding.get("resolution", {}).get("reason", "advisory")})
        else:
            raise V3Error("unresolved-finding", finding["id"] + ":" + finding["type"])
    return limitations


def _evidence(record):
    _require(record.get("source_kind") in _SOURCES, "invalid-source-kind")
    _require(_text(record.get("claim_id")), "missing-claim-id")
    _require(record.get("lifecycle") == "active", "inactive-evidence", record["id"])
    _require(isinstance(record.get("conditions"), dict), "missing-evidence-conditions")
    _require(isinstance(record.get("provenance_refs"), list) and
             all(_text(ref) for ref in record["provenance_refs"]), "invalid-provenance")
    verification = record.get("verification")
    _require(isinstance(verification, dict) and verification.get("status") == "verified" and
             verification.get("method") in {"content_review", "recompute", "deterministic"} and
             _text(verification.get("source_locator")), "evidence-not-verified", record["id"])


def _claim_id(obligation):
    claim = obligation.get("claim")
    if isinstance(claim, dict):
        claim = claim.get("id", claim.get("claim_id"))
    return obligation.get("claim_id", claim)


def _run_manifest(context, record):
    runs = context.get("runs", [])
    _unique(runs, "runs")
    matches = [row for row in runs if row["id"] == context.get("run_id")]
    _require(len(matches) == 1, "measurement-run-not-found")
    run = matches[0]
    _require(not run.get("needs_revalidation"), "measurement-run-stale-knowledge")
    if "knowledge_snapshot_hash" in context:
        _require(run.get("knowledge_snapshot_hash") == context["knowledge_snapshot_hash"],
                 "measurement-run-stale-knowledge")
    _require(run.get("task_id") == context["task_id"] and run.get("status") == "FINISHED" and
             _text(run.get("receipt_id")), "measurement-run-not-finished")
    for kind in ("inputs", "outputs"):
        rows = run.get(kind)
        _require(isinstance(rows, list) and bool(rows), "measurement-run-manifest-missing", kind)
        paths = set()
        for row in rows:
            _require(isinstance(row, dict) and _text(row.get("path")) and
                     _digest(row.get("sha256")), "measurement-run-manifest-invalid", kind)
            path = Path(row["path"])
            _require(path.is_absolute(), "measurement-run-manifest-invalid", kind)
            normalized = str(path.resolve())
            _require(normalized not in paths, "measurement-run-manifest-duplicate", kind)
            paths.add(normalized)
            _source(row["path"], row["sha256"])
        hash_key = "input_hash" if kind == "inputs" else "output_hash"
        _require(run.get(hash_key) == canonical_hash(rows), "measurement-run-manifest-hash", kind)
    verification = record["verification"]
    for kind, locator, hash_key in (("inputs", verification.get("input_locator"), "input_hash"),
                                    ("outputs", verification.get("source_locator"), "output_hash")):
        _require(_text(locator), "missing-source-binding")
        path = Path(locator)
        _require(path.is_absolute(), "measurement-run-source-not-absolute")
        matching = [row for row in run[kind] if
                    str(Path(row["path"]).resolve()) == str(path.resolve()) and
                    row["sha256"] == record.get(hash_key)]
        _require(len(matching) == 1, "measurement-source-not-in-run", kind)
    execution_hash = context.get("execution_hash")
    if execution_hash is not None:
        _require(run.get("execution_hash") == execution_hash, "measurement-run-stale-execution")
    return run


def _measurement(obligation, records, context):
    expected = obligation.get("measurement")
    _require(isinstance(expected, dict), "measurement-contract-required")
    for key in ("claim_id", "metric", "unit", "window", "tolerance"):
        _require(key in expected, "measurement-missing-field", key)
    _require(expected["claim_id"] == _claim_id(obligation), "measurement-claim-mismatch")
    _require(_text(context.get("run_id")), "measurement-run-required")
    for key in ("metric", "unit"):
        _require(_text(expected[key]), "invalid-measurement-field", key)
    _require(expected["window"] not in (None, "", [], {}), "invalid-measurement-window")
    _require(_number(expected["tolerance"]) and expected["tolerance"] >= 0,
             "invalid-measurement-number")
    if "value" in expected:
        _require(_number(expected["value"]), "invalid-measurement-number")
    for key in ("input_hash", "output_hash"):
        if key in expected:
            _require(_digest(expected[key]), "invalid-measurement-hash")
    matches = [row for row in records if row["source_kind"] == "measurement" and
               row["claim_id"] == expected["claim_id"]]
    _require(bool(matches), "current-measurement-required")
    failures = []
    for record in matches:
        try:
            bound = dict(record, **expected)
            _require(_number(record.get("value")) and _digest(record.get("input_hash")) and
                     _digest(record.get("output_hash")), "invalid-measurement-result")
            _run_manifest(context, record)
            for key in ("task_id", "run_id"):
                _require(record.get(key) == context[key] and
                         expected.get(key, context[key]) == context[key],
                         "measurement-binding-mismatch", key)
            for key in _NUMERIC + ("input_hash", "output_hash"):
                _require(key in record and _same(record[key], bound[key]),
                         "measurement-binding-mismatch", key)
            verification = record["verification"]
            _require(verification["method"] == "recompute", "measurement-not-recomputed")
            _source(verification.get("input_locator"), bound["input_hash"])
            source = _source_json(verification["source_locator"], bound["output_hash"])
            if isinstance(source, dict) and source.get("schema_version") == 1:
                from .measurements import verify_artifact
                verify_artifact(_run_manifest(context, record), source)
                _require(verification.get("raw_sources") == source.get("raw_sources"),
                         "measurement-raw-sources-mismatch")
                if "recipe" in expected:
                    _require(_same(expected["recipe"], source.get("recipe")),
                             "measurement-recipe-mismatch")
            rows = source.get("measurements", [source]) if isinstance(source, dict) else []
            _require(isinstance(rows, list), "invalid-measurement-source")
            candidates = [row for row in rows if isinstance(row, dict) and
                          row.get("claim_id") == expected["claim_id"]]
            _require(len(candidates) == 1, "ambiguous-measurement-source")
            actual = candidates[0]
            for key in ("task_id", "run_id", "input_hash", "claim_id", "metric", "unit", "window", "tolerance"):
                wanted = context[key] if key in ("task_id", "run_id") else bound[key]
                _require(key in actual and _same(actual[key], wanted), "source-binding-mismatch", key)
            _require(_number(actual.get("value")) and
                     abs(actual["value"] - bound["value"]) <= bound["tolerance"],
                     "source-value-mismatch")
            return [record["id"]]
        except V3Error as exc:
            failures.append(exc.code + (":" + exc.detail if exc.detail else ""))
    raise V3Error("measurement-invalid", "; ".join(failures))


def _effective_review(conclusion, obligation, context):
    if conclusion["verdict"] != "BLOCK":
        return conclusion
    refs = conclusion.get("finding_ids")
    _require(isinstance(refs, list) and bool(refs) and all(_text(ref) for ref in refs),
             "review-block", conclusion["reason"])
    _require(conclusion.get("blocking_basis") == "findings_only", "review-block", conclusion["reason"])
    current = {row["id"]: row for row in context.get("findings", [])}
    dispositions = []
    for ref in refs:
        _require(ref in current and current[ref].get("item_id") == obligation["id"],
                 "review-block-finding-mismatch", ref)
        finding = current[ref]
        _require(isinstance(finding.get("resolution"), dict), "review-block-finding-unresolved", ref)
        resolved = resolve_finding(finding, finding["resolution"], context)
        _require(resolved["status"] in {"corrected", "not_applicable", "accepted_assumption"},
                 "review-block-finding-unresolved", ref)
        dispositions.append(resolved)
    result = copy.deepcopy(conclusion)
    result["verdict"] = "PASS"
    result["derived_dispositions"] = [row["id"] for row in dispositions]
    return result


def _evaluate(rule, obligation, evidence, context, result):
    _require(rule in _RULES, "unknown-rule", str(rule))
    canonical_hash([obligation, evidence, context])
    _require(isinstance(obligation, dict) and _text(obligation.get("id")), "invalid-obligation")
    _require(obligation.get("owner_rule", rule) == rule, "wrong-owner-rule")
    _require(isinstance(context, dict), "invalid-context")
    for key in _BINDINGS:
        _require(key in context, "missing-context-binding", key)
    _require(_digest(context["subject_hash"]) and _digest(context["plan_hash"]) and
             _text(context["task_id"]) and type(context["revision"]) is int and
             context["revision"] >= 1, "invalid-context-binding")
    _unique(evidence, "evidence")
    _unique(context.get("authorizations", []), "authorizations")
    for record in evidence:
        _evidence(record)
    result["limitations"] = _findings(context, obligation["id"])
    conclusion = None
    if context.get("review") is not None:
        conclusion = _effective_review(_review(obligation, evidence, context), obligation, context)
    if rule == "E2":
        result["evidence_refs"] = _measurement(obligation, evidence, context)
        return "PASS"
    if rule != "E4" or not obligation.get("checks"):
        # A legal design assumption does not need to masquerade as material.
        if rule == "E1" and obligation.get("allow_assumption") is True and obligation.get("claim_kind") == "design":
            for authorization in context.get("authorizations", []):
                try:
                    _authorization(authorization, obligation["id"], context)
                except V3Error:
                    continue
                result["limitations"].append({"authorization_id": authorization["id"],
                                                "reason": authorization["reason"],
                                                "claim_id": _claim_id(obligation)})
                return "ACCEPTED_ASSUMPTION"
        conclusion = _effective_review(_review(obligation, evidence, context), obligation, context)
        if conclusion["verdict"] == "NOT_APPLICABLE":
            applicability = obligation.get("applicability")
            if isinstance(applicability, dict):
                applicability = applicability.get("status")
            _require(conclusion.get("applicability") == "inapplicable" and
                     applicability == "inapplicable", "unsupported-not-applicable")
            result["limitations"].append(conclusion["reason"])
            return "NOT_APPLICABLE"
        _require(conclusion.get("applicability") == "applicable", "applicability-unresolved")
    if rule == "E1":
        supports = conclusion.get("supported_evidence_refs", [])
        matching = [row["id"] for row in evidence if row["id"] in supports and
                    row["claim_id"] == _claim_id(obligation)]
        _require(bool(matching), "claim-support-required")
        result["evidence_refs"] = matching
    elif rule == "E3":
        result["evidence_refs"] = conclusion.get("supported_evidence_refs", [])
    elif rule == "E4":
        checks = obligation.get("checks")
        if checks:
            _unique(checks, "checks")
            proofs = context.get("proofs", [])
            _unique(proofs, "proofs")
            for check in checks:
                _require("proof" not in check and _text(check.get("proof_ref")),
                         "independent-proof-required", check["id"])
                matches = [row for row in proofs if row["id"] == check["proof_ref"]]
                _require(len(matches) == 1, "proof-not-found", check["proof_ref"])
                artifact = matches[0]
                _bound(artifact, context)
                _require(artifact.get("item_id") == obligation["id"], "proof-item-mismatch")
                verification = artifact.get("verification", {})
                _require(verification.get("status") == "verified" and
                         verification.get("method") == "deterministic", "proof-not-verified")
                proof = artifact.get("proof")
                _require(isinstance(proof, dict), "missing-proof")
                bound_proof = dict(proof, **{key: artifact[key] for key in _BINDINGS})
                _source(verification.get("source_locator"), artifact.get("input_hash"))
                operands = proof.get("operands", [])
                _require(any(isinstance(row, dict) and row.get("path") == verification.get("source_locator") and
                             row.get("sha256") == artifact.get("input_hash") for row in operands),
                         "proof-input-not-used")
                _require(_proof(bound_proof, context) is True, "deterministic-check-failed", check["id"])
        else:
            result["evidence_refs"] = conclusion.get("supported_evidence_refs", [])
    elif rule == "E5":
        criteria = obligation.get("acceptance")
        if criteria is None:
            target_refs = obligation.get("target_refs")
            _require(isinstance(target_refs, list) and all(_text(ref) for ref in target_refs),
                     "acceptance-required")
            criteria = [{"id": ref} for ref in target_refs]
        _unique(criteria, "acceptance")
        _require(bool(criteria), "acceptance-required")
        assessments = conclusion.get("acceptance_results")
        _unique(assessments, "acceptance-results")
        _require({row["id"] for row in assessments} == {row["id"] for row in criteria},
                 "acceptance-coverage-mismatch")
        limitations = conclusion.get("limitations", context["review"].get("limitations"))
        _require(isinstance(limitations, list), "acceptance-limitations-required")
        for assessment in assessments:
            _require(assessment.get("passed") is True and _text(assessment.get("reason")),
                     "acceptance-failed", assessment["id"])
        result["limitations"].extend(limitations)
        result["evidence_refs"] = conclusion.get("supported_evidence_refs", [])
    return "PASS"


def evaluate(rule, obligation, evidence, context):
    """Evaluate E1-E5; malformed, stale, unsupported or failed inputs BLOCK.

    E2 consumes obligation.measurement and hash-verified local JSON sources.
    E4 consumes obligation.checks proof_ref IDs and independent context.proofs.
    E5 requires
    obligation.acceptance and review conclusion.acceptance_results, never just
    rollback_target. Incoming PASS/status fields have no authority.
    """
    result = {"rule": rule, "item_id": obligation.get("id") if isinstance(obligation, dict) else None,
              "subject_hash": context.get("subject_hash") if isinstance(context, dict) else None,
              "plan_hash": context.get("plan_hash") if isinstance(context, dict) else None,
              "verdict": "BLOCK", "reasons": [], "evidence_refs": [], "limitations": []}
    try:
        result["verdict"] = _evaluate(rule, obligation, evidence, context, result)
    except Exception as exc:
        result["verdict"] = "BLOCK"
        result["reasons"].append({"code": getattr(exc, "code", "policy-internal-error"),
                                   "detail": getattr(exc, "detail", type(exc).__name__)})
    return result


def validate_promotion(proposal):
    """Validate promotion only; never write knowledge or count reuse as proof.

    proposal has id, kind, content, conditions, source_refs, sources, review and
    (for expert_policy) confirmations. sources is a DAG of {id, provenance_refs,
    source_locator?}; every leaf has an original locator. Review and distinct
    human confirmation events bind promotion_content_hash(proposal).
    """
    _require(isinstance(proposal, dict), "invalid-promotion")
    canonical_hash(proposal)
    _require(_text(proposal.get("id")) and proposal.get("kind") in {
        "check_knowledge", "expert_opinion", "expert_policy", "case", "experience"},
        "invalid-promotion-kind")
    _require(bool(proposal.get("content")) and isinstance(proposal.get("conditions"), dict),
             "promotion-content-required")
    sources = proposal.get("sources")
    _unique(sources, "sources")
    _require(bool(sources), "promotion-sources-required")
    by_id = {row["id"]: row for row in sources}
    refs = proposal.get("source_refs")
    _require(isinstance(refs, list) and bool(refs) and all(ref in by_id for ref in refs),
             "promotion-source-ref-missing")
    roots, visiting, visited = set(), set(), set()

    def visit(source_id):
        _require(source_id not in visiting and source_id != proposal["id"],
                 "promotion-provenance-cycle", source_id)
        if source_id in visited:
            return
        visiting.add(source_id)
        source = by_id[source_id]
        parents = source.get("provenance_refs")
        _require(isinstance(parents, list) and all(_text(ref) for ref in parents),
                 "promotion-provenance-required", source_id)
        if parents:
            for parent in parents:
                _require(parent in by_id, "promotion-source-ref-missing", parent)
                visit(parent)
        else:
            _require(_text(source.get("source_locator")), "promotion-original-source-required", source_id)
            roots.add(source["source_locator"].strip())
        visiting.remove(source_id)
        visited.add(source_id)

    for source_id in by_id:
        visit(source_id)
    digest = promotion_content_hash(proposal)
    review = proposal.get("review", {})
    _require(isinstance(review, dict) and review.get("actor_kind") == "human" and
             _text(review.get("actor_id")) and _text(review.get("event_id")) and
             review.get("status") == "approved" and review.get("revoked") is False and
             review.get("content_hash") == digest and _text(review.get("reason")),
             "promotion-human-review-required")
    if proposal["kind"] == "expert_policy":
        confirmations = proposal.get("confirmations")
        _unique(confirmations, "confirmations")
        _require(len(confirmations) >= 2, "promotion-two-events-required")
        events = set()
        for event in confirmations:
            _require(event.get("actor_kind") == "human" and _text(event.get("actor_id")) and
                     _text(event.get("event_id")) and event["event_id"] not in events and
                     event.get("content_hash") == digest and event.get("decision") == "confirm" and
                     event.get("revoked") is False, "promotion-invalid-confirmation")
            events.add(event["event_id"])
    referenced_roots = set()

    def collect(source_id):
        row = by_id[source_id]
        if row["provenance_refs"]:
            for parent in row["provenance_refs"]:
                collect(parent)
        else:
            referenced_roots.add(row["source_locator"].strip())

    for ref in refs:
        collect(ref)
    return {"ok": True, "id": proposal["id"], "kind": proposal["kind"],
            "content_hash": digest, "source_refs": sorted(set(refs)),
            "independent_sources": sorted(referenced_roots),
            "limitations": ["Human approval is policy authority, not factual verification.",
                            "Source reuse and duplicate projections do not add support."]}


def promotion_content_hash(proposal):
    """Approval content includes scope/provenance, never approval events themselves."""
    return canonical_hash({key: value for key, value in proposal.items()
                           if key not in {"review", "confirmations"}})
