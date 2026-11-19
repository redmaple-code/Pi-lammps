"""Validate MD-1 documentation links and planning trace; no product execution."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]


def read_json(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_dag(items, key, dependency_key):
    mapping = {item[key]: item for item in items}
    assert len(mapping) == len(items), "Duplicate IDs"
    active, done = set(), set()

    def visit(identity):
        assert identity in mapping, f"Missing dependency: {identity}"
        assert identity not in active, f"Dependency cycle: {identity}"
        if identity in done:
            return
        active.add(identity)
        for dependency in mapping[identity][dependency_key]:
            visit(dependency)
        active.remove(identity)
        done.add(identity)

    for identity in mapping:
        visit(identity)


def main():
    planning = read_json("tasks.json")
    requirements = read_json("requirements.json")["requirements"]
    source_map = read_json("source-map.json")
    baseline = read_json("input-baseline.json")
    modules, tasks = planning["modules"], planning["tasks"]
    assert len(modules) == 21 and len(tasks) == 63
    assert_dag(modules, "id", "dependsOn")
    assert_dag(tasks, "id", "dependsOn")
    module_map = {m["id"]: m for m in modules}
    task_ids = {t["id"] for t in tasks}

    original_mig = (ROOT.parent / "功能迁移与验收矩阵.md").read_text(encoding="utf-8")
    original_gsf = (ROOT.parent / "development-spec/10-acceptance.md").read_text(encoding="utf-8")
    source_ids = set(re.findall(r"^\| (MIG-\d{3}) \|", original_mig, re.M))
    source_ids |= set(re.findall(r"^\| (GSF-\d{3}) /", original_gsf, re.M))
    mapped_ids = [r["requirementId"] for r in requirements]
    assert len(source_ids) == 116 and len(mapped_ids) == len(set(mapped_ids)) == 116
    assert set(mapped_ids) == source_ids, "Requirements are missing or invented"

    scenario_ids = set()
    document_task_ids = set()
    for module in modules:
        path = ROOT / module["document"]
        content = path.read_text(encoding="utf-8")
        assert content.startswith(f'# {module["id"]} ')
        assert module["implementationStatus"] == "notImplemented"
        assert module["verificationStatus"] == "notRun"
        assert module["assignedPerson"] is None
        own = re.findall(r"^\| (ACC-\d{2}-\d{2}) \|", content, re.M)
        assert len(own) == 3 and not (set(own) & scenario_ids)
        scenario_ids.update(own)
        listed_tasks = re.findall(r"^\| (DEV-\d{2}-\d) \|", content, re.M)
        assert len(listed_tasks) == 3
        document_task_ids.update(listed_tasks)
        dep_line = next(line for line in content.splitlines() if line.startswith("- 实施依赖："))
        assert set(re.findall(r"\[(MOD-\d{2})\]", dep_line)) == set(module["dependsOn"])
        for section in ["范围、责任与依据", "对外接口", "实现机制与不变量", "数据、存储与所有权", "主路径与异常处理", "模块验收场景"]:
            assert section in content, (module["id"], section)
    assert document_task_ids == task_ids and len(scenario_ids) == 63

    for task in tasks:
        assert task["moduleId"] in module_map
        assert task["status"] == "planned" and task["assignedPerson"] is None
        assert task["implementationCommit"] is None and task["verificationRefs"] == []
        assert task["productAcceptance"] is None
        assert set(task["acceptanceScenarios"]) <= scenario_ids
    for req in requirements:
        assert req["moduleId"] in module_map
        assert set(req["implementationTasks"]) <= task_ids
        assert set(req["moduleScenarios"]) <= scenario_ids
        assert req["verificationStatus"] == "notRun" and req["implementationStatus"] == "notImplemented"
        assert req["requirementScenario"]["mappingRole"] == "planned"
        assert (ROOT / req["requirementSource"]).exists()
        assert req["sourceRowId"] == req["requirementId"]
        content = (ROOT / req["moduleDocument"]).read_text(encoding="utf-8")
        assert req["requirementId"] in content, (req["moduleId"], req["requirementId"])

    checked_links = 0
    for path in ROOT.rglob("*.md"):
        content = path.read_text(encoding="utf-8")
        assert content.count("```") % 2 == 0, f"Unclosed fence: {path}"
        for link in re.findall(r"\]\(([^)]+)\)", content):
            if link.startswith(("http:", "https:", "#")):
                continue
            target = path.parent / link.split("#", 1)[0]
            assert target.exists(), f"Broken link: {path.name}: {link}"
            checked_links += 1
    for record in source_map["existing"]:
        path = ROOT / record["path"]
        assert record["mappingRole"] == "existing" and path.is_file()
        content = path.read_text(encoding="utf-8")
        for entry in record["entry"].split(" / "):
            assert entry in content, f"Missing observed entry: {record['path']} / {entry}"
    assert len(source_map["planned"]) == 21
    for record in source_map["planned"]:
        assert record["mappingRole"] == "planned"

    for source in baseline["sources"]:
        assert digest(ROOT / source["path"]) == source["sha256"], f"Source changed: {source['path']}"
    repo = WORKSPACE / "redmaple-agent"
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "-C", str(repo), "status", "--porcelain"], text=True).strip()
    assert head == baseline["productCommit"] and status == "", "Product baseline changed"
    minimum = sum(m["estimatePersonDays"]["min"] for m in modules)
    maximum = sum(m["estimatePersonDays"]["max"] for m in modules)
    assert [minimum, maximum] == [73, 119]
    report = {
        "revision": "MD-1", "validationDate": "2026-11-17",
        "modules": len(modules), "tasks": len(tasks), "requirements": len(requirements),
        "moduleScenarios": len(scenario_ids), "requirementSpecificScenarioPlans": len(requirements),
        "moduleAndTaskDependenciesAcyclic": True, "allRequirementIdsMappedExactlyOnce": True,
        "checkedLocalLinks": checked_links, "existingSourceEntriesChecked": len(source_map["existing"]),
        "inputHashesCurrent": True, "productCommit": head, "productWorkingTreeClean": True,
        "estimatePersonDays": [minimum, maximum], "productTestsRunThisTurn": False,
        "scope": "Document structure and references only. No claim of implementation, scientific validity or product acceptance."
    }
    output = ROOT / "evidence/document-validation.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
