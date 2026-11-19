"""Registered static command adapters; all launches use the injected audit bridge.

Registrations are trusted server DATA, never loaded executable knowledge:
{id, executable:absolute-path, executable_sha256, version:{args,contains,returncode},
 kinds:[...], command:[exact argv including executable], entrypoints:[relative],
 grammar:[{prefix:[exact tokens], roles:[word|number|static|input|include|output],
           repeat?:word|number|static}], environment:{}}.

Unknown commands, interpolation, shell syntax and dynamic dependencies are
UNSUPPORTED. A registration describes a deliberately bounded static language,
not a claim to parse arbitrary programs. Input paths are cwd-relative. Outputs
are relative to the isolated output cwd; {output:relative} argv tokens work too.
The output workspace contains read-only verified copies of the frozen closure;
original frozen inputs remain separate and are rechecked after execution.
"""
import hashlib
import math
import os
import re
import shlex
import uuid
from pathlib import Path, PureWindowsPath

from evidence_v3.contracts import V3Error, atomic_json, canonical_hash, read_json
from evidence_v3.execution import file_hash, inspect_inputs, manifest_equal


def _need(condition, detail):
    if not condition:
        raise V3Error("UNSUPPORTED", detail)


def _relative(raw):
    _need(isinstance(raw, str) and bool(raw) and "\\" not in raw and ":" not in raw and "\0" not in raw, "non-static path")
    _need(not Path(raw).is_absolute() and not PureWindowsPath(raw).is_absolute() and all(part not in {"", ".", ".."} for part in raw.split("/")), "path escapes workspace")
    return raw


def _finite_number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _audited(payload):
    _need(isinstance(payload, dict) and bool(payload.get("audit_id")) and payload.get("whitelisted") is True and not payload.get("error"), "audit bridge did not certify execution")
    return payload


def _command_text(argv):
    # Local audited bridge matches the exact executable prefix before shlex.
    # Keep that prefix unquoted; quote only argv tails for its POSIX parser.
    _need(isinstance(argv[0], str) and "\n" not in argv[0] and "\r" not in argv[0], "invalid executable path")
    return argv[0] + (" " + shlex.join(argv[1:]) if len(argv) > 1 else "")


def _pid_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def build_registration(profile, executable, command, *, kinds, registration_id=None):
    """Bind data-only grammar to one locally fingerprinted binary and argv.

    `profile` must be a trusted file/config dictionary, not a record loaded from
    review_experience. This validates structure only; no content is executed.
    """
    _need(isinstance(profile, dict) and isinstance(profile.get("id"), str) and profile["id"], "invalid data profile")
    allowed = {"id", "version_args", "version_contains", "version_returncode", "grammar", "entrypoints", "entrypoint_option"}
    _need(set(profile) <= allowed and {"id", "version_args", "version_contains", "version_returncode", "grammar"} <= set(profile), "invalid data profile fields")
    _need(("entrypoints" in profile) != ("entrypoint_option" in profile), "profile must declare explicit or flag-bound entrypoints")
    path = Path(executable)
    _need(path.is_absolute() and path.is_file() and not path.is_symlink(), "configured executable unavailable")
    _need(isinstance(command, list) and command and all(isinstance(arg, str) and arg and "\0" not in arg for arg in command), "invalid registered argv")
    _need(Path(command[0]).resolve() == path.resolve(), "argv executable mismatch")
    _need(isinstance(kinds, list) and kinds and all(kind in {"production", "validation", "build", "analyze"} for kind in kinds), "invalid registered action kinds")
    _need(isinstance(profile["version_args"], list) and all(isinstance(arg, str) for arg in profile["version_args"]), "invalid version probe")
    _need(isinstance(profile["version_contains"], str) and bool(profile["version_contains"]) and type(profile["version_returncode"]) is int, "invalid version expectation")
    _need(isinstance(profile["grammar"], list) and bool(profile["grammar"]), "incomplete static grammar")
    if "entrypoints" in profile:
        entrypoints = profile["entrypoints"]
        _need(isinstance(entrypoints, list) and bool(entrypoints) and all(isinstance(item, str) and item for item in entrypoints), "entrypoints required")
    else:
        option = profile["entrypoint_option"]
        _need(isinstance(option, str) and bool(option), "entrypoint option required")
        positions = [index for index, token in enumerate(command[:-1]) if token == option]
        _need(len(positions) == 1, "registered command must bind one entrypoint option")
        entrypoints = [command[positions[0] + 1]]
        _need(bool(entrypoints[0]) and not entrypoints[0].startswith("{"), "entrypoint option must bind a static path")
    _need(isinstance(registration_id, str) and registration_id if registration_id is not None else True, "invalid registration id")
    return {"id": registration_id or profile["id"], "executable": str(path.resolve()), "executable_sha256": file_hash(path),
            "version": {"args": list(profile["version_args"]), "contains": profile["version_contains"], "returncode": profile["version_returncode"]},
            "kinds": list(kinds), "command": list(command), "entrypoints": list(entrypoints),
            "grammar": profile["grammar"], "environment": {}}


