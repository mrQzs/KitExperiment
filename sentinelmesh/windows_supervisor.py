"""Native Windows evidence handling for SentinelMesh: the half without Job Objects.

`.agent/design/windows-supervisor.md` (section 3.4) splits the Windows supervisor
into two halves; this module is the *evidence* half, and by user decision
(section 5, item 1) it carries no Job Object, no ``supervise`` and no platform
selection: `sentinelmesh.supervised_record` still starts a run through
`sentinelmesh.posix_supervisor`, and `sentinelmesh.supervised_operation` still
asks that module whether two files are the recorded streams.  ``SupervisedRun`` is
re-exported from there unchanged, so a later contract can wire a Windows
``supervise`` onto the same run type without touching a caller.

What Windows cannot do, measured 2026-10-10 on Windows Server 2025 (Python
3.12.10, Administrator), and how it is kept by hand here:

* there is no ``os.O_NOFOLLOW``, no ``os.O_NONBLOCK`` and no ``os.pread``;
* an exclusive open does *not* fail at a dangling symbolic link: it creates the
  file through the link, so the path is examined with ``lstat`` before anything
  is opened and anything that is not a plain regular file - a link, a reparse
  point, a directory - is refused without opening it.  A dangling link planted
  in the window between that ``lstat`` and the exclusive open is still refused
  (the pin's own ``lstat`` sees a reparse point, nothing is leaked), but the
  file created through it stays where the link pointed: nothing here removes
  by path, so a file at a location this call never examined is left alone;
* a file this process holds open (a write descriptor or a read-only pin) cannot
  be unlinked, renamed or replaced, so a rollback closes every descriptor first
  and only then removes what it created;
* a descriptor opened without ``O_BINARY`` is a *text* descriptor: writing turns
  LF into CR LF and reading folds CR LF back and stops at 0x1A, so every open
  here asks for ``O_BINARY``.

Identity is ``(st_dev, st_ino)``; a "plain regular file" is a regular file whose
``st_file_attributes`` carries no ``FILE_ATTRIBUTE_REPARSE_POINT``.  Everything is
fail-closed: a rejection raises `sentinelmesh.runtime.RuntimeErrorCode` with a
stable code, and the helpers are plain ``os`` calls, so they run on every
platform.  Importing this module has no side effect: no ``ctypes`` import, no file
read, no environment read and no process start.
"""
from __future__ import annotations

import hashlib
import os
import stat
from typing import Any, Mapping, Optional

# The run type and the constants of the POSIX supervisor, reused unchanged: the
# two supervisors must describe one run the same way.  ``MAX_STREAM_DIGEST_BYTES``
# is read through this module *when* a fact is taken, so a test may lower it for
# both supervisors at once.
from . import posix_supervisor as _posix
from .posix_supervisor import (
    SupervisedRun,
    _BLOCK_BYTES,
    _EVIDENCE_MODE,
    _STDERR_NAME,
    _STDOUT_NAME,
    _STREAM_NAMES,
    _close_fd,
    _digest_blocks,
    _identity_of,
    _valid_facts,
)
from .runtime import RuntimeErrorCode

__all__ = ["SupervisedRun", "streams_match"]

# Windows has no ``O_NOFOLLOW`` and no ``O_NONBLOCK``: the link refusal is made
# by hand (``lstat`` first, identity of what was opened proved equal to what was
# seen), and ``O_BINARY`` is what keeps the C runtime from turning a byte stream
# into a text stream.  Whether the flag exists is a fact about the platform, so
# it is settled once at import time, which reads no file and starts nothing.
_O_BINARY = getattr(os, "O_BINARY", 0)


def _plain_regular(info: Any) -> bool:
    """Return whether ``info`` describes a plain regular file and not a link.

    A reparse point - a symbolic link, a mount point - is not the file the path
    was examined for, and on Windows an exclusive open would happily create
    *through* a dangling one, so it is refused before anything is opened.
    """
    return stat.S_ISREG(info.st_mode) and not (
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _create_evidence(evidence_dir: str, name: str) -> int:
    """Create one evidence file exclusively, or raise ``E_RUNTIME_EVIDENCE``.

    The path is examined first: an entry that is already there - a file, a
    directory, even a dangling link - is a previous run's (or another writer's)
    evidence and is not read, overwritten or followed.  Only a path ``lstat``
    reports as missing is opened ``O_EXCL``, and ``O_BINARY`` keeps what the
    child writes byte for byte.
    """
    path = os.path.join(evidence_dir, name)
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} could not be created exclusively: {exc}") from exc
    else:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} already exists")
    try:
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, _EVIDENCE_MODE)
    except OSError as exc:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} could not be created exclusively: {exc}") from exc


