"""Initialise the UKB store used by MDagent and create the two administrator identities.

  python tools/kb/bootstrap.py --admin <your-id> [--data-root DIR]

- data root defaults to MDagent's own paths.data_root() (env MDSIM_DATA_ROOT overrides);
  the UKB store lives in <data_root>/ukb (MDAGENT_UKB_ROOT overrides).
- creates the human administrator (prints its token once) and the system administrator
  ``mdagent-system`` whose token is written to <store>/system_auto.token for the machine
  observation path (option A). Keep that file readable only by the service account.
"""
import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "mcp"))

from evolution.kb import adapter  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin", required=True, help="human administrator id")
    ap.add_argument("--data-root")
    ap.add_argument("--force", action="store_true", help="re-create the system token if the store already exists")
    a = ap.parse_args()
    root = adapter.ukb_root(a.data_root or adapter.data_root())
    packs = [os.path.join(REPO, "ukb", "data", "seed"), os.path.join(REPO, "data", "domain", "mdagent_legacy")]
    api = adapter._ukb_module()
    if not os.path.exists(os.path.join(root, "ukb.sqlite")):
        kb = api.UKB.init(root, packs=packs, actor="bootstrap:mdagent")
        human = kb.add_admin(a.admin, "bootstrap")
        system = kb.add_admin(adapter.SYSTEM_ADMIN, "bootstrap")
        kb.close()
        print("initialised", root)
        print("admin", a.admin, "token", human)
    else:
        kb = api.UKB(root)
        try:
            system = kb.reset_token(adapter.SYSTEM_ADMIN, "bootstrap") if a.force else None
            if system is None:
                print("store exists; use --force to rotate the system token", file=sys.stderr)
        except Exception:
            system = kb.add_admin(adapter.SYSTEM_ADMIN, "bootstrap")
        kb.close()
        print("store", root)
        print("to create or rotate a human token: python -m ukb --root %s admin-add <id>" % root)
    if system:
        path = os.path.join(root, "system_auto.token")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(system + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        print("system token written to", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
