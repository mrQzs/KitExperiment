"""Native POSIX (macOS) process supervision for SentinelMesh.

This module is the high-risk supervisor described in
`.agent/design/posix-runtime-runner.md` (section 2) and restricted to macOS by
user decision (section 5, item 1): Linux and Windows Job Object supervision stay
``NOT_RUN``.  It never replaces the historical `SubprocessRunner`; it is a
separate, fail-closed entry point for callers that already hold a canonical
`InvocationSpec` plus an explicit environment mapping.

What one run guarantees:

* the child is started with *exactly* the caller's environment, proven to digest
  to ``spec.environment_sha256`` first (`verified_environment`); ``os.environ``
  is never inherited, so a credential variable the caller did not name cannot
  reach the child;
* a variable whose name looks like a credential is refused unless the caller
  allowlists that name explicitly; only names are ever recorded, never values;
* the child gets its own session (`start_new_session=True`), so the supervisor
  owns a whole process group and can stop descendants as well as the leader;
* stdout/stderr go to files created ``O_EXCL`` in the caller's evidence
  directory, never to pipes, so a descendant holding the output open cannot make
  the supervisor hang;
* a caller may pass a ``before_start`` hook that runs once after those two files
  exist and before ``Popen``; if it raises, the two files are removed and the
  exception is re-raised unchanged, so nothing starts and no evidence of an
  attempt that never happened survives;
* on timeout or cancellation the group is SIGTERMed, then SIGKILLed as a group
  inside bounded windows, and the leftovers are reaped until the group is
  confirmed empty.  ``cleanup_confirmed=False`` (no false "clean" report) is
  what blocks a replay in `reconcile`;
* an interrupt that arrives while the stop or reap windows are running is
  absorbed: those steps finish inside their original deadlines, because the
  group still has to be killed and reaped before the run can report anything.
  Pure Python cannot make this airtight: an interrupt that lands in the few
  bytecodes outside the guarded waits (between two attempts, or around the
  reap's first probe and kill) still escapes.

Everything is fail-closed: a rejection raises
`sentinelmesh.runtime.RuntimeErrorCode` with a stable code and starts no process,
creates no file and reads no credential.  Importing this module has no side
effect: no ``ctypes`` import, no file read, no environment read and no process
start.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import signal
import subprocess
import time
from typing import Any, Callable, Mapping, Optional

from .process_identity import (
    _scalar_text,
    boot_id,
    file_sha256,
    resolve_executable,
    verified_environment,
)
from .runtime import InvocationSpec, Lifecycle, ProcessIdentity, RuntimeErrorCode

# Bounded shutdown windows.  After a timeout or a cancellation the whole process
# group is asked to terminate, and only if the leader is still alive after the
# grace period is the group SIGKILLed.  ``_REAP_SECONDS`` bounds how long the
# group is polled before cleanup is reported unconfirmed.
_TERM_GRACE_SECONDS = 1.0
_KILL_WAIT_SECONDS = 2.0
_REAP_SECONDS = 2.0
_REAP_POLL_SECONDS = 0.05

# Credential-shaped environment names are refused by default (Code-Kit
# ``offline_runner`` rule, user decision 2026-10-04, section 5 item 4).  The match
# is a substring test on the upper-cased name, so ``my_Token`` and ``MONKEY``
# (which contains ``KEY``) are both refused unless allowlisted.
_CREDENTIAL_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "AUTH")

# Evidence file names, created ``O_EXCL`` inside the caller's evidence
# directory; an existing file is never overwritten, read or removed.
_STDOUT_NAME = "stdout.log"
_STDERR_NAME = "stderr.log"
_EVIDENCE_MODE = 0o600


@dataclass(frozen=True)
class SupervisedRun:
    """One finished supervision: stop reason, lifecycle, observation, identity.

    ``observation`` is the five-key record `reconcile` consumes (three keys when
    the process never started) and ``lifecycle`` the canonical transition
    record.  No field carries an environment value: only names are recorded, in
    ``environment_names``.
    """

    stop_reason: str
    exit_code: Optional[int]
    lifecycle: Lifecycle
    observation: Mapping[str, Any]
    identity: Optional[ProcessIdentity]
    stdout_path: str
    stderr_path: str
    stragglers_killed: bool
    environment_names: tuple[str, ...]
    duration_seconds: float


def _validated_allowlist(allowed_secret_names: Any) -> frozenset[str]:
    """Return the allowlisted credential names, or raise ``E_RUNTIME_ENVIRONMENT``.

    The container type is fixed (``set`` or ``frozenset``) so a caller cannot
    smuggle in a mapping keyed by something else, and every element must be
    exactly ``str``: a subclass could compare equal to a credentialed name while
    resolving to a different variable.
    """
    if type(allowed_secret_names) not in (frozenset, set):
        raise RuntimeErrorCode("E_RUNTIME_ENVIRONMENT", "allowed_secret_names must be a set or frozenset")
    for name in allowed_secret_names:
        if type(name) is not str:
            raise RuntimeErrorCode("E_RUNTIME_ENVIRONMENT", "allowed secret names must be text")
    return frozenset(allowed_secret_names)


def _reject_credential_names(snapshot: Mapping[str, str], allowed: frozenset[str]) -> None:
    """Refuse a credential-shaped environment name unless it is allowlisted."""
    for name in snapshot:
        upper = name.upper()
        if any(marker in upper for marker in _CREDENTIAL_MARKERS) and name not in allowed:
            raise RuntimeErrorCode(
                "E_RUNTIME_ENVIRONMENT",
                f"environment name {name!r} looks like a credential and is not allowlisted",
            )


def _validated_worktree_id(worktree_id: Any) -> str:
    """Return ``worktree_id`` once it is exactly non-empty scalar text."""
    if type(worktree_id) is not str or not worktree_id or not _scalar_text(worktree_id):
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "worktree_id must be non-empty scalar text")
    return worktree_id


def _validated_evidence_dir(evidence_dir: Any) -> str:
    """Return ``evidence_dir`` once it is an absolute existing directory."""
    if type(evidence_dir) is not str or not os.path.isabs(evidence_dir) or not os.path.isdir(evidence_dir):
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "evidence_dir must be an absolute existing directory")
    return evidence_dir


def _open_evidence(evidence_dir: str, name: str) -> int:
    """Create one evidence file exclusively, or raise ``E_RUNTIME_EVIDENCE``."""
    path = os.path.join(evidence_dir, name)
    try:
        # ``O_EXCL``: an evidence file that already exists is a previous run's
        # (or another writer's) file, and overwriting it would destroy evidence.
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _EVIDENCE_MODE)
    except OSError as exc:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} could not be created exclusively: {exc}") from exc


def _open_evidence_files(evidence_dir: str) -> tuple[str, str, int, int]:
    """Create both evidence files, rolling the first one back if the second fails."""
    stdout_path = os.path.join(evidence_dir, _STDOUT_NAME)
    stderr_path = os.path.join(evidence_dir, _STDERR_NAME)
    stdout_fd = _open_evidence(evidence_dir, _STDOUT_NAME)
    try:
        stderr_fd = _open_evidence(evidence_dir, _STDERR_NAME)
    except BaseException:
        # Nothing was started and no evidence must be left behind: the partial
        # file belongs to this call only, so removing it restores the directory
        # to its previous state without touching any pre-existing file.
        _close_fd(stdout_fd)
        _remove_evidence(stdout_path)
        raise
    return stdout_path, stderr_path, stdout_fd, stderr_fd


def _close_fd(descriptor: int) -> None:
    """Close one descriptor, ignoring an already-closed handle."""
    try:
        os.close(descriptor)
    except OSError:
        pass


def _remove_evidence(path: str) -> None:
    """Remove an evidence file this call created, ignoring a missing file."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _signal_group(pgid: int, sig: int) -> None:
    """Signal the whole process group; a report of failure never stops the run.

    ``ProcessLookupError`` means the group is already gone, and
    ``PermissionError`` means the kernel refused the signal (macOS answers EPERM
    for a group whose only members are unreaped zombies, measured 2026-10-04).
    Neither says the group is empty: only the signal-0 probe in
    `_group_alive` decides that, so a failed send is ignored and the bounded
    steps that follow continue to run.
    """
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return


