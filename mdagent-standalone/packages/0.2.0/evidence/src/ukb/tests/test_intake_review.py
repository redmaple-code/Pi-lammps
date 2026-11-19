import unittest

from tests.helpers import ORIGIN, KBTestCase
from ukb.core.errors import UKBError


class Intake(KBTestCase):
    def test_invalid_payloads_rejected_and_recorded(self):
        cases = [
            ({"mode": "new", "type_id": "nope", "title": "x", "fields": {}, "origin": ORIGIN}, "TYPE_UNKNOWN"),
            (self.lesson(fields={"situation": "s", "risk": "r"}), "PAYLOAD_INVALID"),
            (self.lesson(fields={"situation": "s", "risk": "r", "fix": "f", "unknown": "x"}), "PAYLOAD_INVALID"),
            (self.lesson(tags=[]), "TAG_REQUIRED"),
            (self.lesson(tags=[{"facet": "topic", "tag": "no-such-tag"}]), "TAG_UNKNOWN"),
            (self.lesson(sources=[]), "SOURCE_REQUIRED"),
            (self.lesson(sources=[{"source_type": "nope", "title": "t", "locator": "l"}]), "SOURCE_INVALID"),
            (self.lesson(conditions=[{"key": "temp_q", "op": "lt", "value": "hot"}]), "CONDITION_INVALID"),
            (self.lesson(conditions=[{"key": "temp_q", "op": "eq", "value": 3, "unit": "other"}]), "CONDITION_INVALID"),
            (self.lesson(conditions=[{"key": "mode_m", "value": "m9"}]), "CONDITION_INVALID"),
            (self.lesson(extra_key=1), "PAYLOAD_INVALID"),
            (self.lesson(files=[{"path": "a.md", "role": "report", "description": "d", "content_text": "x"}]),
             "PAYLOAD_INVALID"),
            (self.lesson(origin=None), "ORIGIN_REQUIRED"),
            (self.lesson(origin={"kind": "dream", "ref": "x", "summary_of_evidence": "y"}), "ORIGIN_INVALID"),
            (self.lesson(origin={"kind": "task", "ref": "x"}), "ORIGIN_INVALID"),
        ]
        for p, code in cases:
            if p.get("origin") is None:
                p.pop("origin", None)
            with self.assertRaises(UKBError, msg=code) as cm:
                self.kb.propose(p, "mcp", "agent:a")
            self.assertEqual(cm.exception.code, code)
            self.assertIn("intake_id", cm.exception.detail)
        n = self.kb.conn.execute("SELECT count(*) FROM intake WHERE state='invalid'").fetchone()[0]
        self.assertEqual(n, len(cases))
        self.assertEqual(self.kb.conn.execute("SELECT count(*) FROM entry").fetchone()[0], 0)

    def test_people_may_submit_without_origin(self):
        p = self.lesson()
        p.pop("origin")
        self.assertTrue(self.kb.propose(p, "webui", "admin1")["ok"])

    def test_candidate_invisible_until_approved(self):
        r = self.kb.propose(self.lesson(), "mcp", "agent:a")
        self.assertEqual(self.kb.catalog("agent")["count"], 0)
        with self.assertRaises(UKBError) as cm:
            self.kb.read("agent", entry_refs=[r["entry_id"]])
        self.assertEqual(cm.exception.code, "ENTRY_NOT_VISIBLE")
        self.assertEqual(self.kb.catalog("reviewer")["count"], 1)

    def test_text_fields_trimmed_but_files_verbatim(self):
        src = "  indented first line\n\tsecond\nlast line\n\n"
        p = self.case_payload(files=[{"path": "in.main", "role": "input", "description": "  main input  ",
                                      "content_text": "units x\nsize 10\nsteps 500\nrun 500\n"},
                                     {"path": "notes.md", "role": "report", "description": "d",
                                      "content_text": "# Setup\nx\n# Result\ny\n"},
                                     {"path": "post.py", "role": "script", "description": "d", "content_text": src}])
        p["title"] = "  Case with spaces  "
        r = self.add_approved(p)
        it = self.kb.read("agent", entry_refs=[r["entry_id"]])["items"][0]
        self.assertEqual(it["title"], "Case with spaces")
        self.assertEqual([f["description"] for f in it["document"]["files"] if f["path"] == "in.main"], ["main input"])
        self.assertEqual(self.kb.read_original("agent", r["entry_id"], "post.py")["text"], src)

    def test_origin_kept_for_review(self):
        r = self.kb.propose(self.lesson(), "mcp", "agent:a")
        v = self.kb.review_view(r["task_id"])
        self.assertEqual(v["origin"]["ref"], "task-42")
        b = self.kb.prereview_bundle(r["task_id"], 0)
        self.assertIn("origin", [i["part"] for i in b["items"]])

    def test_agent_daily_limit(self):
        self.set_setting("agent_daily_proposal_limit", 2)
        self.kb.propose(self.lesson("a"), "mcp", "agent:busy")
        self.kb.propose(self.lesson("b"), "mcp", "agent:busy")
        with self.assertRaises(UKBError) as cm:
            self.kb.propose(self.lesson("c"), "mcp", "agent:busy")
        self.assertEqual(cm.exception.code, "RATE_LIMITED")
        self.assertTrue(self.kb.propose(self.lesson("d"), "mcp", "agent:other")["ok"])


