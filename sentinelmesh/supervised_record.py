"""Durable start records and the replay gate for the native supervisor.

This module is the third piece described in `.agent/design/posix-runtime-runner.md`
(section 6): it keeps the supervisor's handoff and observation on disk in the
caller's evidence directory, and it answers the replay question from *those two
files alone*.

What a record is:

* ``handoff.json`` is written before the process exists, from inside the
  supervisor's ``before_start`` hook, so a run that is interrupted between the
  start and the end still proves that a handoff was accepted;
* ``observation.json`` is written once the supervisor returns, and carries the
  closed five-key (started) or three-key (not started) observation record
  `sentinelmesh.runtime.reconcile` consumes, plus the stream facts the
  supervisor read from the descriptors it pinned: schema
  ``sentinelmesh-observation-v2`` puts the outer ``streams`` key next to the
  unchanged ``observation``, so a v1 record is refused as damaged evidence
  rather than read as if it carried no facts at all;
* both are canonical JSON (`canonical_json`: sorted keys, compact, UTF-8, trailing
  newline), mode ``0600``, written to a ``.<name>.partial`` file created
  ``O_EXCL`` and fsynced, then published with ``os.link``.  The link is atomic,
  so a reader never sees a half-written record, and an existing record is never
  overwritten, read or removed;
* reads are equally strict: ``O_NOFOLLOW`` (a symlink is evidence tampering),
  regular file only, at most `_MAX_RECORD_BYTES`, canonical bytes only, and the
  exact key set.  Anything else fails closed with ``E_RUNTIME_EVIDENCE``.

On a platform without ``O_NOFOLLOW`` (Windows) every one of those refusals is
made by hand: the path is examined with ``lstat`` before it is opened and
anything that is not a plain regular file - a link, a reparse point, a directory
- is refused without opening it, the file is opened in binary mode so the C
runtime cannot fold CR LF or stop at 0x1A, and the identity of what was really
opened is proved equal to what that ``lstat`` saw, so a link planted at the path
afterwards cannot be followed.  The ``.partial`` file is examined the same way
before it is created, because an exclusive open there creates *through* a
dangling link instead of failing.

`replay_decision` never re-derives a status from anything but the two records
and the expected `InvocationSpec`, and it is read-only: it creates, modifies and
deletes nothing.  ``streams`` takes no part in that decision; it is there so
that `recorded_streams` can hand the facts of a recorded run to a caller that
needs to check the files on disk against them.  Persistence across a power loss
is *not* verified here (no directory fsync, no ``F_FULLFSYNC``); Linux
process-group supervision stays ``NOT_RUN``.

Which supervisor a run goes through is a fact about the platform, settled on
every call by `native_supervisor`: ``nt`` is the Windows Job Object supervisor
(`sentinelmesh.windows_supervisor`), ``posix`` the process-group one
(`sentinelmesh.posix_supervisor`), and any other platform is refused with
``E_RUNTIME_PLATFORM`` before a record is written or a process is started.  The
two supervisors run the same checks in the same order, pin the same evidence
files and return the same `SupervisedRun`, so nothing above this module knows
which of them ran - which is also why a file identity a platform reports wider
than canonical JSON's integer profile is written as its decimal text here
rather than refused (see `_record_identity`).

Everything is fail-closed: a rejection raises
`sentinelmesh.runtime.RuntimeErrorCode` with a stable code.  Importing this
module has no side effect: no ``ctypes`` import, no file read, no environment
read and no process start.
"""
from __future__ import annotations

import os
import stat
from types import ModuleType
from typing import Any, Mapping, Optional

from . import posix_supervisor, windows_supervisor
from .core import MAX_SAFE_INTEGER, canonical_json, strict_json_loads
from .posix_supervisor import SupervisedRun
from .runtime import InvocationSpec, RuntimeErrorCode, classify_start, reconcile

# The two record file names inside the caller's evidence directory.
HANDOFF_NAME = "handoff.json"
OBSERVATION_NAME = "observation.json"

# Schema tags.  Bumping one changes the bytes every record is compared against,
# so an old record can never satisfy a new reader.
_HANDOFF_SCHEMA = "sentinelmesh-handoff-v1"
_OBSERVATION_SCHEMA = "sentinelmesh-observation-v2"

