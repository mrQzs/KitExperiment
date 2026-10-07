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
  the supervisor hang.  Both files are created, then each is *pinned* with a
  second, read-only descriptor opened by path and proved by ``fstat`` to be the
  very file the write descriptor created - compared with the write descriptor,
  never with whatever the path happens to name - so the facts about the two
  streams are read from the file the child actually wrote to;
* a rollback, and the cleanup of a ``before_start`` hook that raised, removes an
  evidence file only while the path still holds the file this call created
  (same ``st_dev``/``st_ino``): a file another process put at the path is left
  alone, and a file this call created that was renamed away stays where it was
  moved to.  The caller's own exception is re-raised unchanged, so nothing
  starts and no evidence of an attempt that never happened survives;
* on timeout or cancellation the group is SIGTERMed, then SIGKILLed as a group
  inside bounded windows, and the leftovers are reaped until the group is
  confirmed empty.  ``cleanup_confirmed=False`` (no false "clean" report) is
  what blocks a replay in `reconcile`;
* an interrupt that arrives while the stop or reap windows are running is
  absorbed: those steps finish inside their original deadlines, because the
  group still has to be killed and reaped before the run can report anything.
  Pure Python cannot make this airtight: an interrupt that lands in the few
  bytecodes outside the guarded waits (between two attempts, or around the
  reap's first probe and kill) still escapes.  An interrupt that arrives while
  the stream facts are being digested is *not* absorbed: the group is reaped by
  then, so the run cannot report and the exception reaches the caller;
* once the group is reaped, every run that started a process carries
  ``streams``: for each stream the device, inode, byte count and SHA-256 of what
  the pinned descriptor can read, bounded by the size ``fstat`` reports at that
  moment and by ``MAX_STREAM_DIGEST_BYTES``.  The digest is computed one block at
  a time, in bounded memory and bounded time, and a stream larger than that
  limit - a child that stretched its own file with ``ftruncate`` writes nothing -
  is recorded as ``None`` without a single byte being read.  A stream that cannot
  be read at all is likewise ``None`` rather than a failed run, and
  `streams_match` later answers whether the two files still are exactly those
  facts, so a write that arrives after the reap - by a descendant that escaped
  the group, or by a path substitution - is detectable.

Everything is fail-closed: a rejection raises
`sentinelmesh.runtime.RuntimeErrorCode` with a stable code and starts no process,
creates no file and reads no credential.  Importing this module has no side
effect: no ``ctypes`` import, no file read, no environment read and no process
start.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import signal
import stat
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
from .runtime import InvocationSpec, Lifecycle, ProcessIdentity, RuntimeErrorCode, _runtime_text

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

# Stream names, as keys of ``SupervisedRun.streams`` and of the ``streams``
# record read back by `sentinelmesh.supervised_record.recorded_streams`.
_STREAM_NAMES = ("stdout", "stderr")
# One stream fact is closed: these four keys and nothing else, because a fact
# that carried anything else could not be compared against a file later.
_FACT_KEYS = frozenset({"device", "inode", "bytes", "sha256"})
_SHA256_ALPHABET = frozenset("0123456789abcdef")
# Read granularity for a stream fact and for the `streams_match` check.  No read
# ever asks for more than this at once, and no digest ever holds more than one
# block: a stream is digested block by block, so the supervisor's memory does not
# grow with what the child wrote.
_BLOCK_BYTES = 1 << 16
# Upper bound on the size of a stream that is digested at all.  It is read from
# this module *when* a fact is taken and when a recorded fact is checked, so a
# test may lower it.  A stream beyond it has no fact and nothing of it is read:
# a child can stretch its own output file to any size with ``ftruncate`` without
# writing a byte (a sparse file), and reading that would keep the supervisor from
# ever returning a run.
MAX_STREAM_DIGEST_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class SupervisedRun:
    """One finished supervision: stop reason, lifecycle, observation, identity.

    ``observation`` is the five-key record `reconcile` consumes (three keys when
    the process never started) and ``lifecycle`` the canonical transition
    record.  No field carries an environment value: only names are recorded, in
    ``environment_names``.

    ``streams`` is ``None`` when no process ever started, and otherwise the
    ``{"stdout": fact-or-None, "stderr": fact-or-None}`` mapping computed from
    the pinned descriptors once the group was reaped: what the two evidence
    files held at that moment, not what the paths hold later.  A stream beyond
    ``MAX_STREAM_DIGEST_BYTES``, and one that could not be read at all, is
    ``None``.
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
    streams: Optional[Mapping[str, Optional[Mapping[str, Any]]]] = None


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
    """Return ``worktree_id`` once it is exactly non-empty scalar text.

    It must also pass the text rule `ProcessIdentity` applies after the start
    (`_runtime_text`, which also refuses the C1 controls U+0080-U+009F): an id
    refused only there raised after ``Popen`` and left the child running
    unsupervised (found reviewing posix-reconcile-integration a001).
    """
    if type(worktree_id) is not str or not worktree_id or not _scalar_text(worktree_id):
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "worktree_id must be non-empty scalar text")
    _runtime_text(worktree_id, "E_RUNTIME_IDENTITY", "worktree_id")
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


def _open_pinned(evidence_dir: str, name: str, write_fd: int) -> int:
    """Pin one just-created evidence file with a read-only descriptor.

    The pin is opened by path, so ``O_NOFOLLOW`` refuses a link planted at that
    name and ``O_NONBLOCK`` keeps a special file from blocking the open, and it
    is then proved to be the file the write descriptor created: same
    ``st_dev``/``st_ino``.  Without that proof the facts could describe a file
    this call never created, which is what a substitution at the path would be.

    A pin that cannot be opened or does not match raises
    ``E_RUNTIME_EVIDENCE``: nothing has started, so the caller must learn that
    the run cannot be evidenced rather than receive facts about another file.
    The descriptor is closed before raising in every failing case, so a refused
    pin leaks nothing.
    """
    path = os.path.join(evidence_dir, name)
    try:
        pinned = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} could not be pinned: {exc}") from exc
    try:
        pinned_info = os.fstat(pinned)
        created_info = os.fstat(write_fd)
    except OSError as exc:
        _close_fd(pinned)
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} could not be pinned: {exc}") from exc
    if not stat.S_ISREG(pinned_info.st_mode) or (
        pinned_info.st_dev,
        pinned_info.st_ino,
    ) != (created_info.st_dev, created_info.st_ino):
        _close_fd(pinned)
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} is not the created evidence file")
    return pinned


def _open_evidence_files(evidence_dir: str) -> tuple[str, str, int, int, int, int]:
    """Create and pin both evidence files, rolling everything back on failure.

    Returns ``(stdout_path, stderr_path, stdout_write_fd, stderr_write_fd,
    stdout_pin_fd, stderr_pin_fd)``.  Both files are created and pinned here,
    before the ``before_start`` hook, so a run whose evidence cannot be pinned
    never reaches a hook or a ``Popen``.

    Nothing may have started and no evidence of an attempt that did not happen
    may survive, so every descriptor opened by this call is closed and every
    file this call created is removed.  The removal is by identity, never by
    path alone: a file this call did *not* create (the second ``O_EXCL`` open
    fails exactly when the name is taken), or a file that took the place of one
    it created after the pin failed, is left exactly as it was found.
    """
    stdout_path = os.path.join(evidence_dir, _STDOUT_NAME)
    stderr_path = os.path.join(evidence_dir, _STDERR_NAME)
    created: list[tuple[str, tuple[int, int]]] = []
    stdout_fd: Optional[int] = None
    stdout_pin: Optional[int] = None
    stderr_fd: Optional[int] = None
    stderr_pin: Optional[int] = None
    try:
        stdout_fd = _open_evidence(evidence_dir, _STDOUT_NAME)
        created.append((stdout_path, _identity_of(stdout_fd)))
        stdout_pin = _open_pinned(evidence_dir, _STDOUT_NAME, stdout_fd)
        stderr_fd = _open_evidence(evidence_dir, _STDERR_NAME)
        created.append((stderr_path, _identity_of(stderr_fd)))
        stderr_pin = _open_pinned(evidence_dir, _STDERR_NAME, stderr_fd)
    except BaseException:
        # Whatever failed (an OSError, an identity mismatch, an interrupt), the
        # start did not happen: close everything this call opened and remove
        # only the files this call created and still owns at their path.
        _close_fd(stderr_pin)
        _close_fd(stderr_fd)
        _close_fd(stdout_pin)
        _close_fd(stdout_fd)
        for path, identity in created:
            _remove_own_evidence(path, identity)
        raise
    return stdout_path, stderr_path, stdout_fd, stderr_fd, stdout_pin, stderr_pin


def _identity_of(descriptor: int) -> tuple[int, int]:
    """Return ``(st_dev, st_ino)`` of an open descriptor, taken once.

    It is read from the write descriptor immediately after the ``O_EXCL``
    creation, so a later rollback can tell the file this call created from
    whatever the path has come to name: the path alone says nothing about which
    file is standing on it.
    """
    info = os.fstat(descriptor)
    return (info.st_dev, info.st_ino)


def _close_fd(descriptor: Optional[int]) -> None:
    """Close one descriptor, ignoring an already-closed or absent handle."""
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _remove_own_evidence(path: str, identity: tuple[int, int]) -> None:
    """Remove an evidence file only while the path still holds the created one.

    The file is unlinked only if ``lstat`` shows a regular file whose
    ``(st_dev, st_ino)`` is exactly the identity the write descriptor had right
    after this call created it.  A path that now names somebody else's file, a
    path that no longer names anything, a path that cannot be inspected at all
    or anything that is not a regular file is left untouched - unlinking there
    would destroy evidence this call never created, while the file this call
    created and that was renamed away stays where it was moved to.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return
    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identity:
        return
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


def _digest_blocks(descriptor: int, limit: int) -> tuple[int, Optional[str]]:
    """Digest at most ``limit`` bytes of ``descriptor``, one block at a time.

    Returns ``(bytes_read, digest)``, or ``(bytes_read, None)`` when a read failed
    with an ``OSError`` and the caller has to answer a mismatch.  The digest is
    updated block by block, never by joining the blocks first, so the memory this
    needs stays bounded by ``_BLOCK_BYTES`` whatever the stream holds, and every
    piece handed to the hash is at most one block.
    """
    digest = hashlib.sha256()
    total = 0
    while total < limit:
        try:
            chunk = os.read(descriptor, min(_BLOCK_BYTES, limit - total))
        except OSError:
            return total, None
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return total, digest.hexdigest()


def _fact_of(descriptor: int) -> Optional[dict[str, Any]]:
    """Return what one pinned stream holds, or ``None`` if it cannot be read.

    The identity comes from the pinned descriptor itself, and the bytes are read
    with ``os.pread`` from offset 0 up to the size that same ``fstat`` reported:
    a growing file can never turn the digest into a read that never ends, and a
    shrinking one simply stops early.  ``pread`` is used rather than a positional
    read so the digest could not be disturbed by another read of the file.

    A stream whose reported size is beyond ``MAX_STREAM_DIGEST_BYTES`` - read
    from this module when the fact is taken - has no fact at all and not one byte
    of it is read: a child can stretch its own output to any size with
    ``ftruncate`` without writing anything, and reading that would keep the
    supervisor from ever returning a run.  The digest is otherwise built one block
    at a time, so the memory this needs does not grow with what the child wrote.

    An ``OSError`` makes this stream's fact ``None`` instead of failing the run:
    a process has already run, and an error raised there would tell the caller
    the start was refused (see `sentinelmesh.supervised_record`).  Any other
    exception, an interrupt above all, is not absorbed: the group is reaped, so
    there is nothing left that must finish before the run can report, and the
    caller has to learn that no facts were produced.
    """
    try:
        info = os.fstat(descriptor)
        if info.st_size > MAX_STREAM_DIGEST_BYTES:
            return None
        digest = hashlib.sha256()
        total = 0
        while total < info.st_size:
            chunk = os.pread(descriptor, min(_BLOCK_BYTES, info.st_size - total), total)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    except OSError:
        return None
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "bytes": total,
        "sha256": digest.hexdigest(),
    }