class Tagging(KBTestCase):
    def test_tag_resolves_by_slug_label_and_alias(self):
        self.add_approved(self.lesson(tags=[{"facet": "topic", "tag": "Cold widgets"}]))
        for ref in ("widgets-cold", "Cold widgets", "cold WIDGETS"):
            self.assertEqual(self.kb.catalog("agent", {"topic": [ref]})["count"], 1, ref)
        self.assertEqual(self.kb.catalog("agent", {"topic": ["小部件"]})["count"], 1)

    def test_new_tag_candidate_flow(self):
        p = self.lesson(tags=[{"facet": "topic", "tag": "widgets"},
                              {"facet": "method", "new_tag": {"slug": "twist", "label": "Twisting",
                                                              "description": "Twisting method", "aliases": ["拧"]},
                               "reason": "no existing method fits"}])
        r = self.kb.propose(p, "mcp", "agent:a")
        self.assertEqual(self.kb.conn.execute("SELECT status FROM tag WHERE slug='twist'").fetchone()[0], "candidate")
        self.prereview(r["task_id"])
        self.admin(r["task_id"])
        self.assertEqual(self.kb.conn.execute("SELECT status FROM tag WHERE slug='twist'").fetchone()[0], "active")
        for ref in ("拧", "Twisting", "twist"):
            self.assertEqual(self.kb.catalog("agent", {"method": [ref]})["count"], 1, ref)

    def test_new_tag_needs_reason_and_unique_name(self):
        p = self.lesson(tags=[{"facet": "topic", "new_tag": {"slug": "x1", "label": "X", "description": "d"}}])
        with self.assertRaises(UKBError):
            self.kb.propose(p, "mcp", "agent:a")
        for nt in ({"slug": "widgets", "label": "X", "description": "d"},
                   {"slug": "wdg2", "label": "Widgets", "description": "d"},
                   {"slug": "wdg3", "label": "W3", "description": "d", "aliases": ["widget"]}):
            p = self.lesson(tags=[{"facet": "topic", "new_tag": nt, "reason": "r"}])
            with self.assertRaises(UKBError, msg=nt["slug"]) as cm:
                self.kb.propose(p, "mcp", "agent:a")
            self.assertEqual(cm.exception.code, "TAG_EXISTS", nt["slug"])
        self.assertTrue(self.kb.propose(self.lesson(tags=[{"facet": "method", "new_tag": {
            "slug": "widgets", "label": "Widgets method", "description": "same slug in another facet is fine"},
            "reason": "r"}, {"facet": "topic", "tag": "widgets"}]), "mcp", "agent:a")["ok"])

    def test_registry_alias_collision_refused(self):
        with self.assertRaises(UKBError):
            self.kb.propose_registry("tag", {"facet": "topic", "slug": "zz", "label": "ZZ", "description": "d",
                                             "aliases": ["widget"]}, "r", "agent:a")
        with self.assertRaises(UKBError):
            self.kb.propose_registry("tag", {"facet": "topic", "slug": "zz", "label": "Gadgets", "description": "d"},
                                     "r", "agent:a")

    def test_rejected_candidate_tag_is_retired_and_slug_reusable(self):
        p = self.lesson(tags=[{"facet": "topic", "tag": "widgets"},
                              {"facet": "method", "new_tag": {"slug": "odd", "label": "Odd", "description": "d"},
                               "reason": "r"}])
        r = self.kb.propose(p, "mcp", "agent:a")
        self.prereview(r["task_id"])
        self.admin(r["task_id"], args={"tag_decisions": {"reject": ["method/odd"]}})
        self.assertEqual(self.kb.conn.execute("SELECT status FROM tag WHERE slug='odd'").fetchone()[0], "deprecated")
        tags = self.kb.read("agent", entry_refs=[r["entry_id"]])["items"][0]["tags"]
        self.assertEqual([t["tag"] for t in tags], ["widgets"])
        r2 = self.kb.propose(self.lesson("Second", tags=[{"facet": "topic", "tag": "widgets"},
                                                         {"facet": "method", "new_tag": {
                                                             "slug": "odd", "label": "Odd v2", "description": "d2"},
                                                          "reason": "now needed"}]), "mcp", "agent:a")
        self.assertTrue(r2["ok"])
        self.assertEqual(self.kb.conn.execute("SELECT status, label FROM tag WHERE slug='odd'").fetchone()[:],
                         ("candidate", "Odd v2"))

    def test_candidate_tag_retired_when_entry_rejected(self):
        p = self.lesson(tags=[{"facet": "topic", "tag": "widgets"},
                              {"facet": "method", "new_tag": {"slug": "roll", "label": "Roll", "description": "d"},
                               "reason": "r"}])
        r = self.kb.propose(p, "mcp", "agent:a")
        self.prereview(r["task_id"])
        self.admin(r["task_id"], decision="reject", rationale="not useful")
        self.assertEqual(self.kb.conn.execute("SELECT status FROM tag WHERE slug='roll'").fetchone()[0], "deprecated")
        with self.assertRaises(UKBError):
            self.kb.catalog("agent", {"method": ["roll"]})

    def test_merge_new_tag_into_existing_at_approval(self):
        p = self.lesson(tags=[{"facet": "topic", "tag": "widgets"},
                              {"facet": "method", "new_tag": {"slug": "spinning2", "label": "Spinning 2",
                                                              "description": "d"}, "reason": "r"}])
        r = self.kb.propose(p, "mcp", "agent:a")
        self.prereview(r["task_id"], {"tag_opinion": {"ok": False, "merge_new_into": [
            {"new": "method/spinning2", "existing": "method/spin"}], "note": "spin already covers it"}})
        self.admin(r["task_id"], args={"tag_decisions": {"merge_new_into": [
            {"new": "method/spinning2", "existing": "method/spin"}]}})
        tags = sorted("%s/%s" % (t["facet"], t["tag"])
                      for t in self.kb.read("agent", entry_refs=[r["entry_id"]])["items"][0]["tags"])
        self.assertEqual(tags, ["method/spin", "topic/widgets"])
        self.assertEqual(self.kb.conn.execute("SELECT status FROM tag WHERE slug='spinning2'").fetchone()[0],
                         "deprecated")

    def test_prereview_sees_taxonomy_with_descriptions(self):
        r = self.kb.propose(self.lesson(tags=[{"facet": "topic", "tag": "widgets"}, {"facet": "method",
                                                                                      "tag": "spin"}]), "mcp", "agent:a")
        b = self.kb.prereview_bundle(r["task_id"], 0)
        tax = [i for i in b["items"] if i["part"] == "taxonomy"][0]["content"]
        facets = {f["facet"]: f for f in tax}
        self.assertEqual(sorted(facets), ["method", "topic"])
        topic = {t["tag"]: t for t in facets["topic"]["tags"]}
        self.assertEqual(topic["widgets"]["description"], "Entries about widgets. Not gadgets.")
        self.assertIn("gadgets", topic)
        self.assertEqual(topic["widgets-cold"]["parent"], "widgets")

    def test_prereview_must_judge_type_and_tags(self):
        r = self.kb.propose(self.lesson(), "mcp", "agent:a")
        for missing in ("type_opinion", "tag_opinion"):
            b = self.kb.prereview_bundle(r["task_id"], 0)
            op = {"type_opinion": {"ok": True}, "tag_opinion": {"ok": True}, "recommendation": "approve",
                  "note": "x", "checklist": [{"item": c, "ok": True} for c in b["instructions"]["checklist"]]}
            op.pop(missing)
            with self.assertRaises(UKBError) as cm:
                self.kb.prereview_submit(r["task_id"], "llm:p", [b["page_token"]], op)
            self.assertEqual(cm.exception.code, "OPINION_INCOMPLETE", missing)
        b = self.kb.prereview_bundle(r["task_id"], 0)
        bad = {"type_opinion": {"ok": False, "suggest": "nope"}, "tag_opinion": {"ok": True},
               "recommendation": "approve", "note": "x",
               "checklist": [{"item": c, "ok": True} for c in b["instructions"]["checklist"]]}
        with self.assertRaises(UKBError):
            self.kb.prereview_submit(r["task_id"], "llm:p", [b["page_token"]], bad)
        bad["type_opinion"] = {"ok": True}
        bad["tag_opinion"] = {"ok": False, "drop": ["topic/nonexistent"]}
        with self.assertRaises(UKBError):
            self.kb.prereview_submit(r["task_id"], "llm:p", [b["page_token"]], bad)

    def test_single_choice_facet(self):
        r = self.kb.propose_registry("facet", {"facet_id": "grade", "label": "Grade", "description": "one only",
                                               "multi": False}, "test", "agent:a")
        self.admin(r["task_id"])
        for slug in ("g1", "g2"):
            r = self.kb.propose_registry("tag", {"facet": "grade", "slug": slug, "label": slug.upper(),
                                                 "description": slug}, "test", "agent:a")
            self.admin(r["task_id"])
        with self.assertRaises(UKBError):
            self.kb.propose(self.lesson(tags=[{"facet": "topic", "tag": "widgets"}, {"facet": "grade", "tag": "g1"},
                                              {"facet": "grade", "tag": "g2"}]), "mcp", "agent:a")

    def test_exact_duplicate_flagged_not_merged(self):
        a = self.add_approved(self.lesson())
        b = self.kb.propose(self.lesson(sources=[{"source_type": "case", "title": "run 2", "locator": "runs/2"}]),
                            "mcp", "agent:b")
        self.assertIsNotNone(b["exact_duplicate_of"])
        self.assertNotEqual(a["entry_id"], b["entry_id"])
        c = self.kb.propose(self.lesson(fields={"situation": "When the widget is cold!", "risk": "Gears slip.",
                                                "fix": "Warm it first."}), "mcp", "agent:b")
        self.assertIsNone(c["exact_duplicate_of"])

    def test_risk_rules_flag_or_quarantine(self):
        r = self.kb.propose(self.lesson(fields={"situation": "near the DangerZone", "risk": "r", "fix": "f"}),
                            "mcp", "agent:a")
        self.assertEqual(r["risk_hits"], ["rr-keyword"])
        self.assertFalse(r["quarantined"])
        self.assertEqual([x["rule_id"] for x in self.kb.review_view(r["task_id"])["risk"]], ["rr-keyword"])
        q = self.kb.propose(self.lesson("Q", fields={"situation": "forbiddenword", "risk": "r", "fix": "f"}),
                            "mcp", "agent:a")
        self.assertTrue(q["quarantined"])
        self.assertEqual(self.kb.catalog("reviewer")["items"][-1]["status"] in ("quarantined", "candidate"), True)
        self.prereview(q["task_id"])
        self.admin(q["task_id"])
        self.assertEqual(self.kb.conn.execute("SELECT status FROM entry WHERE entry_id=?",
                                              (q["entry_id"],)).fetchone()[0], "active")