# Record byte ceiling: 64 KiB, the same bound the design states.  It holds on
# both sides: a record that would not fit is refused before it is written (a
# handoff that could never be read back would start a run whose replay can
# never be decided), and no more than this many bytes are ever read.
_MAX_RECORD_BYTES = 65536
_RECORD_MODE = 0o600

# ``O_NOFOLLOW`` is POSIX-only: where the platform has no such flag (Windows) the
# link refusal is done by hand instead -- ``lstat`` the path, refuse anything that
# is not a plain regular file, open it in binary mode, and compare the identity of
# what was really opened with the ``lstat`` one.  Windows also creates *through* a
# dangling link on an exclusive open, which is why the partial path is examined
# before it is created.  Whether the flag exists is a fact about the platform, so
# it is settled once at import time, which still reads no file and starts nothing.
_HAS_NOFOLLOW = hasattr(os, "O_NOFOLLOW")
# ``O_BINARY`` is what keeps the C runtime from reading a record as text, which
# would fold CR LF and stop at 0x1A; it is 0 where the platform has no such flag.
_O_BINARY = getattr(os, "O_BINARY", 0)

# Closed key sets.  A record that carries more or fewer keys was not written by
# this module, and `reconcile` would happily ignore the extras, so it is refused
# instead of being read.
_HANDOFF_KEYS = frozenset({"schema", "request_id", "run_id", "environment_sha256", "spec_sha256"})
_OBSERVATION_KEYS = frozenset({"schema", "spec_sha256", "observation", "streams"})
_OBSERVATION_INNER_KEYS = frozenset({"run_id", "environment_sha256", "state"})
_OBSERVATION_STARTED_KEYS = _OBSERVATION_INNER_KEYS | {"process_identity", "cleanup_confirmed"}
_IDENTITY_INNER_KEYS = frozenset({"pid", "boot_id", "executable_sha256", "worktree_id"})
_STREAM_NAMES = frozenset({"stdout", "stderr"})
_FACT_KEYS = frozenset({"device", "inode", "bytes", "sha256"})
_SHA256_ALPHABET = frozenset("0123456789abcdef")

# A file identity is whatever the platform says it is, and Windows reports one
# 64 bits wide (``st_ino`` is the file reference number, whose high 16 bits are
# the volume serial, and ``st_dev`` the 64-bit volume serial), which is past the
# canonical JSON integer profile.  A number outside the profile is written as its
# decimal text instead: the record stays canonical JSON, the value is neither
# rounded nor hashed into something it is not, and it is read back as the number
# it was, so a recorded fact stays directly comparable with an ``os.lstat``.
_DECIMAL_DIGITS = frozenset("0123456789")
# The widest identity either platform can report: 64 bits, sign included.
_IDENTITY_LIMIT = 2**64 - 1
# The shortest text form of a number beyond the profile (``2**53``) and the
# longest one a 64-bit identity can have (``2**64 - 1``, sign included), so a
# text identity is recognisable by its length alone.
_IDENTITY_MIN_DIGITS = 16
_IDENTITY_MAX_DIGITS = 21


def native_supervisor() -> ModuleType:
    """Return the supervisor module for this platform, chosen on every call.

    The choice is a fact about the running platform, so it is read from
    ``os.name`` here rather than settled once at import time: a caller that
    patches the platform (a test, or a process that reports another one) gets
    the same rule the real platform gets.  Nothing is cached, and nothing is
    imported that this module has not already imported.
    """
    if os.name == "nt":
        return windows_supervisor
    if os.name == "posix":
        return posix_supervisor
    raise RuntimeErrorCode("E_RUNTIME_PLATFORM", "this platform has no native supervisor")


def supervise(
    spec: InvocationSpec,
    *,
    environment: Mapping[str, str],
    evidence_dir: str,
    worktree_id: str,
    allowed_secret_names: frozenset[str] = frozenset(),
    before_start: Optional[Any] = None,
) -> SupervisedRun:
    """Run one invocation under the platform's supervisor, arguments unchanged.

    This is a delegation and nothing else: the specification, the environment
    mapping, the hook and the allowed secret names reach the chosen supervisor
    as the very objects this call was given, and whatever it raises - or returns
    - comes out unchanged.  No check is duplicated here and no exception is
    caught, so a caller sees the supervisor's own rejection codes.
    """
    return native_supervisor().supervise(
        spec,
        environment=environment,
        evidence_dir=evidence_dir,
        worktree_id=worktree_id,
        allowed_secret_names=allowed_secret_names,
        before_start=before_start,
    )


