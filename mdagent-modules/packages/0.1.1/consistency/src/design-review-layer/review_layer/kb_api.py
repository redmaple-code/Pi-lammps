"""Compatibility wrapper for the generic knowledge-base interface.

The implementation lives in the knowledge-base module itself:
``evolution/kb/client.py``. This wrapper locates that tree whether the review
layer sits beside the repository (development workspace) or inside it (this
repository), then re-exports the public names.
"""
import sys
from pathlib import Path

_LAYER = Path(__file__).resolve().parents[1]
_CANDIDATES = (_LAYER, _LAYER.parent, _LAYER.parent / "MDagent-evidence-v3")
for _candidate in _CANDIDATES:
    if (_candidate / "evolution" / "kb" / "client.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from evolution.kb.client import (  # noqa: E402,F401
    KBError,
    KnowledgeBase,
    ensure_admin_token,
    ensure_system_admin,
    token_file,
)

__all__ = ["KBError", "KnowledgeBase", "ensure_admin_token", "ensure_system_admin", "token_file"]
