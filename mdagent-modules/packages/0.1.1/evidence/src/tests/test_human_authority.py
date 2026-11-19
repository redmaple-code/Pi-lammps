# -*- coding: utf-8 -*-
"""Out-of-band human authority: park -> owner decision -> attestation, fail-closed."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MCP_DIR = os.path.join(ROOT, "mcp")
if MCP_DIR not in sys.path:
    sys.path.insert(0, MCP_DIR)
if os.path.join(ROOT, "evolution") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "evolution"))

import human_authority  # noqa: E402
from evidence_v3.contracts import canonical_hash  # noqa: E402


def request():
    return {"id": "auth-plan", "scope": "plan", "decision": "confirm",
            "subject_hash": "0" * 64, "reason": "approve the stated plan"}


class HumanAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="human-auth-")
        self.addCleanup(self.temporary.cleanup)
        self.inbox = os.path.join(self.temporary.name, "inbox")
        self.environment = {"MDSIM_HUMAN_INBOX": self.inbox}
        self._old = {key: os.environ.get(key) for key in
                     ("MDSIM_HUMAN_INBOX", "MDSIM_HUMAN_INBOX_SECRET", "MDSIM_HUMAN_INBOX_MAX_AGE")}
        os.environ["MDSIM_HUMAN_INBOX"] = self.inbox
        os.environ.pop("MDSIM_HUMAN_INBOX_SECRET", None)
        os.environ.pop("MDSIM_HUMAN_INBOX_MAX_AGE", None)
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def decide(self, request_hash, actor="owner", decision="approve", issued=None, **extra):
        value = {"schema": 1, "kind": "mdagent-authorization-decision",
                 "request_hash": request_hash, "task_id": "task-a",
                 "decision": decision, "actor_id": actor,
                 "reason": "reviewed", "issued_at": issued or int(time.time())}
        value.update(extra)
        signature = human_authority.sign(value)
        if signature:
            value["signature"] = signature
        human_authority._atomic_json(
            Path(self.inbox) / "decisions" / (request_hash + ".json"), value)
        return value

    def test_missing_decision_parks_and_denies(self):
        req = request()
        self.assertIsNone(human_authority.verify("task-a", req, {}))
        rows = human_authority.pending("task-a")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["request_hash"], canonical_hash(req))
        # 重复调用不重复入队
        self.assertIsNone(human_authority.verify("task-a", req, {}))
        self.assertEqual(len(human_authority.pending("task-a")), 1)

    def test_approve_returns_exact_attestation(self):
        req = request()
        request_hash = canonical_hash(req)
        self.decide(request_hash)
        verified = human_authority.verify("task-a", req, {})
        self.assertIsNotNone(verified)
        self.assertEqual(set(verified), {"actor_id", "source_refs", "authorization_source"})
        source = verified["authorization_source"]
        self.assertEqual(set(source), {"kind", "source_id", "actor_id",
                                       "request_hash", "verified"})
        self.assertEqual(source["kind"], "trusted_human_event")
        self.assertEqual(source["request_hash"], request_hash)
        self.assertIs(source["verified"], True)
        self.assertEqual(verified["source_refs"], ["human-inbox:" + request_hash])

    def test_reject_and_expired_and_wrong_task_are_denied(self):
        req = request()
        request_hash = canonical_hash(req)
        self.decide(request_hash, decision="deny")
        self.assertIsNone(human_authority.verify("task-a", req, {}))
        os.remove(Path(self.inbox) / "decisions" / (request_hash + ".json"))
        self.decide(request_hash, issued=int(time.time()) - human_authority.MAX_AGE_SEC - 5)
        self.assertIsNone(human_authority.verify("task-a", req, {}))
        os.remove(Path(self.inbox) / "decisions" / (request_hash + ".json"))
        self.decide(request_hash)
        self.assertIsNone(human_authority.verify("task-b", req, {}))

    def test_extra_or_missing_fields_are_denied(self):
        req = request()
        request_hash = canonical_hash(req)
        self.decide(request_hash, extra="unexpected")
        self.assertIsNone(human_authority.verify("task-a", req, {}))

    def test_hash_binding_rejects_changed_request(self):
        req = request()
        self.decide(canonical_hash(req))
        changed = dict(req, subject_hash="1" * 64)
        self.assertIsNone(human_authority.verify("task-a", changed, {}))

    def test_hmac_required_when_secret_is_set(self):
        os.environ["MDSIM_HUMAN_INBOX_SECRET"] = "owner-only-secret"
        req = request()
        request_hash = canonical_hash(req)
        unsigned = {"schema": 1, "kind": "mdagent-authorization-decision",
                    "request_hash": request_hash, "task_id": "task-a",
                    "decision": "approve", "actor_id": "owner",
                    "reason": "reviewed", "issued_at": int(time.time())}
        human_authority._atomic_json(
            Path(self.inbox) / "decisions" / (request_hash + ".json"), unsigned)
        self.assertIsNone(human_authority.verify("task-a", req, {}))
        signed = dict(unsigned, signature=human_authority.sign(unsigned))
        human_authority._atomic_json(
            Path(self.inbox) / "decisions" / (request_hash + ".json"), signed)
        self.assertIsNotNone(human_authority.verify("task-a", req, {}))

    def test_cli_pending_and_approve(self):
        req = request()
        self.assertIsNone(human_authority.verify("task-a", req, {}))
        cli = os.path.join(ROOT, "tools", "human_approve.py")
        env = dict(os.environ)
        pending = subprocess.run([sys.executable, cli, "pending"], capture_output=True,
                                 text=True, env=env)
        self.assertEqual(pending.returncode, 0, pending.stderr)
        self.assertIn(canonical_hash(req), pending.stdout)
        approve = subprocess.run([sys.executable, cli, "approve", canonical_hash(req),
                                  "--actor", "owner", "--reason", "reviewed", "--yes"],
                                 capture_output=True, text=True, env=env)
        self.assertEqual(approve.returncode, 0, approve.stderr)
        self.assertIsNotNone(human_authority.verify("task-a", req, {}))


class McpAuthorizationWiringTests(unittest.TestCase):
    """The regression that used to be permanent: authorization through md_mcp."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mcp-auth-")
        self.addCleanup(self.temporary.cleanup)
        self._old = {key: os.environ.get(key) for key in
                     ("MDSIM_DATA_ROOT", "MDSIM_HUMAN_INBOX", "MDSIM_KIT_BASE")}
        os.environ["MDSIM_DATA_ROOT"] = os.path.join(self.temporary.name, "data")
        os.environ["MDSIM_HUMAN_INBOX"] = os.path.join(self.temporary.name, "inbox")
        os.environ["MDSIM_KIT_BASE"] = os.path.join(self.temporary.name, "kit")
        self.addCleanup(self._restore)
        import md_mcp
        self.md_mcp = md_mcp
        self.intent = {"action": "production",
                       "goals": [{"id": "g1", "text": "authorize a stated plan"}],
                       "conditions": {}, "unknowns": [], "source_refs": ["user:request"],
                       "queries": []}

    def _restore(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_authorization_becomes_possible_after_owner_decision(self):
        state = self.md_mcp.sim_evidence({"action": "init", "task_id": "task-auth",
                                          "intent": self.intent, "request_id": "rq-init"})
        self.assertNotIn("error", state, state)
        authorization = {"id": "auth-plan", "scope": "plan", "decision": "confirm",
                         "subject_hash": state["subject_hash"],
                         "reason": "owner reviewed the goal and plan"}
        denied = self.md_mcp.sim_evidence({"action": "authorization", "task_id": "task-auth",
                                           "expected_revision": state["revision"],
                                           "authorization": authorization})
        self.assertEqual(denied.get("error"), "HUMAN_AUTHORITY_REQUIRED")
        self.assertIn("pending_request", denied)
        parked = json.load(open(denied["pending_request"], encoding="utf-8"))
        decision = {"schema": 1, "kind": "mdagent-authorization-decision",
                    "request_hash": parked["request_hash"], "task_id": "task-auth",
                    "decision": "approve", "actor_id": "owner",
                    "reason": "reviewed", "issued_at": int(time.time())}
        human_authority._atomic_json(
            Path(os.environ["MDSIM_HUMAN_INBOX"]) / "decisions" /
            (parked["request_hash"] + ".json"), decision)
        accepted = self.md_mcp.sim_evidence({"action": "authorization", "task_id": "task-auth",
                                             "expected_revision": state["revision"],
                                             "authorization": authorization})
        self.assertNotIn("error", accepted, accepted)
        self.assertEqual(accepted["authorizations"][0]["actor_kind"], "human")
        self.assertEqual(accepted["authorizations"][0]["actor_id"], "owner")


if __name__ == "__main__":
    unittest.main(verbosity=2)