def _record_identity(value: int) -> Any:
    """Return ``value`` as a record holds it: a number, or its decimal text.

    An identity inside the canonical JSON integer profile is written as the
    number it is; one beyond it is written as its shortest decimal text, because
    that is the only form canonical JSON can hold for it.  The text carries a
    leading ``-`` where the number is negative, so both forms are the same
    number either way.
    """
    if abs(value) <= MAX_SAFE_INTEGER:
        return value
    return str(value)


def _recorded_identity(value: Any) -> Optional[int]:
    """Return the file identity a record held, or ``None`` when it is not one.

    A number is accepted as it is.  Text is accepted only in the one form
    `_record_identity` writes: the decimal text of a number beyond the profile,
    with at most one leading ``-``, no leading zero, no sign, no space, no
    exponent and no radix prefix, and no wider than a 64-bit identity.  Every
    other value - a boolean, a float, ``None``, the text of a number the profile
    already covers, a text beyond 64 bits - is damaged evidence, because it
    would give one identity two different written forms.
    """
    if type(value) is int:
        return value
    if type(value) is not str:
        return None
    digits = value[1:] if value.startswith("-") else value
    if not _IDENTITY_MIN_DIGITS <= len(digits) <= _IDENTITY_MAX_DIGITS:
        return None
    if digits[0] == "0" or any(character not in _DECIMAL_DIGITS for character in digits):
        # ``str.isdigit`` would accept fullwidth and other decimal marks; a
        # record holds ASCII digits only.
        return None
    number = int(value)
    if not MAX_SAFE_INTEGER < abs(number) <= _IDENTITY_LIMIT:
        return None
    return number


def _handoff_content(spec: InvocationSpec) -> dict[str, Any]:
    """Return the handoff record for ``spec``: the start about to be attempted."""
    return {
        "schema": _HANDOFF_SCHEMA,
        "request_id": spec.request_id,
        "run_id": spec.run_id,
        "environment_sha256": spec.environment_sha256,
        "spec_sha256": spec.digest(),
    }


def _observation_content(
    spec: InvocationSpec,
    observation: Mapping[str, Any],
    streams: Optional[Mapping[str, Optional[Mapping[str, Any]]]],
) -> dict[str, Any]:
    """Return the observation record for a finished run.

    ``streams`` is the supervisor's own mapping, or ``None`` when no process ever
    started; it is copied into plain values so a record can only ever hold what
    canonical JSON can express.
    """
    return {
        "schema": _OBSERVATION_SCHEMA,
        "spec_sha256": spec.digest(),
        "observation": dict(observation),
        "streams": _record_streams(streams),
    }


def _copy_streams(streams: Optional[Mapping[str, Optional[Mapping[str, Any]]]]) -> Optional[dict[str, Any]]:
    """Return a fresh copy of a stream mapping, so no caller can share one.

    Both callers hand it a mapping that has already been validated (the
    supervisor's own, or one `_validated_streams` just accepted and decoded), so
    the shape is not re-checked here; what matters is that the value written or
    handed back is a new container every time.
    """
    if streams is None:
        return None
    return {name: (None if streams[name] is None else dict(streams[name])) for name in ("stdout", "stderr")}


def _record_streams(streams: Optional[Mapping[str, Optional[Mapping[str, Any]]]]) -> Optional[dict[str, Any]]:
    """Return the stream facts as a record holds them: identities in text form.

    This is `_copy_streams` for the *written* record: the facts are the
    supervisor's own, so their shape is not re-checked, but a file identity is
    put into the form `_record_identity` prescribes - a number inside the
    canonical JSON integer profile, its decimal text beyond it.  Byte counts and
    digests are copied as they are: a count and a hex digest are not platform
    identities, and each has exactly one form.
    """
    if streams is None:
        return None
    recorded: dict[str, Any] = {}
    for name in ("stdout", "stderr"):
        fact = streams[name]
        recorded[name] = None if fact is None else {
            "device": _record_identity(fact["device"]),
            "inode": _record_identity(fact["inode"]),
            "bytes": fact["bytes"],
            "sha256": fact["sha256"],
        }
    return recorded


