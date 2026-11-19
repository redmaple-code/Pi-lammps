# -*- coding: utf-8 -*-
"""MCP contract: business failures must be visible (isError) and typed, not exceptions."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MCP_DIR = os.path.join(ROOT, "mcp")
FIXTURE_KIT = os.path.join(HERE, "fixtures", "kit")
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)
if os.path.join(ROOT, "evolution") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "evolution"))

import md_mcp  # noqa: E402
import pipeline_check  # noqa: E402


class IsErrorTests(unittest.TestCase):
    def test_fail_closed_marker(self):
        self.assertTrue(md_mcp._is_error({"error": "x", "fail_closed": True}))

    def test_error_string_without_fail_closed(self):
        self.assertTrue(md_mcp._is_error({"error": "execution-permit-required"}))

    def test_error_statuses(self):
        for status in ("PIPELINE_ERROR", "RESEARCH_ERROR", "KIT_UNAVAILABLE", "NO_LOG"):
            self.assertTrue(md_mcp._is_error({"status": status}), status)
        self.assertTrue(md_mcp._is_error({"status": "SOMETHING_ERROR"}))

    def test_degraded_and_success_are_not_errors(self):
        self.assertFalse(md_mcp._is_error({"status": "DEGRADED", "verified": False}))
        self.assertFalse(md_mcp._is_error({"status": "OK", "strategies": []}))
        self.assertFalse(md_mcp._is_error({"cases": {}}))

    def test_non_dict_is_not_error(self):
        self.assertFalse(md_mcp._is_error("text"))
        self.assertFalse(md_mcp._is_error(None))


class ToolSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mcp-contract-")
        self.addCleanup(self.temporary.cleanup)
        self._old = {key: os.environ.get(key) for key in
                     ("MDSIM_DATA_ROOT", "MDSIM_HUMAN_INBOX", "MDSIM_KIT_BASE")}
        os.environ["MDSIM_DATA_ROOT"] = os.path.join(self.temporary.name, "data")
        os.environ["MDSIM_HUMAN_INBOX"] = os.path.join(self.temporary.name, "inbox")
        os.environ["MDSIM_KIT_BASE"] = os.path.join(self.temporary.name, "empty-kit")
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_selfcheck_reports_missing_kit_and_registrations(self):
        result = md_mcp.sim_selfcheck({})
        self.assertFalse(result["execution"]["available"])
        self.assertIn("claude_interface", result["execution"]["missing_kit_files"])
        self.assertIn("remediation", result["execution"])
        self.assertFalse(result["tools"]["sim_exec"])

    def test_selfcheck_available_when_kit_and_registration_present(self):
        kit = os.path.join(self.temporary.name, "kit")
        runtime = os.path.join(kit, "agents", "runtime", "v3.3")
        os.makedirs(runtime)
        for name in ("claude_interface", "preflight_agent", "launch_agent",
                     "monitor_agent", "recover_agent", "analyze_agent", "research_agent"):
            with open(os.path.join(runtime, name + ".py"), "w", encoding="utf-8") as stream:
                stream.write("# fixture\n")
        executable = os.path.join(self.temporary.name, "lmp.exe")
        open(executable, "wb").close()
        config = {"mode": "local", "local_base": kit,
                  "evidence_execution_registrations": [{
                      "profile_id": "local-lammps-static-v1",
                      "executable": executable,
                      "command": [executable, "-in", "in.production", "-log", "{output:run.log}"],
                      "kinds": ["production", "validation"],
                      "entrypoints": ["in.production"]}]}
        os.environ["MDSIM_KIT_BASE"] = kit
        with mock.patch.object(md_mcp, "CFG", config), mock.patch.object(md_mcp, "MODE", "local"):
            result = md_mcp.sim_selfcheck({})
        self.assertTrue(result["execution"]["available"], result)
        self.assertEqual(result["execution"]["missing_kit_files"], [])
        self.assertTrue(result["tools"]["sim_exec"])

    def test_sim_status_without_bus_is_no_active_run(self):
        result = md_mcp.sim_status({})
        self.assertEqual(result["status"], "NO_ACTIVE_RUN")
        self.assertEqual(result["case_count"], 0)
        self.assertNotIn("error", result)

    def test_sim_exec_returns_typed_gap_instead_of_attribute_error(self):
        with mock.patch.object(md_mcp, "CFG", {"mode": "local"}), \
                mock.patch.object(md_mcp, "MODE", "local"):
            result = md_mcp.sim_exec({"task_id": "t", "cmd": "echo hi"})
        self.assertEqual(result["error"], "execution-profile-not-configured")
        self.assertTrue(result["fail_closed"])
        self.assertTrue(result["detail"])
        self.assertNotIn("execution-internal-error", result.get("error", ""))

    def test_pipeline_check_missing_kit_is_typed(self):
        prod = os.path.join(self.temporary.name, "prod")
        os.makedirs(prod)
        with open(os.path.join(prod, "log.run"), "w", encoding="utf-8") as stream:
            stream.write("0 300.0 -5000.0 0 0 0 1.0 1000.0\n")
        old = pipeline_check.PIPELINE_EVENTS_LOG
        pipeline_check.PIPELINE_EVENTS_LOG = os.path.join(self.temporary.name, "events.jsonl")
        try:
            result = pipeline_check.run(prod, "log.run", "case_t",
                                        workdir=os.path.join(self.temporary.name, "wd"))
        finally:
            pipeline_check.PIPELINE_EVENTS_LOG = old
        self.assertEqual(result["status"], "KIT_UNAVAILABLE")
        self.assertTrue(result["missing_kit"])
        self.assertIn("remediation", result)

    def test_example_config_is_safe_and_explicit(self):
        example = json.load(open(os.path.join(MCP_DIR, "md_config.json.example"),
                                 encoding="utf-8"))
        self.assertIn("evidence_execution_registrations", example)
        self.assertEqual(example["evidence_execution_registrations"], [],
                         "模板必须保持 fail-closed 空列表,由安装器播种")


class StdioIsErrorTests(unittest.TestCase):
    def test_business_failure_sets_iserror(self):
        data_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, data_root, True)
        payload = "\n".join(json.dumps(row) for row in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "sim_evidence",
                        "arguments": {"action": "no-such-action", "task_id": "t"}}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "sim_status", "arguments": {}}},
        ]) + "\n"
        env = dict(os.environ, MDSIM_DATA_ROOT=data_root, MDSIM_KIT_BASE=FIXTURE_KIT)
        proc = subprocess.run([sys.executable, os.path.join(MCP_DIR, "md_mcp.py")],
                              input=payload, capture_output=True, text=True,
                              timeout=60, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        replies = {json.loads(line)["id"]: json.loads(line)
                   for line in proc.stdout.splitlines() if line.strip()}
        self.assertTrue(replies[2]["result"].get("isError"), replies[2])
        self.assertEqual(replies[2]["result"]["structuredContent"]["error"], "unknown-action")
        # NO_ACTIVE_RUN 是正常状态,不是工具故障,不应设 isError
        self.assertNotIn("isError", replies[3]["result"])
        self.assertEqual(replies[3]["result"]["structuredContent"]["status"], "NO_ACTIVE_RUN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
