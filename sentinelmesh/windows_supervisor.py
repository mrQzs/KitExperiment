"""Native Windows process supervision for SentinelMesh: evidence handling plus Job Objects.

`.agent/design/windows-supervisor.md` (section 3.4) splits the Windows supervisor
into two halves, and this module now carries both.  The *evidence* half - the
exclusive ``O_EXCL`` files, the read-only pins, the identity-checked rollback and
the stream facts below - was the `win-evidence` contract.  The `win-supervise`
contract adds the Job Object half on top: `supervise` starts one explicitly
described process ``CREATE_SUSPENDED``, puts it into a Job with
``KILL_ON_JOB_CLOSE`` before it runs a single instruction, resumes it, and on a
timeout or a cancellation ends the whole job with ``TerminateJobObject`` (exit
code ``TERMINATED_EXIT_CODE``).  Its signature, check order, hook, evidence
files, observation record and `SupervisedRun` are the POSIX supervisor's, so the
two supervisors describe one run the same way.

Platform selection is deliberately *not* here (that is the fourth contract's):
`sentinelmesh.supervised_record` still starts a run through
`sentinelmesh.posix_supervisor`, and `sentinelmesh.supervised_operation` still
asks that module whether two files are the recorded streams.  ``SupervisedRun``
and ``streams_match`` are re-exported from there unchanged, so a later contract
can wire this Windows ``supervise`` onto the same run type without touching a
caller.

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
import subprocess
import time
from typing import Any, Callable, Mapping, Optional

# The run type, the constants and the check/observation helpers of the POSIX
# supervisor, reused unchanged: the two supervisors must describe one run the
# same way, and `supervise` runs the very same checks, hook and observation the
# POSIX one does.  ``MAX_STREAM_DIGEST_BYTES`` is read through this module *when*
# a fact is taken, so a test may lower it for both supervisors at once.
from . import posix_supervisor as _posix
from .posix_supervisor import (
    SupervisedRun,
    _BLOCK_BYTES,
    _EVIDENCE_MODE,
    _KILL_WAIT_SECONDS,
    _REAP_POLL_SECONDS,
    _REAP_SECONDS,
    _STDERR_NAME,
    _STDOUT_NAME,
    _STREAM_NAMES,
    _close_fd,
    _digest_blocks,
    _identity_of,
    _observation_of,
    _reject_credential_names,
    _valid_facts,
    _validated_allowlist,
    _validated_evidence_dir,
    _validated_worktree_id,
    _wait_for_leader,
)
from .process_identity import (
    boot_id,
    file_sha256,
    resolve_executable,
    verified_environment,
)
from .runtime import InvocationSpec, Lifecycle, ProcessIdentity, RuntimeErrorCode

__all__ = ["SupervisedRun", "TERMINATED_EXIT_CODE", "streams_match", "supervise"]

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


# ---------------------------------------------------------------------------
# Job Object supervision (`.agent/design/windows-supervisor.md`, section 3.5).
#
# The evidence half above records a run; this half starts and stops one.  It is
# the Windows counterpart of what the POSIX supervisor gets from process groups:
# a Job Object holds the leader and, because the leader is joined while it is
# still suspended, every descendant it later spawns, so ending the job ends the
# whole tree at once.  None of it is reachable until ``supervise`` runs, and
# ``ctypes`` is not even imported until then (see `_win32`).
# ---------------------------------------------------------------------------

# The exit code every member is given when the job is ended on a timeout or a
# cancellation: Windows' own ``STATUS_CONTROL_C_EXIT`` (0xC000013A), the value a
# console process gets when it is Ctrl-C'd.  It is a fixed, distinctive number,
# so a killed leader is told apart from a program that chose to exit 1.
TERMINATED_EXIT_CODE = 0xC000013A

# ``subprocess`` has no name for the flag that creates a process with its primary
# thread suspended, so it is spelled here.  It is the whole reason a descendant
# cannot escape the job: the process exists but runs not one instruction until it
# has joined, so a child it might spawn is created inside the job from the start.
_CREATE_SUSPENDED = 0x4

# The one limit flag this supervisor sets and the two information classes it uses
# (see `_Job`): kill the members when the job handle closes, and read the live
# member count back.
_KILL_ON_JOB_CLOSE = 0x2000
_JOB_EXTENDED_LIMIT_INFORMATION = 9
_JOB_BASIC_ACCOUNTING_INFORMATION = 1

# The lazily built bindings, cached after the first ``supervise`` so every later
# run reuses the same object - and so importing this module still loads no
# ``ctypes`` at all.
_WIN32: Optional["_Win32Bindings"] = None


class _Win32Bindings:
    """The lazily built Win32 ``ctypes`` bindings, held together as one object.

    ``k32`` and ``ntdll`` are the two DLL handles; ``extended_limit`` and
    ``basic_accounting`` are the structure *types* the two job information
    classes are built from; ``byref``, ``sizeof`` and ``last_error`` are the
    ``ctypes`` helpers `_Job` calls.  It is a plain attribute holder so the shape
    is obvious and nothing runs at import time.
    """

    k32: Any
    ntdll: Any
    extended_limit: Any
    basic_accounting: Any
    byref: Any
    sizeof: Any
    last_error: Any


def _win32() -> "_Win32Bindings":
    """Build (once) and return the ``ctypes`` bindings the Job Object half needs.

    ``ctypes`` is imported *here*, not at module load, so importing this module
    reads no library and starts nothing - the import-safety the evidence half
    already promised.  Every entry point is declared with an explicit prototype
    (an undeclared one would pass or read back the wrong-width argument), and the
    result is cached in a module variable so a second ``supervise`` reuses it.
    """
    global _WIN32
    if _WIN32 is not None:
        return _WIN32
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.QueryInformationJobObject.restype = wintypes.BOOL
    k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.c_void_p]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.TerminateJobObject.restype = wintypes.BOOL
    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    # The resume is an undocumented ``ntdll`` export; 0 is its success code.
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.restype = ctypes.c_long
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _BasicAccounting(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    api = _Win32Bindings()
    api.k32 = k32
    api.ntdll = ntdll
    api.extended_limit = _ExtendedLimit
    api.basic_accounting = _BasicAccounting
    api.byref = ctypes.byref
    api.sizeof = ctypes.sizeof
    api.last_error = ctypes.get_last_error
    _WIN32 = api
    return api


class _Job:
    """One Win32 Job Object with ``KILL_ON_JOB_CLOSE`` and the processes in it.

    A job groups every process assigned to it - the leader and, because it is
    assigned while suspended, every descendant it later spawns - so ending the
    job ends the whole tree at once.  ``KILL_ON_JOB_CLOSE`` is what makes the
    kernel clean up the tree if this supervisor dies holding the job handle.
    """

    def __init__(self, api: Any) -> None:
        self.api = api
        self.handle = api.k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(f"CreateJobObjectW failed (last error {api.last_error()})")
        info = api.extended_limit()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not api.k32.SetInformationJobObject(self.handle, _JOB_EXTENDED_LIMIT_INFORMATION,
                                               api.byref(info), api.sizeof(info)):
            api.k32.CloseHandle(self.handle)
            self.handle = None
            raise OSError(f"SetInformationJobObject failed (last error {api.last_error()})")

    def assign_and_resume(self, process_handle: Any) -> bool:
        """Assign the process to the job, then resume it; return whether it runs.

        The order is fixed and is the whole point of ``CREATE_SUSPENDED``: a
        process resumed *before* it joins the job could spawn a descendant
        outside it.  If the assignment fails the process is left suspended and
        ``False`` is returned so the caller terminates it; it is never resumed
        outside a job.
        """
        if not self.api.k32.AssignProcessToJobObject(self.handle, process_handle):
            return False
        return self.api.ntdll.NtResumeProcess(process_handle) == 0

    def active_processes(self) -> Optional[int]:
        """Return the job's live member count, or ``None`` if it cannot be read.

        A count that cannot be read is not zero: the caller must treat it as an
        unconfirmed cleanup rather than claim the tree is empty.
        """
        acct = self.api.basic_accounting()
        if not self.api.k32.QueryInformationJobObject(self.handle, _JOB_BASIC_ACCOUNTING_INFORMATION,
                                                      self.api.byref(acct), self.api.sizeof(acct), None):
            return None
        return int(acct.ActiveProcesses)

    def terminate(self, exit_code: int) -> None:
        """End every member at once with ``exit_code``; a failure is ignored."""
        self.api.k32.TerminateJobObject(self.handle, exit_code)

    def terminate_process(self, process_handle: Any) -> None:
        """End one process that could not join the job (still suspended)."""
        self.api.k32.TerminateProcess(process_handle, 1)

    def close(self) -> None:
        """Close the job handle once; ``KILL_ON_JOB_CLOSE`` ends any member left."""
        if self.handle is not None:
            self.api.k32.CloseHandle(self.handle)
            self.handle = None


def _start_in_job(
    job: _Job,
    spec: InvocationSpec,
    executable: str,
    snapshot: Mapping[str, str],
    stdout_fd: int,
    stderr_fd: int,
) -> "subprocess.Popen[bytes]":
    """Start the leader suspended, join it to the job, resume it; return it.

    ``CREATE_SUSPENDED`` means the process exists but runs nothing until it is in
    the job, so ``assign_and_resume`` is the first thing that lets it run.  An
    ``OSError`` from ``Popen`` (an image that cannot be created) propagates
    unchanged, exactly like the POSIX branch.  A process created but unable to
    join or resume is terminated while still suspended - not one instruction of
    it has run - waited for, and then reported as a failed start by raising
    ``OSError`` so the caller takes the same ``start_failed`` exit.
    """
    proc = subprocess.Popen(
        list(spec.command),
        executable=executable,
        cwd=spec.cwd,
        env=snapshot,
        stdin=subprocess.DEVNULL,
        stdout=stdout_fd,
        stderr=stderr_fd,
        creationflags=_CREATE_SUSPENDED,
        close_fds=True,
    )
    if not job.assign_and_resume(int(proc._handle)):
        job.terminate_process(int(proc._handle))
        _wait_for_leader(proc, _KILL_WAIT_SECONDS)
        raise OSError("the process could not be assigned to the job")
    return proc


def _stop_job(job: _Job, proc: "subprocess.Popen[bytes]", stop_reason: str) -> str:
    """End the whole job and wait for the leader; return the upgraded reason.

    There is no graceful stop and no grace period (user decision, section 5 item
    4): a timeout or a cancellation is ended in one step with
    ``TerminateJobObject`` at ``TERMINATED_EXIT_CODE``, which is what turns a
    ``timeout`` into a ``timeout_killed``.  A cancellation is already an operator
    decision, so its reason is kept.  The leader is then waited for inside the
    kill window; the descendants are reaped separately by `_reap_job`.
    """
    job.terminate(TERMINATED_EXIT_CODE)
    if stop_reason == "timeout":
        stop_reason = "timeout_killed"
    _wait_for_leader(proc, _KILL_WAIT_SECONDS)
    return stop_reason


def _reap_job(job: _Job) -> tuple[bool, bool]:
    """Reap leftover job members; return ``(stragglers_killed, cleanup_confirmed)``.

    The leader has already been waited for, so a surviving member is a descendant
    a leader left behind.  They are ended with ``TerminateJobObject`` and the
    member count is polled until it reaches zero; if it has not after
    ``_REAP_SECONDS`` the cleanup is reported unconfirmed rather than claimed
    clean, and a count that cannot be read (``None``) cannot confirm it either.

    A ``KeyboardInterrupt`` raised by a probe or by the poll interval is absorbed
    and the poll restarts: neither the interval bound nor the deadline moves, so
    the run still reports a confirmed cleanup whenever the job really empties.
    """
    active = job.active_processes()
    if active == 0:
        return False, True
    if active is None:
        return False, False
    job.terminate(TERMINATED_EXIT_CODE)
    deadline = time.monotonic() + _REAP_SECONDS
    while True:
        try:
            active = job.active_processes()
            if active == 0:
                return True, True
            if active is None or time.monotonic() >= deadline:
                return True, False
            time.sleep(min(_REAP_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        except KeyboardInterrupt:
            continue


def supervise(
    spec: InvocationSpec,
    *,
    environment: Mapping[str, str],
    evidence_dir: str,
    worktree_id: str,
    allowed_secret_names: frozenset[str] = frozenset(),
    before_start: Optional[Callable[[], None]] = None,
) -> SupervisedRun:
    """Start one explicitly described process under a Job Object to its end.

    Every check runs before anything is created or started, in the same order the
    POSIX supervisor uses so a caller learns the most specific rejection:
    platform, invocation shape, working directory, environment digest against the
    locked spec, credential names, worktree id, executable identity, evidence
    directory, ``before_start`` hook.  The Windows ``ctypes`` bindings are loaded
    only after all of them pass, and nothing is written to the evidence directory
    and no process is started until then.

    The leader is created ``CREATE_SUSPENDED``, assigned to a Job with
    ``KILL_ON_JOB_CLOSE`` and only then resumed, so a descendant it spawns is
    inside the job from its first instruction.  On a timeout or a cancellation the
    whole job is ended with ``TerminateJobObject`` at ``TERMINATED_EXIT_CODE``
    and the leftovers are reaped by the job's member count; a cancellation is
    reported, never re-raised, and an interrupt during the stop or reap windows
    is absorbed so those steps finish.

    Returns the same `SupervisedRun` the POSIX supervisor returns, with an
    ``observation`` that can be handed straight to
    `sentinelmesh.runtime.reconcile`.  ``before_start`` is an optional caller
    hook invoked once after both evidence files exist and are pinned; if it
    raises, the two files are removed (descriptors closed first, as Windows
    requires) and the same exception is re-raised, so no process starts and no
    half-written evidence survives.
    """
    started_at = time.monotonic()
    if os.name != "nt":
        # Refusing on any other platform is what keeps "native Windows
        # supervision" from silently degrading into a guess; the POSIX supervisor
        # refuses Windows the same way.
        raise RuntimeErrorCode("E_RUNTIME_PLATFORM", "the native Windows supervisor requires Windows")
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
    # Facts that must be captured before the start: the exec'd file's digest and
    # this boot session, so the identity can never be taken from the child.
    executable = resolve_executable(spec.command[0], cwd=spec.cwd, environment=snapshot)
    executable_sha256 = file_sha256(executable)
    boot = boot_id()
    evidence = _validated_evidence_dir(evidence_dir)
    # A caller hook that is present but not callable is a programming error, and
    # it is checked after the evidence directory - the last of the start-time
    # checks - for the same reason the POSIX supervisor checks it there.
    if before_start is not None and not callable(before_start):
        raise RuntimeErrorCode("E_RUNTIME_INVOCATION", "before_start must be callable")
    # Only now, with every check passed, are the Win32 bindings loaded: ``ctypes``
    # is imported here for the first time, not at module import.
    api = _win32()

    # The identities of the two write descriptors come back with them, taken once
    # inside the rollback, so a cleanup below can tell the file this call created
    # from whatever has taken its place at the path.
    (stdout_path, stderr_path, stdout_fd, stderr_fd, stdout_pin, stderr_pin,
     stdout_identity, stderr_identity) = _open_evidence_files(evidence)
    job: Optional[_Job] = None
    # The two pins outlive the start: they are what the stream facts are read
    # through once the job is reaped, so every way out closes them (a failed
    # hook, a failed start, a normal return, an interrupt while the facts are
    # digested).  The job handle is closed the same way.
    try:
        if before_start is not None:
            try:
                before_start()
            except BaseException:
                # Whatever the hook raised (including ``KeyboardInterrupt``), the
                # start did not happen: close every descriptor first (Windows
                # cannot unlink an open file) and then remove only the files the
                # path still holds as this call created them.  The four
                # descriptors are set to ``None`` so the ``finally`` below does
                # not close a number ``os.open`` may already have reused.
                _abandon_evidence(
                    ((stdout_path, stdout_identity), (stderr_path, stderr_identity)),
                    (stderr_pin, stderr_fd, stdout_pin, stdout_fd),
                )
                stdout_fd = stderr_fd = stdout_pin = stderr_pin = None
                raise
        environment_names = tuple(sorted(snapshot))
        lifecycle = Lifecycle().transition("STARTING")
        try:
            job = _Job(api)
            proc = _start_in_job(job, spec, executable, snapshot, stdout_fd, stderr_fd)
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
            # start.  ``None`` guards a number the OS may already have reused.
            _close_fd(stdout_fd)
            _close_fd(stderr_fd)
            stdout_fd = stderr_fd = None

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
            stop_reason = _stop_job(job, proc, stop_reason)
        stragglers_killed, cleanup_confirmed = _reap_job(job)

        # Only now, with the job reaped, are the two streams digested - through
        # the pins, so the facts describe what the child actually wrote even if
        # it renamed or removed the file the path still names.
        streams: Optional[Mapping[str, Optional[Mapping[str, Any]]]] = _streams_of(stdout_pin, stderr_pin)
        exit_code = proc.returncode
        state = "STOPPED" if stop_reason == "completed" and exit_code == 0 else "FAILED"
        if cleanup_confirmed:
            lifecycle = lifecycle.transition(state)
        else:
            # An unconfirmed job may still hold a live descendant, so only the
            # lifecycle records that this run's start state is unknown: the
            # observation keeps the leader's outcome next to the cleanup fact,
            # and ``reconcile`` refuses a replay on that fact.
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
        if job is not None:
            job.close()