class ControlledExecutionAdapter:
    """Callable authorization adapter plus .launch(frozen_spec).

    bridge_exec is md_mcp._bridge_exec; no solver subprocess is spawned here.
    The bridge must support synchronous exec and its timeout contract. Unsupported
    resource limits are rejected rather than asserted. A failed/timeout bridge
    call remains uncertain to TaskService, and must never be retried blindly.
    """
    def __init__(self, registrations, bridge_exec, *, worker_launcher=None):
        _need(isinstance(registrations, list) and callable(bridge_exec), "adapter registration/bridge required")
        self.registrations = registrations
        self.bridge_exec = bridge_exec
        self.worker_launcher = worker_launcher

    def _registration(self, spec):
        matches = [row for row in self.registrations if isinstance(row, dict) and row.get("command") == spec.get("command") and spec.get("kind") in row.get("kinds", [])]
        _need(len(matches) == 1, "no unique registered command")
        row = matches[0]
        path = Path(row.get("executable", ""))
        _need(path.is_absolute() and path.is_file() and not path.is_symlink() and str(path) == spec["command"][0], "executable path is not pinned")
        _need(file_hash(path) == row.get("executable_sha256"), "executable fingerprint changed")
        limits = spec.get("limits", {})
        _need(set(limits) == {"timeout_seconds"} and type(limits["timeout_seconds"]) is int and 1 <= limits["timeout_seconds"] <= 330, "unsupported resource limits")
        _need(spec.get("environment", {}) == row.get("environment", {}), "unregistered environment")
        _need(not spec.get("environment"), "bridge environment overrides are not certified")
        return row

    def _closure(self, row, spec):
        projection = inspect_inputs(spec["cwd"], spec["inputs"])
        by_path = {entry["path"]: entry for entry in projection}
        visited, active, outputs = set(), set(), set()
        grammar = row.get("grammar")
        _need(isinstance(grammar, list) and bool(grammar), "static grammar required")

        def dependency(raw, recursive=False):
            path = _relative(raw)
            _need(path in by_path, "undeclared dependency: " + path)
            if not recursive:
                visited.add(path)
                return
            _need(path not in active, "recursive include cycle")
            if path in visited:
                return
            active.add(path)
            for line in by_path[path]["content"].splitlines():
                try:
                    tokens = shlex.split(line, comments=True, posix=True)
                except ValueError as exc:
                    raise V3Error("UNSUPPORTED", "non-static script syntax") from exc
                if not tokens:
                    continue
                # No interpolation, continuation, inline commands or shell escape.
                _need(not any(any(char in token for char in ("$", "`", ";", "|", "&", "\\", "\n")) for token in tokens), "dynamic script semantics")
                rules = [rule for rule in grammar if isinstance(rule, dict) and isinstance(rule.get("prefix"), list) and tokens[:len(rule["prefix"])] == rule["prefix"]]
                _need(len(rules) == 1, "unknown or ambiguous script command")
                rule = rules[0]
                tail = tokens[len(rule["prefix"]):]
                roles = rule.get("roles", [])
                _need(isinstance(roles, list) and len(tail) >= len(roles), "script arity")
                if len(tail) > len(roles):
                    _need(rule.get("repeat") in {"word", "number", "static"}, "unknown script arguments")
                    roles = roles + [rule["repeat"]] * (len(tail) - len(roles))
                for token, role in zip(tail, roles):
                    if role in {"input", "include"}:
                        dependency(token, role == "include")
                    elif role == "output":
                        outputs.add(_relative(token))
                    elif role == "number":
                        try:
                            _need(math.isfinite(float(token)), "invalid numeric argument")
                        except ValueError as exc:
                            raise V3Error("UNSUPPORTED", "non-static number") from exc
                    elif role == "word":
                        _need(bool(re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", token)), "unclassified path or expression")
                    elif role == "static":
                        _need(bool(re.fullmatch(r"[A-Za-z0-9_+*/-]+", token)) or _finite_number(token), "dynamic or path-like argument")
                    else:
                        raise V3Error("UNSUPPORTED", "unknown script argument role")
            active.remove(path)
            visited.add(path)

        entrypoints = row.get("entrypoints")
        _need(isinstance(entrypoints, list) and bool(entrypoints), "script entrypoints required")
        for entry in entrypoints:
            dependency(entry, True)
        _need(visited == set(by_path), "manifest is not the complete exact dependency closure")
        for argument in spec["command"][1:]:
            if argument.startswith("{output:") and argument.endswith("}"):
                outputs.add(_relative(argument[8:-1]))
            elif argument in by_path:
                continue
            else:
                _need(bool(re.fullmatch(r"[-A-Za-z0-9_+.]+", argument)), "unclassified command path or dynamic argument")
        for output in outputs:
            _need(not any(output.casefold() == entry.casefold() or output.casefold().startswith(entry.casefold() + "/") or entry.casefold().startswith(output.casefold() + "/") for entry in visited), "input/output collision")
        _need(bool(outputs), "explicit output locations required")
        return {"inputs": [{"path": name, "sha256": by_path[name]["sha256"]} for name in sorted(visited)], "outputs": sorted(outputs)}

    def __call__(self, spec, context):
        row = self._registration(spec)
        closure = self._closure(row, spec)
        version = row.get("version", {})
        _need(isinstance(version.get("args"), list) and all(isinstance(arg, str) for arg in version["args"]) and isinstance(version.get("contains"), str) and bool(version["contains"]), "version probe required")
        version_timeout = min(spec["limits"]["timeout_seconds"], 30)
        payload = _audited(self.bridge_exec({"cmd": _command_text([row["executable"]] + version["args"]), "cwd": spec["cwd"], "description": "registered executable version probe", "background": False, "timeout_seconds": version_timeout}))
        _need(payload.get("timeout_enforced") is True and type(payload.get("timeout_seconds")) is int and payload["timeout_seconds"] == version_timeout, "audit bridge did not certify the requested version-probe deadline")
        _need(type(payload.get("returncode")) is int and payload["returncode"] == version.get("returncode", 0) and version["contains"] in (payload.get("stdout", "") + payload.get("stderr", "")), "executable version probe mismatch")
        return {"kind": spec["kind"], "supported": True, "dependencies_complete": True, "constraints_enforced": True, "adapter_id": row["id"], "registration_hash": canonical_hash(row), "executable": row["executable"], "executable_sha256": row["executable_sha256"], "version_audit_id": payload["audit_id"], "version": version["contains"], "closure": closure, "limitations": ["Only registered static script commands are supported.", "Synchronous timeout only; no general process sandbox or memory limit."]}

    @staticmethod
    def _journal_location(frozen_spec):
        output = Path(frozen_spec.get("output_dir", ""))
        _need(output.is_absolute() and output.is_dir() and not output.is_symlink(), "output journal workspace unavailable")
        output = output.resolve()
        _need(output.parent.name == "outputs" and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", output.name)), "invalid output journal location")
        task_dir = output.parent.parent
        current = output
        while current != task_dir:
            _need(not current.is_symlink() and not (hasattr(current, "is_junction") and current.is_junction()), "unsafe output journal location")
            current = current.parent
        return task_dir, output, task_dir / "handoffs" / (output.name + ".json")

    def handoff(self, frozen_spec):
        """Persist one launch identity, then dispatch synchronous work off-lock."""
        row = self._registration(frozen_spec)
        task_dir, output, journal_path = self._journal_location(frozen_spec)
        registration_hash = canonical_hash(row)
        spec_hash = canonical_hash(frozen_spec)
        launch_id = "handoff-" + uuid.uuid4().hex
        journal = {"schema": 1, "state": "starting", "task_id": task_dir.name,
                   "permit_id": output.name, "launch_id": launch_id,
                   "registration_hash": registration_hash, "spec_hash": spec_hash,
                   "output_dir": str(output)}
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        job_path = journal_path.with_name(journal_path.stem + ".job.json")
        try:
            descriptor = os.open(journal_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise V3Error("HANDOFF_ALREADY_EXISTS") from exc
        else:
            os.close(descriptor)
        atomic_json(journal_path, journal)
        atomic_json(job_path, {"launch_id": launch_id, "frozen_spec": frozen_spec,
                               "registrations": self.registrations})
        try:
            if not callable(self.worker_launcher):
                raise V3Error("DURABLE_WORKER_UNAVAILABLE")
            worker = self.worker_launcher(job_path)
            if not isinstance(worker, dict) or worker.get("started") is not True:
                raise V3Error("WORKER_HANDOFF_UNCERTAIN")
            if "pid" in worker:
                _need(type(worker["pid"]) is int and worker["pid"] > 0, "invalid worker process identity")
        except Exception as exc:
            try:
                atomic_json(journal_path, {**journal, "state": "unknown",
                    "error_code": getattr(exc, "code", type(exc).__name__)})
            except Exception:
                pass
            raise V3Error("HANDOFF_UNKNOWN", type(exc).__name__) from exc
        if type(worker.get("pid")) is int:
            current = read_json(journal_path)
            atomic_json(journal_path, {**current, "worker_pid": worker["pid"]})
        launch = {"started": True, "launch_id": launch_id,
                  "registration_hash": registration_hash, "execution_complete": False}
        if type(worker.get("pid")) is int:
            launch["pid"] = worker["pid"]
        return launch

    def reconcile(self, run, receipt):
        """Read only the server-derived journal; never starts or retries work."""
        _need(isinstance(run, dict) and isinstance(receipt, dict), "invalid reconciliation request")
        frozen_spec = run.get("frozen_spec")
        _need(isinstance(frozen_spec, dict), "frozen execution specification unavailable")
        row = self._registration(frozen_spec)
        task_dir, output, journal_path = self._journal_location(frozen_spec)
        spec_hash = canonical_hash(frozen_spec)
        registration_hash = canonical_hash(row)
        journal = read_json(journal_path)
        _need(isinstance(journal, dict) and journal.get("schema") == 1 and
              journal.get("task_id") == run.get("task_id") == task_dir.name and
              journal.get("permit_id") == run.get("permit_id") == output.name and
              journal.get("output_dir") == str(output) and
              journal.get("spec_hash") == spec_hash and
              journal.get("registration_hash") == registration_hash and
              isinstance(journal.get("launch_id"), str) and bool(journal["launch_id"]),
              "handoff journal binding mismatch")
        prior_launch = run.get("launch")
        expected_launch = receipt.get("launch_id") or (
            prior_launch.get("launch_id") if isinstance(prior_launch, dict) else None)
        _need(expected_launch is None or expected_launch == journal["launch_id"], "handoff launch identity mismatch")
        if journal["state"] == "starting":
            if not _pid_alive(journal.get("worker_pid")):
                raise V3Error("START_UNKNOWN")
            return {"run_id": run["id"], "started": True, "launch_id": journal["launch_id"],
                    "registration_hash": registration_hash, "execution_complete": False}
        _need(journal["state"] == "complete" and isinstance(journal.get("result"), dict),
              "START_UNKNOWN" if journal.get("state") == "unknown" else "invalid handoff journal")
        result = journal["result"]
        _need(result.get("started") is True and result.get("execution_complete") is True and
              type(result.get("returncode")) is int and result.get("registration_hash") == registration_hash,
              "invalid completed handoff journal")
        closure = self._closure(row, frozen_spec)
        expected_outputs = set(closure["outputs"])
        actual_outputs = set()
        outputs = result.get("outputs")
        _need(isinstance(outputs, list), "completed handoff outputs unavailable")
        for entry in outputs:
            _need(isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(entry.get("sha256"), str), "invalid handoff output")
            path = Path(entry["path"])
            _need(path.is_absolute() and not path.is_symlink() and path.resolve().is_relative_to(output), "unsafe handoff output")
            name = path.resolve().relative_to(output).as_posix()
            _need(name in expected_outputs and name not in actual_outputs and file_hash(path) == entry["sha256"], "handoff output verification failed")
            current = path
            while current != output:
                _need(not current.is_symlink() and not (hasattr(current, "is_junction") and current.is_junction()), "unsafe handoff output")
                current = current.parent
            actual_outputs.add(name)
        _need(actual_outputs == expected_outputs, "handoff output set mismatch")
        return {"run_id": run["id"], "started": True, "launch_id": journal["launch_id"],
                "registration_hash": registration_hash, "execution_complete": True,
                "returncode": result["returncode"], "outputs": outputs}

    def launch(self, frozen_spec):
        row = self._registration(frozen_spec)
        closure = self._closure(row, frozen_spec)
        root = Path(frozen_spec["cwd"]).resolve()
        output = Path(frozen_spec["output_dir"])
        _need(output.is_absolute() and output.is_dir() and not output.is_symlink(), "output workspace required")
        output = output.resolve()
        _need(output != root and not output.is_relative_to(root) and not root.is_relative_to(output), "input/output workspace overlap")
        _need(not any(output.iterdir()), "output workspace must be empty")
        for entry in closure["inputs"]:
            destination = output / entry["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = (root / entry["path"]).read_bytes()
            _need(hashlib.sha256(payload).hexdigest() == entry["sha256"], "frozen input changed")
            with open(destination, "xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            destination.chmod(0o444)
        command = [str(output / arg[8:-1]) if arg.startswith("{output:") and arg.endswith("}") else arg for arg in frozen_spec["command"]]
        payload = _audited(self.bridge_exec({"cmd": _command_text(command), "cwd": str(output), "description": "registered frozen task action", "background": False, "timeout_seconds": frozen_spec["limits"]["timeout_seconds"]}))
        _need(payload.get("timeout_enforced") is True and type(payload.get("timeout_seconds")) is int and payload["timeout_seconds"] == frozen_spec["limits"]["timeout_seconds"], "audit bridge did not certify the requested execution deadline")
        _need(type(payload.get("returncode")) is int, "completion receipt missing")
        for entry in closure["inputs"]:
            _need(file_hash(root / entry["path"]) == entry["sha256"] and file_hash(output / entry["path"]) == entry["sha256"], "executed input changed")
        actual = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
        _need(actual <= {row["path"] for row in closure["inputs"]} | set(closure["outputs"]), "undeclared runtime output")
        outputs = []
        for name in closure["outputs"]:
            path = output / name
            _need(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(output), "declared output missing or unsafe")
            outputs.append({"path": str(path.resolve()), "sha256": file_hash(path)})
        return {"started": True, "execution_complete": True, "returncode": payload["returncode"], "launch_id": payload["audit_id"], "audit_id": payload["audit_id"], "outputs": outputs, "registration_hash": canonical_hash(row)}
