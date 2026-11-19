"""Real temporary Python trees only: no kit, credentials or solver execution."""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))
import evidence_bridge
from evidence_bridge import BridgeExecutionError, run_bridge


def bridge_function(script, config=None):
    # Loading md_mcp normally reads live config. Compile only the owned function.
    tree = ast.parse((ROOT / "mcp" / "md_mcp.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_bridge_exec")
    namespace = {"os": os, "sys": sys, "json": json, "MODE": "local",
                 "CFG": config or {}, "DISC_WRITE_GATE": "test discipline",
                 "_local_path": lambda rel: str(script)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / "mcp" / "md_mcp.py"), "exec"), namespace)
    return namespace["_bridge_exec"]


class BridgeDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def script(self, name, content):
        path = self.root / name
        path.write_text(content, encoding="utf-8")
        return path

    def tree(self, redirected=False):
        for pattern in ("*.heartbeat", "*.pid"):
            for path in self.root.glob(pattern):
                path.unlink()
        leaf = self.script("leaf.py", """import os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
(root / 'leaf.pid').write_text(str(os.getpid()))
# Bound the fixture itself so even a regressed implementation cannot leak forever.
stop = time.monotonic() + 20
while time.monotonic() < stop:
    with (root / 'leaf.heartbeat').open('ab', buffering=0) as f:
        f.write(b'x')
    time.sleep(0.03)
""")
        child = self.script("child.py", """import os, subprocess, sys, time
from pathlib import Path
root = Path(sys.argv[1])
(root / 'child.pid').write_text(str(os.getpid()))
p = subprocess.Popen([sys.executable, sys.argv[2], str(root)])
while p.poll() is None:
    with (root / 'child.heartbeat').open('ab', buffering=0) as f:
        f.write(b'x')
    time.sleep(0.03)
p.wait()
""")
        options = ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL" if redirected else ""
        parent = self.script("parent.py", """import subprocess, sys
from pathlib import Path
root = Path(__file__).resolve().parent
(root / 'parent.pid').write_text(str(__import__('os').getpid()))
# A partial valid audit receipt must not override a deadline failure.
print('{"audit_id":"partial","whitelisted":true,"returncode":0}', flush=True)
p = subprocess.Popen([sys.executable, %r, str(root), %r]%s)
p.wait()
""" % (str(child), str(leaf), options))
        return parent

    def assert_tree_stopped(self):
        # Require actual activity first, then observe both descendants after return.
        for name in ("child", "leaf"):
            heartbeat = self.root / (name + ".heartbeat")
            self.assertTrue(heartbeat.is_file(), name + " did not start")
            size = heartbeat.stat().st_size
            self.assertGreater(size, 0)
            time.sleep(0.35)
            self.assertEqual(heartbeat.stat().st_size, size, name + " still writing")
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            for name in ("parent", "child", "leaf"):
                pid = int((self.root / (name + ".pid")).read_text())
                handle = kernel.OpenProcess(0x00100000, False, pid)
                if handle:
                    try:
                        self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0,
                                         name + " process remains alive")
                    finally:
                        kernel.CloseHandle(handle)
                else:
                    self.assertEqual(ctypes.get_last_error(), 87, "Cannot inspect test PID")

    def test_real_parent_child_grandchild_deadline(self):
        for redirected in (False, True):
            with self.subTest(redirected=redirected):
                parent = self.tree(redirected)
                started = time.monotonic()
                with self.assertRaises(BridgeExecutionError) as caught:
                    run_bridge([sys.executable, str(parent)], timeout=1.5)
                error = caught.exception
                self.assertEqual(error.code, "bridge-deadline-exceeded")
                self.assertTrue(error.timed_out)
                self.assertTrue(error.tree_terminated)
                self.assertTrue(error.bridge_reaped)
                self.assertLess(time.monotonic() - started, 12)
                self.assert_tree_stopped()

    def test_bridge_timeout_never_consumes_partial_audit_json(self):
        result = bridge_function(self.tree())({"cmd": "anonymous", "timeout_seconds": 1.5})
        self.assertEqual(result["error"], "bridge-deadline-exceeded")
        self.assertTrue(result["tree_terminated"])
        self.assertEqual(result["execution_state"], "START_UNKNOWN")
        self.assertNotIn("audit_id", result)
        self.assertNotIn("returncode", result)
        self.assert_tree_stopped()

    def test_uncertain_termination_remains_error_after_real_cleanup(self):
        terminate = evidence_bridge._terminate_tree

        def uncertain(*args):
            terminate(*args)
            return False, "injected unconfirmed termination"

        with patch.object(evidence_bridge, "_terminate_tree", side_effect=uncertain):
            result = bridge_function(self.tree())({"cmd": "anonymous", "timeout_seconds": 1.5})
        self.assertEqual(result["error"], "bridge-termination-unknown")
        self.assertFalse(result["tree_terminated"])
        self.assertTrue(result["bridge_reaped"])
        self.assertTrue(result["fail_closed"])
        self.assertEqual(result["execution_state"], "START_UNKNOWN")
        self.assert_tree_stopped()

    def test_normal_audit_json_protocol_is_preserved(self):
        script = self.script("audit.py", """import json
print('[CLAUDE_INTERFACE] diagnostic')
print(json.dumps({'audit_id': 'audit-a', 'whitelisted': True, 'returncode': 7,
                  'stdout': 'payload output', 'stderr': 'payload error'}))
""")
        result = bridge_function(script)({"cmd": "anonymous", "description": "probe", "timeout_seconds": 5})
        self.assertEqual(result["audit_id"], "audit-a")
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(result["stdout"], "payload output")
        self.assertEqual(result["stderr"], "payload error")
        self.assertEqual(result["cmd"], "anonymous")
        self.assertEqual(result["description"], "probe")
        self.assertEqual(result["audit_log"], "logs/claude_operations.jsonl")
        self.assertIs(result["timeout_enforced"], True)
        self.assertEqual(result["timeout_seconds"], 5)

    def test_timeout_capability_is_server_issued_and_request_bound(self):
        script = self.script("claimed.py", "print('{\"audit_id\":\"a\",\"whitelisted\":true,\"returncode\":0,\"timeout_enforced\":false,\"timeout_seconds\":999}')")
        result = bridge_function(script)({"cmd": "anonymous", "timeout_seconds": 4})
        self.assertIs(result["timeout_enforced"], True)
        self.assertEqual(result["timeout_seconds"], 4)
        failed = self.script("failed.py", "print('{\"audit_id\":\"a\",\"whitelisted\":true,\"returncode\":0,\"timeout_enforced\":true,\"timeout_seconds\":4}'); raise SystemExit(2)")
        result = bridge_function(failed)({"cmd": "anonymous", "timeout_seconds": 4})
        self.assertNotIn("timeout_enforced", result)
        self.assertNotIn("timeout_seconds", result)

    def test_non_json_completion_fallback_is_preserved(self):
        script = self.script("plain.py", "import sys; print('plain output'); print('detail', file=sys.stderr); sys.exit(3)")
        result = bridge_function(script)({"cmd": "anonymous"})
        self.assertEqual(result["returncode"], 3)
        self.assertEqual(result["stdout"], "plain output")
        self.assertIn("detail", result["stderr"])
        self.assertNotIn("timeout_enforced", result)
        self.assertNotIn("timeout_seconds", result)

    def test_background_and_identity_switch_rejected_before_spawn(self):
        script = self.script("unused.py", "raise AssertionError('must not execute')")
        with patch.object(evidence_bridge.subprocess, "Popen", side_effect=AssertionError("spawned")):
            result = bridge_function(script)({"cmd": "anonymous", "background": True})
            self.assertEqual(result["error"], "bridge-background-unsupported")
            result = bridge_function(script, {"exec_bridge": {"sudo_user": "other"}})({"cmd": "anonymous"})
            self.assertEqual(result["error"], "bridge-identity-switch-unsupported")

    def test_invalid_deadline_rejected_before_spawn(self):
        for value in (0, -1, True, float("nan"), float("inf"), "1"):
            with self.subTest(value=value), self.assertRaises(BridgeExecutionError) as caught:
                run_bridge([sys.executable, "-c", "pass"], timeout=value)
            self.assertEqual(caught.exception.code, "bridge-invalid-deadline")

    @unittest.skipUnless(os.name == "nt", "Windows taskkill prerequisite")
    def test_missing_taskkill_refuses_before_spawn(self):
        with patch.object(evidence_bridge, "_taskkill_path", return_value=None):
            with self.assertRaises(BridgeExecutionError) as caught:
                run_bridge([sys.executable, "-c", "pass"], timeout=1)
        self.assertEqual(caught.exception.code, "bridge-tree-termination-unavailable")


if __name__ == "__main__":
    unittest.main()