def _is_sha256_text(value: Any) -> bool:
    """Return whether ``value`` is 64 lowercase hexadecimal characters."""
    if type(value) is not str or len(value) != 64:
        return False
    return all(character in _SHA256_ALPHABET for character in value)


def _validated_streams(value: Any, observation: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Return the recorded stream facts once they match the observation's shape.

    A run that never started has nothing to say about the streams, so only
    ``null`` is accepted for it: a fact about a process that never ran would make
    a cancelled start look like an evidenced one.  A run that did start may
    carry ``null`` for a stream that could not be read, because a stream the
    supervisor could not digest must not fail a run that already happened.

    JSON has one number type, so ``true`` is not an integer here even though
    ``True == 1`` in Python: a byte count of ``true`` would be one byte.

    A file identity is read back the way it was written: a number inside the
    canonical JSON integer profile, or the decimal text of a number beyond it,
    and nothing else - see `_recorded_identity`.  The facts come back as a fresh
    mapping whose identities are integers, so what a caller holds is directly
    comparable with what ``os.lstat`` reports for the same file.
    """
    started = frozenset(observation) == _OBSERVATION_STARTED_KEYS
    if not started:
        if value is not None:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "stream facts require a started observation")
        return None
    if type(value) is not dict or frozenset(value) != _STREAM_NAMES:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "stream facts must cover both streams")
    decoded: dict[str, Any] = {}
    for name in ("stdout", "stderr"):
        fact = value[name]
        if fact is None:
            decoded[name] = None
            continue
        if type(fact) is not dict or frozenset(fact) != _FACT_KEYS:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "stream facts have an unexpected shape")
        # A device number may be negative (it is whatever the filesystem says);
        # an inode or a byte count may not.  Each identity is decoded into the
        # number it stands for, so a record written as text and a record written
        # as a number describe the same file the same way.
        device = _recorded_identity(fact["device"])
        if device is None:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "stream device must be a recorded file identity")
        inode = _recorded_identity(fact["inode"])
        if inode is None or inode < 0:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE",
                                   "stream inode must be a recorded file identity that is not negative")
        if type(fact["bytes"]) is not int or fact["bytes"] < 0:
            # A byte count is never written as text: it is a count, and one
            # count has exactly one form.
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "stream bytes must be a number that is not negative")
        if not _is_sha256_text(fact["sha256"]):
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "stream sha256 must be a lowercase hexadecimal digest")
        decoded[name] = {"device": device, "inode": inode, "bytes": fact["bytes"], "sha256": fact["sha256"]}
    return decoded


def _write_all(descriptor: int, raw: bytes) -> None:
    """Write every byte of ``raw``; a short write is retried, never truncated."""
    view = memoryview(raw)
    while view:
        view = view[os.write(descriptor, view):]


def _discard_partial(path: str) -> None:
    """Remove the ``.partial`` file this call created, ignoring a missing file."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _write_record(evidence_dir: str, name: str, content: Mapping[str, Any]) -> None:
    """Publish one record atomically, or raise ``E_RUNTIME_EVIDENCE``.

    The bytes are canonical JSON, so the published file is byte-identical on
    every platform and can be compared (not merely parsed) when it is read back.
    The write itself goes to a fresh ``.partial`` file created ``O_EXCL``: an
    earlier partial is somebody else's file and is neither read nor overwritten,
    and a partial this call created is removed whether the publish succeeds or
    not, so a failure leaves no debris behind.  ``os.link`` publishes the record
    only when the bytes are already durable, and it is the only step that can see
    the destination: a record that exists is never overwritten or removed.
    """
    try:
        raw = canonical_json(content)
    except ValueError as exc:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} is not canonical JSON: {exc}") from exc
    if len(raw) > _MAX_RECORD_BYTES:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} would exceed {_MAX_RECORD_BYTES} bytes")
    path = os.path.join(evidence_dir, name)
    partial = os.path.join(evidence_dir, "." + name + ".partial")
    descriptor: Optional[int] = None
    created = False
    try:
        try:
            if _HAS_NOFOLLOW:
                descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, _RECORD_MODE)
            else:
                # No ``O_NOFOLLOW`` here, and an exclusive open on Windows creates
                # *through* a dangling link instead of failing, so the partial path
                # is examined first: any entry there - a leftover partial, a
                # directory, a link - is refused without creating anything, and
                # nothing that is already there is read or removed.
                if _entry_exists(partial):
                    raise RuntimeErrorCode("E_RUNTIME_EVIDENCE",
                                           f"{name} could not be recorded: {os.path.basename(partial)} is taken")
                descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, _RECORD_MODE)
            created = True
            _write_all(descriptor, raw)
            os.fsync(descriptor)
        except BaseException:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise
        # A close that fails after a successful write is reported, not ignored:
        # the record is only published once every step has succeeded.
        os.close(descriptor)
        os.link(partial, path)
    except OSError as exc:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} could not be recorded: {exc}") from exc
    finally:
        if created:
            _discard_partial(partial)


