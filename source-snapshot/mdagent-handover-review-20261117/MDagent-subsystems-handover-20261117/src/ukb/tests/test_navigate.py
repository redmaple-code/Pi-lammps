import unittest

from tests.helpers import KBTestCase
from ukb.core.errors import UKBError


class Navigate(KBTestCase):
    def setUp(self):
        super().setUp()
        self.w = self.add_approved(self.lesson("W lesson", tags=[{"facet": "topic", "tag": "widgets"},
                                                                 {"facet": "method", "tag": "spin"}]))
        self.wc = self.add_approved(self.lesson("WC lesson", tags=[{"facet": "topic", "tag": "widgets-cold"},
                                                                   {"facet": "method", "tag": "press"}]))
        self.g = self.add_approved(self.lesson("G lesson", tags=[{"facet": "topic", "tag": "gadgets"},
                                                                 {"facet": "method", "tag": "spin"}]))
        self.cand = self.kb.propose(self.lesson("Pending", tags=[{"facet": "topic", "tag": "widgets"}]),
                                    "mcp", "agent:z")

    def ids(self, res):
        return sorted(x["ref"].split("@")[0] for x in res["items"])

    def test_scope_semantics(self):
        cat = self.kb.catalog
        self.assertEqual(self.ids(cat("agent", {"topic": ["widgets"]})), sorted([self.w["entry_id"], self.wc["entry_id"]]))
        self.assertEqual(self.ids(cat("agent", {"topic": ["widgets-cold"]})), [self.wc["entry_id"]])
        self.assertEqual(self.ids(cat("agent", {"topic": ["widgets", "gadgets"]})),
                         sorted([self.w["entry_id"], self.wc["entry_id"], self.g["entry_id"]]))
        self.assertEqual(self.ids(cat("agent", {"topic": ["widgets"], "method": ["spin"]})), [self.w["entry_id"]])
        self.assertEqual(self.ids(cat("agent", {"topic": ["小部件"]})), self.ids(cat("agent", {"topic": ["widgets"]})))
        with self.assertRaises(UKBError) as cm:
            cat("agent", {"topic": ["nothing"]})
        self.assertEqual(cm.exception.code, "TAG_UNKNOWN")
        with self.assertRaises(UKBError):
            cat("agent", {"nofacet": ["x"]})

    def test_overview_counts(self):
        ov = self.kb.overview("agent")
        topic = [f for f in ov["facets"] if f["facet"] == "topic"][0]
        self.assertEqual(topic["entries"], 3)
        t = self.kb.overview("agent", "topic")
        by = {x["tag"]: x for x in t["tags"]}
        self.assertNotIn("widgets-cold", by, "level 1 shows top tags only")
        self.assertEqual(by["widgets"]["entries"], 2)
        self.assertEqual(by["widgets"]["children"], 1)
        self.assertEqual(by["widgets"]["description"], "Entries about widgets. Not gadgets.")
        self.assertIn("小部件", by["widgets"]["aliases"])
        self.assertNotIn("chars", by["widgets"])
        kids = {x["tag"]: x for x in self.kb.overview("agent", "topic", parent="小部件")["tags"]}
        self.assertEqual(list(kids), ["widgets-cold"])
        self.assertEqual(kids["widgets-cold"]["entries"], 1)
        within = self.kb.overview("agent", "method", within={"topic": ["widgets"]})
        m = {x["tag"]: x["entries"] for x in within["tags"]}
        self.assertEqual(m, {"press": 1, "spin": 1})

    def test_profiles_and_status_visibility(self):
        self.assertEqual(len(self.kb.catalog("agent")["items"]), 3)
        self.assertEqual(len(self.kb.catalog("reviewer")["items"]), 4)
        r = self.kb.propose_status(self.g["entry_id"], "deprecated", "old", "agent:b")
        self.approve(r["task_id"])
        self.assertEqual(len(self.kb.catalog("agent")["items"]), 2)
        self.assertEqual(len(self.kb.catalog("agent_wide")["items"]), 3)
        with self.assertRaises(UKBError) as cm:
            self.kb.catalog("nope")
        self.assertEqual(cm.exception.code, "PROFILE_UNKNOWN")

    def test_read_full_text_and_paging(self):
        for i in range(30):
            self.add_approved(self.lesson("Bulk %02d" % i, fields={"situation": "s" * 1400, "risk": "r" * 1400,
                                                                   "fix": "f" * 1900},
                                          tags=[{"facet": "topic", "tag": "gadgets"}]))
        seen, cur, pages = [], None, 0
        while True:
            r = self.kb.read("agent", {"topic": ["gadgets"]}, cursor=cur)
            self.assertLessEqual(r["chars"], 40000 + 6000)
            seen += [x["ref"] for x in r["items"]]
            for x in r["items"]:
                self.assertIn("situation", x["fields"])
            pages += 1
            cur = r["next_cursor"]
            if not cur:
                break
        self.assertEqual(len(seen), 31)
        self.assertEqual(len(set(seen)), 31)
        self.assertGreater(pages, 1)

    def test_cursor_stale_when_scope_changes(self):
        for i in range(30):
            self.add_approved(self.lesson("Bulk %02d" % i, fields={"situation": "s" * 1400, "risk": "r" * 1400,
                                                                   "fix": "f" * 1900}))
        r = self.kb.read("agent", {"topic": ["widgets"]})
        self.add_approved(self.lesson("Late arrival"))
        with self.assertRaises(UKBError) as cm:
            self.kb.read("agent", {"topic": ["widgets"]}, cursor=r["next_cursor"])
        self.assertEqual(cm.exception.code, "CURSOR_STALE")

    def test_must_read(self):
        spec = {"label": "core", "visible_statuses": ["active"], "read_budget_chars": 40000,
                "must_read": {"entry_ids": [self.w["entry_id"]], "scope": {"method": ["press"]}}}
        r = self.kb.propose_registry("profile", {"profile_id": "core", "spec": spec}, "core reads", "agent:a")
        self.admin(r["task_id"])
        mr = self.kb.must_read("core")
        self.assertEqual(sorted(x["entry_id"] for x in mr["items"]), sorted([self.w["entry_id"], self.wc["entry_id"]]))

    def test_conditions_are_returned_for_reading(self):
        e = self.add_approved(self.lesson("Cond", conditions=[{"key": "temp_q", "op": "range", "value": [1, 5]},
                                                              {"key": "mode_m", "value": "m1"}]))
        it = self.kb.read("agent", entry_refs=[e["entry_id"]])["items"][0]
        self.assertEqual([c["key"] for c in it["conditions"]], ["mode_m", "temp_q"])
        self.assertEqual(it["conditions"][1]["unit"], "uq")

    def test_lint_flags_untagged_and_budget(self):
        r = self.kb.propose_registry("facet", {"facet_id": "topic", "label": "主题", "description": "d",
                                               "scope_budget_chars": 1000}, "tighten", "agent:a")
        self.admin(r["task_id"])
        for i in range(10):
            self.add_approved(self.lesson("Fill %d" % i, fields={"situation": "s" * 300, "risk": "r", "fix": "f"},
                                          tags=[{"facet": "topic", "tag": "gadgets"}]))
        codes = [i["code"] for i in self.kb.lint()["issues"]]
        self.assertIn("SCOPE_OVER_BUDGET", codes)


if __name__ == "__main__":
    unittest.main()
