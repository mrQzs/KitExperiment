"""Adapter and subprocess runtime primitives for SentinelMesh.

The canonical identity/lifecycle layer (`LIFECYCLE`, `ALLOWED_TRANSITIONS`,
`validate_argv`, `InvocationSpec`, `ProcessIdentity`, `Lifecycle`,
`classify_start`, `reconcile`, plus the private `_runtime_text`,
`_observation_identity` helpers) is the item-by-item port of the Code-Kit
runtime contract in `.agent/design/runtime-reference/runtime.py.txt`: equal
return values, equal raised `RuntimeErrorCode` codes and equal check order for
every input.  It is import-safe: constructing or classifying these values
performs no process start, no credential read and no filesystem side effect.
The historical `Invocation`/`ProcessResult`/`SubprocessRunner` helpers below
remain unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
import re
import signal
import subprocess
import time
import math
from typing import Any, Mapping, Optional, Protocol, Tuple

from .config import Profile


class RuntimeErrorCode(ValueError):
    """Canonical runtime error carrying a stable machine readable ``code``.

    The textual form is ``CODE: message`` so a log line always names the stable
    code first; ``RuntimeErrorSM`` is retained as the historical alias and stays
    catchable through this class.
    """

    def __init__(self, code: str, message: str = "") -> None:
        if not isinstance(code, str) or not code:
            code = "E_RUNTIME"
        self.code = code
        self.message = message if isinstance(message, str) else str(message)
        super().__init__((f"{code}: {self.message}") if self.message else code)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.code!r}, {self.message!r})"


class RuntimeErrorSM(RuntimeErrorCode):
    """Historical SentinelMesh runtime error; same ``code``/``message`` fields."""


# Canonical lifecycle states: START_UNKNOWN is recoverable and is never terminal.
LIFECYCLE = (
    "NEW",
    "STARTING",
    "STARTED",
    "STOPPING",
    "STOPPED",
    "FAILED",
    "START_UNKNOWN",
)

ALLOWED_TRANSITIONS = {
    "NEW": {"STARTING", "FAILED"},
    "STARTING": {"STARTED", "START_UNKNOWN", "FAILED"},
    "STARTED": {"STOPPING", "FAILED"},
    "STOPPING": {"STOPPED", "FAILED", "START_UNKNOWN"},
    "STOPPED": set(),
    "FAILED": set(),
    "START_UNKNOWN": {"STARTED", "STOPPING", "FAILED"},
}

STATUS_NOT_STARTED = "NOT_STARTED"
STATUS_STARTED = "STARTED"
STATUS_START_UNKNOWN = "START_UNKNOWN"
STATUS_IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
STATUS_FAILED = "FAILED"
STATUS_STOPPED = "STOPPED"

# Runtime records cross a JSON boundary in the scheduler.  Keep counters and
# identifiers within the same exact-integer envelope as the rest of Code-Kit;
# accepting a larger integer would make a later float/JSON round-trip lossy.
_MAX_SAFE_INTEGER = 2**53 - 1


def _runtime_text(value: Any, code: str, field: str) -> str:
    """Validate text fields before hashing or passing them to an adapter."""
    if type(value) is not str or not value or "\x00" in value:
        raise RuntimeErrorCode(code, f"{field} must be non-empty text without NUL")
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF
           for char in value):
        raise RuntimeErrorCode(code, f"{field} contains a control or non-scalar character")
    return value


def validate_argv(command: Any, *, shell: bool = False) -> Tuple[str, ...]:
    """Return the canonical argv tuple, rejecting shell carriers and unsafe text.

    Only the executable slot is constrained to be non-empty and free of CR/LF;
    ordinary arguments may be empty strings or contain newlines.  NUL bytes and
    surrogates are rejected everywhere, and an empty argv is never valid.
    """
    if type(shell) is not bool:
        raise RuntimeErrorCode("E_RUNTIME_SHELL_ARGV", "shell must be boolean")
    if shell:
        raise RuntimeErrorCode("E_RUNTIME_SHELL_ARGV", "shell execution must use a registered shell command contract")
    if not isinstance(command, (list, tuple)) or not command:
        raise RuntimeErrorCode("E_RUNTIME_ARGV", "argv must be a non-empty array")
    result = []
    for index, item in enumerate(command):
        if type(item) is not str or (index == 0 and not item):
            raise RuntimeErrorCode("E_RUNTIME_ARGV", f"argv[{index}] must be text with a non-empty executable")
        if "\x00" in item:
            raise RuntimeErrorCode("E_RUNTIME_ARGV", f"argv[{index}] contains NUL")
        if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
            raise RuntimeErrorCode("E_RUNTIME_ARGV", f"argv[{index}] contains a non-Unicode scalar")
        if index == 0 and ("\r" in item or "\n" in item):
            raise RuntimeErrorCode("E_RUNTIME_ARGV", "executable contains a line break")
        result.append(item)
    return tuple(result)


@dataclass(frozen=True)
class InvocationSpec:
    """Canonical invocation identity: validated on construction, digestable."""

    request_id: str
    run_id: str
    command: tuple[str, ...]
    cwd: str
    environment_sha256: str
    timeout_seconds: int
    shell: bool = False

    def __post_init__(self) -> None:
        _runtime_text(self.request_id, "E_RUNTIME_INVOCATION", "request_id")
        _runtime_text(self.run_id, "E_RUNTIME_INVOCATION", "run_id")
        _runtime_text(self.cwd, "E_RUNTIME_INVOCATION", "cwd")
        if type(self.shell) is not bool:
            raise RuntimeErrorCode("E_RUNTIME_INVOCATION", "shell must be boolean")
        # ``validate_argv`` accepts lists for callers that are still building
        # a command, but an InvocationSpec is the locked runtime snapshot.
        # Store its canonical tuple so mutating the caller's list cannot alter
        # the invocation or its digest after construction.
        object.__setattr__(self, "command", validate_argv(self.command, shell=self.shell))
        if (type(self.environment_sha256) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", self.environment_sha256)):
            raise RuntimeErrorCode("E_RUNTIME_INVOCATION", "environment SHA-256 must be lowercase")
        if (type(self.timeout_seconds) is not int or self.timeout_seconds <= 0
                or self.timeout_seconds > _MAX_SAFE_INTEGER):
            raise RuntimeErrorCode("E_RUNTIME_TIMEOUT", "timeout must be a positive integer")

    def validate(self) -> "InvocationSpec":
        """Return ``self``; construction already validated every field."""
        return self

    def digest(self) -> str:
        """Return the canonical SHA-256 of this specification."""
        payload = "\0".join((self.request_id, self.run_id, *self.command, self.cwd, self.environment_sha256, str(self.timeout_seconds)))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProcessIdentity:
    """Opaque process identity bound to one run; exactly four required fields."""

    pid: int
    boot_id: str
    executable_sha256: str
    worktree_id: str

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0 or self.pid > _MAX_SAFE_INTEGER:
            raise RuntimeErrorCode("E_RUNTIME_PID", "pid must be positive")
        _runtime_text(self.boot_id, "E_RUNTIME_IDENTITY", "boot_id")
        _runtime_text(self.worktree_id, "E_RUNTIME_IDENTITY", "worktree_id")
        if (type(self.executable_sha256) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", self.executable_sha256)):
            raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "executable_sha256 must be lowercase SHA-256")

    def to_dict(self) -> dict[str, Any]:
        """Return the stable mapping form of this identity."""
        return {
            "pid": self.pid,
            "boot_id": self.boot_id,
            "executable_sha256": self.executable_sha256,
            "worktree_id": self.worktree_id,
        }


@dataclass(frozen=True)
class Lifecycle:
    """Immutable lifecycle record; every transition returns a new record."""

    state: str = "NEW"
    generation: int = 0
    identity: ProcessIdentity | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not str or self.state not in LIFECYCLE:
            raise RuntimeErrorCode("E_RUNTIME_TRANSITION", "unknown lifecycle state")
        if (type(self.generation) is not int or self.generation < 0
                or self.generation > _MAX_SAFE_INTEGER):
            raise RuntimeErrorCode("E_RUNTIME_FENCE", "generation must be a non-negative integer")
        if self.identity is not None and not isinstance(self.identity, ProcessIdentity):
            raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "identity must be ProcessIdentity")
        if self.state == "STARTED" and self.identity is None:
            raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "STARTED requires process identity")

    def transition(
        self,
        state: str,
        *,
        identity: Optional[ProcessIdentity] = None,
        expected_generation: Optional[int] = None,
    ) -> "Lifecycle":
        """Return a new record moved to ``state`` with ``generation + 1``."""
        if expected_generation is not None:
            if (type(expected_generation) is not int or expected_generation < 0
                    or expected_generation > _MAX_SAFE_INTEGER
                    or expected_generation != self.generation):
                raise RuntimeErrorCode("E_RUNTIME_FENCE", "lifecycle generation changed")
        if self.state == "START_UNKNOWN" and expected_generation is None:
            raise RuntimeErrorCode("E_RUNTIME_FENCE", "START_UNKNOWN requires a generation fence")
        if state not in LIFECYCLE or state not in ALLOWED_TRANSITIONS[self.state]:
            raise RuntimeErrorCode("E_RUNTIME_TRANSITION", f"invalid transition {self.state}->{state}")
        if identity is not None and not isinstance(identity, ProcessIdentity):
            raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "identity must be ProcessIdentity")
        # Once an OS identity has been observed it is the fence for every
        # subsequent lifecycle transition.  Allowing a caller to replace it
        # while moving through STOPPING/STOPPED (or FAILED) would make a
        # stale/reused PID look like the original process and would erase the
        # evidence needed for conservative recovery.  STARTING and
        # START_UNKNOWN may still attach the first identity when STARTED is
        # confirmed.
        if self.identity is not None and identity is not None and identity != self.identity:
            raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "process identity changed")
        if state == "STARTED" and identity is None:
            raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "STARTED requires process identity")
        if self.generation >= _MAX_SAFE_INTEGER:
            raise RuntimeErrorCode("E_RUNTIME_FENCE", "lifecycle generation exhausted")
        # Test identity presence explicitly; relying on truthiness would let
        # a false-y ProcessIdentity subclass silently discard a newly
        # observed identity and keep the stale one in the lifecycle record.
        return replace(self, state=state, generation=self.generation + 1,
                       identity=identity if identity is not None else self.identity)


def _observation_identity(value: Any) -> ProcessIdentity:
    """Decode the identity attached to a STARTED observation.

    A run/environment pair is not enough to prove that an OS process belongs
    to this invocation.  Accept either the in-memory value used by adapters or
    its closed JSON shape; callers must attach all four identity facts.
    """
    if isinstance(value, ProcessIdentity):
        return value
    if not isinstance(value, Mapping) or set(value) != {"pid", "boot_id", "executable_sha256", "worktree_id"}:
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "process_identity must be a closed object")
    try:
        return ProcessIdentity(**dict(value))
    except RuntimeErrorCode:
        raise
    except (TypeError, ValueError) as exc:
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "process_identity is invalid") from exc


def classify_start(
    *,
    handoff_accepted: bool,
    started_observation: Optional[Mapping[str, Any]],
    expected: InvocationSpec,
) -> str:
    """Classify a start attempt from handoff evidence and a start observation.

    Model text, observer prose and process exit codes are never consulted: only
    the handoff flag and the observation's ``run_id``, ``environment_sha256``,
    ``state`` and ``process_identity`` decide the status.
    """

    if type(expected) is not InvocationSpec:
        raise RuntimeErrorCode("E_RUNTIME_INVOCATION", "expected InvocationSpec is required")
    if type(handoff_accepted) is not bool:
        raise RuntimeErrorCode("E_RUNTIME_OBSERVATION", "handoff_accepted must be boolean")
    if started_observation is not None and not isinstance(started_observation, Mapping):
        raise RuntimeErrorCode("E_RUNTIME_OBSERVATION", "started_observation must be an object")
    if started_observation is None:
        return "START_UNKNOWN" if handoff_accepted else "NOT_STARTED"
    if started_observation.get("run_id") != expected.run_id or started_observation.get("environment_sha256") != expected.environment_sha256:
        return "IDENTITY_MISMATCH"
    observed_state = started_observation.get("state")
    if type(observed_state) is not str:
        return "START_UNKNOWN"
    if observed_state == "NOT_STARTED":
        # A process identity is direct evidence that an OS object existed.
        # Do not let a contradictory NOT_STARTED label authorize a replay;
        # validate the identity and retain the conservative unknown outcome.
        if "process_identity" in started_observation:
            _observation_identity(started_observation["process_identity"])
            return "START_UNKNOWN"
        return "NOT_STARTED"
    if not handoff_accepted:
        return "IDENTITY_MISMATCH"
    if observed_state == "STARTED":
        if "process_identity" not in started_observation:
            return "START_UNKNOWN"
        _observation_identity(started_observation["process_identity"])
        return "STARTED"
    if observed_state in {"FAILED", "STOPPED"}:
        return str(observed_state)
    return "START_UNKNOWN"


def reconcile(expected: InvocationSpec, observed: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the five-key reconciliation record for one expected invocation."""

    if not isinstance(observed, Mapping):
        raise RuntimeErrorCode("E_RUNTIME_OBSERVATION", "observed must be an object")
    status = classify_start(handoff_accepted=True, started_observation=observed, expected=expected)
    terminal_identity = False
    if status in {"FAILED", "STOPPED"} and observed.get("cleanup_confirmed") is True:
        if "process_identity" in observed:
            _observation_identity(observed["process_identity"])
            terminal_identity = True
    return {"status": status, "run_id": expected.run_id, "expected_environment_sha256": expected.environment_sha256,
            "observed_environment_sha256": observed.get("environment_sha256"), "replay_allowed": status == "NOT_STARTED" or (terminal_identity and status in {"FAILED", "STOPPED"})}


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    timeout_seconds: float
    shell: bool = False
    script: str | None = None

    def validate(self) -> None:
        if type(self.shell) is not bool:
            raise RuntimeErrorSM("E_INVOCATION_SHELL")
        if self.shell and (not isinstance(self.script, str) or not self.script.strip()):
            raise RuntimeErrorSM("E_INVOCATION_SHELL_SCRIPT_REQUIRED")
        if self.shell and self.argv:
            raise RuntimeErrorSM("E_INVOCATION_CARRIERS_CONFLICT")
        if self.shell and "\x00" in self.script:
            raise RuntimeErrorSM("E_INVOCATION_SCRIPT")
        if not isinstance(self.argv, tuple):
            raise RuntimeErrorSM("E_INVOCATION_ARGV")
        if not self.shell and (
            not self.argv
            or any(not isinstance(x, str) or not x or "\x00" in x for x in self.argv)
        ):
            raise RuntimeErrorSM("E_INVOCATION_ARGV")
        if not isinstance(self.cwd, str) or not self.cwd or "\x00" in self.cwd:
            raise RuntimeErrorSM("E_INVOCATION_CWD")
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(float(self.timeout_seconds)) or self.timeout_seconds <= 0:
            raise RuntimeErrorSM("E_INVOCATION_TIMEOUT")
        if not os.path.isdir(self.cwd):
            raise RuntimeErrorSM("E_INVOCATION_CWD")
        if not isinstance(self.env, Mapping):
            raise RuntimeErrorSM("E_INVOCATION_ENV")
        for key, value in self.env.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or "\x00" in key
                or "\x00" in value
            ):
                raise RuntimeErrorSM("E_INVOCATION_ENV")