def _streams_of(stdout_pin: int, stderr_pin: int) -> dict[str, Optional[dict[str, Any]]]:
    """Return the facts of both pinned streams, once the group is reaped."""
    return {
        _STREAM_NAMES[0]: _fact_of(stdout_pin),
        _STREAM_NAMES[1]: _fact_of(stderr_pin),
    }


def _is_sha256_text(value: Any) -> bool:
    """Return whether ``value`` is exactly 64 lowercase hexadecimal characters."""
    if type(value) is not str or len(value) != 64:
        return False
    return all(character in _SHA256_ALPHABET for character in value)


def _valid_facts(value: Any) -> bool:
    """Return whether ``value`` is a closed stream fact of the right types.

    The types are exact: ``bool`` is a subclass of ``int`` and ``True == 1``, so a
    byte count of ``True`` would stand for one byte, and a digest in upper case
    would never compare equal to the one computed here.  A device number may be
    negative, an inode or a byte count may not.

    A byte count beyond ``MAX_STREAM_DIGEST_BYTES`` - read from this module when
    the fact is checked - is refused here, before any file is opened: a fact the
    supervisor would never have produced cannot describe a stream it read, and
    checking it would mean reading a file nobody can bound.
    """
    if type(value) is not dict or frozenset(value) != _FACT_KEYS:
        return False
    for key in ("device", "inode", "bytes"):
        if type(value[key]) is not int:
            return False
    if value["inode"] < 0 or value["bytes"] < 0:
        return False
    if value["bytes"] > MAX_STREAM_DIGEST_BYTES:
        return False
    return _is_sha256_text(value["sha256"])


