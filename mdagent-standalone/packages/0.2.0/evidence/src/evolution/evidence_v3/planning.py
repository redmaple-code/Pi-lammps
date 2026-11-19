"""Structural planning and coverage; semantic decisions belong to reviewers.

The AI adapter supplies the intent, applicability and discovery review. This
module validates their shape and bindings, never invents a checklist or judges
whether a source supports a claim. Only policy RuleResults satisfy obligations.
"""
import copy

from .contracts import RULES, STAGES, V3Error, canonical_hash, validate_id

_ACTIONS = {"consult", "design", "validation", "production", "build", "analyze"}
_VERDICTS = {"PASS", "BLOCK", "ACCEPTED_ASSUMPTION", "NOT_APPLICABLE"}


def _need(condition, code, detail=""):
    if not condition:
        raise V3Error(code, detail)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _texts(value, name, nonempty=True):
    _need(isinstance(value, list) and (bool(value) or not nonempty)
          and all(_text(v) for v in value), "invalid-plan-field", name)
    _need(len(set(value)) == len(value), "duplicate-plan-reference", name)
    return value


def validate_intent(intent):
    """Validate an adapter's structured intent, without classifying raw prose."""
    _need(isinstance(intent, dict), "invalid-intent")
    canonical_hash(intent)
    result = copy.deepcopy(intent)
    _need(isinstance(result.get("action"), str) and result["action"] in _ACTIONS, "invalid-intent-action")
    goals = result.get("goals")
    _need(isinstance(goals, list) and bool(goals), "invalid-intent-goals")
    ids = set()
    for goal in goals:
        _need(isinstance(goal, dict) and _text(goal.get("text")), "invalid-intent-goal")
        identity = validate_id(goal.get("id"))
        _need(identity not in ids, "duplicate-intent-goal", identity)
        ids.add(identity)
        if "source_refs" in goal:
            _texts(goal["source_refs"], "goal.source_refs")
    _need(isinstance(result.get("conditions"), dict), "invalid-intent-conditions")
    unknowns = result.get("unknowns")
    _need(isinstance(unknowns, list), "invalid-intent-unknowns")
    for unknown in unknowns:
        _need(_text(unknown) or (isinstance(unknown, dict) and _text(unknown.get("text"))), "invalid-intent-unknown")
        if isinstance(unknown, dict):
            validate_id(unknown.get("id"))
            if "source_refs" in unknown:
                _texts(unknown["source_refs"], "unknown.source_refs")
    _texts(result.get("source_refs"), "intent.source_refs")
    queries = result.get("queries", [])
    _need(isinstance(queries, list), "invalid-intent-queries")
    for query in queries:
        _need(_text(query) or (isinstance(query, dict) and _text(query.get("text"))), "invalid-intent-query")
        if isinstance(query, dict) and "purpose" in query:
            _need(_text(query["purpose"]), "invalid-query-purpose")
    return result


def hash_plan(plan):
    """Stable full-plan hash excluding its own digest field."""
    _need(isinstance(plan, dict), "invalid-plan")
    return canonical_hash({k: v for k, v in plan.items() if k != "plan_hash"})


def _bundle(bundle, intent_hash):
    _need(isinstance(bundle, dict), "invalid-retrieval-bundle")
    _need(bundle.get("intent_hash") == intent_hash, "stale-retrieval-intent")
    digest = canonical_hash({k: v for k, v in bundle.items() if k != "bundle_hash"})
    _need(bundle.get("bundle_hash") == digest, "stale-retrieval-bundle")
    for field in ("queries", "candidates", "health", "manifest", "exclusions", "limitations"):
        _need(isinstance(bundle.get(field), list), "invalid-retrieval-field", field)
    _need(bundle.get("status") in ("ok", "empty", "error", "unavailable"), "invalid-retrieval-status")
    for entry in bundle["health"]:
        _need(isinstance(entry, dict) and _text(entry.get("source"))
              and entry.get("status") in ("ok", "empty", "error", "unavailable"), "invalid-source-health")
    query_ids = set()
    for query in bundle["queries"]:
        _need(isinstance(query, dict) and _text(query.get("id"))
              and _text(query.get("text")) and _text(query.get("purpose")), "invalid-retrieval-query")
        _need(query["id"] not in query_ids, "duplicate-retrieval-query")
        query_ids.add(query["id"])
    return digest


