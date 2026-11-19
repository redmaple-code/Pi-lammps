"""Type specifications: validate a spec itself, then validate payload fields against it.

Purely structural. Never inspects meaning of text.
"""
import math
import re

from .errors import UKBError

FIELD_KINDS = {"text", "number", "integer", "bool", "enum", "list", "object", "ref"}
TIERS = {"entry", "document"}
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_ENTRY_REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
MAX_DEPTH = 4


def _spec_err(msg, **kw):
    return UKBError("TYPE_SPEC_INVALID", msg, **kw)


def _check_field_spec(name, fs, depth, path):
    where = path + name
    if not isinstance(fs, dict):
        raise _spec_err("field spec must be an object", field=where)
    kind = fs.get("kind")
    if kind not in FIELD_KINDS:
        raise _spec_err("unknown field kind", field=where, kind=kind)
    allowed = {"kind", "required", "max_len", "min", "max", "values", "item", "fields", "max_items", "description"}
    extra = set(fs) - allowed
    if extra:
        raise _spec_err("unknown keys in field spec", field=where, keys=sorted(extra))
    if "required" in fs and not isinstance(fs["required"], bool):
        raise _spec_err("required must be boolean", field=where)
    if kind == "text" and "max_len" in fs and (not isinstance(fs["max_len"], int) or fs["max_len"] < 1):
        raise _spec_err("max_len must be a positive integer", field=where)
    if kind == "enum":
        vals = fs.get("values")
        if not isinstance(vals, list) or not vals or not all(isinstance(v, str) for v in vals) \
                or len(set(vals)) != len(vals):
            raise _spec_err("enum needs a non-empty list of distinct strings", field=where)
    if kind == "list":
        item = fs.get("item")
        if item is None:
            raise _spec_err("list needs item", field=where)
        if isinstance(item, str):
            item = {"kind": item}
        if depth >= MAX_DEPTH:
            raise _spec_err("nesting too deep", field=where)
        _check_field_spec("[]", item, depth + 1, where)
    if kind == "object":
        sub = fs.get("fields")
        if not isinstance(sub, dict) or not sub:
            raise _spec_err("object needs fields", field=where)
        if depth >= MAX_DEPTH:
            raise _spec_err("nesting too deep", field=where)
        for k, v in sub.items():
            if not _NAME_RE.match(k):
                raise _spec_err("invalid field name", field=where + "." + k)
            _check_field_spec(k, v, depth + 1, where + ".")


def validate_spec(spec, *, known_facets, known_roles, known_formats):
    """Raises TYPE_SPEC_INVALID on any problem. Returns the spec unchanged."""
    if not isinstance(spec, dict):
        raise _spec_err("spec must be an object")
    allowed = {"tier", "fields", "required_facets", "render", "review_checklist", "require_sources", "document",
               "description"}
    extra = set(spec) - allowed
    if extra:
        raise _spec_err("unknown keys in spec", keys=sorted(extra))
    tier = spec.get("tier", "entry")
    if tier not in TIERS:
        raise _spec_err("unknown tier", tier=tier)
    fields = spec.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise _spec_err("fields must be a non-empty object")
    for name, fs in fields.items():
        if not _NAME_RE.match(name):
            raise _spec_err("invalid field name", field=name)
        _check_field_spec(name, fs, 0, "")
    if not any(fs.get("required") for fs in fields.values()):
        raise _spec_err("at least one field must be required")
    render = spec.get("render")
    if not isinstance(render, list) or not render:
        raise _spec_err("render must be a non-empty list")
    if len(set(render)) != len(render):
        raise _spec_err("render has duplicates")
    missing = [r for r in render if r not in fields]
    if missing:
        raise _spec_err("render refers to unknown fields", fields=missing)
    unrendered = [f for f in fields if f not in render]
    if unrendered:
        raise _spec_err("every field must be rendered", fields=unrendered)
    rf = spec.get("required_facets", [])
    if not isinstance(rf, list) or any(f not in known_facets for f in rf):
        raise _spec_err("required_facets refers to unknown facets", facets=rf)
    rc = spec.get("review_checklist", [])
    if not isinstance(rc, list) or not all(isinstance(x, str) and x.strip() for x in rc):
        raise _spec_err("review_checklist must be a list of non-empty strings")
    if not isinstance(spec.get("require_sources", True), bool):
        raise _spec_err("require_sources must be boolean")
    doc = spec.get("document")
    if tier == "document":
        if not isinstance(doc, dict):
            raise _spec_err("document tier needs a document block")
        dallowed = {"summary_max_chars", "allowed_roles", "required_roles", "summary_must_cite",
                    "allowed_formats", "max_files", "max_total_bytes"}
        dextra = set(doc) - dallowed
        if dextra:
            raise _spec_err("unknown keys in document block", keys=sorted(dextra))
        smc = doc.get("summary_max_chars")
        if not isinstance(smc, int) or smc < 100:
            raise _spec_err("summary_max_chars must be an integer >= 100")
        ar = doc.get("allowed_roles")
        if not isinstance(ar, list) or not ar or any(r not in known_roles for r in ar):
            raise _spec_err("allowed_roles must list registered artifact roles", roles=ar)
        rr = doc.get("required_roles", [])
        if not isinstance(rr, list) or any(r not in ar for r in rr):
            raise _spec_err("required_roles must be a subset of allowed_roles", roles=rr)
        af = doc.get("allowed_formats")
        if af is not None and (not isinstance(af, list) or any(f not in known_formats for f in af)):
            raise _spec_err("allowed_formats must list registered file formats", formats=af)
        if not isinstance(doc.get("summary_must_cite", False), bool):
            raise _spec_err("summary_must_cite must be boolean")
        for k in ("max_files", "max_total_bytes"):
            if k in doc and (not isinstance(doc[k], int) or doc[k] < 1):
                raise _spec_err("%s must be a positive integer" % k)
    elif doc is not None:
        raise _spec_err("document block only allowed for document tier")
    return spec