def _stream_matches(path: str, fact: Mapping[str, Any]) -> bool:
    """Return whether the file at ``path`` still is exactly ``fact``.

    The file is opened the way a pin is (``O_NOFOLLOW``, ``O_NONBLOCK``), so a
    symlink or a fifo cannot stand in for it, and ``fstat`` is then compared
    against the recorded device, inode and size: a file that took the original's
    place, a renamed-away original and a truncated one all differ there.  Only
    then are bytes compared, at most ``fact["bytes"] + 1`` of them, so a file
    that grew after the fact was taken is refused without reading it whole, and
    they are digested one block at a time so the check stays bounded too.

    Nothing here raises: an ``OSError`` (a missing file, a permission refusal, a
    read failure) is a mismatch, never an exception, because the question is
    asked about evidence a caller may no longer be able to inspect; and a
    ``ValueError`` - a path holding a NUL or a character the platform cannot
    encode, such as a lone surrogate - is a mismatch as well, since such a path
    names no file this run could have written.  The descriptor is closed on
    every way out, including the ones that answer ``False``.
    """
    try:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except (OSError, ValueError):
            return False
        try:
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    return False
                if (info.st_dev, info.st_ino, info.st_size) != (
                    fact["device"],
                    fact["inode"],
                    fact["bytes"],
                ):
                    return False
                read, digest = _digest_blocks(descriptor, fact["bytes"] + 1)
            except OSError:
                return False
            if digest is None or read != fact["bytes"]:
                return False
            return digest == fact["sha256"]
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
    except ValueError:
        # A path that cannot even be turned into bytes names no file; anything
        # else that could still surface as a ValueError is a mismatch too.
        return False