def _items(items, intent):
    _need(isinstance(items, list) and bool(items), "empty-plan")
    goal_ids = {g["id"] for g in intent["goals"]}
    seen, normalized = set(), []
    for item in items:
        _need(isinstance(item, dict), "invalid-obligation")
        identity = validate_id(item.get("id"))
        _need(identity not in seen, "duplicate-obligation", identity)
        seen.add(identity)
        for field in ("question", "claim", "rationale"):
            _need(_text(item.get(field)), "invalid-obligation-field", identity + "." + field)
        targets = _texts(item.get("target_refs"), "target_refs")
        _need(bool(goal_ids.intersection(targets)), "unmapped-obligation", identity)
        _texts(item.get("origin_refs"), "origin_refs")
        _need(isinstance(item.get("owner_rule"), str) and item["owner_rule"] in RULES,
              "invalid-owner-rule", identity)
        _need(isinstance(item.get("due_stage"), str) and item["due_stage"] in STAGES,
              "invalid-due-stage", identity)
        applicable = item.get("applicability")
        _need(isinstance(applicable, dict) and applicable.get("status") in ("applicable", "unknown", "inapplicable")
              and _text(applicable.get("reason")), "invalid-applicability", identity)
        _texts(applicable.get("source_refs"), "applicability.source_refs")
        _texts(item.get("required_sources", []), "required_sources", False)
        if "required" in item:
            _need(type(item["required"]) is bool, "invalid-required-flag", identity)
        output = copy.deepcopy(item)
        # Caller-supplied success/status is not a policy result.
        output["status"] = "awaiting_evidence"
        normalized.append(output)
    return normalized


def _review(review, intent, bundle, raw_items):
    _need(isinstance(review, dict), "missing-discovery-review")
    validate_id(review.get("id"))
    _need(review.get("status") == "success", "discovery-review-not-successful")
    _texts(review.get("source_refs"), "discovery_review.source_refs")
    for field, expected in (("intent_hash", canonical_hash(intent)),
                            ("retrieval_hash", bundle["bundle_hash"]),
                            ("items_hash", canonical_hash(raw_items))):
        _need(review.get(field) == expected, "stale-discovery-review", field)
    goal_map = review.get("goal_coverage")
    _need(isinstance(goal_map, dict), "invalid-goal-coverage")
    items_by_id = {item["id"]: item for item in raw_items}
    _need(set(goal_map) == {g["id"] for g in intent["goals"]}, "incomplete-goal-coverage")
    for goal, identities in goal_map.items():
        _texts(identities, "goal_coverage." + goal)
        _need(all(identity in items_by_id and goal in items_by_id[identity]["target_refs"]
                  for identity in identities), "invalid-goal-mapping", goal)
    counter = review.get("counterexample_review")
    _need(isinstance(counter, dict) and counter.get("status") == "success", "counterexample-review-incomplete")
    _texts(counter.get("source_refs"), "counterexample_review.source_refs")
    identities = _texts(counter.get("reviewed_item_ids"), "counterexample_review.reviewed_item_ids")
    _need(set(identities) == set(items_by_id), "counterexample-items-incomplete")
    query_refs = _texts(counter.get("query_refs"), "counterexample_review.query_refs", False)
    known_queries = {q["id"] for q in bundle["queries"] if q["purpose"] == "counterexample"}
    _need(set(query_refs).issubset(known_queries), "unknown-counterexample-query")
    _texts(counter.get("limitations", []), "counterexample_review.limitations", False)
    _need(bool(query_refs) or bool(counter.get("limitations")), "missing-counterexample-query-or-limitation")
    unknowns = review.get("remaining_unknowns")
    _need(isinstance(unknowns, list), "missing-remaining-unknowns")
    unknown_ids = set()
    for unknown in unknowns:
        _need(isinstance(unknown, dict) and _text(unknown.get("text")), "invalid-remaining-unknown")
        identity = validate_id(unknown.get("id"))
        _need(identity not in unknown_ids, "duplicate-remaining-unknown", identity)
        unknown_ids.add(identity)
        _need(unknown.get("due_stage") in STAGES and type(unknown.get("blocking")) is bool,
              "invalid-remaining-unknown-disposition", identity)
        _texts(unknown.get("source_refs"), "remaining_unknown.source_refs")
    return copy.deepcopy(review)