# ----- payload validation -----

def _val_err(issues):
    return UKBError("PAYLOAD_INVALID", "fields do not match the type spec", issues=issues)


def _check_value(fs, value, where, issues):
    kind = fs["kind"]
    if kind == "text":
        if not isinstance(value, str) or not value.strip():
            issues.append({"field": where, "problem": "must be non-empty text"})
            return
        ml = fs.get("max_len")
        if ml and len(value) > ml:
            issues.append({"field": where, "problem": "too long", "max_len": ml, "len": len(value)})
    elif kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            issues.append({"field": where, "problem": "must be a finite number"})
            return
        _range(fs, value, where, issues)
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            issues.append({"field": where, "problem": "must be an integer"})
            return
        _range(fs, value, where, issues)
    elif kind == "bool":
        if not isinstance(value, bool):
            issues.append({"field": where, "problem": "must be boolean"})
    elif kind == "enum":
        if value not in fs["values"]:
            issues.append({"field": where, "problem": "not an allowed value", "values": fs["values"]})
    elif kind == "ref":
        if not isinstance(value, str) or not _ENTRY_REF_RE.match(value):
            issues.append({"field": where, "problem": "must be an entry id"})
    elif kind == "list":
        if not isinstance(value, list):
            issues.append({"field": where, "problem": "must be a list"})
            return
        mi = fs.get("max_items")
        if mi and len(value) > mi:
            issues.append({"field": where, "problem": "too many items", "max_items": mi})
        item = fs["item"] if isinstance(fs["item"], dict) else {"kind": fs["item"]}
        for i, v in enumerate(value):
            _check_value(item, v, "%s[%d]" % (where, i), issues)
    elif kind == "object":
        if not isinstance(value, dict):
            issues.append({"field": where, "problem": "must be an object"})
            return
        _check_fields(fs["fields"], value, where + ".", issues)


def _range(fs, value, where, issues):
    if "min" in fs and value < fs["min"]:
        issues.append({"field": where, "problem": "below min", "min": fs["min"]})
    if "max" in fs and value > fs["max"]:
        issues.append({"field": where, "problem": "above max", "max": fs["max"]})


def _check_fields(spec_fields, values, prefix, issues):
    for k in values:
        if k not in spec_fields:
            issues.append({"field": prefix + k, "problem": "unknown field"})
    for k, fs in spec_fields.items():
        if k not in values or values[k] is None or values[k] == "" or values[k] == []:
            if fs.get("required"):
                issues.append({"field": prefix + k, "problem": "required"})
            continue
        _check_value(fs, values[k], prefix + k, issues)


def validate_fields(spec, fields):
    if not isinstance(fields, dict):
        raise _val_err([{"field": "", "problem": "fields must be an object"}])
    issues = []
    _check_fields(spec["fields"], fields, "", issues)
    if issues:
        raise _val_err(issues)
    return {k: v for k, v in fields.items() if v is not None and v != "" and v != []}


def walk_text(spec_fields, values):
    """Yield every text value (for char counting and locator extraction)."""
    for k, fs in spec_fields.items():
        if k not in values:
            continue
        yield from _walk(fs, values[k])


def _walk(fs, v):
    kind = fs["kind"]
    if kind == "text" and isinstance(v, str):
        yield v
    elif kind == "list" and isinstance(v, list):
        item = fs["item"] if isinstance(fs["item"], dict) else {"kind": fs["item"]}
        for x in v:
            yield from _walk(item, x)
    elif kind == "object" and isinstance(v, dict):
        yield from walk_text(fs["fields"], v)
    elif kind in ("number", "integer", "enum", "bool", "ref"):
        yield str(v)