class Review(KBTestCase):
    def test_prereview_mandatory(self):
        r = self.kb.propose(self.lesson(), "mcp", "agent:a")
        with self.assertRaises(UKBError) as cm:
            self.admin(r["task_id"])
        self.assertEqual(cm.exception.code, "PREREVIEW_REQUIRED")
        self.prereview(r["task_id"])
        self.assertEqual(self.admin(r["task_id"])["state"], "approved")

    def test_submitter_cannot_prereview_own(self):
        r = self.kb.propose(self.lesson(), "mcp", "agent:writer")
        with self.assertRaises(UKBError) as cm:
            self.prereview(r["task_id"], agent="llm:writer")
        self.assertEqual(cm.exception.code, "SELF_PREREVIEW")
        self.prereview(r["task_id"], agent="llm:other")
        with self.assertRaises(UKBError) as cm:
            self.prereview(r["task_id"], agent="llm:other")
        self.assertEqual(cm.exception.code, "PREREVIEW_DUPLICATE")

    def test_one_admin_approval_is_enough(self):
        r = self.kb.propose(self.lesson(), "webui", "admin1")
        self.prereview(r["task_id"])
        self.assertEqual(self.admin(r["task_id"], rationale="")["state"], "approved")

    def test_queue_needs_filters(self):
        a = self.kb.propose(self.lesson("A"), "mcp", "agent:a")
        b = self.kb.propose(self.lesson("B"), "mcp", "agent:a")
        self.prereview(b["task_id"], {"recommendation": "request_changes"})
        pre = [t["task_id"] for t in self.kb.queue(needs="prereview")]
        adm = self.kb.queue(needs="admin")
        self.assertIn(a["task_id"], pre)
        self.assertNotIn(b["task_id"], pre)
        self.assertEqual([t["task_id"] for t in adm if t["kind"] == "new"], [b["task_id"]])
        self.assertEqual(adm[-1]["prereview_recommendations"], ["request_changes"])

    def test_prereview_must_cover_all_pages_and_checklist(self):
        r = self.kb.propose(self.lesson(), "mcp", "agent:a")
        b = self.kb.prereview_bundle(r["task_id"], 0)
        op = {"type_opinion": {"ok": True}, "tag_opinion": {"ok": True}, "checklist": [], "recommendation": "approve",
              "note": "x"}
        with self.assertRaises(UKBError) as cm:
            self.kb.prereview_submit(r["task_id"], "llm:p", [b["page_token"]], op)
        self.assertEqual(cm.exception.code, "OPINION_INCOMPLETE")
        with self.assertRaises(UKBError) as cm:
            self.kb.prereview_submit(r["task_id"], "llm:p", ["forged"], op)
        self.assertEqual(cm.exception.code, "PREREVIEW_INCOMPLETE")
        with self.assertRaises(UKBError):
            self.kb.prereview_submit(r["task_id"], "admin1", [b["page_token"]], op)

    def test_prereview_reads_scope_full_text(self):
        a = self.add_approved(self.lesson("Existing one"))
        other = self.add_approved(self.lesson("Gadget one", tags=[{"facet": "topic", "tag": "gadgets"}]))
        r = self.kb.propose(self.lesson("New one", tags=[{"facet": "topic", "tag": "widgets-cold"}]), "mcp", "agent:b")
        _, pages = self.prereview(r["task_id"])
        refs = [it["ref"].split("@")[0] for p in pages for it in p["items"] if it["part"] == "scope"]
        self.assertIn(a["entry_id"], refs)
        self.assertNotIn(other["entry_id"], refs)
        full = [it for p in pages for it in p["items"] if it["part"] == "scope"][0]["content"]
        self.assertIn("fields", full)

    def test_prereview_paging(self):
        for i in range(12):
            self.add_approved(self.lesson("Existing %d" % i, fields={"situation": "s" * 1400, "risk": "r" * 1400,
                                                                     "fix": "f" * 1900}))
        r = self.kb.propose(self.lesson("Paged"), "mcp", "agent:b")
        self.assertGreater(self.kb.prereview_bundle(r["task_id"], 0)["pages"], 1)
        self.assertTrue(self.prereview(r["task_id"])[0]["ok"])

    def test_prereview_tokens_invalidated_by_scope_change(self):
        r = self.kb.propose(self.lesson("Target"), "mcp", "agent:a")
        b = self.kb.prereview_bundle(r["task_id"], 0)
        self.add_approved(self.lesson("Arrived meanwhile"))
        op = {"type_opinion": {"ok": True}, "tag_opinion": {"ok": True}, "recommendation": "approve", "note": "x",
              "checklist": [{"item": c, "ok": True} for c in b["instructions"]["checklist"]]}
        with self.assertRaises(UKBError) as cm:
            self.kb.prereview_submit(r["task_id"], "llm:p", [b["page_token"]], op)
        self.assertEqual(cm.exception.code, "PREREVIEW_INCOMPLETE")
        _, pages = self.prereview(r["task_id"])
        titles = [it["content"]["title"] for p in pages for it in p["items"] if it["part"] == "scope"]
        self.assertIn("Arrived meanwhile", titles)

    def test_auth_and_channel(self):
        r = self.kb.propose(self.lesson(), "mcp", "agent:a")
        self.prereview(r["task_id"])
        v = self.kb.review_view(r["task_id"])
        with self.assertRaises(UKBError) as cm:
            self.kb.decide(r["task_id"], "admin1", "wrong-token", "approve", "x", v["bound_hash"], "webui")
        self.assertEqual(cm.exception.code, "AUTH_FAILED")
        with self.assertRaises(UKBError) as cm:
            self.kb.decide(r["task_id"], "llm:pre", "x", "approve", "x", v["bound_hash"], "webui")
        self.assertEqual(cm.exception.code, "AUTH_FAILED")
        with self.assertRaises(UKBError) as cm:
            self.kb.decide(r["task_id"], "admin1", self.admin_tok, "approve", "x", v["bound_hash"], "mcp")
        self.assertEqual(cm.exception.code, "CHANNEL_DENIED")
        self.kb.set_admin_active("admin1", False, "test")
        with self.assertRaises(UKBError):
            self.admin(r["task_id"])

    def test_stale_review_refused(self):
        r = self.kb.propose(self.lesson(), "mcp", "agent:a")
        self.prereview(r["task_id"])
        with self.assertRaises(UKBError) as cm:
            self.kb.decide(r["task_id"], "admin1", self.admin_tok, "approve", "x", "0" * 64, "webui")
        self.assertEqual(cm.exception.code, "STALE_REVIEW")

    def test_correct_then_approve(self):
        r = self.kb.propose(self.lesson(), "mcp", "agent:a")
        self.prereview(r["task_id"])
        res = self.admin(r["task_id"], decision="correct",
                         args={"correction": {"title": "Better title",
                                              "fields": {"situation": "When the widget is cold.",
                                                         "risk": "Gears slip.", "fix": "Warm it to room level."}}})
        self.assertEqual(res["state"], "approved")
        got = self.kb.read("agent", entry_refs=[r["entry_id"]])["items"][0]
        self.assertEqual((got["title"], got["rev_no"], got["fields"]["fix"]), ("Better title", 2, "Warm it to room level."))
        with self.assertRaises(UKBError):
            self.kb.read("agent", entry_refs=[r["entry_id"] + "@1"])

    def test_correct_tags_only(self):
        r = self.kb.propose(self.lesson(), "mcp", "agent:a")
        self.prereview(r["task_id"])
        self.admin(r["task_id"], decision="correct", args={"correction": {"tags": [{"facet": "method", "tag": "spin"}]}})
        got = self.kb.read("agent", entry_refs=[r["entry_id"]])["items"][0]
        self.assertEqual(got["rev_no"], 1)
        self.assertEqual(sorted(t["tag"] for t in got["tags"]), ["spin", "widgets"])

    def test_correction_invalid_refused(self):
        r = self.kb.propose(self.lesson(), "mcp", "agent:a")
        self.prereview(r["task_id"])
        with self.assertRaises(UKBError) as cm:
            self.admin(r["task_id"], decision="correct", args={"correction": {"fields": {"situation": "x"}}})
        self.assertEqual(cm.exception.code, "PAYLOAD_INVALID")
        self.assertEqual(self.kb.review_view(r["task_id"])["state"], "open")

    def test_request_changes_and_reject_visible_to_submitter(self):
        r = self.kb.propose(self.lesson(), "mcp", "agent:a")
        self.prereview(r["task_id"])
        self.admin(r["task_id"], decision="request_changes", rationale="split into two")
        mine = self.kb.my_submissions("agent:a")["items"][0]
        self.assertEqual(mine["task_state"], "changes_requested")
        self.assertIn("split into two", [f["note"] for f in mine["feedback"]])
        self.assertIn("next", mine)

    def test_reinforce_into_existing(self):
        a = self.add_approved(self.lesson())
        b = self.kb.propose(self.lesson("Same idea, other words",
                                        sources=[{"source_type": "case", "title": "run 9", "locator": "runs/9"}]),
                            "mcp", "agent:b")
        self.prereview(b["task_id"], {"duplicate_of": [{"entry_id": a["entry_id"], "note": "same lesson"}]})
        self.admin(b["task_id"], decision="reinforce_into", args={"target_entry_id": a["entry_id"]})
        got = self.kb.read("agent", entry_refs=[a["entry_id"]])["items"][0]
        self.assertEqual([x["locator"] for x in got["reinforced_by"]], ["runs/9"])
        self.assertEqual(self.kb.catalog("agent")["count"], 1)

    def test_reinforce_mode(self):
        a = self.add_approved(self.lesson())
        r = self.kb.propose({"mode": "reinforce", "target_entry_id": a["entry_id"], "note": "confirmed again",
                             "sources": [{"source_type": "measurement", "title": "m", "locator": "lab/7"}],
                             "origin": ORIGIN}, "mcp", "agent:b")
        with self.assertRaises(UKBError):
            self.admin(r["task_id"])
        self.prereview(r["task_id"])
        self.admin(r["task_id"])
        self.assertEqual(len(self.kb.read("agent", entry_refs=[a["entry_id"]])["items"][0]["reinforced_by"]), 1)

    def test_revise_keeps_old_head_until_approved(self):
        a = self.add_approved(self.lesson())
        r = self.kb.propose({"mode": "revise", "target_entry_id": a["entry_id"], "base_rev_no": 1,
                             "title": "Alpha lesson v2",
                             "fields": {"situation": "When the widget is cold.", "risk": "Gears slip.",
                                        "fix": "Warm it first, then check."},
                             "sources": [{"source_type": "case", "title": "run 1", "locator": "runs/1"}],
                             "origin": ORIGIN}, "mcp", "agent:b")
        self.assertEqual(self.kb.read("agent", entry_refs=[a["entry_id"]])["items"][0]["title"], "Alpha lesson")
        with self.assertRaises(UKBError) as cm:
            self.kb.propose({"mode": "revise", "target_entry_id": a["entry_id"], "title": "x", "origin": ORIGIN,
                             "fields": {"situation": "s", "risk": "r", "fix": "f"}}, "mcp", "agent:c")
        self.assertEqual(cm.exception.code, "TASK_CONFLICT")
        self.prereview(r["task_id"])
        self.admin(r["task_id"])
        got = self.kb.read("agent", entry_refs=[a["entry_id"]])["items"][0]
        self.assertEqual((got["title"], got["rev_no"]), ("Alpha lesson v2", 2))
        self.assertEqual(self.kb.read("agent", entry_refs=[a["entry_id"] + "@1"])["items"][0]["title"], "Alpha lesson")

    def test_stale_base_revision(self):
        a = self.add_approved(self.lesson())
        with self.assertRaises(UKBError) as cm:
            self.kb.propose({"mode": "revise", "target_entry_id": a["entry_id"], "base_rev_no": 7, "title": "x",
                             "origin": ORIGIN, "fields": {"situation": "s", "risk": "r", "fix": "f"}}, "mcp", "agent:b")
        self.assertEqual(cm.exception.code, "STALE_BASE")

    def test_supersede_relation_and_status(self):
        a = self.add_approved(self.lesson())
        b = self.kb.propose(self.lesson("Newer lesson", fields={"situation": "cold widget", "risk": "slip",
                                                                "fix": "better fix"}), "mcp", "agent:b")
        self.prereview(b["task_id"], {"supersedes": [{"entry_id": a["entry_id"], "note": "newer"}]})
        self.admin(b["task_id"], args={"relations": [{"rel": "supersedes", "entry_id": a["entry_id"],
                                                      "retire_other": True, "note": "newer"}]})
        self.assertEqual(self.kb.conn.execute("SELECT status FROM entry WHERE entry_id=?",
                                              (a["entry_id"],)).fetchone()[0], "superseded")
        self.assertEqual([x["ref"].split("@")[0] for x in self.kb.catalog("agent")["items"]], [b["entry_id"]])
        self.assertTrue(self.kb.lint()["ok"])

    def test_status_change_flow(self):
        a = self.add_approved(self.lesson())
        r = self.kb.propose_status(a["entry_id"], "revoked", "it was wrong", "agent:b")
        self.admin(r["task_id"])
        self.assertEqual(self.kb.catalog("agent")["count"], 0)
        with self.assertRaises(UKBError):
            self.kb.propose_status(a["entry_id"], "active", "undo", "agent:b")

    def test_feedback_opens_recheck(self):
        a = self.add_approved(self.lesson())
        out = self.kb.feedback(a["entry_id"], "agent:x", "misleading", "did not apply")
        self.assertIsNotNone(out["recheck_task"])
        self.assertIsNone(self.kb.feedback(a["entry_id"], "agent:x", "misleading", "again")["recheck_task"])
        self.admin(out["recheck_task"], args={"target_status": "deprecated"})
        self.assertEqual(self.kb.conn.execute("SELECT status FROM entry WHERE entry_id=?",
                                              (a["entry_id"],)).fetchone()[0], "deprecated")

    def test_feedback_recheck_keep(self):
        a = self.add_approved(self.lesson())
        out = self.kb.feedback(a["entry_id"], "agent:x", "reused_failed", "wrong env")
        self.admin(out["recheck_task"], args={"target_status": "keep"})
        self.assertEqual(self.kb.conn.execute("SELECT status FROM entry WHERE entry_id=?",
                                              (a["entry_id"],)).fetchone()[0], "active")

    def test_tag_change_proposal(self):
        a = self.add_approved(self.lesson())
        r = self.kb.propose_tags(a["entry_id"], [{"facet": "method", "tag": "spin"}], [], "also spinning", "agent:b")
        self.admin(r["task_id"])
        self.assertEqual(self.kb.catalog("agent", {"method": ["spin"]})["count"], 1)
        r = self.kb.propose_tags(a["entry_id"], [], ["topic/widgets"], "wrong", "agent:b")
        with self.assertRaises(UKBError) as cm:
            self.admin(r["task_id"])
        self.assertEqual(cm.exception.code, "TAG_REQUIRED")