def prepare_plan(intent, bundle, proposal):
    """Validate a nonempty dynamic proposal and its bound discovery review.

    The full retrieval snapshot is retained so health cannot turn into silence.
    Required unhealthy sources block coverage; optional outages remain visible.
    Task-derived origin_refs need not be knowledge IDs.
    """
    intent = validate_intent(intent)
    intent_hash = canonical_hash(intent)
    _bundle(bundle, intent_hash)
    _need(isinstance(proposal, dict), "invalid-plan-proposal")
    canonical_hash(proposal)
    raw_items = proposal.get("items")
    items = _items(raw_items, intent)
    review = _review(proposal.get("discovery_review"), intent, bundle, raw_items)
    findings = proposal.get("findings", [])
    _need(isinstance(findings, list) and all(isinstance(f, dict) and _text(f.get("id")) for f in findings), "invalid-plan-findings")
    _need(len({f["id"] for f in findings}) == len(findings), "duplicate-plan-finding")
    plan = {"schema_version": 3, "revision": 1, "status": "active", "intent": intent,
            "intent_hash": intent_hash, "retrieval_hash": bundle["bundle_hash"],
            "retrieval": copy.deepcopy(bundle), "proposal_items": copy.deepcopy(raw_items),
            "items": items, "discovery_review": review, "findings": copy.deepcopy(findings),
            "change_history": []}
    plan["plan_hash"] = hash_plan(plan)
    return plan


def _validate_plan(plan):
    _need(isinstance(plan, dict), "invalid-plan")
    _need(plan.get("plan_hash") == hash_plan(plan), "stale-plan")
    _need(plan.get("schema_version") == 3 and plan.get("status") in ("active", "revised"), "inactive-plan")
    intent = validate_intent(plan.get("intent"))
    _need(plan.get("intent_hash") == canonical_hash(intent), "stale-plan-intent")
    bundle = plan.get("retrieval")
    _bundle(bundle, plan["intent_hash"])
    _need(plan.get("retrieval_hash") == bundle["bundle_hash"], "stale-plan-retrieval")
    normalized = _items(plan.get("proposal_items"), intent)
    _need(normalized == plan.get("items"), "changed-plan-items")
    _review(plan.get("discovery_review"), intent, bundle, plan["proposal_items"])
    _need(type(plan.get("revision")) is int and plan["revision"] >= 1, "invalid-plan-revision")
    return plan


def coverage(plan, rule_results, stage, subject_hash):
    """Consume exact owner/hash-bound policy results, checking only structure.

    No item.status, relevance score, evidence count or source category can
    substitute for a result. Conflicting current verdicts never use last-wins.
    Later-stage obligations remain visible but do not block earlier stages.
    """
    reasons, items, remaining = [], [], []
    try:
        _validate_plan(plan)
        _need(isinstance(stage, str) and stage in STAGES, "unknown-stage")
        _need(_text(subject_hash), "missing-subject-hash")
        _need(isinstance(rule_results, list), "invalid-rule-results")
        canonical_hash(rule_results)
        position = STAGES.index(stage)
        for item in plan["items"]:
            due = STAGES.index(item["due_stage"]) <= position
            current = [r for r in rule_results if isinstance(r, dict)
                       and r.get("item_id") == item["id"] and r.get("rule") == item["owner_rule"]
                       and r.get("plan_hash") == plan["plan_hash"] and r.get("subject_hash") == subject_hash]
            verdicts = {r.get("verdict") for r in current if isinstance(r.get("verdict"), str)}
            invalid = any(not isinstance(r.get("verdict"), str) or r["verdict"] not in _VERDICTS or
                          not isinstance(r.get("reasons"), list) or
                          not isinstance(r.get("evidence_refs"), list) or
                          not isinstance(r.get("limitations"), list) for r in current)
            if not current:
                status = "missing-result"
            elif invalid:
                status = "invalid-result"
            elif len(verdicts) != 1:
                status = "CONFLICT"
            else:
                status = next(iter(verdicts))
            satisfied = status in {"PASS", "ACCEPTED_ASSUMPTION", "NOT_APPLICABLE"}
            entry = {"id": item["id"], "owner_rule": item["owner_rule"], "due_stage": item["due_stage"],
                     "due": due, "status": status, "ok": satisfied,
                     "result_refs": [r.get("id", canonical_hash(r)) for r in current],
                     "limitations": [x for r in current for x in r.get("limitations", [])]}
            items.append(entry)
            if not satisfied:
                remaining.append(copy.deepcopy(entry))
                if due:
                    reasons.append("coverage:" + status.lower() + ":" + item["id"])
            if due:
                for source in item.get("required_sources", []):
                    health = [h for h in plan["retrieval"]["health"] if h["source"] == source]
                    if not health or any(h["status"] in ("error", "unavailable") for h in health):
                        reasons.append("coverage:required-source-unhealthy:" + item["id"] + ":" + source)
        for unknown in plan["discovery_review"]["remaining_unknowns"]:
            remaining.append(dict(unknown, status="unknown"))
            if unknown["blocking"] and STAGES.index(unknown["due_stage"]) <= position:
                reasons.append("coverage:remaining-unknown:" + unknown["id"])
    except V3Error as exc:
        reasons.append("coverage:" + exc.code)
    except Exception:
        # Unexpected validator failures must never become a successful gate.
        reasons.append("coverage:internal-error")
    return {"ok": not reasons, "reasons": list(dict.fromkeys(reasons)), "items": items, "remaining": remaining}


