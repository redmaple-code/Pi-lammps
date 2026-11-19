"""Deadline for a trusted, synchronous, same-identity local execution bridge.

The bridge must wait for ALL descendants and must not detach, change identity,
submit remote/background work, or escape the POSIX process group. This is a wall
clock process-tree deadline, not a memory/CPU sandbox or containment for hostile
programs. Windows taskkill needs the live bridge root to discover its tree; root
exit before cleanup is therefore uncertain, never a confirmed timeout cleanup.
"""
import math
import os
import signal
import subprocess
import time


class BridgeExecutionError(RuntimeError):
    """No trustworthy completion receipt; callers must retain START_UNKNOWN."""

    def __init__(self, code, *, timed_out=False, tree_terminated=False,
                 bridge_reaped=False, detail=""):
        super().__init__(code)
        self.code = code
        self.timed_out = timed_out
        self.tree_terminated = tree_terminated
        self.bridge_reaped = bridge_reaped
        self.detail = detail

    def as_dict(self):
        return {"error": self.code, "timed_out": self.timed_out,
                "tree_terminated": self.tree_terminated,
                "bridge_reaped": self.bridge_reaped,
                "execution_state": "START_UNKNOWN", "fail_closed": True,
                "detail": self.detail}


def _taskkill_path():
    # Resolve the OS utility without trusting PATH, cwd, or caller environment.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    count = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not count or count >= len(buffer):
        return None
    path = os.path.join(buffer.value, "taskkill.exe")
    return path if os.path.isfile(path) else None


def _terminate_tree(proc, taskkill, cleanup_timeout):
    """Attempt tree termination BEFORE any root-only kill; never raise."""
    try:
        if os.name == "nt":
            # poll only observes; do not kill/reap the root before tree discovery.
            root_alive = proc.poll() is None
            result = subprocess.run(
                [taskkill, "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=cleanup_timeout, shell=False,
            )
            if root_alive and result.returncode == 0:
                return True, "taskkill /T /F completed"
            return False, "tree discovery uncertain (root exited or taskkill failed)"
        os.killpg(proc.pid, signal.SIGKILL)
        return True, "SIGKILL delivered to bridge process group"
    except Exception as exc:
        return False, "tree termination failed: " + type(exc).__name__


def _cleanup(proc, taskkill, cleanup_timeout):
    terminated, detail = _terminate_tree(proc, taskkill, cleanup_timeout)
    try:
        # Bounded even if an unsupported escaped descendant retained pipe handles.
        proc.communicate(timeout=cleanup_timeout)
        return terminated, True, detail
    except Exception as exc:
        detail += "; pipe drain failed: " + type(exc).__name__
    # Only AFTER the tree attempt, make a best effort to reap the bridge itself.
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=cleanup_timeout)
        reaped = True
    except Exception as exc:
        reaped = False
        detail += "; bridge reap failed: " + type(exc).__name__
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(proc, name, None)
        reader = getattr(proc, name + "_thread", None)
        # Windows communicate readers may still own a blocked pipe if an
        # unsupported escaped process inherited it. close() would block too.
        if stream is not None and not (reader and reader.is_alive()):
            try:
                stream.close()
            except Exception:
                pass
    return False, reaped, detail


def run_bridge(argv, *, timeout, env=None, cleanup_timeout=5):
    """Return CompletedProcess only after normal synchronous bridge completion.

    Timeout always raises, including confirmed tree termination: killing work is
    not a completion/audit receipt. Cleanup failure must never become success.
    Cleanup has its own bounded grace period after the execution deadline.
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0
            or isinstance(cleanup_timeout, bool)
            or not isinstance(cleanup_timeout, (int, float))
            or not math.isfinite(cleanup_timeout) or cleanup_timeout <= 0):
        raise BridgeExecutionError("bridge-invalid-deadline")
    if os.name not in {"nt", "posix"}:
        raise BridgeExecutionError("bridge-platform-unsupported")
    try:
        taskkill = _taskkill_path() if os.name == "nt" else None
    except Exception as exc:
        raise BridgeExecutionError("bridge-tree-termination-unavailable",
                                   detail=type(exc).__name__) from exc
    if os.name == "nt" and not taskkill:
        raise BridgeExecutionError("bridge-tree-termination-unavailable")
    options = {"start_new_session": True} if os.name == "posix" else {}
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace", env=env,
            shell=False, **options,
        )
    except Exception as exc:
        raise BridgeExecutionError("bridge-spawn-failed", detail=type(exc).__name__) from exc
    try:
        stdout, stderr = proc.communicate(timeout=max(0, deadline - time.monotonic()))
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        terminated, reaped, detail = _cleanup(proc, taskkill, cleanup_timeout)
        code = "bridge-deadline-exceeded" if terminated and reaped else "bridge-termination-unknown"
        raise BridgeExecutionError(code, timed_out=True,
                                   tree_terminated=terminated, bridge_reaped=reaped,
                                   detail=detail) from exc
    except BaseException as exc:
        terminated, reaped, detail = _cleanup(proc, taskkill, cleanup_timeout)
        if not isinstance(exc, Exception):
            raise
        raise BridgeExecutionError("bridge-communication-failed",
                                   tree_terminated=terminated, bridge_reaped=reaped,
                                   detail=detail) from exc
