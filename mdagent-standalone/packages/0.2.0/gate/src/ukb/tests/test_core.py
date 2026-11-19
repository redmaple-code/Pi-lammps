import json
import sqlite3
import unittest

from tests.helpers import ORIGIN, KBTestCase
from ukb.api import UKB
from ukb.core import registry
from ukb.core.errors import UKBError
from ukb.core.typespec import validate_spec


class SchemaAndAudit(KBTestCase):
    def test_schema_version_checked(self):
        self.kb.conn.execute("UPDATE schema_meta SET value='999' WHERE key='schema_version'")
        with self.assertRaises(UKBError) as cm:
            UKB(self.root)
        self.assertEqual(cm.exception.code, "SCHEMA_MISMATCH")

    def test_init_twice_refused(self):
        with self.assertRaises(UKBError) as cm:
            UKB.init(self.root)
        self.assertEqual(cm.exception.code, "ALREADY_INITIALIZED")

    def test_revision_immutable_and_audit_append_only(self):
        r = self.add_approved(self.lesson())
        c = self.kb.conn
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("UPDATE entry_revision SET title='x' WHERE entry_id=?", (r["entry_id"],))
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("DELETE FROM entry_revision")
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("UPDATE audit_log SET actor='x'")
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("DELETE FROM audit_log")
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("UPDATE review_decision SET rationale='x'")
        with self.assertRaises(sqlite3.DatabaseError):
            c.execute("UPDATE intake SET raw_json='{}'")

    def test_audit_chain_detects_tampering(self):
        self.add_approved(self.lesson())
        self.assertTrue(self.kb.verify_audit()["ok"])
        c = self.kb.conn
        c.execute("DROP TRIGGER audit_no_update")
        c.execute("UPDATE audit_log SET actor='mallory' WHERE seq=3")
        res = self.kb.verify_audit()
        self.assertFalse(res["ok"])
        self.assertEqual(res["bad_seq"], 3)
        self.assertFalse(self.kb.lint()["ok"])

    def test_db_constraints(self):
        c = self.kb.conn
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO entry VALUES ('ex','lesson','bogus',NULL,NULL,'a','t','t')")
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO reviewer VALUES ('llm:x','admin','s','h',1,'t')")
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO reviewer VALUES ('bob','admin',NULL,NULL,1,'t')")
        with self.assertRaises(sqlite3.IntegrityError):
            c.execute("INSERT INTO review_decision VALUES ('d','t','admin1','admin','webui','prereview','x',NULL,NULL,"
                      "NULL,'h','t')")


