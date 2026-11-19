"""Detached trusted worker for a frozen execution handoff."""
import json
import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent / "evolution") not in sys.path:
    sys.path.insert(0, str(HERE.parent / "evolution"))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from evidence_v3.contracts import atomic_json, canonical_hash, read_json
import md_mcp
import evidence_execution


def main(path):
    job_path = Path(path).resolve()
    journal_path = job_path.with_name(job_path.name.removesuffix(".job.json") + ".json")
    job = read_json(job_path)
    journal = read_json(journal_path)
    if (not isinstance(job, dict) or not isinstance(journal, dict) or
            journal.get("state") != "starting" or
            job.get("launch_id") != journal.get("launch_id") or
            canonical_hash(job.get("frozen_spec")) != journal.get("spec_hash")):
        raise RuntimeError("handoff job binding mismatch")
    matching = [row for row in job.get("registrations", [])
                if isinstance(row, dict) and row.get("command") == job["frozen_spec"].get("command")
                and job["frozen_spec"].get("kind") in row.get("kinds", [])]
    if len(matching) != 1 or canonical_hash(matching[0]) != journal.get("registration_hash"):
        raise RuntimeError("handoff registration binding mismatch")
    adapter = evidence_execution.ControlledExecutionAdapter(
        job["registrations"], md_mcp._bridge_exec)
    task_dir, output, expected_journal = adapter._journal_location(job["frozen_spec"])
    if (expected_journal.resolve() != journal_path or
            journal.get("task_id") != task_dir.name or
            journal.get("permit_id") != output.name or
            journal.get("output_dir") != str(output)):
        raise RuntimeError("handoff path binding mismatch")
    try:
        result = adapter.launch(job["frozen_spec"])
        current = read_json(journal_path)
        if current.get("launch_id") != job["launch_id"]:
            raise RuntimeError("handoff journal identity changed")
        atomic_json(journal_path, {**current, "state": "complete", "result": result})
    except Exception as exc:
        try:
            current = read_json(journal_path)
            if current.get("launch_id") == job["launch_id"]:
                atomic_json(journal_path, {**current, "state": "unknown",
                    "error_code": getattr(exc, "code", type(exc).__name__),
                    "error_detail": type(exc).__name__})
        finally:
            raise


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except Exception:
        traceback.print_exc(file=sys.stderr)
        raise SystemExit(1)