def _group_alive(pgid: int) -> bool:
    """Return whether the process group still has a member (fail-closed).

    ``PermissionError`` means the group exists but cannot be signalled, which is
    reported as alive: never claim an unverified group is clean.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_leader(proc: "subprocess.Popen[bytes]", seconds: float) -> bool:
    """Wait up to ``seconds`` for the leader; return whether it exited.

    A ``KeyboardInterrupt`` that arrives while the group is being stopped is
    absorbed: the escalation and the reaping must still happen, and they happen
    inside their original windows, so the wait is retried with whatever time is
    left of ``seconds`` instead of ending early.

    The retry runs even when no time is left: CPython's ``Popen.wait`` keeps
    waiting until the deadline after an interrupt, may reap the leader there and
    still re-raises, and ``timeout=0`` checks the leader once more, so an
    interrupt at the deadline cannot turn an exited leader into a live one
    (found reviewing attempt-002).
    """
    deadline = time.monotonic() + seconds
    while True:
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            return True
        except subprocess.TimeoutExpired:
            return False
        except KeyboardInterrupt:
            continue


def _stop_group(proc: "subprocess.Popen[bytes]", stop_reason: str) -> str:
    """Stop the leader's whole group; return the upgraded stop reason.

    SIGTERM goes to the group first (the leader may be trapping it while its
    descendants are not, and vice versa).  Only when the leader is still alive
    after ``_TERM_GRACE_SECONDS`` is the whole group SIGKILLed, which is what
    turns a ``timeout`` into a ``timeout_killed``; a cancellation is already an
    operator decision, so its reason is kept.
    """
    _signal_group(proc.pid, signal.SIGTERM)
    if _wait_for_leader(proc, _TERM_GRACE_SECONDS):
        return stop_reason
    _signal_group(proc.pid, signal.SIGKILL)
    if stop_reason == "timeout":
        stop_reason = "timeout_killed"
    _wait_for_leader(proc, _KILL_WAIT_SECONDS)
    return stop_reason


def _reap_group(pgid: int) -> tuple[bool, bool]:
    """Reap leftover group members; return ``(stragglers_killed, cleanup_confirmed)``.

    The leader has already been waited for, so a surviving group can only contain
    descendants.  They are killed as a group and the group is polled until it is
    confirmed empty; if it is still not empty after ``_REAP_SECONDS`` the run
    reports an unconfirmed cleanup instead of claiming the group is clean.

    A ``KeyboardInterrupt`` raised by a probe or by the poll interval is absorbed
    and the poll restarts: neither the interval bound nor the deadline moves, so
    the run still reports a confirmed cleanup whenever the group really empties.
    """
    if not _group_alive(pgid):
        return False, True
    _signal_group(pgid, signal.SIGKILL)
    deadline = time.monotonic() + _REAP_SECONDS
    while True:
        try:
            if not _group_alive(pgid):
                return True, True
            if time.monotonic() >= deadline:
                return True, False
            time.sleep(min(_REAP_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        except KeyboardInterrupt:
            continue


def _observation_of(
    spec: InvocationSpec,
    state: str,
    identity: Optional[ProcessIdentity],
    cleanup_confirmed: bool,
) -> dict[str, Any]:
    """Return the observation record `reconcile` consumes.

    It is deliberately closed: five keys once a process existed (the JSON shape of
    the identity plus the cleanup fact that gates a replay), three keys when
    nothing ever started.  No environment value appears in it.
    """
    if identity is None:
        return {
            "run_id": spec.run_id,
            "environment_sha256": spec.environment_sha256,
            "state": state,
        }
    return {
        "run_id": spec.run_id,
        "environment_sha256": spec.environment_sha256,
        "state": state,
        "process_identity": identity.to_dict(),
        "cleanup_confirmed": bool(cleanup_confirmed),
    }


def supervise(
    spec: InvocationSpec,
    *,
    environment: Mapping[str, str],
    evidence_dir: str,
    worktree_id: str,
    allowed_secret_names: frozenset[str] = frozenset(),
    before_start: Optional[Callable[[], None]] = None,
) -> SupervisedRun:
    """Start one explicitly described process and supervise it to its end.

    Every check runs before anything is created or started, in the order that
    lets a caller learn the most specific rejection: platform, invocation shape,
    working directory, environment digest against the locked spec, credential
    names, worktree id, executable identity, evidence directory.  Nothing is
    written to the evidence directory and no process is started until all of them
    pass.

    Returns a `SupervisedRun` whose ``observation`` can be handed straight to
    `sentinelmesh.runtime.reconcile`.  A cancellation is reported, never
    re-raised, so the caller still receives the evidence and the observation.

    ``before_start`` is an optional caller hook invoked exactly once, with no
    arguments, after both evidence files exist and immediately before
    ``Popen``.  It is where a caller records its own handoff evidence; if it
    raises, this call removes the two evidence files it created and re-raises
    the same exception, so no process is started and no half-written evidence
    is left behind (see `sentinelmesh.supervised_record`).
    """
    started_at = time.monotonic()
    if os.name != "posix":
        # Windows Job Object supervision is NOT_RUN; refusing here is what keeps
        # "native POSIX supervision" from silently degrading into a guess.
        raise RuntimeErrorCode("E_RUNTIME_PLATFORM", "the native supervisor requires a POSIX platform")
    if type(spec) is not InvocationSpec:
        raise RuntimeErrorCode("E_RUNTIME_INVOCATION", "spec must be an InvocationSpec")
    if not os.path.isabs(spec.cwd) or not os.path.isdir(spec.cwd):
        raise RuntimeErrorCode("E_RUNTIME_INVOCATION", "spec.cwd must be an absolute existing directory")
    # Exactly one read of the caller's mapping, verified against the digest the
    # invocation was locked to; a mapping that digests to anything else is a
    # different environment and fails closed before a process exists.
    snapshot = verified_environment(spec, environment)
    allowed = _validated_allowlist(allowed_secret_names)
    _reject_credential_names(snapshot, allowed)
    _validated_worktree_id(worktree_id)
    # Facts that must be captured before the fork: the exec'd file's digest and
    # this boot session, so the identity can never be taken from the child.
    executable = resolve_executable(spec.command[0], cwd=spec.cwd, environment=snapshot)
    executable_sha256 = file_sha256(executable)
    boot = boot_id()
    evidence = _validated_evidence_dir(evidence_dir)
    # A caller hook that is present but not callable is a programming error.
    # It is checked *after* the evidence directory, which is the last of the
    # start-time checks: both rejections say the call cannot start anything, and
    # reporting the directory first lets a caller that fixes them one at a time
    # learn each rule in the order the checks run.
    if before_start is not None and not callable(before_start):
        raise RuntimeErrorCode("E_RUNTIME_INVOCATION", "before_start must be callable")

    stdout_path, stderr_path, stdout_fd, stderr_fd = _open_evidence_files(evidence)
    if before_start is not None:
        try:
            before_start()
        except BaseException:
            # Whatever the hook raised (including ``KeyboardInterrupt``), the
            # start did not happen, so the two evidence files this call created
            # exclusively for it must go with it: leaving them would record an
            # attempt that never reached ``Popen``.  The exception is re-raised
            # unchanged so the caller still sees its own failure.
            _close_fd(stdout_fd)
            _close_fd(stderr_fd)
            _remove_evidence(stdout_path)
            _remove_evidence(stderr_path)
            raise
    environment_names = tuple(sorted(snapshot))
    lifecycle = Lifecycle().transition("STARTING")
    try:
        proc = subprocess.Popen(
            list(spec.command),
            executable=executable,
            cwd=spec.cwd,
            env=snapshot,
            stdin=subprocess.DEVNULL,
            stdout=stdout_fd,
            stderr=stderr_fd,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        # The exec never happened: the evidence files record an attempt, no
        # process identity exists, and ``NOT_STARTED`` is the only observation
        # ``reconcile`` may act on.
        lifecycle = lifecycle.transition("FAILED")
        return SupervisedRun(
            "start_failed",
            None,
            lifecycle,
            _observation_of(spec, "NOT_STARTED", None, False),
            None,
            stdout_path,
            stderr_path,
            False,
            environment_names,
            max(0.0, time.monotonic() - started_at),
        )
    finally:
        # The child holds its own copies after a successful ``Popen``; these two
        # descriptors belong to the supervisor and must not outlive the start.
        _close_fd(stdout_fd)
        _close_fd(stderr_fd)

    identity = ProcessIdentity(proc.pid, boot, executable_sha256, worktree_id)
    lifecycle = lifecycle.transition("STARTED", identity=identity)

    try:
        proc.wait(timeout=spec.timeout_seconds)
        stop_reason = "completed"
    except subprocess.TimeoutExpired:
        stop_reason = "timeout"
    except KeyboardInterrupt:
        # Cancellation is a reportable outcome, not an exception to propagate:
        # the caller still gets the evidence files and the observation.
        stop_reason = "cancelled"
    lifecycle = lifecycle.transition("STOPPING")
    if stop_reason != "completed":
        stop_reason = _stop_group(proc, stop_reason)
    stragglers_killed, cleanup_confirmed = _reap_group(proc.pid)

    exit_code = proc.returncode
    state = "STOPPED" if stop_reason == "completed" and exit_code == 0 else "FAILED"
    if cleanup_confirmed:
        lifecycle = lifecycle.transition(state)
    else:
        # An unconfirmed group may still hold a live descendant, so only the
        # lifecycle records that this run's start state is unknown: the
        # observation keeps the leader's outcome next to the cleanup fact, and
        # ``reconcile`` refuses a replay on that fact.
        lifecycle = lifecycle.transition("START_UNKNOWN")
    return SupervisedRun(
        stop_reason,
        exit_code,
        lifecycle,
        _observation_of(spec, state, identity, cleanup_confirmed),
        identity,
        stdout_path,
        stderr_path,
        stragglers_killed,
        environment_names,
        max(0.0, time.monotonic() - started_at),
    )
