"""Bounded, task-local discovery orchestration for the v3 evidence workflow.

``discover(request, data_root, transport=None, *, max_rounds=4,
 deadline_seconds=60, max_calls=12, reviewer_id=None)`` runs intent recognition,
read-only retrieval and dynamic check proposal in that order. Each later round
adds queries derived from the recognized intent and prior proposal gaps/unknowns.

The returned mapping has ``status`` (``success``, ``incomplete`` or ``blocked``),
``complete`` (whether this bounded pass finished without unresolved gaps), the
source-bound ``intent``, the latest ``proposal``, ``remaining_unknowns``, and a
``rounds`` list. Every round retains its full retrieval ``bundle`` plus explicit
copies of exclusions, health, truncation and limitations, the model citations,
and proposal. ``budget`` reports rounds, retrieval/model calls, elapsed seconds
and conservative UTF-8-byte context accounting. ``success`` means only that the
bounded protocol finished with no observed unresolved gaps; it never means
semantic or scientific exhaustiveness. Lexical retrieval and model self-report
are not proof that all relevant questions were found.

All operations are read-only. The only model access is the existing
``evidence_review`` boundary; callers can inject a transport for tests. A daemon
worker and a shared deadline bound blocking adapter calls from this caller's
perspective. The internal cumulative context ceiling is deliberately fixed at
512 KiB (UTF-8 bytes, not a provider tokenizer); retrieval results, model
requests and responses all count against it. No knowledge or task state is
written here.
"""
import copy
import math
import queue
import sys
import threading
import time
from pathlib import Path

_MCP = str(Path(__file__).resolve().parent)
_EVOLUTION = str(Path(__file__).resolve().parents[1] / "evolution")
for _path in (_MCP, _EVOLUTION):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import evidence_review
from evidence_v3 import planning, retrieval
from evidence_v3.contracts import canonical_bytes, canonical_hash

_MAX_CONTEXT_BYTES = 512 * 1024
_MAX_ROUNDS = 32
_MAX_CALLS = 128
_MAX_DERIVED_QUERIES = 128
_RETRIEVAL_LIMIT = 100
_MAX_RETRIEVAL_LIMIT = 500


