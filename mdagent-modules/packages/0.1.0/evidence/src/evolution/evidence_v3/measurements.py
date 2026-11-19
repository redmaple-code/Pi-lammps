"""Data-only measurement recipes. Raw run outputs, never submitted values, count.

materialize writes a derived immutable task artifact, not knowledge or task state.
The service must register artifact_manifest in run.outputs under its task lock.
"""
import csv
import hashlib
import io
import json
import math
import os
import statistics
import uuid
from pathlib import Path

from .contracts import V3Error, canonical_hash


def _need(condition, code, detail=""):
    if not condition:
        raise V3Error(code, detail)


def _number(value):
    _need(type(value) in (int, float, str) and not isinstance(value, bool), "INVALID_MEASUREMENT_NUMBER")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise V3Error("INVALID_MEASUREMENT_NUMBER") from exc
    _need(math.isfinite(result), "INVALID_MEASUREMENT_NUMBER")
    return result


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _need(key not in result, "DUPLICATE_MEASUREMENT_KEY", key)
        result[key] = value
    return result


def _pointer(value, pointer):
    _need(isinstance(pointer, str) and (pointer == "" or pointer.startswith("/")), "INVALID_MEASUREMENT_POINTER")
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        try:
            if isinstance(value, list):
                _need(token.isdigit() and str(int(token)) == token, "INVALID_MEASUREMENT_POINTER")
                value = value[int(token)]
            else:
                _need(isinstance(value, dict), "INVALID_MEASUREMENT_POINTER")
                value = value[token]
        except (KeyError, IndexError) as exc:
            raise V3Error("INVALID_MEASUREMENT_POINTER") from exc
    return value


def _raw(run, source):
    _need(isinstance(source, dict) and set(source) == {"path", "sha256"}, "INVALID_MEASUREMENT_SOURCE")
    path = Path(source["path"])
    _need(path.is_absolute(), "INVALID_MEASUREMENT_SOURCE")
    _need(sum(row == source for row in run.get("outputs", [])) == 1, "MEASUREMENT_SOURCE_NOT_IN_RUN")
    current = path
    while current != current.parent:
        _need(not current.is_symlink() and not (hasattr(current, "is_junction") and current.is_junction()), "UNSAFE_MEASUREMENT_SOURCE")
        current = current.parent
    payload = path.read_bytes()
    _need(hashlib.sha256(payload).hexdigest() == source["sha256"], "MEASUREMENT_RAW_CHANGED")
    return payload.decode("utf-8")