def _open_pinned(evidence_dir: str, name: str, write_fd: int) -> int:
    """Pin one just-created evidence file with a read-only descriptor.

    The pin is opened by path, and what was opened is then proved to be the very
    file the write descriptor created: same ``st_dev``/``st_ino`` as the write
    descriptor and as the file the ``lstat`` of the path saw.  Without that proof
    the facts could describe a file this call never created, which is what a
    substitution at the path would be, and without the ``lstat`` first a reparse
    point could be followed.

    A pin that cannot be opened or does not match raises ``E_RUNTIME_EVIDENCE``:
    nothing has started, so the caller must learn that the run cannot be
    evidenced rather than receive facts about another file.  The descriptor is
    closed before raising in every failing case, so a refused pin leaks nothing.
    """
    path = os.path.join(evidence_dir, name)
    try:
        seen = os.lstat(path)
    except OSError as exc:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} could not be pinned: {exc}") from exc
    if not _plain_regular(seen):
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} is not a plain regular file")
    try:
        pinned = os.open(path, os.O_RDONLY | _O_BINARY)
    except OSError as exc:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} could not be pinned: {exc}") from exc
    try:
        pinned_info = os.fstat(pinned)
        created_info = os.fstat(write_fd)
    except OSError as exc:
        _close_fd(pinned)
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} could not be pinned: {exc}") from exc
    pinned_identity = (pinned_info.st_dev, pinned_info.st_ino)
    if (
        not stat.S_ISREG(pinned_info.st_mode)
        or pinned_identity != (created_info.st_dev, created_info.st_ino)
        or pinned_identity != (seen.st_dev, seen.st_ino)
    ):
        _close_fd(pinned)
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} is not the created evidence file")
    return pinned


def _open_evidence_files(evidence_dir: str) -> tuple[Any, ...]:
    """Create and pin both evidence files, rolling everything back on failure.

    Returns ``(stdout_path, stderr_path, stdout_write_fd, stderr_write_fd,
    stdout_pin_fd, stderr_pin_fd, stdout_identity, stderr_identity)``.  Both
    files are created and pinned here, before the ``before_start`` hook, so a run
    whose evidence cannot be pinned never reaches a hook or a ``Popen``.

    Nothing may have started and no evidence of an attempt that did not happen
    may survive, so every descriptor this call opened is closed and every file
    this call created is removed.  The removal is by identity, never by path
    alone: a file this call did *not* create (the exclusive open fails exactly
    when the name is taken), or a file that took the place of one it created
    after the pin failed, is left exactly as it was found.  On Windows the
    closing has to come first: an open file cannot be unlinked.
    """
    stdout_path = os.path.join(evidence_dir, _STDOUT_NAME)
    stderr_path = os.path.join(evidence_dir, _STDERR_NAME)
    # Each created file is registered before its identity is taken: a file whose
    # identity could not be read stays registered with ``None`` and is closed but
    # not removed, because nothing proves the path still names it.
    created: list[list[Any]] = []
    stdout_fd: Optional[int] = None
    stdout_pin: Optional[int] = None
    stderr_fd: Optional[int] = None
    stderr_pin: Optional[int] = None
    try:
        stdout_fd = _create_evidence(evidence_dir, _STDOUT_NAME)
        created.append([stdout_path, None])
        created[-1][1] = _identity_of(stdout_fd, _STDOUT_NAME)
        stdout_pin = _open_pinned(evidence_dir, _STDOUT_NAME, stdout_fd)
        stderr_fd = _create_evidence(evidence_dir, _STDERR_NAME)
        created.append([stderr_path, None])
        created[-1][1] = _identity_of(stderr_fd, _STDERR_NAME)
        stderr_pin = _open_pinned(evidence_dir, _STDERR_NAME, stderr_fd)
    except BaseException:
        # Whatever failed (an OSError, an identity mismatch, an interrupt), the
        # start did not happen: close everything this call opened and remove only
        # the files this call created and still owns at their path.  The same
        # exception reaches the caller unchanged.
        _abandon_evidence(created, (stderr_pin, stderr_fd, stdout_pin, stdout_fd))
        raise
    return (stdout_path, stderr_path, stdout_fd, stderr_fd, stdout_pin, stderr_pin,
            created[0][1], created[1][1])


