"""HTTP tests for the isolated localhost evidence v3 interface."""
import copy
import json
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "mcp"), str(ROOT / "evolution")]
import webui.server as web


class WebUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.data = self.root / "data"
        self.calls = []
        # Isolate server-owned source directories while retaining real retrieval.
        from evidence_v3 import retrieval
        sources = patch.object(retrieval, "__file__", str(self.root / "code/evolution/evidence_v3/retrieval.py"))
        sources.start()
        self.addCleanup(sources.stop)
        for directory in (self.root / "code/mcp/review_experience", self.root / "code/external_corpus",
                          self.data / "memory/review_experience", self.data / "external_corpus"):
            directory.mkdir(parents=True)
        (self.data / "memory/atoms.jsonl").write_text("", encoding="utf-8")
        (self.data / "memory/scenarios.json").write_text("[]", encoding="utf-8")
        self.server = web.create_server(self.data, workspace_root=self.workspace,
                                        transport=self.transport,
                                        human_verifier=self.human_verifier, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close_server)
        self.port = self.server.server_port
        self.csrf = self.server.app.token

    def human_verifier(self, task_id, request, context):
        actor_id = "test-owner"
        request_hash = web.canonical_hash(request)
        source_id = "test-event:" + request_hash
        return {"actor_id": actor_id, "source_refs": [source_id],
                "authorization_source": {"kind": "trusted_human_event", "source_id": source_id,
                    "actor_id": actor_id, "request_hash": request_hash, "verified": True}}

    def _close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, body=None, *, csrf=True, host=None, headers=None):
        conn = HTTPConnection(host or "127.0.0.1", self.port, timeout=30)
        values = {"Host": f"127.0.0.1:{self.port}"}
        payload = None
        if body is not None:
            values["Content-Type"] = "application/json"
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            values["Content-Length"] = str(len(payload))
        if csrf:
            values["X-Evidence-CSRF"] = self.csrf
        values.update(headers or {})
        try:
            conn.request(method, path, body=payload, headers=values)
            response = conn.getresponse()
            raw = response.read()
            result = json.loads(raw) if "json" in response.getheader("Content-Type", "") else raw
            return response.status, result
        finally:
            conn.close()

    def transport(self, request):
        self.calls.append(copy.deepcopy(request))
        operation = request["operation"]
        context = request["context"]
        if operation == "recognize_intent":
            text = context["request"]
            citation = {"path": "/request", "quote": text}
            return {"context_hash": request["context_hash"], "intent": {
                "action": "design", "goals": [{"id": "goal-a", "text": "Review the requested procedure",
                                                  "source_refs": [citation]}],
                "conditions": {}, "unknowns": [], "source_refs": [citation]}}
        if operation == "propose_checks":
            intent = context["intent"]
            citation = {"path": "/intent/goals/0/text", "quote": intent["goals"][0]["text"]}
            refs = intent["source_refs"]
            item = {"id": "check-a", "question": "What evidence supports the requested procedure?",
                    "claim": "The proposed procedure is bounded by the stated goal",
                    "target_refs": [intent["goals"][0]["id"]], "owner_rule": "E1", "due_stage": "PLAN",
                    "origin_refs": refs, "rationale": "Derived from the provided task goal",
                    "applicability": {"status": "applicable", "reason": "Bound to the stated goal",
                                      "source_refs": refs}}
            counter_queries = [row["id"] for row in context["retrieval"]["queries"]
                               if row["purpose"] == "counterexample"]
            proposal = {"items": [item], "discovery_review": {
                "goal_coverage": {intent["goals"][0]["id"]: [item["id"]]},
                "goal_coverage_rationale": "The task goal maps to one explicit obligation.",
                "counterexample_review": {"rationale": "Considered omitted evidence and alternative outcomes.",
                    "reviewed_item_ids": [item["id"]], "query_refs": counter_queries,
                    "limitations": ["No counterexample query was supplied by the initial request."],
                    "citations": [citation]},
                "remaining_unknowns": []}}
            return {"context_hash": request["context_hash"], "citations": [citation],
                    "proposal": proposal}
        raise AssertionError("unexpected transport operation: " + operation)

    def test_session_and_static_page_are_local_and_no_store(self):
        status, session = self.request("GET", "/api/session", csrf=False)
        self.assertEqual(status, 200)
        self.assertFalse(session["execution_enabled"])
        self.assertFalse(session["fixture"])
        status, page = self.request("GET", "/", csrf=False)
        self.assertEqual(status, 200)
        self.assertIn("证据审阅工作台".encode("utf-8"), page)

    def test_discover_and_commit_use_task_service(self):
        status, response = self.request("POST", "/api/discover", {"request": "Review this anonymous procedure"})
        self.assertEqual(status, 200, response)
        draft = response["draft"]
        self.assertEqual(draft["request"], "Review this anonymous procedure")
        self.assertTrue(draft["bundle"]["health"])
        self.assertTrue(draft["proposal"]["items"])
        self.assertEqual([row["operation"] for row in self.calls], ["recognize_intent", "propose_checks"])

        status, committed = self.request("POST", "/api/commit", {
            "draft_id": draft["id"], "task_id": "web-task-a", "request_id": "web-create-a"})
        self.assertEqual(status, 200, committed)
        state = committed["state"]
        self.assertEqual(state["task_id"], "web-task-a")
        self.assertEqual(state["plan"]["items"][0]["id"], "check-a")
        self.assertEqual(state["current_stage"], "PLAN")

    def test_mutations_require_csrf_origin_and_current_cas(self):
        status, denied = self.request("POST", "/api/discover", {"request": "x"}, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "CSRF_REQUIRED")
        status, denied = self.request("POST", "/api/discover", {"request": "x"},
                                      headers={"Origin": "http://example.invalid"})
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "ORIGIN_REJECTED")

        status, draft_response = self.request("POST", "/api/discover", {"request": "Review a procedure"})
        self.assertEqual(status, 200, draft_response)
        status, committed = self.request("POST", "/api/commit", {
            "draft_id": draft_response["draft"]["id"], "task_id": "cas-task-a", "request_id": "create-cas-a"})
        self.assertEqual(status, 200, committed)
        state = committed["state"]
        status, stale = self.request("POST", "/api/command", {
            "action": "review", "task_id": state["task_id"], "expected_revision": state["revision"],
            "expected_state_seq": state["state_seq"] - 1, "kind": "plan"})
        self.assertEqual(status, 409)
        self.assertIn("CONFLICT", stale["error"])

    def test_execution_is_disabled_by_default_and_untrusted_actions_are_hidden(self):
        status, result = self.request("POST", "/api/start", {"task_id": "absent"})
        self.assertEqual(status, 400)
        self.assertEqual(result["error"], "TASK_NOT_FOUND")
        status, response = self.request("GET", "/api/session", csrf=False)
        self.assertFalse(response["execution_enabled"])
        self.assertFalse(response["fixture"])

    def discover(self, task_id="http-task"):
        status, result = self.request("POST", "/api/discover", {"request": "Inspect an anonymous object"})
        self.assertEqual(status, 200, result)
        return result["draft"]

    def commit(self, draft, task_id="http-task"):
        status, result = self.request("POST", "/api/commit", {
            "draft_id": draft["id"], "task_id": task_id, "request_id": "create-" + task_id})
        self.assertEqual(status, 200, result)
        return result["state"]

    def post_task(self, route, state, **args):
        return self.request("POST", route, dict(task_id=state["task_id"],
            expected_revision=state["revision"], expected_state_seq=state["state_seq"], **args))

    def test_reconcile_http_route_forwards_only_server_checked_uncertain_runs(self):
        state = self.commit(self.discover(), "reconcile-task")
        run_id = "reconcile-run"
        waiting = copy.deepcopy(state)
        waiting["runs"] = [{"id": run_id, "status": "RUNNING", "kind": "validation"}]
        reconciled = {"id": run_id, "status": "RUNNING",
                      "reconciliation": {"started": True, "execution_complete": False}}
        received = []
        self.server.app.start_handler = lambda args: received.append(args) or reconciled
        with (patch.object(self.server.app, "current", return_value=waiting),
              patch.object(self.server.app, "snapshot", side_effect=lambda task_id, result=None:
                           {"state": waiting, "result": result})):
            status, response = self.post_task("/api/reconcile", waiting, run_id=run_id)
        self.assertEqual(status, 200, response)
        self.assertEqual(response["result"], reconciled)
        self.assertEqual(received, [{"action": "reconcile", "task_id": state["task_id"],
                                     "run_id": run_id, "receipt": {}}])

        finished = copy.deepcopy(waiting)
        finished["runs"][0]["status"] = "FINISHED"
        with patch.object(self.server.app, "current", return_value=finished):
            status, rejected = self.post_task("/api/reconcile", finished, run_id=run_id)
        self.assertEqual(status, 400, rejected)
        self.assertEqual(rejected["error"], "RUN_NOT_UNCERTAIN")
        self.assertEqual(len(received), 1)

    def test_multiple_rounds_bind_latest_intent_bundle_and_proposal(self):
        count = 0
        def transport(request):
            nonlocal count
            result = self.transport(request)
            if request["operation"] == "propose_checks":
                count += 1
                if count == 1:
                    result["proposal"]["discovery_review"]["remaining_unknowns"] = [{
                        "id": "unknown-a", "text": "Need a different anonymous outcome", "due_stage": "PLAN",
                        "blocking": True, "source_refs": request["context"]["intent"]["source_refs"]}]
            return result
        self.server.app.transport = transport
        draft = self.discover()
        self.assertTrue(draft["discovery"]["complete"], draft["discovery"])
        self.assertEqual(len(draft["discovery"]["rounds"]), 2)
        latest = draft["discovery"]["rounds"][-1]
        self.assertEqual(draft["intent"], latest["intent"])
        self.assertNotEqual(draft["intent"], draft["discovery"]["intent"])
        self.assertEqual(draft["bundle"], latest["bundle"])
        state = self.commit(draft)
        self.assertEqual(state["intent"], latest["intent"])
        self.assertEqual(state["plan"]["retrieval_hash"], latest["bundle"]["bundle_hash"])

    def test_failed_latest_round_keeps_matching_prior_tuple_and_blocks_commit(self):
        count = 0
        def transport(request):
            nonlocal count
            if request["operation"] == "propose_checks":
                count += 1
                if count > 1:
                    raise RuntimeError("external model unavailable")
            result = self.transport(request)
            if request["operation"] == "propose_checks":
                result["proposal"]["discovery_review"]["remaining_unknowns"] = [{
                    "id": "unknown-a", "text": "Need an alternative explanation", "due_stage": "PLAN",
                    "blocking": True, "source_refs": request["context"]["intent"]["source_refs"]}]
            return result
        self.server.app.transport = transport
        draft = self.discover()
        self.assertFalse(draft["discovery"]["complete"])
        self.assertEqual(len(draft["discovery"]["rounds"]), 2)
        self.assertIsNone(draft["discovery"]["rounds"][-1]["proposal"])
        self.assertEqual(draft["bundle"], draft["discovery"]["rounds"][0]["bundle"])
        status, result = self.request("POST", "/api/commit", {
            "draft_id": draft["id"], "task_id": "must-not-exist", "request_id": "blocked-create"})
        self.assertEqual(status, 400, result)
        self.assertEqual(result["error"], "DISCOVERY_INCOMPLETE")
        self.assertFalse((self.data / "tasks/must-not-exist").exists())

    def test_no_proposal_retains_diagnostics_and_cannot_commit(self):
        self.server.app.transport = lambda request: (_ for _ in ()).throw(RuntimeError("model unavailable"))
        draft = self.discover()
        self.assertIsNone(draft["proposal"])
        self.assertFalse(draft["discovery"]["complete"])
        self.assertTrue(draft["discovery"]["failure"])
        status, result = self.request("POST", "/api/commit", {
            "draft_id": draft["id"], "task_id": "blocked-task", "request_id": "blocked-create"})
        self.assertEqual(status, 400, result)
        self.assertEqual(result["error"], "DISCOVERY_INCOMPLETE")
        self.assertFalse((self.data / "tasks/blocked-task").exists())

    def lifecycle(self):
        # Reuse the established external model/solver fixture, never its API wrappers.
        from test_evidence_v3_lifecycle import LifecycleTests
        fixture = LifecycleTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.data = fixture.root / "data"
        for directory in (self.data / "memory/review_experience", self.data / "external_corpus"):
            directory.mkdir(parents=True, exist_ok=True)
        (self.data / "memory/atoms.jsonl").write_text("", encoding="utf-8")
        (self.data / "memory/scenarios.json").write_text("[]", encoding="utf-8")
        def transport(request):
            result = fixture.transport(request)
            if request["operation"] == "propose_checks":
                for item in result["proposal"]["items"]:
                    if item["owner_rule"] == "E2":
                        item["measurement"]["window"] = {"start": 0, "stop": 1}
            return result
        import md_mcp
        self.server.app.data_root = str(self.data)
        self.server.app.workspace = fixture.root
        self.server.app.transport = transport
        self.server.app.action_adapter = fixture.adapter
        def audited_fake_bridge(args):
            result = fixture.bridge_exec(args)
            if isinstance(result, dict) and result.get("whitelisted") is True:
                return {**result, "timeout_enforced": True,
                        "timeout_seconds": args["timeout_seconds"]}
            return result
        fixture.adapter.bridge_exec = audited_fake_bridge
        fixture.adapter.worker_launcher = fixture.fake_worker_launcher
        adapter_factory = patch.object(md_mcp, "_evidence_adapter", return_value=fixture.adapter)
        adapter_factory.start()
        self.addCleanup(adapter_factory.stop)
        self.server.app.start_handler = md_mcp.sim_exec
        state = self.commit(self.discover(), fixture.task_id)
        status, response = self.post_task("/api/command", state, action="revise",
            patch={"dependencies": {"cwd": str(fixture.work), "inputs": fixture.inputs}}, request_id="http-inputs")
        self.assertEqual(status, 200, response)
        state = response["state"]
        source = {"id": "design-material", "claim_id": "claim-design", "source_kind": "human", "lifecycle": "active",
            "conditions": {}, "provenance_refs": state["intent"]["source_refs"],
            "verification": {"status": "verified", "method": "content_review", "source_locator": str(fixture.input)},
            "content": fixture.input.read_text(encoding="utf-8"), "source_sha256": web.canonical_hash({})}
        from evidence_v3.contracts import file_hash
        source["source_sha256"] = file_hash(fixture.input)
        status, response = self.post_task("/api/evidence", state, record=source)
        self.assertEqual(status, 200, response)
        return fixture, response["state"]

    def validation_run(self):
        fixture, state = self.lifecycle()
        spec = fixture.spec("validation")
        status, response = self.post_task("/api/human", state, operation="execution", scope="validation",
            authorization_id="http-probe-authorization", spec=spec, actor_id="operator", quote="Allow this bounded probe")
        self.assertEqual(status, 200, response)
        state = response["state"]
        status, response = self.post_task("/api/command", state, action="review", kind="validation", spec=spec)
        self.assertEqual(status, 200, response)
        state = response["state"]
        self.assertEqual(state["reviews"][-1]["id"], spec["review_id"])
        status, response = self.post_task("/api/command", state, action="authorize", spec=spec)
        self.assertEqual(status, 200, response)
        state, permit = response["state"], response["result"]
        before = len(fixture.executions)
        status, denied = self.post_task("/api/start", state, permit_id=permit["id"], spec=spec,
            request_id="http-start", quote="Launch this exact probe", actor_id="operator", confirm_start=True,
            launch_event_id="missing-event")
        self.assertEqual(status, 400, denied)
        self.assertEqual(denied["error"], "LAUNCH_CONFIRMATION_REQUIRED")
        self.assertEqual(len(fixture.executions), before)
        status, auth = self.post_task("/api/human", state, operation="launch", permit_id=permit["id"],
            spec=spec, quote="Launch this exact probe", actor_id="operator")
        self.assertEqual(status, 200, auth)
        state = auth["state"]
        self.assertEqual(auth["launch_event_id"], state["authorizations"][-1]["id"])
        status, denied = self.post_task("/api/start", state, permit_id=permit["id"], spec=spec,
            request_id="http-start", quote="Different quote", actor_id="operator", confirm_start=True,
            launch_event_id=auth["launch_event_id"])
        self.assertEqual(status, 400, denied)
        status, launched = self.post_task("/api/start", state, permit_id=permit["id"], spec=spec,
            request_id="http-start", quote="Launch this exact probe", actor_id="operator", confirm_start=True,
            launch_event_id=auth["launch_event_id"])
        self.assertEqual(status, 200, launched)
        run = launched["result"]
        self.assertEqual(run["status"], "RUNNING")
        fixture.complete_fake_handoff(run)
        status, reconciled = self.post_task("/api/reconcile", launched["state"], run_id=run["id"],
            receipt={"launch_id": run["launch_id"]})
        self.assertEqual(status, 200, reconciled)
        launched = reconciled
        self.assertEqual(launched["result"]["status"], "FINISHED")
        calls = len(fixture.executions)
        status, replay = self.post_task("/api/start", launched["state"], permit_id=permit["id"], spec=spec,
            request_id="http-start", quote="Launch this exact probe", actor_id="operator", confirm_start=True,
            launch_event_id=auth["launch_event_id"])
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay["result"]["id"], launched["result"]["id"])
        self.assertEqual(len(fixture.executions), calls)
        return fixture, replay["state"], replay["result"]

    def test_http_validation_launch_authorization_and_idempotent_replay(self):
        _fixture, state, run = self.validation_run()
        self.assertEqual(state["current_stage"], "PLAN")
        self.assertEqual(run["kind"], "validation")
        self.assertEqual(len(state["runs"]), 1)

    def test_http_measurement_uses_original_finished_run_and_refresh_cas(self):
        _fixture, state, run = self.validation_run()
        status, response = self.post_task("/api/command", state, action="revise",
            patch={"decisions": {"selected_run_id": run["id"]}}, request_id="select-run")
        self.assertEqual(status, 200, response)
        state = response["state"]
        output = next(row for row in run["outputs"] if row["path"].endswith("measurement.json"))
        recipe = {"source": output, "format": "json", "pointer": "/value", "unit": "count", "source_unit": "count",
                  "aggregate": "mean", "window": {"start": 0, "stop": 1}}
        status, response = self.post_task("/api/command", state, action="measure", run_id=run["id"],
            item_id="measurement", evidence_id="http-measurement", recipe=recipe, conditions={"scope": "anonymous"})
        self.assertEqual(status, 200, response)
        state = response["state"]
        measured = state["evidence"][-1]
        self.assertEqual(measured["value"], 7)
        self.assertEqual(measured["run_id"], run["id"])
        self.assertEqual(measured["conditions"], {"scope": "anonymous"})
        changed = Path(output["path"])
        original = changed.read_bytes()
        changed.write_text('{"value":999}', encoding="utf-8")
        status, rejected = self.post_task("/api/command", state, action="measure", run_id=run["id"],
            item_id="measurement", evidence_id="forged-measurement", recipe=recipe, conditions={})
        self.assertEqual(status, 400, rejected)
        changed.write_bytes(original)
        status, refreshed = self.post_task("/api/command", state, action="refresh_knowledge", request_id="http-refresh")
        self.assertEqual(status, 200, refreshed)
        # An unchanged generation is a no-op; force an actual stale CAS token.
        stale_state = dict(refreshed["state"], state_seq=refreshed["state"]["state_seq"] - 1)
        status, stale = self.post_task("/api/command", stale_state, action="refresh_knowledge", request_id="other-refresh")
        self.assertEqual(status, 409, {"error": stale.get("error")})

    def test_global_publication_recovery_without_task(self):
        from test_evidence_v3_recovery import proposal
        from evidence_v3 import knowledge, store
        with patch.object(store, "bump_generation", side_effect=OSError("interrupted generation commit")):
            with self.assertRaises(OSError):
                knowledge.publish(proposal("http-entry"), "reviewer", str(self.data))
        status, inspected = self.request("POST", "/api/command", {"action": "inspect_publication"})
        self.assertEqual(status, 200, inspected)
        self.assertEqual(inspected["result"]["generation"]["status"], "pending")
        event_id = inspected["result"]["transactions"][0]["id"]
        args = {"action": "recover_publication", "event_id": event_id, "decision": "commit",
                "approver": "operator", "reason": "Resume inspected transaction"}
        status, recovered = self.request("POST", "/api/command", args)
        self.assertEqual(status, 200, recovered)
        self.assertEqual(recovered["result"]["status"], "applied")
        status, replay = self.request("POST", "/api/command", args)
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay, recovered)
        status, conflict = self.request("POST", "/api/command", dict(args, decision="rollback"))
        self.assertEqual(status, 400, conflict)

    def test_cli_execution_factory_receives_trusted_bridge(self):
        import md_mcp
        class CLIStub:
            server_port = 1
            def serve_forever(self):
                raise KeyboardInterrupt
            def server_close(self):
                pass
        with patch.object(sys, "argv", ["webui.server", "--data-root", str(self.data), "--enable-execution"]), \
             patch.object(md_mcp, "MODE", "local"), \
             patch.object(web.evidence_api, "configured_action_adapter", wraps=web.evidence_api.configured_action_adapter) as factory, \
             patch.object(web, "create_server", return_value=CLIStub()), \
             patch.dict("os.environ", {}, clear=False):
            web.main()
        self.assertIs(factory.call_args.kwargs["bridge_exec"], md_mcp._bridge_exec)

    def test_path_escape_and_client_selected_knowledge_paths_are_rejected(self):
        with self.assertRaises(web.V3Error):
            self.server.app.path("../outside")
        with self.assertRaises(web.V3Error):
            self.server.app.paths({"knowledge_dirs": [str(self.workspace)]})


if __name__ == "__main__":
    unittest.main()
