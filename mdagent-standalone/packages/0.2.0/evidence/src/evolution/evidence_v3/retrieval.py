"""Read-only lexical recall over the existing stores; every hit is a candidate.

No memory loader is imported: some existing loaders create directories or hide
bad rows. Health and byte manifests describe the snapshot actually inspected.
Scores express recall relevance, never applicability or evidential strength.
"""
import copy
import hashlib
import json
import re
from pathlib import Path

from .contracts import V3Error, canonical_hash


RetrievalBundle = dict
_STATUSES = {"candidate", "canary", "active", "revoked", "superseded"}
_CONSUMERS = {"evidence", "evidence_v3", "design_review"}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _refs(value):
    return isinstance(value, list) and all(_text(x) for x in value)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise V3Error("duplicate-json-key", key)
        result[key] = value
    return result


def _bad_constant(value):
    raise V3Error("invalid-json", value)


def _json(text):
    value = json.loads(text, object_pairs_hook=_unique, parse_constant=_bad_constant)
    canonical_hash(value)  # Also reject exponent overflow such as 1e999.
    return value


def _read(path, manifest, channel):
    raw = path.read_bytes()
    manifest.append({"channel": channel, "source": str(path),
                     "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    return raw.decode("utf-8-sig")


def _health(channel, source, status, **extra):
    return dict(channel=channel, source=str(source), status=status, **extra)


def _error(exc):
    return exc.code if isinstance(exc, V3Error) else type(exc).__name__


def load_knowledge(directories):
    try:
        from ..kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.load_knowledge()
    """Load packages without promotion, directory precedence, or implicit status.

    Legacy packages and entries lacking approval metadata remain candidates.
    Invalid rows are quarantined visibly; duplicate IDs quarantine *all* copies,
    including conflicts with retired rows. No replacement is guessed.
    """
    if not isinstance(directories, list) or not all(_text(x) for x in directories):
        raise V3Error("invalid-knowledge-directories")
    entries, manifest, health = [], [], []
    occurrences = {}
    for directory in sorted({str(Path(p).resolve()) for p in directories}):
        root = Path(directory)
        try:
            if not root.exists():
                health.append(_health("review_experience", root, "unavailable", reason="missing-directory"))
                continue
            files = sorted(root.glob("*.json"))
            if not root.is_dir():
                raise V3Error("invalid-directory", directory)
            if not files:
                health.append(_health("review_experience", root, "empty", count=0))
            for path in files:
                source = str(path)
                try:
                    pack = _json(_read(path, manifest, "review_experience"))
                    if not isinstance(pack, dict) or not _text(pack.get("pack")):
                        raise V3Error("invalid-package-identity")
                    if type(pack.get("schema_version")) is not int or pack["schema_version"] not in (1, 3):
                        raise V3Error("unsupported-package-schema")
                    pack_status = pack.get("status")
                    if pack_status is not None:
                        if not isinstance(pack_status, str) or pack_status not in _STATUSES:
                            raise V3Error("invalid-package-status")
                        if pack_status in ("revoked", "superseded"):
                            health.append(_health("review_experience", source, "empty", reason=pack_status, excluded=True))
                            continue
                    consumer = pack.get("consumer")
                    if consumer is not None and (not isinstance(consumer, str) or consumer not in _CONSUMERS):
                        health.append(_health("review_experience", source, "unavailable",
                                              reason="foreign-consumer", consumer=pack["consumer"]))
                        continue
                    rows = pack.get("entries")
                    if rows is None:
                        # Old package-level catalogues may be recalled, never executed.
                        rows = [dict(pack, id=pack["pack"], statement=pack.get("title", pack["pack"]))]
                    if not isinstance(rows, list):
                        raise V3Error("invalid-package-entries")
                    count, quarantined = 0, []
                    for index, row in enumerate(rows):
                        locator = source + "#entries/" + str(index)
                        if isinstance(row, dict) and _text(row.get("id")):
                            occurrences.setdefault(row["id"], []).append(source)
                        try:
                            if not isinstance(row, dict) or not _text(row.get("id")):
                                raise V3Error("invalid-entry-id")
                            status = row.get("status", row.get("lifecycle"))
                            if isinstance(status, dict):
                                status = status.get("status")
                            if not isinstance(status, str) or status not in _STATUSES:
                                raise V3Error("invalid-entry-status")
                            refs = row.get("source_refs", row.get("refs", []))
                            provenance = row.get("provenance", pack.get("provenance", {}))
                            if not _refs(refs) or not isinstance(provenance, dict):
                                raise V3Error("invalid-entry-source")
                            if not refs and not _text(provenance.get("source")):
                                raise V3Error("missing-entry-source")
                            content = row.get("question_or_claim", row.get("statement", row.get("question", row.get("title"))))
                            if not _text(content):
                                raise V3Error("missing-entry-content")
                            conditions = row.get("conditions", {})
                            if not isinstance(conditions, dict):
                                raise V3Error("invalid-entry-conditions")
                            if status in ("revoked", "superseded"):
                                quarantined.append({"id": row["id"], "reason": status, "excluded": True})
                                continue
                            approval = row.get("approval", pack.get("approval"))
                            version = row.get("version", pack.get("version"))
                            if pack["schema_version"] == 3:
                                if type(version) is not int or version < 1:
                                    raise V3Error("invalid-entry-version")
                                if (not isinstance(approval, dict) or approval.get("actor_kind") != "human"
                                        or approval.get("status") != "approved" or approval.get("revoked") is not False
                                        or not _text(approval.get("actor_id")) or not _text(approval.get("event_id"))
                                        or not _text(approval.get("reason")) or not _text(approval.get("content_hash"))):
                                    raise V3Error("invalid-entry-approval")
                            entries.append({"id": row["id"], "channel": "review_experience",
                                            "source_locator": locator, "pack": pack["pack"],
                                            "version": row.get("version", pack.get("version")),
                                            "lifecycle": status, "source_refs": refs,
                                            "provenance": provenance, "conditions": conditions,
                                            "approval": approval, "candidate_only": True,
                                            "limitations": (["legacy-package"] if pack["schema_version"] == 1 else [])
                                            + ([] if approval else ["approval-not-recorded"]),
                                            "content": content, "record": copy.deepcopy(row)})
                            count += 1
                        except (V3Error, TypeError, ValueError) as exc:
                            quarantined.append({"index": index, "reason": _error(exc), "quarantined": True})
                    broken = any(x.get("quarantined") for x in quarantined)
                    health.append(_health("review_experience", source, "error" if broken else ("ok" if count else "empty"),
                                          count=count, quarantined=quarantined))
                except (OSError, UnicodeError, ValueError, TypeError) as exc:
                    health.append(_health("review_experience", source, "error", reason=_error(exc), quarantined=True))
        except (OSError, ValueError) as exc:
            health.append(_health("review_experience", root, "error", reason=_error(exc)))
    duplicates = {key: sources for key, sources in occurrences.items() if len(sources) > 1}
    if duplicates:
        entries = [e for e in entries if e["id"] not in duplicates]
        for key, sources in sorted(duplicates.items()):
            health.append(_health("review_experience", sources[0], "error", reason="duplicate-entry-id",
                                  entry_id=key, sources=sources, quarantined=True))
    generation = canonical_hash({"manifest": manifest, "health": health})
    return {"entries": entries, "manifest": manifest, "health": health, "generation": generation}


def _read_rows(path, channel, manifest, health, jsonl=False):
    if not path.exists():
        health.append(_health(channel, path, "unavailable", reason="missing-file"))
        return []
    try:
        text = _read(path, manifest, channel)
        if jsonl:
            rows, errors = [], []
            for line_no, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    row = _json(line)
                    if not isinstance(row, dict):
                        raise V3Error("invalid-record")
                    rows.append((line_no, row))
                except (ValueError, TypeError) as exc:
                    errors.append({"line": line_no, "reason": _error(exc)})
        else:
            values = _json(text)
            if not isinstance(values, list) or not all(isinstance(v, dict) for v in values):
                raise V3Error("invalid-record-list")
            rows, errors = list(enumerate(values, 1)), []
        health.append(_health(channel, path, "error" if errors else ("ok" if rows else "empty"),
                              count=len(rows), quarantined=errors))
        return rows
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        health.append(_health(channel, path, "error", reason=_error(exc), quarantined=True))
        return []


def _queries(intent):
    queries = []

    def add(text, purpose, origin):
        if _text(text) and not any(q["text"] == text and q["purpose"] == purpose for q in queries):
            queries.append({"id": "query-" + str(len(queries) + 1), "text": text,
                            "purpose": purpose, "origin_ref": origin})

    for goal in intent["goals"]:
        add(goal["text"], "goal", goal["id"])
    for key, value in intent["conditions"].items():
        add(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value, "condition", key)
    for index, unknown in enumerate(intent["unknowns"]):
        add(unknown if isinstance(unknown, str) else unknown["text"], "unknown", "unknowns/" + str(index))
    for index, query in enumerate(intent.get("queries", [])):
        add(query if isinstance(query, str) else query["text"],
            "expanded" if isinstance(query, str) else query.get("purpose", "expanded"), "queries/" + str(index))
    return queries


def _tokens(text):
    lowered = text.casefold()
    tokens = set(re.findall(r"[a-z0-9_]+", lowered))
    for segment in re.findall(r"[\u3400-\u9fff]+", lowered):
        tokens.add(segment)
        tokens.update(segment[i:i + 2] for i in range(len(segment) - 1))
    return tokens


def _condition_match(expected, actual):
    """Exact structural comparisons only; semantic applicability stays unknown."""
    missing, different = [], []
    for key, value in expected.items():
        if key not in actual:
            missing.append(key)
        elif canonical_hash(value) != canonical_hash(actual[key]):
            different.append(key)
    return {"status": "unknown", "reason": "semantic-review-required",
            "exact_conditions": bool(expected) and not missing and not different,
            "missing": missing, "different": different}


def search(intent, data_root, knowledge_dirs=None, limit=20):
    """Recall from atoms, scenarios, experience packages and local corpus files.

    Expanded/negative queries must be supplied by the intent producer. This
    function does not claim to infer intent or to exhaust open questions.
    """
    try:
        from ..kb import adapter
    except ImportError:
        from kb import adapter
    if adapter.enabled():
        return adapter.search(intent, data_root, knowledge_dirs, limit)
    from .planning import validate_intent
    intent = validate_intent(intent)
    if not _text(data_root) or type(limit) is not int or limit < 1:
        raise V3Error("invalid-retrieval-request")
    root = Path(data_root).resolve()
    code_root = Path(__file__).resolve().parents[2]
    directories = ([str(code_root / "mcp" / "review_experience"),
                    str(root / "memory" / "review_experience")]
                   if knowledge_dirs is None else knowledge_dirs)
    knowledge = load_knowledge(directories)
    manifest, health = list(knowledge["manifest"]), list(knowledge["health"])
    records, exclusions = list(knowledge["entries"]), []
    for h in health:
        for rejected in h.get("quarantined", []) if isinstance(h.get("quarantined"), list) else []:
            exclusions.append(dict(rejected, source=h["source"], channel=h["channel"]))
    retired_atoms = set()
    for filename, channel, is_jsonl in (("atoms.jsonl", "atoms", True), ("scenarios.json", "scenarios", False)):
        path = root / "memory" / filename
        for position, row in _read_rows(path, channel, manifest, health, is_jsonl):
            locator = str(path) + "#" + str(position)
            refs = row.get("refs", []) if channel == "atoms" else ["atom:" + str(x) for x in row.get("atom_ids", [])] if isinstance(row.get("atom_ids", []), list) else None
            identity = row.get("id") if channel == "atoms" else row.get("signature")
            if not _text(identity) or not _refs(refs):
                health.append(_health(channel, locator, "error", reason="invalid-record-identity-or-refs"))
                continue
            if row.get("retracted") or row.get("superseded_by"):
                retired_atoms.add(identity)
                exclusions.append({"id": identity, "source": locator, "reason": "retired-record"})
                continue
            if channel == "scenarios" and any(ref[5:] in retired_atoms for ref in refs if ref.startswith("atom:")):
                exclusions.append({"id": identity, "source": locator, "reason": "retired-scenario-dependency"})
                continue
            records.append({"id": channel + ":" + identity, "channel": channel,
                            "source_locator": locator, "source_refs": refs or [locator],
                            "conditions": row.get("conditions", {}), "candidate_only": True,
                            "lifecycle": "candidate", "record": row})
    corpus_dirs = sorted({str(root / "external_corpus"), str(code_root / "external_corpus")})
    for directory in corpus_dirs:
        try:
            folder = Path(directory)
            if not folder.exists():
                health.append(_health("local_corpus", folder, "unavailable", reason="missing-directory"))
                continue
            if not folder.is_dir():
                raise V3Error("invalid-directory")
            files = sorted(folder.glob("*.jsonl"))
            if not files:
                health.append(_health("local_corpus", folder, "empty", count=0))
            for path in files:
                for position, row in _read_rows(path, "local_corpus", manifest, health, True):
                    locator = str(path) + "#" + str(position)
                    if not _text(row.get("id")):
                        health.append(_health("local_corpus", locator, "error", reason="missing-record-id"))
                        continue
                    if row.get("status") in ("revoked", "superseded"):
                        exclusions.append({"id": row["id"], "source": locator, "reason": row["status"]})
                        continue
                    records.append({"id": "corpus:" + row["id"], "channel": "local_corpus",
                                    "source_locator": locator, "source_refs": [row.get("url") or row["id"]],
                                    "conditions": row.get("conditions", {}), "candidate_only": True,
                                    "lifecycle": "candidate", "verification": "unverified", "record": row})
        except (OSError, ValueError) as exc:
            health.append(_health("local_corpus", directory, "error", reason=_error(exc)))
    queries = _queries(intent)
    ranked = []
    for entry in records:
        content = {k: v for k, v in entry["record"].items()
                   if k not in ("id", "refs", "source_refs", "provenance", "status", "lifecycle")}
        hay = json.dumps(content, ensure_ascii=False, sort_keys=True).casefold()
        hay_tokens = _tokens(hay)
        matches = []
        for query in queries:
            hits = sorted(t for t in _tokens(query["text"]) if t in hay_tokens)
            substring = len(query["text"].strip()) > 1 and query["text"].casefold() in hay
            if hits or substring:
                matches.append({"query_id": query["id"], "tokens": hits, "substring": substring})
        if not matches:
            exclusions.append({"id": entry["id"], "source": entry["source_locator"], "reason": "no-lexical-match"})
            continue
        conditions = entry.get("conditions", {})
        if not isinstance(conditions, dict):
            health.append(_health(entry["channel"], entry["source_locator"], "error", reason="invalid-conditions"))
            continue
        candidate = copy.deepcopy(entry)
        candidate["matches"] = matches
        candidate["score"] = sum(len(m["tokens"]) + int(m["substring"]) for m in matches)
        candidate["applicability"] = _condition_match(conditions, intent["conditions"])
        origins = entry["record"].get("independent_sources", entry["source_refs"])
        if not _refs(origins) or not origins:
            origins = entry["source_refs"]
        candidate["source_group"] = canonical_hash(sorted(set(origins)))
        ranked.append(candidate)
    ranked.sort(key=lambda c: (-c["score"], c["id"], c["source_locator"]))
    unique, seen = [], {}
    for candidate in ranked:
        # A shared source is not an independent proof, but can legitimately
        # contain distinct questions or applicability conditions.
        content = {k: v for k, v in candidate["record"].items()
                   if k not in ("id", "refs", "source_refs", "provenance")}
        group = canonical_hash([candidate["source_group"], content])
        if group in seen:
            exclusions.append({"id": candidate["id"], "source": candidate["source_locator"],
                               "reason": "duplicate-source", "duplicate_of": seen[group]})
        else:
            seen[group] = candidate["id"]
            unique.append(candidate)
    truncated = [c["id"] for c in unique[limit:]]
    limitations = ["lexical-recall-only", "candidates-require-semantic-review", "source-groups-are-not-independent-evidence"]
    if not any(q["purpose"] == "counterexample" for q in queries):
        limitations.append("counterexample-query-not-supplied")
    if truncated:
        limitations.append("top-k-is-not-plan-scope")
    if any(h["status"] in ("error", "unavailable") for h in health):
        limitations.append("some-sources-not-available-or-healthy")
    bundle = {"schema_version": 3, "intent_hash": canonical_hash(intent), "queries": queries,
              "counterexample_queries": [q["id"] for q in queries if q["purpose"] == "counterexample"],
              "health": health, "manifest": manifest, "knowledge_generation": knowledge["generation"],
              "candidates": unique[:limit], "exclusions": exclusions,
              "truncation": {"limit": limit, "total_matches": len(unique), "omitted_ids": truncated},
              "limitations": limitations,
              "status": "error" if any(h["status"] == "error" for h in health) else ("ok" if unique else "empty")}
    bundle["bundle_hash"] = canonical_hash(bundle)
    return bundle