def recompute(run, recipe):
    """Return {value, unit, window, raw_sources, recipe_hash, sample_count}.

    recipe={source:{path,sha256}, format:json|table|text, unit, source_unit,
      column (table), pointer (JSON), aggregate:mean|sum|min|max|first|last,
      window:{start:int,stop:int|null}, conversion?:{scale,offset},
      marker?:{start:str,end:str,occurrence:int}, delimiter?:str,
      header?:bool, unit_column?:str, unit_pointer?:str}.
    Windows use zero-based half-open sample indices after marker selection.
    Text is one numeric sample per line; tables support CSV or whitespace.
    """
    _need(isinstance(recipe, dict), "INVALID_MEASUREMENT_RECIPE")
    allowed = {"source", "format", "unit", "source_unit", "column", "pointer", "aggregate", "window", "conversion", "marker", "delimiter", "header", "unit_column", "unit_pointer"}
    _need(not set(recipe) - allowed, "UNSUPPORTED", "measurement recipe fields")
    canonical_hash(recipe)
    _need(run.get("status") == "FINISHED" and run.get("receipt_id"), "MEASUREMENT_RUN_NOT_FINISHED")
    _need(run.get("output_hash") == canonical_hash(run.get("outputs")), "MEASUREMENT_RUN_MANIFEST_HASH")
    text = _raw(run, recipe.get("source"))
    marker = recipe.get("marker")
    if marker is not None:
        _need(isinstance(marker, dict) and set(marker) == {"start", "end", "occurrence"}, "INVALID_MEASUREMENT_MARKER")
        _need(all(isinstance(marker[k], str) and marker[k] for k in ("start", "end")), "INVALID_MEASUREMENT_MARKER")
        occurrence = marker["occurrence"]
        _need(type(occurrence) is int and occurrence >= 0, "INVALID_MEASUREMENT_MARKER")
        blocks, opened = [], None
        for line in text.splitlines():
            if line == marker["start"]:
                _need(opened is None, "AMBIGUOUS_MEASUREMENT_MARKER")
                opened = []
            elif line == marker["end"]:
                _need(opened is not None, "AMBIGUOUS_MEASUREMENT_MARKER")
                blocks.append("\n".join(opened))
                opened = None
            elif opened is not None:
                opened.append(line)
        _need(opened is None and occurrence < len(blocks), "MEASUREMENT_MARKER_MISSING")
        text = blocks[occurrence]
    unit, source_unit = recipe.get("unit"), recipe.get("source_unit")
    _need(all(isinstance(s, str) and s.strip() for s in (unit, source_unit)), "MEASUREMENT_UNIT_REQUIRED")
    fmt = recipe.get("format")
    if fmt == "json":
        try:
            document = json.loads(text, object_pairs_hook=_pairs, parse_constant=lambda value: (_ for _ in ()).throw(V3Error("INVALID_MEASUREMENT_NUMBER")))
        except (ValueError, UnicodeError) as exc:
            raise V3Error("INVALID_MEASUREMENT_JSON") from exc
        if "unit_pointer" in recipe:
            _need(_pointer(document, recipe["unit_pointer"]) == source_unit, "MEASUREMENT_UNIT_MISMATCH")
        values = _pointer(document, recipe.get("pointer"))
        values = values if isinstance(values, list) else [values]
    elif fmt == "text":
        values = [line.strip() for line in text.splitlines() if line.strip()]
    elif fmt == "table":
        delimiter = recipe.get("delimiter")
        _need(delimiter is None or isinstance(delimiter, str) and len(delimiter) == 1, "INVALID_MEASUREMENT_DELIMITER")
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter)) if delimiter else [line.split() for line in text.splitlines() if line.strip()]
        rows = [row for row in rows if row]
        column = recipe.get("column")
        if recipe.get("header", True):
            _need(bool(rows) and len(set(rows[0])) == len(rows[0]), "INVALID_MEASUREMENT_HEADER")
            header, rows = rows[0], rows[1:]
            _need(isinstance(column, str) and column in header, "MEASUREMENT_COLUMN_MISSING")
            index = header.index(column)
            if "unit_column" in recipe:
                _need(recipe["unit_column"] in header, "MEASUREMENT_COLUMN_MISSING")
                unit_index = header.index(recipe["unit_column"])
                _need(all(len(row) == len(header) and row[unit_index] == source_unit for row in rows), "MEASUREMENT_UNIT_MISMATCH")
            _need(all(len(row) == len(header) for row in rows), "INVALID_MEASUREMENT_ROW")
        else:
            _need(type(column) is int and column >= 0 and "unit_column" not in recipe, "INVALID_MEASUREMENT_COLUMN")
            index = column
        _need(all(len(row) > index for row in rows), "INVALID_MEASUREMENT_ROW")
        values = [row[index] for row in rows]
    else:
        raise V3Error("UNSUPPORTED", "measurement format")
    values = [_number(value) for value in values]
    window = recipe.get("window")
    _need(isinstance(window, dict) and set(window) == {"start", "stop"}, "INVALID_MEASUREMENT_WINDOW")
    start, stop = window["start"], window["stop"]
    _need(type(start) is int and start >= 0 and (stop is None or type(stop) is int and stop > start), "INVALID_MEASUREMENT_WINDOW")
    _need(start < len(values) and (stop is None or stop <= len(values)), "MEASUREMENT_WINDOW_OUT_OF_RANGE")
    values = values[start:stop]
    conversion = recipe.get("conversion", {"scale": 1, "offset": 0})
    _need(isinstance(conversion, dict) and set(conversion) == {"scale", "offset"}, "INVALID_MEASUREMENT_CONVERSION")
    _need(unit == source_unit or "conversion" in recipe, "MEASUREMENT_CONVERSION_REQUIRED")
    scale, offset = _number(conversion["scale"]), _number(conversion["offset"])
    _need(scale != 0, "INVALID_MEASUREMENT_CONVERSION")
    values = [_number(value * scale + offset) for value in values]
    aggregators = {"mean": statistics.fmean, "sum": math.fsum, "min": min, "max": max, "first": lambda rows: rows[0], "last": lambda rows: rows[-1]}
    _need(recipe.get("aggregate") in aggregators, "UNSUPPORTED", "measurement aggregation")
    value = _number(aggregators[recipe["aggregate"]](values))
    return {"value": value, "unit": unit, "window": window, "raw_sources": [recipe["source"]], "recipe_hash": canonical_hash(recipe), "sample_count": len(values)}


