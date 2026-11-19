"""Source health regressions using only temporary task/UKB stores."""
import copy
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_kb_adapter as fixtures
from evolution.evidence_v3.contracts import V3Error, atomic_json, canonical_hash, file_hash, read_json
from evolution.evidence_v3.measurements import materialize
from evolution.evidence_v3.service import TaskService


class EvidenceSourceTests(unittest.TestCase):
    # Compose the fixture: importing/inheriting AdapterCase would collect its tests too.
    def setUp(self):
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.fixture = fixtures.AdapterCase("test_unknown_mode_is_refused")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.addCleanup(self._writable)
        self.adapter = self.fixture.adapter
        self.root = Path(self.fixture.tmp)
        self.service = TaskService(self.root)
        self.intent = {"action": "consult", "goals": [{"id": "g1", "text": "measured evidence"}],
                       "conditions": {}, "unknowns": [], "source_refs": ["request:fixture"]}

    def _writable(self):
        for path in Path(self.fixture.tmp).rglob("*"):
            if path.is_file() and not path.is_symlink():
                path.chmod(0o666)

    def _manifest(self, path):
        return {"path": str(path), "sha256": file_hash(path)}

    def _measurement(self, legacy=False):
        state = self.service.create("source-task", self.intent)
        work = self.root / "measurement"
        work.mkdir()
        input_file = work / "input.json"
        self.raw = work / "raw.json"
        atomic_json(input_file, {"input": "fixture"})
        atomic_json(self.raw, {"values": [2, 4, 6]})
        run = {"id": "run-a", "task_id": state["task_id"], "status": "FINISHED",
               "receipt_id": "receipt-a", "output_dir": str(work),
               "knowledge_snapshot_hash": state["knowledge_snapshot"]["hash"],
               "inputs": [self._manifest(input_file)], "outputs": [self._manifest(self.raw)]}
        run["input_hash"] = canonical_hash(run["inputs"])
        run["output_hash"] = canonical_hash(run["outputs"])
        receipt = {"id": "receipt-a", "kind": "RUN_RECEIPT", "task_id": state["task_id"],
                   "run_id": run["id"], "result": {"outputs": copy.deepcopy(run["outputs"])}}
        recipe = {"source": self._manifest(self.raw), "format": "json", "pointer": "/values",
                  "unit": "count", "source_unit": "count", "aggregate": "mean",
                  "window": {"start": 0, "stop": 3}}
        product = materialize(run, recipe, str(work), evidence_id="measurement-a",
                              claim_id="claim-a", metric="value", tolerance=0)
        record = product["evidence"]
        self.derived = Path(record["verification"]["source_locator"])
        if legacy:
            document = {key: record[key] for key in
                        ("task_id", "run_id", "claim_id", "metric", "unit", "window",
                         "value", "tolerance", "input_hash")}
            self.derived.chmod(0o666)
            atomic_json(self.derived, document)
            record["output_hash"] = file_hash(self.derived)
            record["verification"].pop("raw_sources")
            product["artifact_manifest"] = self._manifest(self.derived)
        run["outputs"].append(product["artifact_manifest"])
        run["output_hash"] = canonical_hash(run["outputs"])
        record["claim"] = "measured evidence from fixture"
        self.service.store.artifact(state["task_id"], "receipts", receipt)
        self.service.store.artifact(state["task_id"], "evidence", record)
        state["receipts"].append(receipt)
        state["runs"].append(run)
        state["evidence"].append(record)
        self.service.store.commit(state["task_id"], state)
        return state, record

    def _approve(self, task_id):
        kb = self.fixture.kb
        bundle = kb.prereview_bundle(task_id, 0)
        tokens = [kb.prereview_bundle(task_id, p)["page_token"] for p in range(bundle["pages"])]
        opinion = {"type_opinion": {"ok": True}, "tag_opinion": {"ok": True},
                   "recommendation": "approve", "note": "synthetic source fixture",
                   "checklist": [{"item": item, "ok": True} for item in bundle["instructions"]["checklist"]]}
        kb.prereview_submit(task_id, "llm:source-fixture", tokens, opinion)
        view = kb.review_view(task_id)
        kb.decide(task_id, "boss", self.fixture.admin_token, "approve", "fixture approval",
                  view["bound_hash"], "cli")

    def _project(self, record):
        result = self.adapter.submit_evidence_record(record, record["task_id"], self.root)
        self.assertEqual(result["status"], "pending_review")
        self.assertEqual(self.adapter.load_knowledge(self.root)["entries"], [])
        self._approve(result["task_id"])
        return "evidence:" + record["task_id"] + ":" + record["id"]

    def test_measurement_intake_rejects_stale_run_knowledge(self):
        state, record = self._measurement()
        state["runs"][0]["knowledge_snapshot_hash"] = "0" * 64
        with self.assertRaises(V3Error) as stale:
            self.adapter._verify_evidence_source(state, record, self.service.store)
        self.assertEqual(stale.exception.code, "STALE_RUN_KNOWLEDGE")
        state["runs"][0]["knowledge_snapshot_hash"] = state["knowledge_snapshot"]["hash"]
        state["runs"][0]["needs_revalidation"] = True
        with self.assertRaises(V3Error) as stale:
            self.adapter._verify_evidence_source(state, record, self.service.store)
        self.assertEqual(stale.exception.code, "STALE_RUN_KNOWLEDGE")

    def test_genuine_measurement_is_approved_searchable_and_stable(self):
        state, record = self._measurement()
        self.adapter._verify_evidence_source(state, record, self.service.store)
        identity = self._project(record)
        loaded = self.adapter.load_knowledge(self.root)["entries"]
        self.assertEqual([entry["id"] for entry in loaded], [identity])
        health = loaded[0]["ukb_metadata"]["evidence_health"]
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["raw_sources"][0]["actual"], file_hash(self.raw))
        before = self.adapter.knowledge_snapshot(self.root)
        self.assertEqual(before, self.adapter.knowledge_snapshot(self.root))
        self.assertIn(identity, [entry["id"] for entry in self.adapter.search(self.intent, self.root)["candidates"]])

    def _raw_loss(self, delete):
        _, record = self._measurement()
        identity = self._project(record)
        before = self.adapter.knowledge_snapshot(self.root)
        dependent = self.service.create("dependent-task", self.intent)
        derived_hash = file_hash(self.derived)
        if delete:
            self.raw.unlink()
        else:
            atomic_json(self.raw, {"values": [200, 400, 600]})
        self.assertEqual(file_hash(self.derived), derived_hash)
        with self.assertRaises(V3Error) as raised:
            self.adapter.submit_evidence_record(record, record["task_id"], self.root)
        self.assertEqual(raised.exception.code, "EVIDENCE_SOURCE_UNVERIFIED")
        loaded = self.adapter.load_knowledge(self.root)
        self.assertEqual(loaded["entries"], [])
        self.assertTrue(any(row["status"] == "error" for row in loaded["health"]))
        after = self.adapter.knowledge_snapshot(self.root)
        self.assertNotEqual(before["hash"], after["hash"])
        self.assertEqual(after["entries"][identity]["ukb_metadata"]["evidence_health"]["status"], "error")
        self.assertNotIn(identity, [entry["id"] for entry in self.adapter.search(self.intent, self.root)["candidates"]])
        with self.assertRaises(V3Error) as stale:
            self.service._generation_ok(dependent, self.service.store.generation())
        self.assertEqual(stale.exception.code, "REVALIDATION_REQUIRED")

    def test_raw_only_mutation_invalidates_dependent_snapshot(self):
        self._raw_loss(False)

    def test_raw_only_deletion_invalidates_dependent_snapshot(self):
        self._raw_loss(True)

    def test_invalid_raw_lists_fail_intake_and_approved_health(self):
        state, record = self._measurement()
        good = copy.deepcopy(record["verification"]["raw_sources"][0])
        invalid = [None, {}, "raw", [], [None], [dict(good, extra=True)],
                   [dict(good, path="relative.json")], [dict(good, path=str(self.root))],
                   [dict(good, sha256="x" * 64)], [dict(good, sha256="A" * 64)],
                   [dict(good, sha256="0" * 64)], [dict(good, path=str(self.root / "missing"))]]
        for index, sources in enumerate(invalid):
            with self.subTest(sources=sources):
                bad = copy.deepcopy(record)
                bad["id"] = "bad-%d" % index
                bad["verification"]["raw_sources"] = sources
                with self.assertRaises(V3Error) as raised:
                    self.adapter._verify_evidence_source(state, bad, self.service.store)
                self.assertEqual(raised.exception.code, "EVIDENCE_SOURCE_UNVERIFIED")
                payload = self.fixture._legacy("evidence_claim", bad, "measured evidence")
                payload["sources"] = [{"source_type": "legacy_ref", "title": "fixture", "locator": str(self.derived)}]
                entry_id = self.fixture._land(payload)
                row = next(row for row in self.adapter._rows(self.root) if row["entry_id"] == entry_id)
                self.assertEqual(self.adapter._review_metadata(self.fixture.kb, row)["evidence_health"]["status"], "error")
        missing = copy.deepcopy(record)
        missing["verification"].pop("raw_sources")
        self.assertEqual(self.adapter._evidence_source_health(missing)["status"], "error")

    def test_every_declared_raw_source_is_checked_on_older_measurements(self):
        state, record = self._measurement(legacy=True)
        good = self._manifest(self.raw)
        second = self.root / "second.json"
        atomic_json(second, {"second": True})
        record["verification"]["raw_sources"] = [good, self._manifest(second)]
        self.adapter._verify_evidence_source(state, record, self.service.store)
        second.unlink()
        health = self.adapter._evidence_source_health(record)
        self.assertEqual([row["status"] for row in health["raw_sources"]], ["ok", "error"])
        with self.assertRaises(V3Error):
            self.adapter._verify_evidence_source(state, record, self.service.store)

    def test_publication_and_older_measurement_without_raw_sources_remain_supported(self):
        state, record = self._measurement(legacy=True)
        self.adapter._verify_evidence_source(state, record, self.service.store)
        self.assertEqual(self.adapter._evidence_source_health(record)["status"], "ok")
        publication = {"id": "paper-a", "claim_id": "claim-a", "source_kind": "publication",
                       "lifecycle": "active", "conditions": {}, "provenance_refs": ["paper:fixture"],
                       "source_sha256": file_hash(self.raw),
                       "verification": {"status": "verified", "method": "content_review", "source_locator": str(self.raw)}}
        self.adapter._verify_evidence_source(state, publication, self.service.store)
        self.assertEqual(self.adapter._evidence_source_health(publication)["status"], "ok")

    def test_policy_still_recomputes_forged_derived_value(self):
        state, record = self._measurement()
        self.derived.chmod(0o666)
        document = read_json(self.derived)
        document["value"] = 999
        atomic_json(self.derived, document)
        record["value"] = 999
        record["output_hash"] = file_hash(self.derived)
        run = state["runs"][0]
        run["outputs"][-1] = self._manifest(self.derived)
        run["output_hash"] = canonical_hash(run["outputs"])
        self.assertEqual(self.adapter._evidence_source_health(record)["status"], "ok")
        with self.assertRaises(V3Error) as raised:
            self.adapter._verify_evidence_source(state, record, self.service.store)
        self.assertEqual(raised.exception.code, "measurement-invalid")

    def test_windows_reparse_attributes_reject_raw_and_primary_ancestors(self):
        _, record = self._measurement()
        original = Path.lstat
        for target in (self.raw, self.raw.parent, self.derived):
            with self.subTest(target=target):
                def lstat(path, *args, **kwargs):
                    info = original(path, *args, **kwargs)
                    if path == target:
                        return SimpleNamespace(st_mode=info.st_mode,
                                               st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    return info
                with patch.object(Path, "lstat", lstat):
                    self.assertEqual(self.adapter._evidence_source_health(record)["status"], "error")

    def test_symlink_components_rejected_without_platform_privileges(self):
        state, record = self._measurement()
        original = Path.lstat
        for target in (self.raw, self.raw.parent, self.derived):
            with self.subTest(target=target):
                def lstat(path, *args, **kwargs):
                    info = original(path, *args, **kwargs)
                    if path == target:
                        return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
                    return info
                with patch.object(Path, "lstat", lstat):
                    self.assertEqual(self.adapter._evidence_source_health(record)["status"], "error")
                    with self.assertRaises(V3Error) as raised:
                        self.adapter._verify_evidence_source(state, record, self.service.store)
                    self.assertEqual(raised.exception.code, "EVIDENCE_SOURCE_UNVERIFIED")

    def test_schema_one_empty_or_mismatched_raw_manifest_is_rejected(self):
        state, record = self._measurement()
        self.derived.chmod(0o666)
        original = read_json(self.derived)
        for sources in ([], [dict(self._manifest(self.raw), sha256="0" * 64)]):
            with self.subTest(sources=sources):
                document = dict(original, raw_sources=sources)
                atomic_json(self.derived, document)
                bad = copy.deepcopy(record)
                bad["output_hash"] = file_hash(self.derived)
                # Matching empty lists still fail schema-v1's nonempty contract.
                if not sources:
                    bad["verification"]["raw_sources"] = []
                self.assertEqual(self.adapter._evidence_source_health(bad)["status"], "error")
                with self.assertRaises(V3Error) as raised:
                    self.adapter._verify_evidence_source(state, bad, self.service.store)
                self.assertEqual(raised.exception.code, "EVIDENCE_SOURCE_UNVERIFIED")

    def test_symlink_ancestor_rejected_even_with_matching_bytes(self):
        _, record = self._measurement()
        alias = self.root / "alias"
        try:
            alias.symlink_to(self.raw.parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        self.addCleanup(alias.unlink)
        record["verification"]["raw_sources"] = [dict(self._manifest(self.raw), path=str(alias / self.raw.name))]
        health = self.adapter._evidence_source_health(record)
        self.assertEqual(health["raw_sources"][0]["reason"], "unsafe-source-path")
        self.assertEqual(health["status"], "error")


if __name__ == "__main__":
    unittest.main()
