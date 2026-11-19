"""Stdlib localhost UI over evidence_api/TaskService; no task-state file writes.

Embedding/test interface: create_server(data_root, workspace_root=...,
    transport=lambda request: response, action_adapter=..., start_handler=...,
    port=0). start_handler receives the exact sim_exec argument dictionary.
No fake transport, launcher, fixture or permissive execution adapter is installed
by default. CLI execution is opt-in and uses the existing local MCP bridge.
"""
import argparse
import copy
import json
import re
import secrets
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
# Support both `python -m webui.server` from the isolated repository and
# direct imports from test runners whose cwd is the workspace parent.
for directory in (ROOT, ROOT / "mcp", ROOT / "evolution"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
import evidence_api
from evidence_v3 import planning
from evidence_v3.contracts import V3Error, canonical_hash, validate_id
from evidence_v3.execution import normalize_spec

MAX_BODY = 512 * 1024
STATIC = Path(__file__).parent / "static"
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/static/style.css": ("style.css", "text/css; charset=utf-8")}
SECRET_KEYS = re.compile(r"(^|_)(api_?key|token|password|secret|credential|cookie|headers?)(_|$)", re.I)
SENSITIVE_NAMES = {"md_config.json", ".env", "credentials.json", "id_rsa", "id_ed25519"}
PUBLIC_ACTIONS = {"status", "revise", "review", "evaluate", "advance", "authorize", "complete",
                  "finish", "refresh_knowledge", "inspect_publication", "recover_publication", "measure"}


def _id(prefix):
    return prefix + "-" + uuid.uuid4().hex


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise V3Error("FIELD_REQUIRED", name)
    return value


def _strict(payload):
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise V3Error("DUPLICATE_JSON_KEY", key)
            result[key] = value
        return result
    def constant(_):
        raise V3Error("INVALID_JSON")
    value = json.loads(payload, object_pairs_hook=pairs, parse_constant=constant)
    canonical_hash(value)
    if not isinstance(value, dict):
        raise V3Error("OBJECT_REQUIRED")
    return value


def _public(value):
    """Never project provider configuration, request headers or process secrets."""
    if isinstance(value, dict):
        return {k: ("[redacted]" if SECRET_KEYS.search(k) or (k == "environment" and v) else _public(v))
                for k, v in value.items() if k not in {"requests", "launch", "completion_verification"}}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return value


class Application:
    def __init__(self, data_root, workspace_root, transport=None, action_adapter=None, start_handler=None,
                 human_verifier=None):
        self.data_root = str(Path(data_root).resolve())
        self.workspace = Path(workspace_root).resolve()
        self.transport = transport
        self.action_adapter = action_adapter
        self.start_handler = start_handler
        self.human_verifier = human_verifier
        self.token = secrets.token_urlsafe(32)
        self.drafts = {}
        self.lock = threading.Lock()

    def service(self):
        return evidence_api.task_service(self.data_root, self.action_adapter,
                                        human_verifier=self.human_verifier)

    def call(self, args, transport=None, *, server_sources=False):
        self.paths(args, server_sources=server_sources)
        result = evidence_api.dispatch(args, self.data_root, self.action_adapter,
                                       self.transport if transport is None else transport,
                                       human_verifier=self.human_verifier)
        if isinstance(result, dict) and result.get("error"):
            raise V3Error(result["error"], result.get("detail", ""))
        return result

    def path(self, raw, *, directory=False):
        raw = _text(raw, "path")
        if "\0" in raw or ".." in Path(raw).parts:
            raise V3Error("UNSAFE_PATH")
        path = Path(raw)
        if not path.is_absolute():
            path = self.workspace / path
        resolved = path.resolve()
        if not resolved.is_relative_to(self.workspace):
            raise V3Error("PATH_OUTSIDE_WORKSPACE")
        if any(part.lower() in SENSITIVE_NAMES or part.lower().startswith(".env") or
               part.lower().endswith((".pem", ".key")) for part in path.parts):
            raise V3Error("SENSITIVE_PATH")
        cursor = path
        while cursor != cursor.parent:
            if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
                raise V3Error("UNSAFE_PATH")
            if cursor == self.workspace:
                break
            cursor = cursor.parent
        if directory and not resolved.is_dir():
            raise V3Error("DIRECTORY_REQUIRED")
        return str(resolved)

    def paths(self, value, *, server_sources=False):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"knowledge_dirs", "project_roots", "source_path"}:
                    raise V3Error("PATH_SELECTION_DISABLED")
                if key in {"cwd", "source_locator", "input_locator"} and child:
                    if key == "source_locator" and server_sources:
                        continue
                    # Locators with an explicit URI scheme are references, never read here.
                    if key != "cwd" and isinstance(child, str) and "://" in child:
                        continue
                    self.path(child, directory=key == "cwd")
                if key == "inputs" and isinstance(child, list):
                    for entry in child:
                        name = entry.get("path", "") if isinstance(entry, dict) else ""
                        if not isinstance(name, str) or "\\" in name or ":" in name or name.startswith("/") or any(p in {"", ".", ".."} for p in name.split("/")):
                            raise V3Error("UNSAFE_INPUT_PATH")
                        self.path(name)
                if key == "environment" and child:
                    raise V3Error("ENVIRONMENT_SELECTION_DISABLED")
                self.paths(child, server_sources=server_sources)
        elif isinstance(value, list):
            for child in value:
                self.paths(child, server_sources=server_sources)

    def current(self, args):
        task_id = validate_id(args.get("task_id"))
        state = self.call({"action": "status", "task_id": task_id})
        if args.get("expected_revision") != state["revision"]:
            raise V3Error("REVISION_CONFLICT", str(state["revision"]))
        if args.get("expected_state_seq") != state["state_seq"]:
            raise V3Error("STATE_SEQ_CONFLICT", str(state["state_seq"]))
        return state

    def spec(self, value):
        self.paths(value)
        result = copy.deepcopy(value)
        result["cwd"] = self.path(result.get("cwd"), directory=True)
        return normalize_spec(result)

    def snapshot(self, task_id, result=None):
        return {"state": self.call({"action": "status", "task_id": task_id}), "result": result}

    def _propose(self, intent, bundle, requested_items=None):
        if requested_items is None:
            return self.call({"action": "propose", "intent": intent, "bundle": bundle}, server_sources=True)
        # This is the orchestration boundary, not the core. The original API
        # computes all citation/discovery hashes and validates the returned plan.
        import evidence_review
        def transport(request):
            request["user_requested_items"] = copy.deepcopy(requested_items)
            request["instructions"] += (" Preserve every user_requested_items record exactly in proposal.items; "
                "perform a fresh discovery/counterexample review of those exact records. "
                "Additional items may be proposed; do not silently drop or rewrite user requests.")
            return evidence_review._call(request, self.transport)
        result = self.call({"action": "propose", "intent": intent, "bundle": bundle},
                           transport=transport, server_sources=True)
        returned = {row["id"]: row for row in result["items"]}
        for item in requested_items:
            if returned.get(item["id"]) != item:
                raise V3Error("REQUESTED_ITEM_NOT_PRESERVED", item["id"])
        return result

    def handle(self, route, args):
        if route == "/api/discover":
            request = _text(args.get("request"), "request")
            discovery = self.call({"action": "discover", "request": request})
            # A later failed round may have no proposal. Keep its diagnostics,
            # but never pair its bundle with an earlier proposal or initial intent.
            rounds = discovery.get("rounds", [])
            selected = next((row for row in reversed(rounds)
                             if isinstance(row.get("proposal"), dict)), None)
            intent = selected.get("intent") if selected else discovery.get("intent")
            bundle = selected.get("bundle") if selected else None
            proposal = selected.get("proposal") if selected else None
            if selected:
                planning.prepare_plan(intent, bundle, proposal)
            if discovery.get("complete") is True and (not selected or selected is not rounds[-1]):
                raise V3Error("DISCOVERY_BINDING_MISMATCH")
            draft_id = _id("draft")
            draft = {"id": draft_id, "request": request, "intent": intent, "bundle": bundle,
                     "proposal": proposal, "discovery": discovery}
            with self.lock:
                if len(self.drafts) >= 64:
                    raise V3Error("DRAFT_LIMIT", "restart UI or use an existing task")
                self.drafts[draft_id] = draft
            return {"draft": draft, "consultation": isinstance(intent, dict) and intent.get("action") == "consult",
                    "complete": discovery.get("complete") is True,
                    "discovery_status": discovery.get("status"),
                    "limitations": discovery.get("limitations", [])}
        if route == "/api/commit":
            with self.lock:
                draft = copy.deepcopy(self.drafts.get(args.get("draft_id")))
            if draft is None:
                raise V3Error("DRAFT_NOT_FOUND")
            if draft.get("discovery", {}).get("complete") is not True:
                raise V3Error("DISCOVERY_INCOMPLETE", "bounded discovery left unresolved gaps")
            if draft["intent"]["action"] == "consult":
                raise V3Error("CONSULTATION_READ_ONLY", "consultation does not create a production task")
            planning.prepare_plan(draft["intent"], draft["bundle"], draft["proposal"])
            task_id = validate_id(args.get("task_id"))
            state = self.call({"action": "init", "task_id": task_id, "intent": draft["intent"],
                               "request_id": validate_id(args.get("request_id"))})
            if state.get("plan") is None:
                state = self.call({"action": "plan", "task_id": task_id, "bundle": draft["bundle"],
                                   "proposal": draft["proposal"], "expected_revision": state["revision"]},
                                  server_sources=True)
            return {"state": state}
        if route == "/api/command":
            action = args.get("action")
            capabilities = set(evidence_api.TOOL["inputSchema"]["properties"]["action"]["enum"])
            if action not in PUBLIC_ACTIONS or action not in capabilities:
                raise V3Error("ACTION_NOT_EXPOSED")
            if action == "status":
                return {"state": self.call({"action": "status", "task_id": args.get("task_id")})}
            # Publication repair is global and must remain reachable even when
            # no task exists or the knowledge generation is pending.
            if action in {"inspect_publication", "recover_publication"}:
                supplied = {"action": action}
                if args.get("event_id"):
                    supplied["event_id"] = validate_id(args["event_id"])
                if action == "recover_publication":
                    supplied.update(event_id=validate_id(args.get("event_id")),
                                    decision=args.get("decision"),
                                    approver=_text(args.get("approver"), "approver"),
                                    reason=_text(args.get("reason"), "reason"))
                return {"result": self.call(supplied)}
            state = self.current(args)
            supplied = copy.deepcopy(args)
            if action == "revise":
                patch = supplied.get("patch", {})
                if set(patch) - {"intent", "dependencies", "decisions"}:
                    raise V3Error("INVALID_PATCH")
                if "intent" in patch:
                    _text(supplied.pop("change_reason", None), "change_reason")
                if "dependencies" in patch:
                    patch["dependencies"]["cwd"] = self.path(patch["dependencies"].get("cwd"), directory=True)
            if action in {"review", "authorize"} and supplied.get("spec") is not None:
                supplied["spec"] = self.spec(supplied["spec"])
            result = self.call(supplied)
            return self.snapshot(state["task_id"], result)
        if route == "/api/items":
            state = self.current(args)
            reason = _text(args.get("reason"), "reason")
            items = args.get("items")
            if not isinstance(items, list) or not items:
                raise V3Error("EMPTY_PLAN")
            old = state["plan"]
            bundle = self.call({"action": "retrieve", "intent": state["intent"]})
            proposal = self._propose(state["intent"], bundle, items)
            prepared = planning.prepare_plan(state["intent"], bundle, proposal)
            review = {"status": "success", "reason": reason,
                      "source_refs": proposal["discovery_review"]["source_refs"],
                      "old_plan_hash": old["plan_hash"], "new_content_hash": canonical_hash(
                          {k: v for k, v in prepared.items() if k != "plan_hash"})}
            changed = [g["id"] for g in old["intent"]["goals"] if g not in state["intent"]["goals"]]
            if changed:
                review["scope_change"] = {"reason": reason, "source_refs": review["source_refs"], "excluded_goal_ids": changed}
            prepared["reviewed_change"] = review
            prepared["plan_hash"] = planning.hash_plan(prepared)
            # The public TaskService retains reviewed_change and performs the
            # same locked CAS used by the API plan action.
            result = self.service().set_plan(state["task_id"], prepared, state["revision"])
            return {"state": result}
        if route == "/api/evidence":
            state = self.current(args)
            record = copy.deepcopy(args.get("record"))
            if not isinstance(record, dict):
                raise V3Error("INVALID_EVIDENCE")
            if record.get("supersedes") and not any(row["id"] == record["supersedes"] for row in state["evidence"]):
                raise V3Error("EVIDENCE_NOT_FOUND")
            # Corrections append a new immutable record; no prior observation is
            # overwritten, and the server policy re-evaluates all material.
            result = self.call({"action": "evidence", "task_id": state["task_id"],
                                "record": record, "expected_revision": state["revision"]})
            return {"state": result}
        if route == "/api/human":
            state = self.current(args)
            quote = _text(args.get("quote"), "human quote")
            operation = args.get("operation")
            event_id = _id("human-event")
            refs = ["webui-human:" + event_id]
            if operation == "revoke":
                result = self.call({"action": "revoke_authorization", "task_id": state["task_id"],
                                    "authorization_id": args.get("authorization_id"), "reason": quote,
                                    "source_refs": refs, "expected_revision": state["revision"]})
                return {"state": result}
            scope = args.get("scope", "plan")
            authorization = {"id": args.get("authorization_id") or _id("authorization"),
                "scope": scope, "decision": "confirm" if scope == "plan" else "allow",
                "reason": quote, "subject_hash": state["subject_hash"]}
            if operation == "confirm":
                if scope != "plan":
                    raise V3Error("INVALID_SCOPE")
            elif operation == "execution":
                spec = self.spec(args.get("spec"))
                if scope != spec["kind"]:
                    raise V3Error("INVALID_SCOPE")
                authorization.update(decision="allow", spec_hash=canonical_hash(spec))
            elif operation == "launch":
                permit_id = validate_id(args.get("permit_id"))
                permit = next((p for p in state["permits"] if p["id"] == permit_id), None)
                if not permit or permit.get("spec_hash") != canonical_hash(self.spec(args.get("spec"))):
                    raise V3Error("PERMIT_SPEC_MISMATCH")
                authorization.update(scope="launch:" + permit_id, decision="launch", spec_hash=permit["spec_hash"])
            elif operation in {"accept", "assumption", "dispute", "correct", "not_applicable"}:
                finding = next((f for f in state["findings"] if f["id"] == args.get("finding_id")), None)
                if operation != "assumption" and not finding:
                    raise V3Error("FINDING_NOT_FOUND")
                item_id = args.get("item_id") if finding is None else finding.get("item_id")
                if not item_id or item_id not in {i["id"] for i in state["plan"]["items"]}:
                    raise V3Error("ITEM_REQUIRED")
                if operation == "assumption":
                    authorization.update(scope=item_id, decision="accept_assumption")
                    result = self.call({"action": "authorization", "task_id": state["task_id"],
                                        "authorization": authorization, "expected_revision": state["revision"]})
                    return {"state": result}
                resolution = {"id": _id("resolution"), "finding_id": finding["id"],
                    "finding_hash": canonical_hash({k: v for k, v in finding.items() if k not in {"status", "resolution"}}),
                    "action": operation, "reason": quote, "source_refs": refs, "task_id": state["task_id"],
                    "revision": state["revision"], "subject_hash": state["subject_hash"], "plan_hash": state["plan"]["plan_hash"]}
                if operation == "accept":
                    authorization.update(scope=item_id, finding_id=finding["id"], decision="accept_uncertainty")
                    authorization.pop("quote", None)
                    self.call({"action": "authorization", "task_id": state["task_id"],
                               "authorization": authorization, "expected_revision": state["revision"]})
                    current = self.call({"action": "status", "task_id": state["task_id"]})
                    resolution["revision"] = current["revision"]
                    resolution["subject_hash"] = current["subject_hash"]
                    resolution["authorization"] = next(a for a in current["authorizations"]
                                                         if a["id"] == authorization["id"])
                if operation in {"correct", "not_applicable"}:
                    for key in ("replacement_proof", "recovery", "supersedes_proof_hash"):
                        if key in args:
                            resolution[key] = args[key]
                    resolution = self.call({"action": "resolve_review", "task_id": state["task_id"],
                                             "finding_id": finding["id"], "resolution": resolution})
                result = self.call({"action": "resolve", "task_id": state["task_id"], "finding_id": finding["id"],
                                    "resolution": resolution, "expected_revision": state["revision"]})
                return {"state": result}
            else:
                raise V3Error("INVALID_HUMAN_OPERATION")
            result = self.call({"action": "authorization", "task_id": state["task_id"],
                                "authorization": authorization, "expected_revision": state["revision"]})
            response = {"state": result}
            if operation == "launch":
                # launch_event_id is the immutable authorization record ID.
                response["launch_event_id"] = authorization["id"]
            return response
        if route == "/api/reconcile":
            state = self.current(args)
            if self.start_handler is None:
                raise V3Error("EXECUTION_DISABLED", "start with --enable-execution and a trusted local adapter")
            run_id = validate_id(args.get("run_id"))
            run = next((row for row in state.get("runs", []) if row.get("id") == run_id), None)
            if not run:
                raise V3Error("RUN_NOT_FOUND", run_id)
            if run.get("status") not in {"STARTING", "START_UNKNOWN", "RUNNING"}:
                raise V3Error("RUN_NOT_UNCERTAIN")
            receipt = args.get("receipt", {})
            if not isinstance(receipt, dict):
                raise V3Error("OBJECT_REQUIRED", "receipt")
            result = self.start_handler({"action": "reconcile", "task_id": state["task_id"],
                                         "run_id": run_id, "receipt": receipt})
            if not isinstance(result, dict):
                raise V3Error("INVALID_RECONCILIATION")
            if result.get("error"):
                raise V3Error(result["error"], result.get("detail", ""))
            return self.snapshot(state["task_id"], result)
        if route == "/api/start":
            state = self.current(args)
            if self.start_handler is None:
                raise V3Error("EXECUTION_DISABLED", "start with --enable-execution and a trusted local adapter")
            quote = _text(args.get("quote"), "human quote")
            event_id = validate_id(args.get("launch_event_id"))
            if args.get("confirm_start") is not True:
                raise V3Error("EXPLICIT_START_REQUIRED")
            spec = self.spec(args.get("spec"))
            permit = next((p for p in state["permits"] if p["id"] == args.get("permit_id")), None)
            if not permit or permit.get("spec_hash") != canonical_hash(spec):
                raise V3Error("PERMIT_SPEC_MISMATCH")
            launch_auth = next((a for a in state["authorizations"] if a.get("id") == event_id), None)
            source = launch_auth.get("authorization_source", {}) if isinstance(launch_auth, dict) else {}
            if not launch_auth or launch_auth.get("scope") != "launch:" + permit["id"] or \
                    launch_auth.get("decision") != "launch" or launch_auth.get("actor_kind") != "human" or \
                    not isinstance(source, dict) or source.get("kind") != "trusted_human_event" or \
                    source.get("actor_id") != launch_auth.get("actor_id") or source.get("verified") is not True or \
                    launch_auth.get("reason") != quote or \
                    launch_auth.get("subject_hash") != state["subject_hash"] or \
                    launch_auth.get("spec_hash") != permit["spec_hash"] or launch_auth.get("revoked") is not False:
                raise V3Error("LAUNCH_CONFIRMATION_REQUIRED")
            result = self.start_handler({"task_id": state["task_id"], "permit_id": permit["id"], "spec": spec,
                                         "request_id": validate_id(args.get("request_id"))})
            if not isinstance(result, dict):
                raise V3Error("INVALID_START_RESPONSE")
            if result.get("error"):
                raise V3Error(result["error"], result.get("detail", ""))
            return self.snapshot(state["task_id"], result)
        raise V3Error("NOT_FOUND")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = "EvidenceLocal/3"

    def log_message(self, *_):
        pass

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(_public(body), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _boundary(self, mutation=False):
        host = self.headers.get("Host", "")
        expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if host not in expected or self.client_address[0] not in {"127.0.0.1", "::1"}:
            raise V3Error("LOCALHOST_REQUIRED")
        if len(self.path) > 2048 or urlsplit(self.path).query or "%" in self.path or "\\" in self.path or ".." in self.path:
            raise V3Error("UNSAFE_URL")
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in expected}:
            raise V3Error("ORIGIN_REJECTED")
        if mutation and not secrets.compare_digest(self.headers.get("X-Evidence-CSRF", ""), self.server.app.token):
            raise V3Error("CSRF_REQUIRED")

    def do_GET(self):
        try:
            self._boundary()
            if self.path == "/api/session":
                capabilities = evidence_api.TOOL["inputSchema"]["properties"]["action"]["enum"]
                return self._send(200, {"csrf": self.server.app.token, "execution_enabled": self.server.app.start_handler is not None,
                    "capabilities": capabilities, "fixture": False, "limits": {"request_bytes": MAX_BODY},
                    "execution_note": "Requires a server-verified adapter; unknown actions return UNSUPPORTED."})
            if self.path not in FILES:
                return self._send(404, {"error": "NOT_FOUND"})
            name, kind = FILES[self.path]
            return self._send(200, (STATIC / name).read_bytes(), kind)
        except V3Error as exc:
            self._send(403, exc.as_dict())

    def do_POST(self):
        self.connection.settimeout(15)
        try:
            self._boundary(mutation=True)
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise V3Error("CONTENT_LENGTH_REQUIRED")
            length = self.headers.get("Content-Length", "")
            if not length.isdigit() or int(length) > MAX_BODY:
                return self._send(413, {"error": "REQUEST_TOO_LARGE", "fail_closed": True})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._send(415, {"error": "JSON_REQUIRED"})
            body = self.rfile.read(int(length))
            if len(body) != int(length):
                raise V3Error("INCOMPLETE_BODY")
            args = _strict(body.decode("utf-8"))
            result = self.server.app.handle(self.path, args)
            self._send(200, result)
        except V3Error as exc:
            code = 409 if "CONFLICT" in exc.code or "stale" in exc.code.lower() else 400
            if exc.code == "NOT_FOUND":
                code = 404
            if exc.code in {"CSRF_REQUIRED", "ORIGIN_REJECTED", "LOCALHOST_REQUIRED"}:
                code = 403
            self._send(code, exc.as_dict())
        except (ValueError, UnicodeError, TypeError):
            self._send(400, {"error": "INVALID_JSON_OR_FIELD", "fail_closed": True})
        except Exception as exc:
            self._send(500, {"error": "WEBUI_INTERNAL_ERROR", "detail": type(exc).__name__, "fail_closed": True})


