"""Run every subsystem test suite shipped in this handoff package.

Usage (from the package root):
    python tools/run_verification.py [--json PATH] [--skip-ukb]

The design-review suite expects a populated UKB store at ``src/data/ukb``.
This script provisions a temporary store there (admin ``redmaple`` + the
three seed sets), runs the suite, and removes the store afterwards unless it
already existed. No runtime store, token or log is left inside the package.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
SRC = PACKAGE / "src"
PY = sys.executable


def _run(name, command, cwd, env=None, timeout=1200):
    started = time.time()
    merged = os.environ.copy()
    if env:
        merged.update(env)
    proc = subprocess.run(command, cwd=str(cwd), env=merged, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    output = (proc.stdout or "") + (proc.stderr or "")
    return {"name": name, "command": command, "cwd": str(Path(cwd).relative_to(PACKAGE)),
            "returncode": proc.returncode, "seconds": round(time.time() - started, 1),
            "output": output}


def _unittest_count(output):
    match = re.search(r"Ran (\d+) tests?", output)
    skipped = re.search(r"skipped=(\d+)", output)
    return (int(match.group(1)) if match else None,
            int(skipped.group(1)) if skipped else 0)


def _node_count(output):
    tests = re.search(r"^# tests (\d+)", output, re.M)
    passed = re.search(r"^# pass (\d+)", output, re.M)
    failed = re.search(r"^# fail (\d+)", output, re.M)
    return (int(tests.group(1)) if tests else None,
            int(passed.group(1)) if passed else None,
            int(failed.group(1)) if failed else None)


def _unittest_suite(name, pattern, cwd=SRC):
    return _run(name, [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "tests",
                       "-p", pattern], cwd)


def _provision_ukb(store: Path):
    admin_file = store.parent / "admin.json"
    env = {"REVIEW_UKB_ADMIN_FILE": str(admin_file)}
    for command in ([PY, "-m", "review_layer.ukb_cli", "init", "--admin", "redmaple"],
                    [PY, "-m", "review_layer.ukb_cli", "seed-all"]):
        proc = subprocess.run(command, cwd=str(SRC / "design-review-layer"), env={**os.environ, **env},
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError("%s failed: %s" % (" ".join(command), proc.stderr or proc.stdout))
    return env


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="verification/RESULTS.json")
    ap.add_argument("--skip-ukb", action="store_true",
                    help="skip the provisioned UKB store; design-review write tests will skip")
    args = ap.parse_args()

    results = []

    results.append(_run("ukb-package",
                        [PY, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                        SRC / "ukb"))
    results.append(_unittest_suite("kb-compat", "test_kb_*.py"))
    results.append(_unittest_suite("evidence-v3", "test_evidence_v3_*.py"))
    for module in ("test_stage_gate.py", "test_stage_state.py", "test_assessment_v2.py",
                   "test_config_check.py", "test_deep_review.py", "test_evidence_grade.py",
                   "test_human_authority.py", "test_md_mcp_contract.py"):
        results.append(_unittest_suite("gate-core:" + module[:-3], module))
    for module in ("test_design_review.py", "test_physics_gate.py", "test_md_proj_mcp.py",
                   "test_model_research.py"):
        results.append(_unittest_suite("dual-gate:" + module[:-3], module))
    results.append(_run("dual-gate:e2e", [PY, "tests/e2e_dual_gate_demo.py"], SRC))

    store = SRC / "data" / "ukb"
    provisioned = False
    try:
        if not args.skip_ukb and not (store / "ukb.sqlite").exists():
            store.parent.mkdir(parents=True, exist_ok=True)
            env = _provision_ukb(store)
            provisioned = True
        else:
            env = {}
        results.append(_run("design-review",
                            [PY, "-m", "unittest", "discover", "-s", "tests"],
                            SRC / "design-review-layer", env))
        results.append(_run("design-review:template",
                            [PY, "-m", "review_layer.template_check"],
                            SRC / "design-review-layer", env))
    finally:
        if provisioned:
            shutil.rmtree(store, ignore_errors=True)
            (SRC / "design-review-layer" / "ukb_admin.json").unlink(missing_ok=True)

    results.append(_run("ukb-freeze", [PY, "tools/freeze.py", "--check"], SRC / "ukb"))
    results.append(_run("flow-gate:node", ["node", "tests/test_flow_gate.mjs"], SRC))
    results.append(_run("usage-patch:node", ["node", "tests/test_patch_dsh_usage.mjs"], SRC))

    logs = PACKAGE / "verification" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for row in results:
        safe = row["name"].replace(":", "_").replace("/", "_")
        (logs / (safe + ".log")).write_text(row.pop("output"), encoding="utf-8")

    summary = []
    failed = 0
    for row in results:
        name = row["name"]
        if name.endswith(":node"):
            total, passed, fail = _node_count(open(logs / (name.replace(":", "_") + ".log"),
                                                   encoding="utf-8").read())
            detail = "%s/%s passed" % (passed, total)
            ok = row["returncode"] == 0 and fail == 0
        elif name in ("ukb-freeze",):
            detail = "sha256 unchanged" if row["returncode"] == 0 else "changed"
            ok = row["returncode"] == 0
        else:
            total, skipped = _unittest_count(open(logs / (name.replace(":", "_") + ".log"),
                                                  encoding="utf-8").read())
            detail = "%s tests, %s skipped" % (total, skipped)
            ok = row["returncode"] == 0
        row["ok"] = ok
        row["detail"] = detail
        failed += 0 if ok else 1
        summary.append(row)

    report = {
        "package": PACKAGE.name,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "all_ok": failed == 0,
        "failures": failed,
        "suites": summary,
    }
    target = PACKAGE / args.json
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for row in summary:
        print("%-34s %-6s %-22s %.1fs" % (row["name"], "OK" if row["ok"] else "FAIL",
                                          row["detail"], row["seconds"]))
    print("\nall_ok=%s failures=%s -> %s" % (report["all_ok"], failed, target))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