class _BudgetStop(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class _RunBudget:
    """One accounting/deadline boundary shared by retrieval and model calls."""

    def __init__(self, transport, deadline_seconds, max_calls):
        self.transport = transport
        self.started = time.monotonic()
        self.deadline = self.started + deadline_seconds
        self.max_calls = max_calls
        self.calls = 0
        self.context_bytes = 0
        self.stop_reason = None
        self.failure = None
        self._lock = threading.Lock()

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.stop_reason = "deadline-exhausted"
            raise _BudgetStop(self.stop_reason)
        return remaining

    def _charge_context(self, size, *, reject_overflow=True):
        with self._lock:
            total = self.context_bytes + size
            if total > _MAX_CONTEXT_BYTES:
                self.stop_reason = "context-budget-exhausted"
                if reject_overflow:
                    raise _BudgetStop(self.stop_reason)
            self.context_bytes = total

    def execute(self, label, operation, *, context_input=None, count_context_output=True):
        """Run one potentially blocking call without waiting past the deadline."""
        remaining = self._remaining()
        input_size = len(canonical_bytes(context_input)) if context_input is not None else 0
        if input_size:
            self._charge_context(input_size)
        if self.calls >= self.max_calls:
            self.stop_reason = "call-budget-exhausted"
            raise _BudgetStop(self.stop_reason)
        self.calls += 1
        result_queue = queue.Queue(maxsize=1)

        def work():
            try:
                result_queue.put((True, operation()))
            except Exception as exc:  # surfaced as a fail-closed operation error
                result_queue.put((False, exc))

        worker = threading.Thread(target=work, name="evidence-discovery-" + label, daemon=True)
        worker.start()
        worker.join(remaining)
        if worker.is_alive():
            self.stop_reason = "deadline-exhausted"
            raise _BudgetStop(self.stop_reason)
        succeeded, value = result_queue.get_nowait()
        if not succeeded:
            self.failure = label + "_error"
            raise value
        if count_context_output:
            try:
                output_size = len(canonical_bytes(value))
                self._charge_context(output_size, reject_overflow=False)
            except (TypeError, ValueError):
                # Let the owning adapter report malformed/non-JSON output.
                pass
        return value

    def model_transport(self, prompt):
        """Transport adapter passed to evidence_review's public model methods."""
        if self.transport is None:
            operation = lambda: evidence_review._call(prompt, None)
        else:
            operation = lambda: self.transport(copy.deepcopy(prompt))
        return self.execute("model", operation, context_input=prompt)

    def retrieval(self, intent, data_root, limit):
        return self.execute("retrieval", lambda: retrieval.search(intent, data_root, limit=limit),
                            context_input=intent)

    def elapsed(self):
        return max(0.0, time.monotonic() - self.started)


def _error_code(value, fallback):
    if isinstance(value, dict):
        code = value.get("error")
        if isinstance(code, str) and code:
            return code
    return fallback


def _result(status, request_hash, intent, rounds, budget, *, proposal=None,
            remaining_unknowns=None, failure=None, limitations=None, reviewer_id=None):
    notes = list(limitations or [])
    for note in ("lexical-retrieval-is-not-semantic-exhaustion",
                 "model-self-report-is-not-proof-of-semantic-exhaustion"):
        if note not in notes:
            notes.append(note)
    result = {
        "schema_version": 3,
        "status": status,
        "complete": status == "success",
        "request_hash": request_hash,
        "intent": copy.deepcopy(intent),
        "intent_citations": copy.deepcopy(intent.get("citations", [])) if isinstance(intent, dict) else [],
        "rounds": rounds,
        "proposal": copy.deepcopy(proposal),
        "remaining_unknowns": copy.deepcopy(remaining_unknowns or []),
        "limitations": notes,
        "semantic_exhaustion_claimed": False,
        "budget": budget,
        "fail_closed": status != "success",
    }
    if reviewer_id is not None:
        result["reviewer_id"] = reviewer_id
    if failure is not None:
        result["failure"] = failure
    return result


def _intent_with_queries(intent, additions, query_state):
    updated = copy.deepcopy(intent)
    queries = updated.setdefault("queries", [])
    seen = set()
    for query in queries:
        if isinstance(query, str):
            seen.add((query, "expanded"))
        elif isinstance(query, dict) and isinstance(query.get("text"), str):
            seen.add((query["text"], query.get("purpose", "expanded")))
    for text, purpose, origin in additions:
        if not isinstance(text, str) or not text.strip() or (text, purpose) in seen:
            continue
        if query_state["added"] >= _MAX_DERIVED_QUERIES:
            query_state["limited"] = True
            break
        queries.append({"text": text, "purpose": purpose, "origin_ref": origin,
                        "source_refs": copy.deepcopy(query_state.get("source_refs", {}).get(origin, [])),
                        "generated_by": "evidence_discovery_v1"})
        seen.add((text, purpose))
        query_state["added"] += 1
    return updated


def _initial_queries(intent, query_state):
    additions = []
    for goal in intent["goals"]:
        text, identity = goal["text"], goal["id"]
        query_state["source_refs"][identity] = copy.deepcopy(goal.get("source_refs", []))
        additions.append(("supporting evidence for: " + text, "expanded", identity))
        additions.append(("counterexample failure alternative: " + text,
                          "counterexample", identity))
    for index, unknown in enumerate(intent.get("unknowns", [])):
        text = _unknown_text(unknown)
        identity = (unknown.get("id", "input-unknown-" + str(index))
                    if isinstance(unknown, dict) else "input-unknown-" + str(index))
        if text:
            query_state["source_refs"][identity] = copy.deepcopy(
                unknown.get("source_refs", []) if isinstance(unknown, dict)
                else intent.get("source_refs", []))
            additions.append((text, "unknown", identity))
            additions.append(("counterexample failure alternative: " + text,
                              "counterexample", identity))
    return _intent_with_queries(intent, additions, query_state)


def _unknown_text(row):
    if isinstance(row, str):
        return row.strip()
    if isinstance(row, dict) and isinstance(row.get("text"), str):
        return row["text"].strip()
    return ""


def _bind_query_sources(intent, bundle):
    """Keep retrieval's normalized query ids linked to originating references."""
    if bundle.get("intent_hash") != canonical_hash(intent):
        raise ValueError("retrieval intent binding mismatch")
    supplied_hash = bundle.get("bundle_hash")
    computed_hash = canonical_hash({key: value for key, value in bundle.items()
                                    if key != "bundle_hash"})
    if supplied_hash != computed_hash:
        raise ValueError("retrieval bundle hash mismatch")
    if not isinstance(bundle.get("queries"), list):
        raise ValueError("retrieval queries missing")
    source_queries = intent.get("queries", [])
    goals = {goal["id"]: goal for goal in intent.get("goals", [])}
    unknown_rows = intent.get("unknowns", [])
    unknowns = {row.get("id"): row for row in unknown_rows
                if isinstance(row, dict) and row.get("id")}
    conditions = intent.get("conditions", {})
    for query in bundle.get("queries", []):
        origin = query.get("origin_ref", "")
        if origin in goals:
            query["source_binding"] = {"kind": "intent_goal", "id": origin,
                                        "source_refs": copy.deepcopy(goals[origin].get("source_refs", []))}
            continue
        if origin in unknowns:
            query["source_binding"] = {"kind": "intent_unknown", "id": origin,
                                        "source_refs": copy.deepcopy(unknowns[origin].get("source_refs", []))}
            continue
        if origin in conditions:
            query["source_binding"] = {"kind": "intent_condition", "key": origin,
                                        "value_hash": canonical_hash(conditions[origin])}
            continue
        if origin.startswith("unknowns/"):
            index_text = origin.split("/", 1)[1]
            if index_text.isdigit() and int(index_text) < len(unknown_rows):
                row = unknown_rows[int(index_text)]
                query["source_binding"] = {"kind": "intent_unknown", "id": row.get("id", origin),
                                            "source_refs": copy.deepcopy(row.get("source_refs", []))
                                            if isinstance(row, dict) else []}
                continue
        if origin.startswith("queries/"):
            index_text = origin.split("/", 1)[1]
            if not index_text.isdigit() or int(index_text) >= len(source_queries):
                continue
            source = source_queries[int(index_text)]
            if isinstance(source, str):
                query["source_binding"] = {"kind": "intent_query", "path": origin,
                                            "source_refs": copy.deepcopy(intent.get("source_refs", []))}
            elif isinstance(source, dict):
                query["source_binding"] = {
                    "kind": "derived_query" if source.get("generated_by") else "intent_query",
                    "path": origin, "origin_ref": source.get("origin_ref", ""),
                    "source_refs": copy.deepcopy(source.get("source_refs", [])),
                }
                if source.get("generated_by"):
                    query["generated_by"] = source["generated_by"]
        if not query.get("source_binding"):
            query["source_binding"] = {
                "kind": "derived_query" if query.get("generated_by") else "intent_query_text",
                "origin_ref": origin,
                "source_refs": copy.deepcopy(query.get("source_refs", [])),
                "text_hash": canonical_hash(query.get("text", "")),
            }
    bundle["bundle_hash"] = canonical_hash({key: value for key, value in bundle.items()
                                             if key != "bundle_hash"})
    return bundle


def _gaps(proposal, bundle, intent=None):
    discovery = proposal["discovery_review"]
    gaps = [copy.deepcopy(value) for value in discovery.get("remaining_unknowns", [])]
    for health in bundle.get("health", []):
        if health.get("status") in {"error", "unavailable"}:
            source = str(health.get("source", "unknown-source"))
            gaps.append({"id": "source-health-" + canonical_hash(health)[:16],
                         "text": "Source health remains unresolved for " + source,
                         "source_refs": [source], "blocking": True})
    if intent is not None:
        represented = {_unknown_text(value).casefold() for value in gaps}
        for unknown in _input_unknowns(intent):
            if unknown["text"].casefold() not in represented:
                gaps.append(unknown)
    goal_coverage = discovery.get("goal_coverage", {})
    for goal_id, item_ids in goal_coverage.items():
        if not item_ids:
            gaps.append({"id": "coverage-" + str(goal_id),
                         "text": "No proposed check is mapped to goal " + str(goal_id),
                         "source_refs": [], "blocking": True})
    for item in proposal.get("items", []):
        applicability = item.get("applicability", {})
        if applicability.get("status") == "unknown":
            gaps.append({"id": "applicability-" + item["id"],
                         "text": "Applicability is unresolved for proposed check: " + item["question"],
                         "source_refs": copy.deepcopy(applicability.get("source_refs", [])),
                         "blocking": True})
    counter = discovery.get("counterexample_review", {})
    query_refs = set(counter.get("query_refs", []))
    available = set(bundle.get("counterexample_queries", []))
    if not query_refs.intersection(available):
        gaps.append({"id": "counterexample-query-binding",
                     "text": "Counterexample review did not cite an executed counterexample query",
                     "source_refs": [], "blocking": True})
    truncation = bundle.get("truncation", {})
    if truncation.get("omitted_ids"):
        gaps.append({"id": "retrieval-truncation",
                     "text": "Additional lexical candidates were omitted by the retrieval limit",
                     "source_refs": [], "blocking": True})
    return gaps


def _input_unknowns(intent):
    """Retain recognized uncertainty unless a later schema can resolve it."""
    result = []
    for index, value in enumerate(intent.get("unknowns", [])):
        text = _unknown_text(value)
        if not text:
            continue
        if isinstance(value, dict):
            identity = value.get("id", "intent-unknown-" + str(index))
            refs = copy.deepcopy(value.get("source_refs", []))
        else:
            identity = "intent-unknown-" + str(index)
            refs = copy.deepcopy(intent.get("source_refs", []))
        result.append({"id": "unresolved-" + str(identity), "text": text,
                       "source_refs": refs, "blocking": True,
                       "origin": "recognized-intent"})
    return result


def _followup_queries(gaps, query_state):
    additions = []
    for index, gap in enumerate(gaps):
        text = _unknown_text(gap)
        if not text:
            continue
        origin = gap.get("id", "gap-" + str(index)) if isinstance(gap, dict) else "gap-" + str(index)
        query_state["source_refs"][origin] = copy.deepcopy(
            gap.get("source_refs", []) if isinstance(gap, dict) else [])
        additions.append((text, "unknown", origin))
        additions.append(("counterexample failure alternative: " + text,
                          "counterexample", origin))
    return additions


def _round_record(number, intent, bundle, proposal=None, failure=None):
    record = {
        "round": number,
        "intent": copy.deepcopy(intent),
        "intent_hash": canonical_hash(intent),
        "bundle": copy.deepcopy(bundle),
        "exclusions": copy.deepcopy(bundle.get("exclusions", [])),
        "health": copy.deepcopy(bundle.get("health", [])),
        "truncation": copy.deepcopy(bundle.get("truncation", {})),
        "limitations": copy.deepcopy(bundle.get("limitations", [])),
        "model_citations": copy.deepcopy(proposal.get("citations", [])) if isinstance(proposal, dict) else [],
        "proposal": copy.deepcopy(proposal),
    }
    if isinstance(proposal, dict):
        review = proposal.get("discovery_review", {})
        record["model_citations"].extend(copy.deepcopy(review.get("citations", [])))
        counter = review.get("counterexample_review", {})
        record["model_citations"].extend(copy.deepcopy(counter.get("citations", [])))
    if failure is not None:
        record["failure"] = failure
    return record


def discover(request, data_root, transport=None, *, max_rounds=4,
             deadline_seconds=60, max_calls=12, reviewer_id=None):
    """Run bounded intent -> retrieval -> proposal discovery without writing.

    Args:
        request: Original user request as a non-empty string or JSON object.
            Strings are wrapped as ``{"request": request}`` for citation paths.
        data_root: Existing evidence data root passed to the read-only retriever.
        transport: Optional injected evidence_review transport, called with each
            full prompt and returning a response object or strict JSON text.
        max_rounds: Maximum retrieval/proposal iterations (1..32; default 4).
        deadline_seconds: Total wall-clock budget for orchestration and adapter
            calls (finite positive number; default 60).
        max_calls: Maximum combined retrieval and model transport calls (1..128;
            default 12). Initial recognition counts as one model call.
        reviewer_id: Optional caller/model-policy label retained in the result;
            it does not enable persistence or alter model configuration.

    Returns:
        A v3 result mapping. ``success`` indicates a bounded pass with no
        observed proposal gaps or retrieval truncation, not semantic exhaustion.
        ``incomplete`` indicates a budget boundary or unresolved gap. ``blocked``
        indicates invalid input or model/retrieval/contract failure. Each round's
        complete bundle, health, exclusions, truncation, limitations and citations
        are retained. No task state or knowledge is mutated.
    """
    valid_request = isinstance(request, (str, dict)) and bool(request)
    if isinstance(request, str):
        valid_request = bool(request.strip())
        raw_request = {"request": request}
    elif isinstance(request, dict):
        raw_request = copy.deepcopy(request)
    else:
        raw_request = None
    valid_budget = (
        type(max_rounds) is int and 1 <= max_rounds <= _MAX_ROUNDS
        and type(max_calls) is int and 1 <= max_calls <= _MAX_CALLS
        and isinstance(deadline_seconds, (int, float)) and not isinstance(deadline_seconds, bool)
        and math.isfinite(deadline_seconds) and 0 < deadline_seconds <= 3600
        and isinstance(data_root, str) and bool(data_root.strip())
        and (reviewer_id is None or (isinstance(reviewer_id, str) and bool(reviewer_id.strip())))
    )
    if not valid_request or not valid_budget:
        return _result("blocked", "", None, [],
                       {"max_rounds": max_rounds, "max_calls": max_calls,
                        "context_bytes": 0, "context_limit_bytes": _MAX_CONTEXT_BYTES,
                        "elapsed_seconds": 0.0, "token_estimator": "utf8_bytes"},
                       failure={"stage": "input", "code": "invalid-request-or-budget"},
                       limitations=["input-validation-failed"], reviewer_id=reviewer_id)
    try:
        request_hash = canonical_hash(raw_request)
    except Exception:
        return _result("blocked", "", None, [],
                       {"max_rounds": max_rounds, "max_calls": max_calls,
                        "context_bytes": 0, "context_limit_bytes": _MAX_CONTEXT_BYTES,
                        "elapsed_seconds": 0.0, "token_estimator": "utf8_bytes"},
                       failure={"stage": "input", "code": "invalid-json-request"},
                       limitations=["input-validation-failed"], reviewer_id=reviewer_id)

    budget = _RunBudget(transport, float(deadline_seconds), max_calls)
    try:
        recognized = evidence_review.recognize_intent(raw_request, transport=budget.model_transport)
    except _BudgetStop as exc:
        budget.stop_reason = exc.reason
        recognized = {"error": exc.reason}
    except Exception as exc:
        recognized = {"error": getattr(exc, "code", type(exc).__name__)}
    if isinstance(recognized, dict) and recognized.get("error"):
        status = "incomplete" if budget.stop_reason else "blocked"
        return _result(status, request_hash, None, [], {
            "max_rounds": max_rounds, "rounds_used": 0, "calls": budget.calls,
            "max_calls": max_calls, "context_bytes": budget.context_bytes,
            "context_limit_bytes": _MAX_CONTEXT_BYTES, "elapsed_seconds": budget.elapsed(),
            "deadline_seconds": deadline_seconds, "token_estimator": "utf8_bytes"},
            failure={"stage": "recognize_intent", "code": budget.stop_reason or
                     _error_code(recognized, "intent-recognition-failed")},
            limitations=["intent-not-recognized"], reviewer_id=reviewer_id)
    try:
        intent = planning.validate_intent(recognized)
    except Exception:
        return _result("blocked", request_hash, recognized, [], {
            "max_rounds": max_rounds, "rounds_used": 0, "calls": budget.calls,
            "max_calls": max_calls, "context_bytes": budget.context_bytes,
            "context_limit_bytes": _MAX_CONTEXT_BYTES, "elapsed_seconds": budget.elapsed(),
            "deadline_seconds": deadline_seconds, "token_estimator": "utf8_bytes"},
            failure={"stage": "recognize_intent", "code": "invalid-recognized-intent"},
            limitations=["intent-not-recognized"], reviewer_id=reviewer_id)
    if budget.stop_reason:
        return _result("incomplete", request_hash, intent, [], {
            "max_rounds": max_rounds, "rounds_used": 0, "calls": budget.calls,
            "max_calls": max_calls, "context_bytes": budget.context_bytes,
            "context_limit_bytes": _MAX_CONTEXT_BYTES, "elapsed_seconds": budget.elapsed(),
            "deadline_seconds": deadline_seconds, "token_estimator": "utf8_bytes"},
            failure={"stage": "budget", "code": budget.stop_reason},
            limitations=["discovery-stopped-after-intent-budget-boundary"], reviewer_id=reviewer_id)

    query_state = {"added": 0, "limited": False, "source_refs": {}}
    current_intent = _initial_queries(intent, query_state)
    rounds = []
    latest_proposal = None
    latest_unknowns = _input_unknowns(intent)
    retrieval_limit = _RETRIEVAL_LIMIT
    failure = None
    overall_limitations = []

    for round_number in range(1, max_rounds + 1):
        if budget.calls >= max_calls:
            budget.stop_reason = "call-budget-exhausted"
            break
        try:
            bundle = budget.retrieval(current_intent, data_root, retrieval_limit)
        except _BudgetStop as exc:
            budget.stop_reason = exc.reason
            break
        except Exception as exc:
            failure = {"stage": "retrieval", "code": getattr(exc, "code", type(exc).__name__)}
            break
        if not isinstance(bundle, dict):
            failure = {"stage": "retrieval", "code": "invalid-retrieval-bundle"}
            break
        if bundle.get("status") not in ("ok", "empty", "error", "unavailable"):
            rounds.append(_round_record(round_number, current_intent, bundle,
                                        failure={"stage": "retrieval", "code": "invalid-retrieval-status"}))
            failure = {"stage": "retrieval", "code": "invalid-retrieval-bundle"}
            break
        if budget.stop_reason == "context-budget-exhausted":
            rounds.append(_round_record(round_number, current_intent, bundle,
                                        failure={"stage": "budget", "code": budget.stop_reason}))
            break
        try:
            bundle = _bind_query_sources(current_intent, bundle)
        except Exception:
            failure = {"stage": "retrieval", "code": "invalid-retrieval-bundle"}
            break
        if bundle.get("status") in {"error", "unavailable"}:
            rounds.append(_round_record(round_number, current_intent, bundle,
                                        failure={"stage": "retrieval", "code": "retrieval-reported-error"}))
            failure = {"stage": "retrieval", "code": "retrieval-reported-error"}
            break
        if query_state["limited"]:
            overall_limitations.append("derived-query-expansion-limit-reached")

        try:
            proposal = evidence_review.propose_checks(current_intent, bundle,
                                                       transport=budget.model_transport)
        except _BudgetStop as exc:
            budget.stop_reason = exc.reason
            rounds.append(_round_record(round_number, current_intent, bundle,
                                        failure={"stage": "propose_checks", "code": exc.reason}))
            break
        except Exception as exc:
            rounds.append(_round_record(round_number, current_intent, bundle,
                                        failure={"stage": "propose_checks",
                                                 "code": getattr(exc, "code", type(exc).__name__)}))
            failure = {"stage": "propose_checks",
                       "code": getattr(exc, "code", type(exc).__name__)}
            break
        if isinstance(proposal, dict) and proposal.get("error"):
            rounds.append(_round_record(round_number, current_intent, bundle,
                                        failure={"stage": "propose_checks", "code": budget.stop_reason or
                                                 _error_code(proposal, "check-proposal-failed")}))
            if budget.stop_reason:
                break
            failure = {"stage": "propose_checks", "code": _error_code(proposal, "check-proposal-failed")}
            break
        if not isinstance(proposal, dict) or not isinstance(proposal.get("discovery_review"), dict):
            rounds.append(_round_record(round_number, current_intent, bundle,
                                        failure={"stage": "propose_checks", "code": "invalid-proposal"}))
            failure = {"stage": "propose_checks", "code": "invalid-proposal"}
            break
        if budget.stop_reason == "context-budget-exhausted":
            rounds.append(_round_record(round_number, current_intent, bundle, proposal,
                                        failure={"stage": "budget", "code": budget.stop_reason}))
            break

        rounds.append(_round_record(round_number, current_intent, bundle, proposal))
        latest_proposal = proposal
        latest_unknowns = _gaps(proposal, bundle, current_intent)
        overall_limitations.extend(bundle.get("limitations", []))
        overall_limitations.extend(proposal["discovery_review"].get("limitations", []))
        counter_limits = proposal["discovery_review"].get("counterexample_review", {}).get("limitations", [])
        overall_limitations.extend(counter_limits)
        if budget.elapsed() >= deadline_seconds:
            budget.stop_reason = "deadline-exhausted"

        if not latest_unknowns and not query_state["limited"] and not budget.stop_reason:
            break
        if round_number >= max_rounds:
            budget.stop_reason = "round-budget-exhausted"
            break
        if budget.calls >= max_calls:
            budget.stop_reason = "call-budget-exhausted"
            break
        if budget.elapsed() >= deadline_seconds:
            budget.stop_reason = "deadline-exhausted"
            break

        additions = _followup_queries(latest_unknowns, query_state)
        current_intent = _intent_with_queries(current_intent, additions, query_state)
        if any(row.get("id") == "retrieval-truncation" for row in latest_unknowns):
            total = bundle.get("truncation", {}).get("total_matches", retrieval_limit)
            if type(total) is int and total > retrieval_limit:
                retrieval_limit = min(total, _MAX_RETRIEVAL_LIMIT)
        if query_state["limited"]:
            overall_limitations.append("derived-query-expansion-limit-reached")

    if failure is not None:
        status = "blocked"
        final_failure = failure
        latest_unknowns = latest_unknowns or []
    elif budget.stop_reason or latest_unknowns:
        status = "incomplete"
        final_failure = {"stage": "budget", "code": budget.stop_reason or "unresolved-discovery-gaps"}
    elif latest_proposal is None:
        status = "blocked"
        final_failure = {"stage": "discovery", "code": "no-complete-round"}
    else:
        status = "success"
        final_failure = None

    return _result(status, request_hash, intent, rounds, {
        "max_rounds": max_rounds, "rounds_used": len(rounds),
        "calls": budget.calls, "max_calls": max_calls,
        "context_bytes": budget.context_bytes, "context_limit_bytes": _MAX_CONTEXT_BYTES,
        "elapsed_seconds": budget.elapsed(), "deadline_seconds": deadline_seconds,
        "token_estimator": "utf8_bytes",
        "retrieval_limit_last_round": retrieval_limit,
        "derived_queries": query_state["added"],
        "query_expansion_limited": query_state["limited"],
    }, proposal=latest_proposal, remaining_unknowns=latest_unknowns,
        failure=final_failure, limitations=list(dict.fromkeys(overall_limitations)),
        reviewer_id=reviewer_id)