class TypeSpecs(KBTestCase):
    def _known(self):
        c = self.kb.conn
        return dict(known_facets=registry.ids(c, "reg_facet", "facet_id"),
                    known_roles=registry.ids(c, "reg_artifact_role", "role_id"),
                    known_formats=registry.ids(c, "reg_file_format", "format_id"))

    def good(self):
        return {"fields": {"a": {"kind": "text", "required": True}}, "render": ["a"]}

    def test_valid_spec(self):
        validate_spec(self.good(), **self._known())

    def test_invalid_specs(self):
        bad = []
        s = self.good(); s["fields"]["b"] = {"kind": "text"}; bad.append(s)  # unrendered field
        s = self.good(); s["render"] = ["a", "zz"]; bad.append(s)
        s = self.good(); s["fields"]["a"]["kind"] = "blob"; bad.append(s)
        s = self.good(); s["require_sources"] = "yes"; bad.append(s)
        s = self.good(); s["required_facets"] = ["nope"]; bad.append(s)
        s = self.good(); s["fields"]["a"]["required"] = False; bad.append(s)
        s = self.good(); s["tier"] = "document"; bad.append(s)
        s = self.good(); s["tier"] = "document"; s["document"] = {"summary_max_chars": 500,
                                                                  "allowed_roles": ["nope"]}; bad.append(s)
        s = self.good(); s["document"] = {"summary_max_chars": 500, "allowed_roles": ["input"]}; bad.append(s)
        s = self.good(); s["extra"] = 1; bad.append(s)
        for s in bad:
            with self.assertRaises(UKBError, msg=json.dumps(s)) as cm:
                validate_spec(s, **self._known())
            self.assertEqual(cm.exception.code, "TYPE_SPEC_INVALID")

    def test_new_type_via_registry_review(self):
        spec = {"type_id": "widget_note", "label": "Widget note", "description": "test type",
                "spec": {"fields": {"body": {"kind": "text", "required": True},
                                    "level": {"kind": "integer", "min": 1, "max": 3}},
                         "render": ["body", "level"], "require_sources": False, "required_facets": ["topic"]}}
        r = self.kb.propose_registry("type", spec, "need a new type", "agent:a")
        self.admin(r["task_id"])
        p = {"mode": "new", "type_id": "widget_note", "title": "N", "fields": {"body": "hello", "level": 2},
             "tags": [{"facet": "topic", "tag": "widgets"}], "origin": ORIGIN}
        e = self.kb.propose(p, "mcp", "agent:a")
        self.assertTrue(e["ok"])
        p["fields"]["level"] = 9
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(cm.exception.code, "PAYLOAD_INVALID")

    def test_type_version_bump_keeps_old_entries(self):
        r = self.add_approved(self.lesson())
        cur = registry.type_spec(self.kb.conn, "lesson")
        spec = json.loads(json.dumps(cur["spec"]))
        spec["fields"]["extra_note"] = {"kind": "text"}
        spec["render"].append("extra_note")
        t = self.kb.propose_registry("type", {"type_id": "lesson", "label": cur["label"],
                                              "description": cur["description"], "spec": spec}, "add field", "agent:a")
        self.admin(t["task_id"])
        self.assertEqual(registry.type_spec(self.kb.conn, "lesson")["version"], cur["version"] + 1)
        got = self.kb.read("agent", entry_refs=[r["entry_id"]])["items"][0]
        self.assertNotIn("extra_note", got["fields"])
        p = self.lesson("Second")
        p["fields"]["extra_note"] = "new field works"
        self.assertTrue(self.kb.propose(p, "mcp", "agent:a")["ok"])


class Registry(KBTestCase):
    def test_registry_change_validation(self):
        with self.assertRaises(UKBError) as cm:
            self.kb.propose_registry("facet", {"facet_id": "Bad Id", "label": "x", "description": "y"}, "r", "a")
        self.assertEqual(cm.exception.code, "REGISTRY_INVALID")
        with self.assertRaises(UKBError):
            self.kb.propose_registry("setting", {"key": "prereview_required_kinds", "value": []},
                                     "prereview cannot be configured away", "a")
        with self.assertRaises(UKBError):
            self.kb.propose_registry("nope", {}, "r", "a")

    def test_file_format_registry_extension(self):
        with self.assertRaises(UKBError):
            self.kb.propose_registry("file_format", {"format_id": "bin2", "label": "b", "patterns": ["*.b2"],
                                                     "kind": "binary"}, "r", "a")
        r = self.kb.propose_registry("file_format", {"format_id": "widgetfmt", "label": "Widget text",
                                                     "patterns": ["*.wdg"], "kind": "text"}, "need it", "a")
        self.admin(r["task_id"])
        self.assertEqual(registry.match_file_format(self.kb.conn, "x/y.wdg")["format_id"], "widgetfmt")

    def test_tag_merge_moves_links_and_aliases(self):
        a = self.add_approved(self.lesson(tags=[{"facet": "topic", "tag": "gadgets"}]))
        r = self.kb.propose_registry("tag", {"facet": "topic", "slug": "gadgets", "status": "merged",
                                             "merge_into": "widgets"}, "same thing", "a")
        self.admin(r["task_id"])
        cat = self.kb.catalog("agent", {"topic": ["gadget"]})
        self.assertEqual([x["ref"].split("@")[0] for x in cat["items"]], [a["entry_id"]])
        self.assertEqual(cat["items"][0]["tags"], ["topic/widgets"])

    def test_parent_cycle_refused(self):
        with self.assertRaises(UKBError):
            registry.validate_change(self.kb.conn, "tag", {"facet": "topic", "slug": "widgets",
                                                           "parent": "widgets-cold"})


if __name__ == "__main__":
    unittest.main()