@dataclass(frozen=True)
class ProcessResult:
    started: bool
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stop_reason: str
    duration_seconds: float
    cleanup: str
    pid: int | None = None

    def __post_init__(self) -> None:
        if type(self.started) is not bool or (self.exit_code is not None and type(self.exit_code) is not int):
            raise RuntimeErrorSM("E_PROCESS_RESULT")
        if type(self.stdout) is not bytes or type(self.stderr) is not bytes:
            raise RuntimeErrorSM("E_PROCESS_RESULT")
        if not isinstance(self.stop_reason, str) or not self.stop_reason.strip() or not isinstance(self.cleanup, str) or not self.cleanup.strip():
            raise RuntimeErrorSM("E_PROCESS_RESULT")
        if not isinstance(self.duration_seconds, (int, float)) or isinstance(self.duration_seconds, bool) or not math.isfinite(float(self.duration_seconds)) or self.duration_seconds < 0:
            raise RuntimeErrorSM("E_PROCESS_RESULT")
        if self.pid is not None and (type(self.pid) is not int or self.pid <= 0):
            raise RuntimeErrorSM("E_PROCESS_RESULT")


class Adapter(Protocol):
    adapter_id: str
    def build_invocation(self, profile: Profile, request: Mapping[str, Any]) -> Invocation: ...