def create_server(data_root, *, workspace_root=ROOT, transport=None, action_adapter=None, start_handler=None,
                  human_verifier=None, port=0):
    """Return an unstarted localhost server; caller owns serve/shutdown/close."""
    server = Server(("127.0.0.1", port), Handler)
    server.app = Application(data_root, workspace_root, transport, action_adapter, start_handler,
                             human_verifier=human_verifier)
    return server


def main():
    parser = argparse.ArgumentParser(description="Local evidence v3 review workspace")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--workspace-root", default=str(ROOT))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--enable-execution", action="store_true", help="Use the configured local MCP bridge; human launch confirmation still required")
    args = parser.parse_args()
    adapter = start = None
    if args.enable_execution:
        import os
        os.environ["MDSIM_DATA_ROOT"] = str(Path(args.data_root).resolve())
        import md_mcp
        if md_mcp.MODE != "local":
            parser.error("--enable-execution requires the MCP local execution mode")
        adapter = evidence_api.configured_action_adapter(md_mcp.CFG, bridge_exec=md_mcp._bridge_exec)
        start = md_mcp.sim_exec
    server = create_server(args.data_root, workspace_root=args.workspace_root, port=args.port,
                           action_adapter=adapter, start_handler=start)
    print(f"Evidence v3 UI http://127.0.0.1:{server.server_port} (execution {'enabled' if start else 'disabled'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
