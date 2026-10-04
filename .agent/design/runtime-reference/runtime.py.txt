"""Cross-platform runtime command and lifecycle contracts.

The module is intentionally observation-driven: platform runners provide real
PID/boot/executable facts, while these pure functions classify them.  No
process is created, killed, or inspected here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional, Tuple


# Runtime records cross a JSON boundary in the scheduler.  Keep counters and
# identifiers within the same exact-integer envelope as the rest of Code-Kit;
# accepting a larger integer would make a later float/JSON round-trip lossy.
_MAX_SAFE_INTEGER = 2**53 - 1


class RuntimeErrorCode(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


def _runtime_text(value: Any, code: str, field: str) -> str:
    """Validate text fields before hashing or passing them to an adapter."""
    if type(value) is not str or not value or "\x00" in value:
        raise RuntimeErrorCode(code, f"{field} must be non-empty text without NUL")
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF
           for char in value):
        raise RuntimeErrorCode(code, f"{field} contains a control or non-scalar character")
    return value


def validate_argv(command: Any, *, shell: bool = False) -> Tuple[str, ...]:
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
    request_id: str
    run_id: str
    command: Tuple[str, ...]
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

    def digest(self) -> str:
        payload = "\0".join((self.request_id, self.run_id, *self.command, self.cwd, self.environment_sha256, str(self.timeout_seconds)))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProcessIdentity:
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


LIFECYCLE = ("NEW", "STARTING", "STARTED", "STOPPING", "STOPPED", "FAILED", "START_UNKNOWN")
ALLOWED_TRANSITIONS = {
    "NEW": {"STARTING", "FAILED"}, "STARTING": {"STARTED", "START_UNKNOWN", "FAILED"},
    "STARTED": {"STOPPING", "FAILED"}, "STOPPING": {"STOPPED", "FAILED", "START_UNKNOWN"},
    "STOPPED": set(), "FAILED": set(), "START_UNKNOWN": {"STARTED", "STOPPING", "FAILED"},
}


@dataclass(frozen=True)
class Lifecycle:
    state: str = "NEW"
    generation: int = 0
    identity: Optional[ProcessIdentity] = None

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

    def transition(self, state: str, *, identity: Optional[ProcessIdentity] = None, expected_generation: Optional[int] = None) -> "Lifecycle":
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


def classify_start(*, handoff_accepted: bool, started_observation: Optional[Mapping[str, Any]], expected: InvocationSpec) -> str:
    """Classify an uncertain handoff without guessing that a process started."""

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
    """Return a conservative reconciliation result for an observation."""

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


__all__ = ["ALLOWED_TRANSITIONS", "InvocationSpec", "Lifecycle", "ProcessIdentity", "RuntimeErrorCode", "classify_start", "reconcile", "validate_argv"]