def revise_plan(old, new):
    """Retain reviewed deltas and old findings; only the service resolves them.

    A scope change records which old goals the new conclusions no longer cover.
    Deleting an obligation does not delete its finding or historical result.
    """
    _validate_plan(old)
    _validate_plan(new)
    review = new.get("reviewed_change")
    _need(isinstance(review, dict) and review.get("status") == "success"
          and _text(review.get("reason")), "unreviewed-plan-change")
    _texts(review.get("source_refs"), "reviewed_change.source_refs")
    _need(review.get("old_plan_hash") == old["plan_hash"], "stale-change-review")
    # Bind the new content without a circular dependency on this review itself.
    content_hash = canonical_hash({k: v for k, v in new.items() if k not in ("plan_hash", "reviewed_change")})
    _need(review.get("new_content_hash") == content_hash, "stale-change-content")
    old_goals = {g["id"]: g for g in old["intent"]["goals"]}
    new_goals = {g["id"]: g for g in new["intent"]["goals"]}
    changed_goals = sorted(key for key, goal in old_goals.items() if new_goals.get(key) != goal)
    if changed_goals:
        scope = review.get("scope_change")
        _need(isinstance(scope, dict) and _text(scope.get("reason")), "unreviewed-scope-change")
        _texts(scope.get("source_refs"), "scope_change.source_refs")
        _need(set(_texts(scope.get("excluded_goal_ids"), "scope_change.excluded_goal_ids")) == set(changed_goals),
              "incomplete-scope-change")
    before = {i["id"]: i for i in old["proposal_items"]}
    after = {i["id"]: i for i in new["proposal_items"]}
    removed = sorted(set(before) - set(after))
    added = sorted(set(after) - set(before))
    changed = sorted(key for key in set(before) & set(after) if before[key] != after[key])
    result = copy.deepcopy(new)
    prior_findings = old.get("findings", [])
    new_findings = new.get("findings", [])
    _need(isinstance(prior_findings, list) and isinstance(new_findings, list), "invalid-plan-findings")
    preserved = {f["id"]: copy.deepcopy(f) for f in prior_findings}
    for finding in new_findings:
        if finding["id"] in preserved:
            _need(finding == preserved[finding["id"]], "finding-rewrite-requires-service", finding["id"])
        else:
            preserved[finding["id"]] = copy.deepcopy(finding)
    result["findings"] = list(preserved.values())
    result["revision"] = old["revision"] + 1
    result["status"] = "revised"
    history = copy.deepcopy(old.get("change_history", []))
    history.append({"old_plan_hash": old["plan_hash"], "new_content_hash": content_hash,
                    "added": added, "removed": [{"id": key, "previous": before[key]} for key in removed],
                    "changed": [{"id": key, "previous": before[key], "replacement": after[key]} for key in changed],
                    "excluded_goal_ids": changed_goals, "reviewed_change": copy.deepcopy(review)})
    result["change_history"] = history
    result["plan_hash"] = hash_plan(result)
    return result
