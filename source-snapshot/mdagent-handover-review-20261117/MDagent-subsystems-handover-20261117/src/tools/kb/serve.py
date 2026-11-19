"""Run the UKB review console (admin approves the migrated candidates) on MDagent's UKB store.

  python tools/kb/serve.py [--data-root DIR] [--host 127.0.0.1] [--port 8765]

Log in with the administrator id and token created by bootstrap.py / `python -m ukb admin-add`.
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
    ap.add_argument("--data-root")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    root = adapter.ukb_root(a.data_root)
    api = adapter._ukb_module()
    from ukb.webui import serve
    httpd, _ = serve(root, a.host, a.port)
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write("warning: console bound to a non-local address; put it behind TLS\n")
    sys.stderr.write("UKB review console for %s on http://%s:%d\n" % (root, a.host, a.port))
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