def _abandon_evidence(created: Any, descriptors: Any) -> None:
    """Close every descriptor of a start that did not happen, then remove its files.

    The order is fixed, because on Windows an open file cannot be unlinked: every
    descriptor is closed first (``None`` for one that was never opened, an
    already-closed one ignored), and only then is each created file removed by
    identity.  Nothing here raises: this runs on the way out of a failure, and an
    exception raised here would replace the caller's own.
    """
    for descriptor in descriptors:
        _close_fd(descriptor)
    for entry in created:
        _remove_own_evidence(entry[0], entry[1])


def _remove_own_evidence(path: str, identity: Optional[tuple[int, int]]) -> None:
    """Remove an evidence file only while the path still holds the created one.

    The file is unlinked only if ``lstat`` shows a plain regular file - a reparse
    point is not it - whose ``(st_dev, st_ino)`` is exactly the identity the write
    descriptor had right after this call created it.  A path that now names
    somebody else's file, a path that no longer names anything, a path that cannot
    be inspected at all or anything that is not a plain regular file is left
    untouched: unlinking there would destroy evidence this call never created,
    while a file this call created that was renamed away stays where it was moved
    to.  Nothing here raises, and a file is never removed by path alone.
    """
    if identity is None:
        return
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        return
    if not _plain_regular(info) or (info.st_dev, info.st_ino) != identity:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def _fact_of(descriptor: int) -> Optional[dict[str, Any]]:
    """Return what one pinned stream holds, or ``None`` if it cannot be read.

    There is no ``os.pread`` here, so the descriptor is positioned at offset 0
    with ``lseek`` first - the facts describe the file, not wherever a reader
    happened to leave it - and the bytes are then read one block at a time up to
    the size that same ``fstat`` reported: a growing file can never turn the
    digest into a read that never ends, and a shrinking one simply stops early.
    ``O_BINARY`` on the pin is what keeps the C runtime from folding CR LF and
    stopping at 0x1A, which would make the digest describe a file nobody wrote.

    A stream whose reported size is beyond ``MAX_STREAM_DIGEST_BYTES`` - read
    from `sentinelmesh.posix_supervisor` when the fact is taken - has no fact at
    all and not one byte of it is read: a child can stretch its own output to any
    size with ``ftruncate`` without writing anything, and reading that would keep
    the supervisor from ever returning a run.  The digest is otherwise built one
    block at a time, so the memory this needs does not grow with what the child
    wrote.

    An ``OSError`` makes this stream's fact ``None`` instead of failing the run: a
    process has already run, and an error raised there would tell the caller the
    start was refused (see `sentinelmesh.supervised_record`).  Any other
    exception, an interrupt above all, is not absorbed: the group is reaped, so
    there is nothing left that must finish before the run can report, and the
    caller has to learn that no facts were produced.
    """
    try:
        info = os.fstat(descriptor)
        if info.st_size > _posix.MAX_STREAM_DIGEST_BYTES:
            return None
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while total < info.st_size:
            chunk = os.read(descriptor, min(_BLOCK_BYTES, info.st_size - total))
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


def _stream_matches(path: str, fact: Mapping[str, Any]) -> bool:
    """Return whether the file at ``path`` still is exactly ``fact``.

    The path is examined first and must hold a plain regular file, so a link, a
    reparse point or a directory cannot stand in for it, and ``fstat`` on what was
    really opened is then compared against the ``lstat`` triple and against the
    recorded device, inode and size: a file that took the original's place, a
    renamed-away original and a truncated one all differ there.  Only then are
    bytes compared, at most ``fact["bytes"] + 1`` of them, so a file that grew
    after the fact was taken is refused without reading it whole, and they are
    digested one block at a time so the check stays bounded too.

    Nothing here raises: an ``OSError`` (a missing file, a permission refusal, a
    read failure) is a mismatch, never an exception, because the question is asked
    about evidence a caller may no longer be able to inspect; and a ``ValueError``
    - a path holding a NUL or a character the platform cannot encode, such as a
    lone surrogate - is a mismatch as well, since such a path names no file this
    run could have written.  The descriptor is closed on every way out, including
    the ones that answer ``False``.
    """
    try:
        try:
            seen = os.lstat(path)
        except (OSError, ValueError):
            return False
        if not _plain_regular(seen):
            return False
        if (seen.st_dev, seen.st_ino, seen.st_size) != (fact["device"], fact["inode"], fact["bytes"]):
            return False
        try:
            descriptor = os.open(path, os.O_RDONLY | _O_BINARY)
        except (OSError, ValueError):
            return False
        try:
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    return False
                if (info.st_dev, info.st_ino, info.st_size) != (seen.st_dev, seen.st_ino, seen.st_size):
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
    the wrong type, is a ``False`` rather than an exception: the caller is asking a
    question about evidence it does not own, and a wrong answer must never look
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