@dataclass(frozen=True)
class StaticAdapter:
    adapter_id: str = "static"
    executable: str = "python"

    def build_invocation(self, profile: Profile, request: Mapping[str, Any]) -> Invocation:
        if not isinstance(request, Mapping):
            raise RuntimeErrorSM("E_REQUEST_SHAPE")
        cwd = str(request.get("cwd", os.getcwd()))
        raw_args = request.get("args", ())
        if not isinstance(raw_args, (tuple, list)):
            raise RuntimeErrorSM("E_REQUEST_ARGS")
        if any(not isinstance(x, str) for x in raw_args):
            raise RuntimeErrorSM("E_REQUEST_ARGS")
        args = tuple(raw_args)
        argv = (self.executable,) + profile.fixed_args + args
        raw_env = request.get("env", {})
        if not isinstance(raw_env, Mapping):
            raise RuntimeErrorSM("E_REQUEST_ENV")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in raw_env.items()):
            raise RuntimeErrorSM("E_REQUEST_ENV")
        env = dict(raw_env)
        return Invocation(argv, cwd, env, profile.timeout_seconds)


class SubprocessRunner:
    """Supervise an owned process. Real platform-specific Job Object support remains an adapter extension."""

    def run(self, invocation: Invocation, *, input_bytes: bytes | None = None) -> ProcessResult:
        invocation.validate()
        if input_bytes is not None and type(input_bytes) is not bytes:
            raise RuntimeErrorSM("E_INVOCATION_INPUT")
        started_at = time.monotonic()
        creationflags = 0
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                list(invocation.argv) if not invocation.shell else invocation.script,
                cwd=invocation.cwd,
                env={**os.environ, **dict(invocation.env)},
                shell=invocation.shell,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
                **kwargs,
            )
        except OSError as exc:
            return ProcessResult(False, None, b"", str(exc).encode("utf-8", "replace"), "start_failed", time.monotonic() - started_at, "not_started")
        try:
            stdout, stderr = proc.communicate(input=input_bytes, timeout=invocation.timeout_seconds)
            reason = "completed"
        except subprocess.TimeoutExpired as exc:
            reason = "timeout"
            stdout = exc.output or b""
            stderr = exc.stderr or b""
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(proc.pid, signal.SIGTERM)
            except (OSError, AttributeError):
                proc.terminate()
            try:
                tail_out, tail_err = proc.communicate(timeout=1.0)
                stdout += tail_out or b""
                stderr += tail_err or b""
            except subprocess.TimeoutExpired:
                proc.kill()
                tail_out, tail_err = proc.communicate()
                stdout += tail_out or b""
                stderr += tail_err or b""
                reason = "timeout_killed"
        except KeyboardInterrupt:
            reason = "cancelled"
            proc.terminate()
            stdout, stderr = proc.communicate()
        return ProcessResult(True, proc.returncode, stdout, stderr, reason, time.monotonic() - started_at, "verified", proc.pid)
