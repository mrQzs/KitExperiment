"""Durable start records and the replay gate for the native POSIX supervisor.

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
  `sentinelmesh.runtime.reconcile` consumes;
* both are canonical JSON (`canonical_json`: sorted keys, compact, UTF-8, trailing
  newline), mode ``0600``, written to a ``.<name>.partial`` file created
  ``O_EXCL`` and fsynced, then published with ``os.link``.  The link is atomic,
  so a reader never sees a half-written record, and an existing record is never
  overwritten, read or removed;
* reads are equally strict: ``O_NOFOLLOW`` (a symlink is evidence tampering),
  regular file only, at most `_MAX_RECORD_BYTES`, canonical bytes only, and the
  exact key set.  Anything else fails closed with ``E_RUNTIME_EVIDENCE``.

`replay_decision` never re-derives a status from anything but the two records and
the expected `InvocationSpec`, and it is read-only: it creates, modifies and
deletes nothing.  Persistence across a power loss is *not* verified here (no
directory fsync, no ``F_FULLFSYNC``); Linux and Windows Job Object supervision
stay ``NOT_RUN``.

Everything is fail-closed: a rejection raises
`sentinelmesh.runtime.RuntimeErrorCode` with a stable code.  Importing this
module has no side effect: no ``ctypes`` import, no file read, no environment
read and no process start.
"""
from __future__ import annotations

import os
import stat
from typing import Any, Mapping, Optional

from .core import canonical_json, strict_json_loads
from .posix_supervisor import SupervisedRun, supervise
from .runtime import InvocationSpec, RuntimeErrorCode, classify_start, reconcile

# The two record file names inside the caller's evidence directory.
HANDOFF_NAME = "handoff.json"
OBSERVATION_NAME = "observation.json"

# Schema tags.  Bumping one changes the bytes every record is compared against,
# so an old record can never satisfy a new reader.
_HANDOFF_SCHEMA = "sentinelmesh-handoff-v1"
_OBSERVATION_SCHEMA = "sentinelmesh-observation-v1"

# Record byte ceiling: 64 KiB, the same bound the design states.  A record that
# does not fit is not a record produced by `record_supervised_run`.
_MAX_RECORD_BYTES = 65536
_RECORD_MODE = 0o600

# Closed key sets.  A record that carries more or fewer keys was not written by
# this module, and `reconcile` would happily ignore the extras, so it is refused
# instead of being read.
_HANDOFF_KEYS = frozenset({"schema", "request_id", "run_id", "environment_sha256", "spec_sha256"})
_OBSERVATION_KEYS = frozenset({"schema", "spec_sha256", "observation"})
_OBSERVATION_INNER_KEYS = frozenset({"run_id", "environment_sha256", "state"})
_IDENTITY_INNER_KEYS = frozenset({"pid", "boot_id", "executable_sha256", "worktree_id"})


def _handoff_content(spec: InvocationSpec) -> dict[str, Any]:
    """Return the handoff record for ``spec``: the start about to be attempted."""
    return {
        "schema": _HANDOFF_SCHEMA,
        "request_id": spec.request_id,
        "run_id": spec.run_id,
        "environment_sha256": spec.environment_sha256,
        "spec_sha256": spec.digest(),
    }


def _observation_content(spec: InvocationSpec, observation: Mapping[str, Any]) -> dict[str, Any]:
    """Return the observation record for a finished run."""
    return {
        "schema": _OBSERVATION_SCHEMA,
        "spec_sha256": spec.digest(),
        "observation": dict(observation),
    }


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
    raw = canonical_json(content)
    path = os.path.join(evidence_dir, name)
    partial = os.path.join(evidence_dir, "." + name + ".partial")
    descriptor: Optional[int] = None
    created = False
    try:
        try:
            descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, _RECORD_MODE)
            created = True
            _write_all(descriptor, raw)
            os.fsync(descriptor)
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
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
    try:
        # ``O_NOFOLLOW``: a record that arrives through a link can be swapped
        # between the check and the read, so it is refused rather than followed.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
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
        if info.st_size > _MAX_RECORD_BYTES:
            raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", f"{name} exceeds {_MAX_RECORD_BYTES} bytes")
        raw = _read_all(descriptor)
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
        _validated_observation(parsed["observation"])
    return parsed


def _read_all(descriptor: int) -> bytes:
    """Read every remaining byte of ``descriptor``."""
    chunks = []
    while True:
        chunk = os.read(descriptor, 1 << 16)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


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
    conservative answer.
    """
    def write_handoff() -> None:
        # ``supervise`` only calls this once its own input checks have passed, so
        # the specification is an ``InvocationSpec`` by the time it runs and the
        # content can be built here instead of before the validations ran.
        _write_record(evidence_dir, HANDOFF_NAME, _handoff_content(spec))

    run = supervise(
        spec,
        environment=environment,
        evidence_dir=evidence_dir,
        worktree_id=worktree_id,
        allowed_secret_names=allowed_secret_names,
        before_start=write_handoff,
    )
    _write_record(evidence_dir, OBSERVATION_NAME, _observation_content(spec, run.observation))
    return run


def replay_decision(spec: InvocationSpec, evidence_dir: str) -> dict[str, Any]:
    """Return the replay record for ``spec``, decided from the records on disk.

    Only the two record files and the expected specification are consulted; no
    environment, process, output file or clock is read, and nothing in the
    evidence directory is created, modified or removed.  A record that cannot be
    read, is not canonical, or does not have the exact shape this module writes
    fails closed with ``E_RUNTIME_EVIDENCE``, as does an observation whose handoff
    is missing: a run that reports an outcome nobody ever handed off cannot be
    replayed on that evidence.
    """
    if type(spec) is not InvocationSpec:
        raise RuntimeErrorCode("E_RUNTIME_INVOCATION", "spec must be an InvocationSpec")
    if type(evidence_dir) is not str or not os.path.isabs(evidence_dir) or not os.path.isdir(evidence_dir):
        raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "evidence_dir must be an absolute existing directory")
    handoff = _read_record(evidence_dir, HANDOFF_NAME)
    observation = _read_record(evidence_dir, OBSERVATION_NAME)
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