def _validated_observation(value: Any) -> dict[str, Any]:
    """Return the observation record once it has the closed supervisor shape.

    The key set alone cannot keep `reconcile` from tripping over a damaged inner
    value, so the inner facts are checked here as well: a record that is not
    exactly what the supervisor wrote is damaged evidence, not an observation to
    interpret.
    """
    if type(value) is not dict:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "observation record must carry an object")
    keys = frozenset(value)
    if keys not in (_OBSERVATION_INNER_KEYS, _OBSERVATION_INNER_KEYS | {"process_identity", "cleanup_confirmed"}):
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "observation record has an unexpected key set")
    for key in ("run_id", "environment_sha256", "state"):
        if type(value[key]) is not str:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"observation {key} must be text")
    if "process_identity" not in keys and "cleanup_confirmed" not in keys:
        return value
    identity = value.get("process_identity")
    if type(identity) is not dict or frozenset(identity) != _IDENTITY_INNER_KEYS:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "process_identity must be a closed object")
    if type(identity["pid"]) is not int:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "process_identity pid must be a number")
    for key in ("boot_id", "executable_sha256", "worktree_id"):
        if type(identity[key]) is not str:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"process_identity {key} must be text")
    if type(value["cleanup_confirmed"]) is not bool:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "cleanup_confirmed must be boolean")
    return value


def _read_record(evidence_dir: str, name: str) -> Optional[dict[str, Any]]:
    """Return one validated record, ``None`` when it is absent.

    A missing record is how an interrupted run looks, so it is not an error; a
    symlink, a directory, an oversized file, non-canonical bytes or a shape this
    module never wrote is damaged evidence and fails closed.
    """
    path = os.path.join(evidence_dir, name)
    # What the ``lstat`` of the path saw, on a platform where the link refusal is
    # made by hand; ``None`` where ``O_NOFOLLOW`` already refuses the link.
    seen: Optional[os.stat_result] = None
    try:
        if _HAS_NOFOLLOW:
            # ``O_NOFOLLOW``: a record that arrives through a link can be swapped
            # between the check and the read, so it is refused rather than followed.
            # ``O_NONBLOCK``: opening a FIFO for reading would otherwise wait for a
            # writer forever, before the regular-file check below could refuse it;
            # it changes nothing for a regular file.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        else:
            # No ``O_NOFOLLOW`` here, so the path is examined before it is opened:
            # a missing path, a path that is not a plain regular file and a reparse
            # point (a link, a mount point) are all refused without opening
            # anything.  ``O_BINARY`` keeps the C runtime from reading the file as
            # text, which would fold CR LF and stop at 0x1A.
            try:
                seen = os.lstat(path)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} could not be opened: {exc}") from exc
            if (not stat.S_ISREG(seen.st_mode)
                    or getattr(seen, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} is not a regular file")
            try:
                descriptor = os.open(path, os.O_RDONLY | _O_BINARY)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} could not be opened: {exc}") from exc
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} could not be opened: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        # A directory (or a device, or a fifo) is not a record; ``stat`` on the
        # path again could race with what was really opened.
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} is not a regular file")
        if seen is not None and (info.st_dev, info.st_ino) != (seen.st_dev, seen.st_ino):
            # What was opened is not the file the ``lstat`` saw: a link planted at
            # the path after it, or the path swapped between the two calls.  The
            # identity is the only thing that settles which file was read.
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} is not the file that was seen at its path")
        if info.st_size > _MAX_RECORD_BYTES:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} exceeds {_MAX_RECORD_BYTES} bytes")
        # The size above is only an early refusal: a file that grows after the
        # fstat is still read no further than one byte past the ceiling.
        raw = _read_at_most(descriptor, _MAX_RECORD_BYTES + 1)
        if len(raw) > _MAX_RECORD_BYTES:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} exceeds {_MAX_RECORD_BYTES} bytes")
    except RuntimeErrorCode:
        raise
    except OSError as exc:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} could not be read: {exc}") from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    try:
        parsed = strict_json_loads(raw)
    except ValueError as exc:
        # ``strict_json_loads`` reports every rejection as a ``ProtocolError``.
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} is not strict JSON: {exc}") from exc
    if type(parsed) is not dict:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} must contain an object")
    try:
        canonical = canonical_json(parsed)
    except ValueError as exc:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} is not canonical JSON: {exc}") from exc
    if canonical != raw:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} is not in canonical form")
    keys = frozenset(parsed)
    if name == HANDOFF_NAME:
        if keys != _HANDOFF_KEYS or parsed["schema"] != _HANDOFF_SCHEMA:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "handoff record has an unexpected shape")
        if any(type(value) is not str for value in parsed.values()):
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "handoff record values must be text")
    else:
        if keys != _OBSERVATION_KEYS or parsed["schema"] != _OBSERVATION_SCHEMA:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "observation record has an unexpected shape")
        if type(parsed["spec_sha256"]) is not str:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "observation spec_sha256 must be text")
        observation = _validated_observation(parsed["observation"])
        # The facts are read with the same strictness as the observation itself:
        # a record whose ``streams`` does not match its ``observation`` was not
        # written by this module, and the facts are the only thing a recovery
        # path would trust.  The canonical bytes were compared above, before
        # anything was decoded, so a record is known to be byte for byte what
        # this module wrote before an identity is turned back into a number.
        parsed["streams"] = _validated_streams(parsed["streams"], observation)
    return parsed


