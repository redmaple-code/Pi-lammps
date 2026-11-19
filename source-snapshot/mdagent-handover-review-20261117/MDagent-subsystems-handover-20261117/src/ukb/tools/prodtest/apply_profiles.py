"""Apply the seed's agent profiles to an existing KB through the registry review path (admin approves).

  python -m tools.prodtest.apply_profiles <kb_root> --admin <id> --token <T>
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)

from ukb.api import UKB  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--admin", required=True)
    ap.add_argument("--token", required=True)
    a = ap.parse_args()
    with open(os.path.join(HERE, "data", "seed", "profile.json"), encoding="utf-8") as f:
        profiles = json.load(f)
    kb = UKB(a.root)
    try:
        for p in profiles:
            r = kb.propose_registry("profile", p, "staged disclosure limits", "cli:" + a.admin)
            v = kb.review_view(r["task_id"])
            kb.decide(r["task_id"], a.admin, a.token, "approve", "profile update", v["bound_hash"], "cli")
            print(p["profile_id"], kb.profile(p["profile_id"]))
    finally:
        kb.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
