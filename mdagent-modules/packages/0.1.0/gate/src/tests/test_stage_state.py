#!/usr/bin/env python3.11
# -*- coding: utf-8 -*-
"""stage_state(任务级阶段状态机)单测——开发手册 §3.3.5 用例表。
含并发/坏JSON/幂等/工件不可变/非法流转折词典。"""
import json
import os
import sys
import tempfile
import threading
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "evolution"))

import stage_state  # noqa: E402


class TestStageState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stage_state_test_")

    def tdir(self, cid):
        return stage_state.task_dir(cid, self.tmp)

    # ---------- init / 读取 ----------
    def test_init_creates_package(self):
        st = stage_state.init_task("TASK-UT-001", risk_level="high", data_root=self.tmp)
        self.assertEqual(st["current_stage"], "PLAN")
        self.assertTrue(os.path.isfile(stage_state.state_path("TASK-UT-001", self.tmp)))
        for name in ("review-log.jsonl", "gate-log.jsonl", "open-issues.md", "work-log.md"):
            self.assertTrue(os.path.exists(os.path.join(self.tdir("TASK-UT-001"), name)),
                            name)

    def test_init_idempotent(self):
        stage_state.init_task("TASK-UT-002", data_root=self.tmp)
        again = stage_state.init_task("TASK-UT-002", data_root=self.tmp)
        self.assertTrue(again.get("already_initialized"))

    def test_read_state_none_when_absent(self):
        self.assertIsNone(stage_state.read_state("TASK-NOPE", self.tmp))

    def test_bad_case_id_rejected(self):
        with self.assertRaises(stage_state.TaskStateError):
            stage_state.init_task("../escape", data_root=self.tmp)

    # ---------- 流转 ----------
    def _mk_decision(self, cid, slug="d"):
        return stage_state.record_decision_artifact(
            cid, {"gate": "G-PLAN", "stage": "PLAN", "decision": "PASS"}, slug, self.tmp)

    def test_transition_ok_and_revises_reset(self):
        cid = "TASK-UT-003"
        stage_state.init_task(cid, data_root=self.tmp)
        ref = self._mk_decision(cid)
        stage_state.bump_revises(cid, "BUILD", self.tmp)  # 预置计数,进段应清零
        st = stage_state.transition(cid, "BUILD", ref, data_root=self.tmp)
        self.assertEqual(st["current_stage"], "BUILD")
        self.assertEqual(st["revises_count"]["BUILD"], 0)
        self.assertEqual(st["last_transition"]["decision_ref"], ref)

    def test_transition_skip_rejected(self):
        cid = "TASK-UT-004"
        stage_state.init_task(cid, data_root=self.tmp)
        ref = self._mk_decision(cid)
        with self.assertRaises(stage_state.TaskStateError) as cm:
            stage_state.transition(cid, "CONFIG", ref, data_root=self.tmp)
        self.assertTrue(str(cm.exception).startswith("TS-003"), str(cm.exception))

    def test_transition_fake_ref_rejected(self):
        cid = "TASK-UT-005"
        stage_state.init_task(cid, data_root=self.tmp)
        with self.assertRaises(stage_state.TaskStateError) as cm:
            stage_state.transition(cid, "BUILD", "decisions/nope.json", data_root=self.tmp)
        self.assertTrue(str(cm.exception).startswith("TS-002"), str(cm.exception))

    def test_rollback_transition_allows_backward(self):
        cid = "TASK-UT-006"
        stage_state.init_task(cid, data_root=self.tmp)
        ref = self._mk_decision(cid)
        stage_state.transition(cid, "BUILD", ref, data_root=self.tmp)
        back = stage_state.transition(cid, "PLAN", ref, data_root=self.tmp,
                                      via_rollback=True)
        self.assertEqual(back["status"], "rolled_back")
        self.assertEqual(back["rollback_of"], "BUILD")

    # ---------- 坏 JSON / 备份 ----------
    def test_corrupt_state_refuses_write_with_backup(self):
        cid = "TASK-UT-007"
        stage_state.init_task(cid, data_root=self.tmp)
        with open(stage_state.state_path(cid, self.tmp), "w", encoding="utf-8") as f:
            f.write("{ broken json !!!")
        with self.assertRaises(stage_state.TaskStateError) as cm:
            stage_state.transition(cid, "BUILD", self._mk_decision(cid), data_root=self.tmp)
        self.assertTrue(str(cm.exception).startswith("TS-004"), str(cm.exception))
        backups = os.listdir(os.path.join(self.tdir(cid), "backups"))
        self.assertTrue(any(b.startswith("state.corrupt-") for b in backups), backups)
        # 原文件字节不变
        with open(stage_state.state_path(cid, self.tmp), encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ broken json !!!")

    # ---------- 工件不可变 ----------
    def test_decision_artifact_immutable(self):
        """同名送审两次：生成两个不同文件，先写的那个字节不变（不可覆盖）。"""
        cid = "TASK-UT-008"
        stage_state.init_task(cid, data_root=self.tmp)
        ref1 = stage_state.record_decision_artifact(cid, {"a": 1}, "x", self.tmp)
        fp1 = os.path.join(stage_state.task_dir(cid, self.tmp), ref1)
        before = open(fp1, encoding="utf-8").read()
        ref2 = stage_state.record_decision_artifact(cid, {"a": 2}, "x", self.tmp)
        self.assertNotEqual(ref1, ref2)
        self.assertEqual(open(fp1, encoding="utf-8").read(), before)
        self.assertIn('"a": 1', before)

    # ---------- 并发 ----------
    def test_concurrent_bump_revises_exact(self):
        cid = "TASK-UT-009"
        stage_state.init_task(cid, data_root=self.tmp)
        results = []

        def worker():
            for _ in range(20):
                results.append(stage_state.bump_revises(cid, "PLAN", self.tmp))

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        final = stage_state.read_state(cid, self.tmp)["revises_count"]["PLAN"]
        self.assertEqual(final, 200)
        self.assertEqual(sorted(results)[-1], 200)
        with open(stage_state.state_path(cid, self.tmp), encoding="utf-8") as f:
            json.load(f)  # 无半写文件

    # ---------- 日志 ----------
    def test_gate_log_append_and_strict_read(self):
        cid = "TASK-UT-010"
        stage_state.init_task(cid, data_root=self.tmp)
        stage_state.append_gate_log(cid, {"gate": "G-PLAN", "result": "GO"}, self.tmp)
        rows = stage_state.read_gate_log(cid, data_root=self.tmp)
        self.assertEqual(rows[-1]["result"], "GO")
        fp = os.path.join(self.tdir(cid), "gate-log.jsonl")
        with open(fp, "a", encoding="utf-8") as f:
            f.write('{"bad": NaN}\n')  # 坏行被跳过,不崩
        rows2 = stage_state.read_gate_log(cid, data_root=self.tmp)
        self.assertEqual(rows2[-1]["result"], "GO")

    # ---------- 决策槽位 ----------
    def test_lock_and_revise_decisions(self):
        cid = "TASK-UT-011"
        stage_state.init_task(cid, data_root=self.tmp)
        stage_state.lock_decisions(cid, {"D7_验收标准": ["判据A"]}, self.tmp)
        with self.assertRaises(stage_state.TaskStateError):
            stage_state.revise_decision(cid, "D9_不存在", "r", "ref", self.tmp)
        doc = stage_state.revise_decision(cid, "D7_验收标准", "收紧", "ref-1", self.tmp)
        self.assertEqual(len(doc["D7_验收标准"]["revisions"]), 1)
        # 已有槽不覆盖
        stage_state.lock_decisions(cid, {"D7_验收标准": ["别的"]}, self.tmp)
        self.assertEqual(stage_state.read_decisions(cid, self.tmp)["D7_验收标准"]["value"],
                         ["判据A"])

    # ---------- 段集合 ----------
    def test_unknown_stage_set_rejected(self):
        with self.assertRaises(stage_state.TaskStateError):
            stage_state.init_task("TASK-UT-012", stage_set="nope@9", data_root=self.tmp)

    def test_custom_stage_set_from_data(self):
        sets_fp = os.path.join(self.tmp, "memory", "stage_sets.json")
        os.makedirs(os.path.dirname(sets_fp), exist_ok=True)
        with open(sets_fp, "w", encoding="utf-8") as f:
            json.dump({"sets": {"custom@1": {"stages": ["A", "B"],
                                             "exit_gates": {"A": "G-A", "B": "G-B"}}}}, f)
        st = stage_state.init_task("TASK-UT-013", stage_set="custom@1", data_root=self.tmp)
        self.assertEqual(st["current_stage"], "A")
        self.assertEqual(sorted(st["revises_count"]), ["A", "B"])

    # ---------- P2 聚合 ----------
    def test_list_task_summaries(self):
        cid = "TASK-UT-014"
        stage_state.init_task(cid, risk_level="high", data_root=self.tmp)
        stage_state.bump_revises(cid, "PLAN", self.tmp)
        rows = stage_state.list_task_summaries(self.tmp)
        row = [r for r in rows if r["case_id"] == cid][0]
        self.assertEqual(row["stage"], "PLAN")
        self.assertEqual(row["strike"], 1)
        self.assertFalse(row["blocked_by_review_loop"])


if __name__ == "__main__":
    unittest.main()