def _entry_exists(path: str) -> bool:
    """Return whether ``path`` names any directory entry, a dangling link included."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{os.path.basename(path)} could not be checked: {exc}") from exc
    return True


def _read_at_most(descriptor: int, limit: int) -> bytes:
    """Read until EOF or until ``limit`` bytes are in hand, whichever comes first."""
    chunks = []
    total = 0
    while total < limit:
        chunk = os.read(descriptor, min(1 << 16, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _decision(status: str, spec: InvocationSpec, observed: Optional[str]) -> dict[str, Any]:
    """Return the five-key replay record for a decision taken from records."""
    return {
        "status": status,
        "run_id": spec.run_id,
        "expected_environment_sha256": spec.environment_sha256,
        "observed_environment_sha256": observed,
        "replay_allowed": status == "NOT_STARTED",
    }


def record_supervised_run(
    spec: InvocationSpec,
    *,
    environment: Mapping[str, str],
    evidence_dir: str,
    worktree_id: str,
    allowed_secret_names: frozenset[str] = frozenset(),
) -> SupervisedRun:
    """Run one supervised invocation and record both the handoff and the outcome.

    The handoff is written from the supervisor's ``before_start`` hook, so it
    exists before the process does; the observation is written as soon as
    ``supervise()`` returns.  Whatever ``supervise()`` and the hook raise comes
    out unchanged: a hook that cannot record its handoff fails the start with
    ``E_RUNTIME_EVIDENCE`` and starts no process, and an observation that cannot
    be recorded is the same code -- the run is over by then, its handoff is on
    disk, and `replay_decision` reports ``START_UNKNOWN`` for it, which is the
    conservative answer.  That last error carries the finished run as its
    ``run`` attribute: the supervised process can write into the evidence
    directory, so a caller must not have to learn from the disk alone whether
    a process started.

    The observation record is schema v2: it carries ``run.streams`` - the facts
    of the two pinned streams - next to the unchanged observation.
    """
    def write_handoff() -> None:
        # ``supervise`` only calls this once its own input checks have passed, so
        # the specification is an ``InvocationSpec`` by the time it runs and the
        # content can be built here instead of before the validations ran.
        # Neither record may exist yet: a stale observation would survive this
        # run's failed publish and be read back as its outcome, allowing a replay
        # of a run that did start (found reviewing attempt-001).
        for name in (HANDOFF_NAME, OBSERVATION_NAME):
            if _entry_exists(os.path.join(evidence_dir, name)):
                raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} already exists in the evidence directory")
        _write_record(evidence_dir, HANDOFF_NAME, _handoff_content(spec))

    run = supervise(
        spec,
        environment=environment,
        evidence_dir=evidence_dir,
        worktree_id=worktree_id,
        allowed_secret_names=allowed_secret_names,
        before_start=write_handoff,
    )
    try:
        _write_record(evidence_dir, OBSERVATION_NAME, _observation_content(spec, run.observation, run.streams))
    except RuntimeErrorCode as exc:
        exc.run = run
        raise
    return run


def _records_for(spec: InvocationSpec, evidence_dir: str) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """Return ``(handoff, observation)`` after the caller and the files are checked.

    Both questions a caller can ask about a finished run - may it be replayed,
    what did the streams look like - are answered from these two records alone, so
    they share the same input checks and the same reads: a directory one function
    accepts and the other refuses would answer two different questions about the
    same evidence.
    """
    if type(spec) is not InvocationSpec:
        raise RuntimeErrorCode("E_RUNTIME_INVOCATION", "spec must be an InvocationSpec")
    if type(evidence_dir) is not str or not os.path.isabs(evidence_dir) or not os.path.isdir(evidence_dir):
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "evidence_dir must be an absolute existing directory")
    return _read_record(evidence_dir, HANDOFF_NAME), _read_record(evidence_dir, OBSERVATION_NAME)


def replay_decision(spec: InvocationSpec, evidence_dir: str) -> dict[str, Any]:
    """Return the replay record for ``spec``, decided from the records on disk.

    Only the two record files and the expected specification are consulted; no
    environment, process, output file or clock is read, and nothing in the
    evidence directory is created, modified or removed.  A record that cannot be
    read, is not canonical, or does not have the exact shape this module writes
    fails closed with ``E_RUNTIME_EVIDENCE``, as does an observation whose handoff
    is missing: a run that reports an outcome nobody ever handed off cannot be
    replayed on that evidence.

    The stream facts are validated with the record and then ignored here: whether
    the evidence files still hold what they held is a question for the chosen
    platform supervisor's ``streams_match`` (see `native_supervisor`), not for
    this decision.
    """
    handoff, observation = _records_for(spec, evidence_dir)
    if handoff is None:
        if observation is not None:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "an observation record exists without its handoff")
        return _decision(
            classify_start(handoff_accepted=False, started_observation=None, expected=spec), spec, None
        )
    if handoff != _handoff_content(spec):
        # The handoff belongs to another invocation (or another environment, or
        # another command): the acceptance is real, but not of *this* run.
        return _decision("IDENTITY_MISMATCH", spec, handoff["environment_sha256"])
    if observation is None:
        # The handoff was accepted and nothing says what became of the start.
        return _decision(
            classify_start(handoff_accepted=True, started_observation=None, expected=spec), spec, None
        )
    if observation["spec_sha256"] != spec.digest():
        return _decision("IDENTITY_MISMATCH", spec, observation["observation"]["environment_sha256"])
    return dict(reconcile(spec, observation["observation"]))


def recorded_streams(spec: InvocationSpec, evidence_dir: str) -> Optional[dict[str, Any]]:
    """Return the stream facts the recorded run carried, or ``None``.

    This is the counterpart of `replay_decision` for the recovery path: it reads
    the same two records with the same strictness, but it answers what the two
    evidence files held when the run ended, so that the chosen platform
    supervisor's ``streams_match`` can be asked whether they still do.  The facts
    are a recovery hint, not a decision: ``None`` means there is no recorded fact
    for this specification (no records at all, an interrupted run, or records
    belonging to another invocation) and is never an error.

    Read-only, like every other question this module answers: nothing is created,
    modified or removed.  A record that cannot be read or does not have the exact
    shape this module writes fails closed with ``E_RUNTIME_EVIDENCE``, and an
    observation without its handoff is exactly as much of a refusal here as it is
    for `replay_decision`.
    """
    handoff, observation = _records_for(spec, evidence_dir)
    if handoff is None:
        if observation is not None:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "an observation record exists without its handoff")
        return None
    if handoff != _handoff_content(spec):
        # Another invocation's handoff: its acceptance is real, but it says
        # nothing about *this* specification's streams.
        return None
    if observation is None:
        # The handoff was accepted and nothing says what became of the start.
        return None
    if observation["spec_sha256"] != spec.digest():
        return None
    return _copy_streams(observation["streams"])