def materialize(run, recipe, artifact_dir, *, evidence_id, claim_id, metric, tolerance, conditions=None, provenance_refs=None):
    """Create O_EXCL immutable JSON in run output directory; no state mutation.

    Caller binds recipe to the reviewed measurement obligation before invoking.
    artifact_dir is service-selected, never selected by a knowledge recipe.
    """
    actual = recompute(run, recipe)
    _need(_number(tolerance) >= 0, "INVALID_MEASUREMENT_TOLERANCE")
    _need(all(isinstance(s, str) and s for s in (evidence_id, claim_id, metric)), "INVALID_MEASUREMENT_ID")
    inputs = run.get("inputs")
    _need(isinstance(inputs, list) and bool(inputs) and run.get("input_hash") == canonical_hash(inputs), "MEASUREMENT_INPUTS_REQUIRED")
    for row in inputs:
        _need(hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest() == row["sha256"], "MEASUREMENT_INPUT_CHANGED")
    root = Path(artifact_dir)
    _need(root.is_absolute() and root.is_dir() and not root.is_symlink(), "INVALID_MEASUREMENT_ARTIFACT_DIR")
    _need(not run.get("output_dir") or root.resolve() == Path(run["output_dir"]).resolve(), "MEASUREMENT_OUTPUT_SCOPE_MISMATCH")
    # The service supplies the directory from permit.frozen_spec.output_dir.
    document = dict(actual, schema_version=1, task_id=run["task_id"], run_id=run["id"], input_hash=inputs[0]["sha256"], claim_id=claim_id, metric=metric, tolerance=tolerance, recipe=recipe)
    payload = (json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    target = root / ("measurement-" + uuid.uuid4().hex + ".json")
    temporary = root / (".measurement-" + uuid.uuid4().hex + ".tmp")
    try:
        # Reserve the destination, then publish a fully flushed file atomically.
        with open(target, "xb"):
            pass
        with open(temporary, "xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        target.chmod(0o444)
    finally:
        if temporary.exists():
            temporary.unlink()
    manifest = {"path": str(target.resolve()), "sha256": hashlib.sha256(payload).hexdigest()}
    evidence = {"id": evidence_id, "source_kind": "measurement", "lifecycle": "active", "conditions": conditions or {}, "provenance_refs": provenance_refs or [run["receipt_id"]], "task_id": run["task_id"], "run_id": run["id"], "input_hash": inputs[0]["sha256"], "output_hash": manifest["sha256"], "claim_id": claim_id, "metric": metric, "unit": actual["unit"], "window": actual["window"], "value": actual["value"], "tolerance": tolerance, "verification": {"status": "verified", "method": "recompute", "input_locator": inputs[0]["path"], "source_locator": manifest["path"], "raw_sources": actual["raw_sources"]}}
    return {"evidence": evidence, "artifact": document, "artifact_manifest": manifest}


def verify_artifact(run, document):
    """Recompute a saved recipe against original run outputs on every E2 check."""
    _need(isinstance(document, dict) and document.get("schema_version") == 1, "MEASUREMENT_RECIPE_REQUIRED")
    actual = recompute(run, document.get("recipe"))
    for key in ("unit", "window", "raw_sources", "recipe_hash", "sample_count", "value"):
        _need(canonical_hash(document.get(key)) == canonical_hash(actual[key]), "MEASUREMENT_RECOMPUTE_MISMATCH", key)
    return actual
