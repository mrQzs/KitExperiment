"""Adapter and subprocess runtime primitives for SentinelMesh.

The canonical identity/lifecycle layer (`LIFECYCLE`, `validate_argv`,
`InvocationSpec`, `ProcessIdentity`, `Lifecycle`, `classify_start`, `reconcile`)
keeps the exact public shape of the Code-Kit/SentinelMesh runtime contract.  It is
import-safe: constructing or classifying these values performs no process
start, no credential read and no filesystem side effect.  The historical
`Invocation`/`ProcessResult`/`SubprocessRunner` helpers below remain unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
import signal
import subprocess
import time
import math
from typing import Any, Mapping, Protocol

from .config import Profile
from .core import SHA256_RE, canonical_json, sha256_bytes


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
_TERMINAL_OBSERVED = (STATUS_FAILED, STATUS_STOPPED)

_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f]")


def _is_surrogate(text: str) -> bool:
    """Return True when ``text`` holds an unpaired UTF-16 surrogate code unit."""
    return any(0xD800 <= ord(char) <= 0xDFFF for char in text)


def _opaque_text(value: Any, code: str, name: str) -> str:
    """Validate an opaque identifier: non-empty, no control unit, no surrogate."""
    if not isinstance(value, str) or not value:
        raise RuntimeErrorCode(code, f"{name} must be a non-empty string")
    if _CONTROL_RE.search(value) is not None:
        raise RuntimeErrorCode(code, f"{name} must not contain control characters")
    if _is_surrogate(value):
        raise RuntimeErrorCode(code, f"{name} must not contain surrogates")
    return value


def validate_argv(command: Any, *, shell: bool = False) -> tuple[str, ...]:
    """Return the canonical argv tuple, rejecting shell carriers and unsafe text.

    Only the executable slot is constrained to be non-empty and free of CR/LF;
    ordinary arguments may be empty strings or contain newlines.  NUL bytes and
    surrogates are rejected everywhere, and an empty argv is never valid.
    """
    if type(shell) is not bool:
        raise RuntimeErrorCode("E_ARGV_SHELL", "shell must be a bool")
    if shell:
        raise RuntimeErrorCode("E_ARGV_SHELL", "shell carrier is not supported")
    if not isinstance(command, (list, tuple)):
        raise RuntimeErrorCode("E_ARGV_TYPE", "command must be a list or tuple")
    items = tuple(command)
    if not items:
        raise RuntimeErrorCode("E_ARGV_EMPTY", "argv must not be empty")
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise RuntimeErrorCode("E_ARGV_TYPE", f"argv[{index}] must be a string")
        if "\x00" in item:
            raise RuntimeErrorCode("E_ARGV_NUL", f"argv[{index}] must not contain NUL")
        if _is_surrogate(item):
            raise RuntimeErrorCode(
                "E_ARGV_SURROGATE", f"argv[{index}] must not contain surrogates"
            )
    if not items[0]:
        raise RuntimeErrorCode("E_ARGV_EXECUTABLE", "argv[0] must be a non-empty executable")
    if "\r" in items[0] or "\n" in items[0]:
        raise RuntimeErrorCode("E_ARGV_EXECUTABLE", "argv[0] must not contain CR/LF")
    return items


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
        if type(self.shell) is not bool:
            raise RuntimeErrorCode("E_SPEC_SHELL", "shell must be a bool")
        if self.shell:
            raise RuntimeErrorCode("E_SPEC_SHELL", "shell carrier is not supported")
        object.__setattr__(self, "command", validate_argv(self.command))
        _opaque_text(self.request_id, "E_SPEC_REQUEST_ID", "request_id")
        _opaque_text(self.run_id, "E_SPEC_RUN_ID", "run_id")
        _opaque_text(self.cwd, "E_SPEC_CWD", "cwd")
        if not isinstance(self.environment_sha256, str) or SHA256_RE.fullmatch(
            self.environment_sha256
        ) is None:
            raise RuntimeErrorCode(
                "E_SPEC_ENVIRONMENT", "environment_sha256 must be lowercase 64 hex"
            )
        if type(self.timeout_seconds) is not int or self.timeout_seconds <= 0:
            raise RuntimeErrorCode(
                "E_SPEC_TIMEOUT", "timeout_seconds must be a positive integer"
            )

    def validate(self) -> "InvocationSpec":
        """Return ``self``; construction already validated every field."""
        return self

    def digest(self) -> str:
        """Return the canonical SHA-256 of this specification."""
        return sha256_bytes(
            canonical_json(
                {
                    "request_id": self.request_id,
                    "run_id": self.run_id,
                    "command": list(self.command),
                    "cwd": self.cwd,
                    "environment_sha256": self.environment_sha256,
                    "timeout_seconds": self.timeout_seconds,
                    "shell": self.shell,
                }
            )
        )


@dataclass(frozen=True)
class ProcessIdentity:
    """Opaque process identity bound to one run; exactly four required fields."""

    pid: int
    boot_id: str
    executable_sha256: str
    worktree_id: str

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0:
            raise RuntimeErrorCode("E_IDENTITY_PID", "pid must be a positive integer")
        _opaque_text(self.boot_id, "E_IDENTITY_FIELD", "boot_id")
        _opaque_text(self.worktree_id, "E_IDENTITY_FIELD", "worktree_id")
        if not isinstance(self.executable_sha256, str) or SHA256_RE.fullmatch(
            self.executable_sha256
        ) is None:
            raise RuntimeErrorCode(
                "E_IDENTITY_EXECUTABLE", "executable_sha256 must be lowercase 64 hex"
            )

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
        if self.state not in LIFECYCLE:
            raise RuntimeErrorCode("E_LIFECYCLE_STATE", f"unknown lifecycle state {self.state!r}")
        if type(self.generation) is not int or self.generation < 0:
            raise RuntimeErrorCode(
                "E_LIFECYCLE_GENERATION", "generation must be a non-negative integer"
            )
        if self.identity is not None and not isinstance(self.identity, ProcessIdentity):
            raise RuntimeErrorCode("E_LIFECYCLE_IDENTITY", "identity must be a ProcessIdentity")

    def transition(
        self,
        state: str,
        *,
        identity: ProcessIdentity | None = None,
        expected_generation: int | None = None,
    ) -> "Lifecycle":
        """Return a new record moved to ``state`` with ``generation + 1``."""
        if state not in LIFECYCLE:
            raise RuntimeErrorCode("E_LIFECYCLE_STATE", f"unknown lifecycle state {state!r}")
        if identity is not None and not isinstance(identity, ProcessIdentity):
            raise RuntimeErrorCode("E_LIFECYCLE_IDENTITY", "identity must be a ProcessIdentity")
        if expected_generation is not None:
            if type(expected_generation) is not int:
                raise RuntimeErrorCode(
                    "E_LIFECYCLE_GENERATION", "expected_generation must be an integer"
                )
            if expected_generation != self.generation:
                raise RuntimeErrorCode(
                    "E_LIFECYCLE_GENERATION",
                    f"generation fence {expected_generation} does not match {self.generation}",
                )
        if self.state == "START_UNKNOWN" and expected_generation is None:
            raise RuntimeErrorCode(
                "E_LIFECYCLE_FENCE",
                "recovering from START_UNKNOWN requires expected_generation",
            )
        if state not in ALLOWED_TRANSITIONS[self.state]:
            raise RuntimeErrorCode(
                "E_LIFECYCLE_TRANSITION",
                f"transition {self.state} -> {state} is not allowed",
            )
        if self.identity is not None and identity is not None and self.identity != identity:
            raise RuntimeErrorCode(
                "E_LIFECYCLE_IDENTITY",
                f"identity is already bound to pid {self.identity.pid}",
            )
        bound = self.identity if identity is None else identity
        return Lifecycle(state, self.generation + 1, bound)


def _identity_bound(expected: InvocationSpec, identity: Any) -> bool:
    """Return True when ``identity`` is present and not contradicted by ``expected``."""
    if isinstance(identity, ProcessIdentity):
        return True
    if isinstance(identity, Mapping):
        observed_run = identity.get("run_id")
        if observed_run is not None and observed_run != expected.run_id:
            return False
        observed_env = identity.get("environment_sha256")
        if observed_env is not None and observed_env != expected.environment_sha256:
            return False
        return True
    return False


def classify_start(
    *,
    handoff_accepted: bool,
    started_observation: Mapping[str, Any] | None,
    expected: InvocationSpec,
) -> str:
    """Classify a start attempt from handoff evidence and a start observation.

    Model text, observer prose and process exit codes are never consulted: only
    the handoff flag and the observation's ``run_id``, ``environment_sha256``,
    ``state`` and ``process_identity`` decide the status.
    """
    if not isinstance(expected, InvocationSpec):
        raise RuntimeErrorCode("E_EXPECTED_TYPE", "expected must be an InvocationSpec")
    if type(handoff_accepted) is not bool:
        raise RuntimeErrorCode("E_HANDOFF_TYPE", "handoff_accepted must be a bool")
    if started_observation is None:
        observation: Mapping[str, Any] = {}
    elif isinstance(started_observation, Mapping):
        observation = started_observation
    else:
        raise RuntimeErrorCode("E_OBSERVATION_TYPE", "started_observation must be a mapping")
    if not observation:
        return STATUS_START_UNKNOWN if handoff_accepted else STATUS_NOT_STARTED
    observed_run = observation.get("run_id")
    observed_env = observation.get("environment_sha256")
    if observed_run != expected.run_id or observed_env != expected.environment_sha256:
        return STATUS_IDENTITY_MISMATCH
    state = observation.get("state")
    identity = observation.get("process_identity")
    if state == STATUS_NOT_STARTED:
        return STATUS_START_UNKNOWN if identity else STATUS_NOT_STARTED
    if state == STATUS_STARTED:
        if identity is None:
            return STATUS_START_UNKNOWN
        return STATUS_STARTED if _identity_bound(expected, identity) else STATUS_START_UNKNOWN
    if state in _TERMINAL_OBSERVED:
        return state
    return STATUS_START_UNKNOWN


def reconcile(expected: InvocationSpec, observed: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the five-key reconciliation record for one expected invocation."""
    if not isinstance(expected, InvocationSpec):
        raise RuntimeErrorCode("E_EXPECTED_TYPE", "expected must be an InvocationSpec")
    if observed is None:
        observation: Mapping[str, Any] = {}
    elif isinstance(observed, Mapping):
        observation = observed
    else:
        raise RuntimeErrorCode("E_OBSERVATION_TYPE", "observed must be a mapping")
    handoff = observation.get("handoff_accepted", False)
    if type(handoff) is not bool:
        raise RuntimeErrorCode("E_HANDOFF_TYPE", "handoff_accepted must be a bool")
    status = classify_start(
        handoff_accepted=handoff,
        started_observation=observation,
        expected=expected,
    )
    identity = observation.get("process_identity")
    confirmed = observation.get("cleanup_confirmed", False) is True
    replay_allowed = status == STATUS_NOT_STARTED or (
        status in _TERMINAL_OBSERVED and _identity_bound(expected, identity) and confirmed
    )
    observed_env = observation.get("environment_sha256")
    return {
        "status": status,
        "run_id": expected.run_id,
        "expected_environment_sha256": expected.environment_sha256,
        "observed_environment_sha256": observed_env if isinstance(observed_env, str) else None,
        "replay_allowed": bool(replay_allowed),
    }


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
