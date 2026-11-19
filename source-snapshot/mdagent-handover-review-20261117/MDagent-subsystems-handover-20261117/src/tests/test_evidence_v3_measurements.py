import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evolution"))
from evidence_v3.contracts import V3Error, canonical_hash
from evidence_v3.measurements import materialize, recompute, verify_artifact
from evidence_v3.policy import evaluate


class MeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "input.txt"
        self.input.write_text("input", encoding="utf-8")
        self.source = self.root / "raw.txt"
        self.source.write_text("BEGIN\nx,u\n2,a\n4,a\n8,a\nEND\n", encoding="utf-8")
        self.run = {"id": "run-a", "task_id": "task-a", "status": "FINISHED", "receipt_id": "receipt-a", "output_dir": str(self.root), "inputs": [self.manifest(self.input)], "outputs": [self.manifest(self.source)]}
        self.refresh()
        self.recipe = {"source": self.manifest(self.source), "format": "table", "delimiter": ",", "column": "x", "unit_column": "u", "source_unit": "a", "unit": "b", "conversion": {"scale": 2, "offset": 1}, "aggregate": "mean", "window": {"start": 1, "stop": 3}, "marker": {"start": "BEGIN", "end": "END", "occurrence": 0}}

    def manifest(self, path):
        return {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def refresh(self):
        for kind in ("input", "output"):
            self.run[kind + "_hash"] = canonical_hash(self.run[kind + "s"])

    def test_table_marker_window_units_and_aggregate(self):
        result = recompute(self.run, self.recipe)
        self.assertEqual(result["value"], 13)
        self.assertEqual(result["sample_count"], 2)

    def test_json_and_text_sources(self):
        for fmt, payload, extras in (("json", '{"rows":[2,4,6],"unit":"a"}', {"pointer": "/rows", "unit_pointer": "/unit"}), ("text", "2\n4\n6\n", {})):
            with self.subTest(fmt=fmt):
                self.source.write_text(payload, encoding="utf-8")
                self.run["outputs"] = [self.manifest(self.source)]
                self.refresh()
                recipe = {"source": self.manifest(self.source), "format": fmt, "unit": "a", "source_unit": "a", "aggregate": "sum", "window": {"start": 0, "stop": None}, **extras}
                self.assertEqual(recompute(self.run, recipe)["value"], 12)

    def test_materialize_is_immutable_unique_and_recomputable(self):
        result = materialize(self.run, self.recipe, str(self.root), evidence_id="record-a", claim_id="claim-a", metric="metric-a", tolerance=0.01)
        manifest = result["artifact_manifest"]
        self.assertEqual(set(manifest), {"path", "sha256"})
        self.assertEqual(result["evidence"]["verification"]["raw_sources"], [self.manifest(self.source)])
        self.run["outputs"].append(manifest)
        self.refresh()
        self.assertEqual(verify_artifact(self.run, result["artifact"])["value"], 13)
        result["artifact"]["value"] = 999
        with self.assertRaises(V3Error):
            verify_artifact(self.run, result["artifact"])
        self.addCleanup(lambda: Path(manifest["path"]).chmod(0o666))

    def test_policy_recomputes_raw_instead_of_trusting_derived_value(self):
        result = materialize(self.run, self.recipe, str(self.root), evidence_id="record-a", claim_id="claim-a", metric="metric-a", tolerance=0.01)
        path = Path(result["artifact_manifest"]["path"])
        self.addCleanup(lambda: path.chmod(0o666))
        self.run["outputs"].append(result["artifact_manifest"])
        self.refresh()
        expected = {k: result["evidence"][k] for k in ("claim_id", "metric", "unit", "window", "tolerance")}
        item = {"id": "item-a", "claim_id": "claim-a", "measurement": expected}
        context = {"task_id": "task-a", "run_id": "run-a", "revision": 1, "subject_hash": "a" * 64, "plan_hash": "b" * 64, "runs": [self.run], "findings": [], "authorizations": []}
        self.assertEqual(evaluate("E2", item, [result["evidence"]], context)["verdict"], "PASS")
        # Forge both derived document and its manifest: raw recomputation still wins.
        path.chmod(0o666)
        result["artifact"]["value"] = 999
        path.write_text(json.dumps(result["artifact"]), encoding="utf-8")
        result["evidence"]["value"] = 999
        result["evidence"]["output_hash"] = self.manifest(path)["sha256"]
        self.run["outputs"][-1] = self.manifest(path)
        self.refresh()
        self.assertEqual(evaluate("E2", item, [result["evidence"]], context)["verdict"], "BLOCK")

    def test_invalid_sources_and_recipes_fail_closed(self):
        changes = [{"value": 123}, {"window": {"start": 0, "stop": 100}}, {"source_unit": "wrong"}, {"aggregate": "eval"}, {"conversion": {"scale": 0, "offset": 0}}, {"marker": {"start": "missing", "end": "END", "occurrence": 0}}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(V3Error):
                recompute(self.run, dict(self.recipe, **change))
        self.source.write_text("changed", encoding="utf-8")
        with self.assertRaises(V3Error):
            recompute(self.run, self.recipe)

    def test_unregistered_and_unfinished_source_rejected(self):
        self.run["status"] = "RUNNING"
        with self.assertRaises(V3Error):
            recompute(self.run, self.recipe)
        self.run["status"] = "FINISHED"
        self.run["outputs"] = []
        self.refresh()
        with self.assertRaises(V3Error):
            recompute(self.run, self.recipe)


if __name__ == "__main__":
    unittest.main()