class Contribute(KBTestCase):
    def test_guide_is_staged(self):
        g = self.kb.guide()
        self.assertIn("lesson", [t["type_id"] for t in g["types"]])
        self.assertNotIn("fields", g["types"][0])
        self.assertNotIn("tags", g["facets"][0])
        self.assertTrue(g["when_to_submit"] and g["do_not_submit"] and g["workflow"])
        self.assertIn("task", g["origin"]["kinds"])
        case = self.kb.guide("case")
        self.assertEqual(case["tier"], "document")
        self.assertIn("reuse", case["fields"])
        self.assertIn("*.zip", case["file_rules"]["denied"])
        self.assertNotIn("file_rules", self.kb.guide("lesson"))

    def test_prepare_returns_titles_and_open_proposals(self):
        a = self.add_approved(self.lesson("Known lesson"))
        b = self.add_approved(self.lesson("Cold one", tags=[{"facet": "topic", "tag": "widgets-cold"}]))
        g = self.add_approved(self.lesson("Gadget one", tags=[{"facet": "topic", "tag": "gadgets"}]))
        pend = self.kb.propose(self.lesson("Still pending"), "mcp", "agent:z")
        out = self.kb.prepare("agent", "lesson", [{"facet": "topic", "tag": "widgets"}])
        ids = sorted(x["ref"].split("@")[0] for x in out["existing"])
        self.assertEqual(ids, sorted([a["entry_id"], b["entry_id"]]))
        self.assertNotIn(g["entry_id"], ids)
        self.assertNotIn("fields", out["existing"][0])
        self.assertEqual([p["task_id"] for p in out["open_proposals_in_scope"]], [pend["task_id"]])
        with self.assertRaises(UKBError):
            self.kb.prepare("agent", "lesson", [{"facet": "topic", "tag": "nothing"}])
        with self.assertRaises(UKBError):
            self.kb.prepare("agent", "lesson", [])

    def test_prepare_with_new_tag_uses_parent_scope(self):
        a = self.add_approved(self.lesson("Known lesson"))
        out = self.kb.prepare("agent", "lesson", [{"facet": "topic", "new_tag": {
            "slug": "widgets-hot", "label": "Hot widgets", "description": "d", "parent": "widgets"}, "reason": "r"}])
        self.assertEqual([x["ref"].split("@")[0] for x in out["existing"]], [a["entry_id"]])
        self.assertEqual(out["new_tags_proposed"], ["topic/widgets-hot"])


if __name__ == "__main__":
    unittest.main()
