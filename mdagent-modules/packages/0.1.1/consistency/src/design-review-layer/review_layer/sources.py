"""Pluggable sources for experience and evidence.

The layer only *reads* from these sources. Default is Null (libraries not yet
connected, per owner instruction); when the experience library / evidence base
is ready, switch the type in layer_config.json — no code change in the engine.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


class ExperienceSource(Protocol):
    def lookup(self, query: dict) -> list[dict]: ...


class EvidenceSource(Protocol):
    def lookup(self, refs: list[str]) -> list[dict]: ...


class NullExperienceSource:
    def lookup(self, query: dict) -> list[dict]:
        return []


class NullEvidenceSource:
    def lookup(self, refs: list[str]) -> list[dict]:
        return []


class SeedExperienceSource:
    """Read-only reader for MDagent seed packs (mcp/review_experience/*.json).

    Skips packs with consumer=evidence (those belong to the evidence system)
    and entries with status=revoked. Keyword match on statement/conditions.
    """

    def __init__(self, pack_dir: str):
        self.pack_dir = Path(pack_dir)
        self._entries = self._load()

    def _load(self) -> list[dict]:
        out = []
        for p in sorted(self.pack_dir.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("consumer") == "evidence":
                continue
            for e in d.get("entries") or []:
                if e.get("status") == "revoked":
                    continue
                e = dict(e)
                e["_pack"] = d.get("pack") or p.stem
                out.append(e)
        return out

    def lookup(self, query: dict) -> list[dict]:
        terms = [str(v).lower() for v in query.values() if v]
        hits = []
        for e in self._entries:
            text = (str(e.get("statement", "")) + " " + json.dumps(e.get("conditions", {}), ensure_ascii=False)).lower()
            score = sum(1 for t in terms if t and t in text)
            if score:
                hits.append(dict(e, _score=score))
        hits.sort(key=lambda x: (-x.get("_score", 0), str(x.get("id"))))
        return hits[:24]


class DataRootEvidenceSource:
    """Read-only reader for MDagent data root (atoms/scenarios). Not enabled yet."""

    def __init__(self, data_root: str):
        self.root = Path(data_root)

    def lookup(self, refs: list[str]) -> list[dict]:
        out = []
        atoms = self.root / "experience" / "atoms.jsonl"
        if atoms.exists():
            wanted = set(refs)
            for line in atoms.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not wanted or d.get("id") in wanted:
                    out.append(d)
        return out


def build_sources(cfg: dict):
    exp = cfg.get("experience") or {"type": "null"}
    evi = cfg.get("evidence") or {"type": "null"}
    experience = (SeedExperienceSource(exp["path"]) if exp.get("type") == "mdagent_seed"
                  else NullExperienceSource())
    evidence = (DataRootEvidenceSource(evi["path"]) if evi.get("type") == "mdagent_data"
                else NullEvidenceSource())
    return experience, evidence