def streams_match(evidence_dir: Any, streams: Any) -> bool:
    """Return whether both evidence files still are exactly ``streams``.

    ``streams`` must be the mapping a `SupervisedRun` carried: exactly the two
    stream names, each holding a closed fact whose byte count is within
    ``MAX_STREAM_DIGEST_BYTES``.  A stream whose fact is ``None`` could not be
    read when the run ended, and no file can be shown to be what nobody could
    read, so such a mapping is refused.  Anything malformed, and any argument of
    the wrong type, is a ``False`` rather than an exception: the caller is asking
    a question about evidence it does not own, and a wrong answer must never look
    like a refusal to answer - not even for an evidence directory whose path the
    platform cannot encode.
    """
    try:
        if type(evidence_dir) is not str:
            return False
        if type(streams) is not dict or frozenset(streams) != set(_STREAM_NAMES):
            return False
        facts: dict[str, Any] = {}
        for name in _STREAM_NAMES:
            fact = streams[name]
            if not _valid_facts(fact):
                return False
            facts[name] = fact
        return all(
            _stream_matches(os.path.join(evidence_dir, name + ".log"), facts[name]) for name in _STREAM_NAMES
        )
    except (OSError, ValueError):
        return False


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
    names, worktree id, executable identity, evidence directory, ``before_start``
    hook.  Nothing is written to the evidence directory and no process is started
    until all of them pass.

    Returns a `SupervisedRun` whose ``observation`` can be handed straight to
    `sentinelmesh.runtime.reconcile`.  A cancellation is reported, never
    re-raised, so the caller still receives the evidence and the observation.

    ``before_start`` is an optional caller hook invoked exactly once, with no
    arguments, after both evidence files exist *and are pinned* and immediately
    before ``Popen``.  It is where a caller records its own handoff evidence; if
    it raises, this call removes the two evidence files it created and re-raises
    the same exception, so no process is started and no half-written evidence
    is left behind (see `sentinelmesh.supervised_record`).  The removal is by
    identity: a file another process put at one of the two paths while the hook
    ran is left exactly as it was found, and a file this call created that was
    renamed away stays where it was moved to.

    On return, ``streams`` holds what each pinned stream held once the group was
    reaped, bounded by ``MAX_STREAM_DIGEST_BYTES`` and digested one block at a
    time; `streams_match` answers later whether the files still are exactly
    that.
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

    stdout_path, stderr_path, stdout_fd, stderr_fd, stdout_pin, stderr_pin = _open_evidence_files(evidence)
    # What each write descriptor was, taken once, so a cleanup below can tell the
    # file this call created from whatever has taken its place at the path.
    stdout_identity = _identity_of(stdout_fd)
    stderr_identity = _identity_of(stderr_fd)
    # The two pins outlive the start: they are what the stream facts are read
    # through once the group is reaped, so every way out of this call closes
    # them -- a failed hook, a failed exec, a normal return, and an exception
    # the caller will see (an interrupt while the facts are digested above all).
    try:
        if before_start is not None:
            try:
                before_start()
            except BaseException:
                # Whatever the hook raised (including ``KeyboardInterrupt``), the
                # start did not happen, so the two evidence files this call
                # created exclusively for it must go with it: leaving them would
                # record an attempt that never reached ``Popen``.  Only the ones
                # the path still holds as this call created them are removed; a
                # file another process put there in the meantime is left alone.
                # The exception is re-raised unchanged so the caller still sees
                # its own failure.
                _close_fd(stdout_fd)
                _close_fd(stderr_fd)
                _remove_own_evidence(stdout_path, stdout_identity)
                _remove_own_evidence(stderr_path, stderr_identity)
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
            # process identity exists, no stream was ever written, and
            # ``NOT_STARTED`` is the only observation ``reconcile`` may act on.
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
            # The child holds its own copies after a successful ``Popen``; these
            # two descriptors belong to the supervisor and must not outlive the
            # start.
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

        # Only now, with the group reaped, are the two streams digested --
        # through the pins, so the facts describe what the child actually wrote
        # even if it renamed or removed the file the path still names.
        streams: Optional[Mapping[str, Optional[Mapping[str, Any]]]] = _streams_of(stdout_pin, stderr_pin)
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
            streams,
        )
    finally:
        _close_fd(stdout_pin)
        _close_fd(stderr_pin)
