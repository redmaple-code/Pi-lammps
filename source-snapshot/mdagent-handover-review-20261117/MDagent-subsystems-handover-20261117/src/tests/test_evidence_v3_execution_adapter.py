import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evolution"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from evidence_v3.contracts import V3Error
from evidence_execution import ControlledExecutionAdapter, build_registration


class ExecutionAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "workspace"
        self.root.mkdir()
        self.exe = self.root / "solver"
        self.exe.write_text("trusted executable", encoding="utf-8")
        self.input = self.root / "main.in"
        self.input.write_text("consume data.dat\nemit result.out\n", encoding="utf-8")
        self.data = self.root / "data.dat"
        self.data.write_text("input", encoding="utf-8")
        self.profile = {"id": "registered-solver", "version_args": ["-h"], "version_contains": "solver 1", "version_returncode": 0, "entrypoints": ["main.in"], "grammar": [{"prefix": ["consume"], "roles": ["input"]}, {"prefix": ["emit"], "roles": ["output"]}]}
        self.registration = build_registration(self.profile, str(self.exe), [str(self.exe), "-in", "main.in", "-log", "{output:run.log}"], kinds=["validation"], registration_id="registered-solver")
        self.calls = []
        def bridge(payload):
            self.calls.append(payload)
            certified = {"timeout_enforced": True, "timeout_seconds": payload["timeout_seconds"]}
            if "-h" in payload["cmd"]:
                return {"audit_id": "version-audit", "whitelisted": True, "returncode": 0, "stdout": "solver 1", "stderr": "", **certified}
            out = Path(payload["cwd"])
            (out / "run.log").write_text("done", encoding="utf-8")
            (out / "result.out").write_text("done", encoding="utf-8")
            return {"audit_id": "run-audit", "whitelisted": True, "returncode": 0, "stdout": "", "stderr": "", **certified}
        def worker_launcher(job_path):
            from evidence_v3.contracts import atomic_json, read_json
            job = read_json(job_path)
            result = self.adapter.launch(job["frozen_spec"])
            journal_path = job_path.with_name(job_path.name.removesuffix(".job.json") + ".json")
            journal = read_json(journal_path)
            atomic_json(journal_path, {**journal, "state": "complete", "result": result})
            return {"started": True}
        self.adapter = ControlledExecutionAdapter([self.registration], bridge, worker_launcher=worker_launcher)

    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def spec(self):
        return {"kind": "validation", "command": self.registration["command"], "cwd": str(self.root), "limits": {"timeout_seconds": 10}, "inputs": [{"path": "main.in", "sha256": self.digest(self.input)}, {"path": "data.dat", "sha256": self.digest(self.data)}]}

    def test_authorization_probes_version_and_closes_static_inputs(self):
        attestation = self.adapter(self.spec(), {})
        self.assertTrue(attestation["supported"])
        self.assertEqual(attestation["closure"]["inputs"], sorted(attestation["closure"]["inputs"], key=lambda x: x["path"]))
        self.assertEqual(self.calls[0]["cmd"], str(self.exe) + " -h")

    def test_launch_uses_audited_bridge_and_declared_outputs(self):
        spec = self.spec()
        output = self.root.parent / "outputs"
        output.mkdir()
        spec = dict(spec, output_dir=str(output))
        result = self.adapter.launch(spec)
        self.assertEqual(result["returncode"], 0)
        self.assertEqual({Path(row["path"]).name for row in result["outputs"]}, {"run.log", "result.out"})
        self.assertNotIn("subprocess", self.calls[-1]["cmd"])

    def test_missing_or_wrong_timeout_attestation_blocks(self):
        original = self.adapter.bridge_exec
        self.adapter.bridge_exec = lambda payload: {"audit_id": "a", "whitelisted": True,
            "returncode": 0, "stdout": "solver 1", "stderr": ""}
        with self.assertRaises(V3Error):
            self.adapter(self.spec(), {})
        self.adapter.bridge_exec = lambda payload: {"audit_id": "a", "whitelisted": True,
            "returncode": 0, "stdout": "solver 1", "stderr": "",
            "timeout_enforced": True, "timeout_seconds": payload["timeout_seconds"] + 1}
        with self.assertRaises(V3Error):
            self.adapter(self.spec(), {})
        self.adapter.bridge_exec = original

    def test_detached_worker_process_completes_bound_journal(self):
        from evidence_v3.contracts import read_json
        harness = self.root / "worker_host.py"
        mcp = Path(__file__).resolve().parents[1] / "mcp"
        evolution = Path(__file__).resolve().parents[1] / "evolution"
        harness.write_text("\n".join([
            "import pathlib, sys, types",
            "bridge = types.ModuleType('md_mcp')",
            "def execute(payload):",
            "    out = pathlib.Path(payload['cwd'])",
            "    (out / 'run.log').write_text('worker complete', encoding='utf-8')",
            "    (out / 'result.out').write_text('anonymous output', encoding='utf-8')",
            "    return {'audit_id': 'test-worker-audit', 'whitelisted': True, 'returncode': 0,",
            "            'stdout': '', 'stderr': '', 'timeout_enforced': True,",
            "            'timeout_seconds': payload['timeout_seconds']}",
            "bridge._bridge_exec = execute",
            "sys.modules['md_mcp'] = bridge",
            "sys.path[:0] = [%r, %r]" % (str(mcp), str(evolution)),
            "import evidence_worker",
            "evidence_worker.main(sys.argv[1])",
            ""]), encoding="utf-8")
        processes = []

        def launch_worker(job_path):
            process = subprocess.Popen([sys.executable, str(harness), str(job_path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(self.root), close_fds=True)
            processes.append(process)
            return {"started": True, "pid": process.pid}

        adapter = ControlledExecutionAdapter([self.registration], self.adapter.bridge_exec,
                                             worker_launcher=launch_worker)
        spec = self.spec()
        output = self.root.parent / "task-worker" / "outputs" / "permit-worker"
        output.mkdir(parents=True)
        frozen = dict(spec, cwd=str(self.root), output_dir=str(output), inputs_read_only=True)
        receipt = adapter.handoff(frozen)
        self.assertTrue(receipt["started"])
        self.assertFalse(receipt["execution_complete"])
        worker_output, worker_error = processes[0].communicate(timeout=15)
        self.assertEqual(processes[0].returncode, 0, worker_error.decode("utf-8", "replace"))
        run = {"id": "run-worker", "task_id": "task-worker", "permit_id": "permit-worker",
               "launch": receipt, "frozen_spec": frozen}
        reconciled = adapter.reconcile(run, {})
        journal_path = output.parent.parent / "handoffs" / "permit-worker.json"
        self.assertEqual(read_json(journal_path)["state"], "complete")
        self.assertTrue(reconciled["execution_complete"])
        self.assertEqual(reconciled["launch_id"], receipt["launch_id"])
        self.assertEqual({Path(row["path"]).name for row in reconciled["outputs"]}, {"run.log", "result.out"})

    def test_handoff_journal_reconciles_completed_outputs(self):
        spec = self.spec()
        output = self.root.parent / "task" / "outputs" / "permit-a"
        output.mkdir(parents=True)
        frozen = dict(spec, cwd=str(self.root), output_dir=str(output), inputs_read_only=True)
        receipt = self.adapter.handoff(frozen)
        self.assertTrue(receipt["started"])
        self.assertFalse(receipt["execution_complete"])
        run = {"id": "run-a", "task_id": "task", "permit_id": "permit-a",
               "launch": receipt, "frozen_spec": frozen}
        reconciled = None
        for _ in range(200):
            try:
                reconciled = self.adapter.reconcile(run, {})
                if reconciled.get("execution_complete"):
                    break
            except V3Error as exc:
                if exc.code != "unreadable-json":
                    raise
            import time
            time.sleep(0.01)
        self.assertIsNotNone(reconciled)
        self.assertTrue(reconciled["execution_complete"])
        self.assertEqual(reconciled["launch_id"], receipt["launch_id"])
        self.assertEqual({Path(row["path"]).name for row in reconciled["outputs"]}, {"run.log", "result.out"})

    def test_reconcile_rejects_untrusted_launch_id_or_journal_binding(self):
        spec = self.spec()
        output = self.root.parent / "task" / "outputs" / "permit-b"
        output.mkdir(parents=True)
        frozen = dict(spec, cwd=str(self.root), output_dir=str(output), inputs_read_only=True)
        receipt = self.adapter.handoff(frozen)
        run = {"id": "run-b", "task_id": "task", "permit_id": "permit-b",
               "launch": receipt, "frozen_spec": frozen}
        with self.assertRaises(V3Error):
            self.adapter.reconcile(run, {"launch_id": "client-forged"})

    def test_reconcile_requires_live_worker_or_completion_record(self):
        spec = self.spec()
        output = self.root.parent / "task" / "outputs" / "permit-c"
        output.mkdir(parents=True)
        frozen = dict(spec, cwd=str(self.root), output_dir=str(output), inputs_read_only=True)
        adapter = ControlledExecutionAdapter([self.registration], self.adapter.bridge_exec,
                                              worker_launcher=lambda job_path: {"started": True, "pid": 99999999})
        receipt = adapter.handoff(frozen)
        run = {"id": "run-c", "task_id": "task", "permit_id": "permit-c",
               "launch": receipt, "frozen_spec": frozen}
        with self.assertRaises(V3Error) as caught:
            adapter.reconcile(run, {})
        self.assertEqual(caught.exception.code, "START_UNKNOWN")

    def test_dynamic_or_unknown_semantics_are_unsupported(self):
        self.input.write_text("read_data $DATA\n", encoding="utf-8")
        spec = self.spec()
        spec["inputs"][0]["sha256"] = self.digest(self.input)
        with self.assertRaises(V3Error):
            self.adapter(spec, {})

    def test_static_repeat_accepts_bounded_literal_tail(self):
        self.input.write_text("set 1 2 3 4\n", encoding="utf-8")
        self.profile["grammar"] = [{"prefix": ["set"], "roles": ["word"], "repeat": "static"}]
        self.registration = build_registration(
            self.profile, str(self.exe),
            [str(self.exe), "-in", "main.in", "-log", "{output:run.log}"],
            kinds=["validation"], registration_id="registered-solver",
        )
        bridge = self.adapter.bridge_exec
        self.adapter = ControlledExecutionAdapter([self.registration], bridge)
        spec = self.spec()
        spec["inputs"] = [{"path": "main.in", "sha256": self.digest(self.input)}]
        self.assertTrue(self.adapter(spec, {})["supported"])

    def test_static_repeat_rejects_path_like_tail(self):
        self.input.write_text("set 1 ../escape\n", encoding="utf-8")
        self.profile["grammar"] = [{"prefix": ["set"], "roles": ["word"], "repeat": "static"}]
        self.registration = build_registration(
            self.profile, str(self.exe),
            [str(self.exe), "-in", "main.in", "-log", "{output:run.log}"],
            kinds=["validation"], registration_id="registered-solver",
        )
        bridge = self.adapter.bridge_exec
        self.adapter = ControlledExecutionAdapter([self.registration], bridge)
        spec = self.spec()
        spec["inputs"] = [{"path": "main.in", "sha256": self.digest(self.input)}]
        with self.assertRaises(V3Error):
            self.adapter(spec, {})

    def test_entrypoint_option_and_embedded_nul_registration_validation(self):
        profile = dict(self.profile)
        profile.pop("entrypoints")
        profile["entrypoint_option"] = "-in"
        registration = build_registration(profile, str(self.exe),
            [str(self.exe), "-in", "main.in", "-log", "{output:run.log}"],
            kinds=["validation"], registration_id="registered-solver")
        self.assertEqual(registration["entrypoints"], ["main.in"])
        with self.assertRaises(V3Error):
            build_registration(self.profile, str(self.exe),
                [str(self.exe), "-in", "bad\0arg"], kinds=["validation"],
                registration_id="registered-solver")

    def test_fingerprint_or_version_change_blocks(self):
        self.exe.write_text("changed", encoding="utf-8")
        with self.assertRaises(V3Error):
            self.adapter(self.spec(), {})

    def test_extra_limits_and_unregistered_command_block(self):
        spec = self.spec()
        spec["limits"] = {"timeout_seconds": 10, "memory_mb": 100}
        with self.assertRaises(V3Error):
            self.adapter(spec, {})
        spec = self.spec()
        spec["command"] = [str(self.exe), "-in", "other.in", "-log", "{output:run.log}"]
        with self.assertRaises(V3Error):
            self.adapter(spec, {})


if __name__ == "__main__":
    unittest.main()
