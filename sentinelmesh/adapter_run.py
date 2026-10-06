"""Run an adapter invocation under the native POSIX supervisor and reduce its stream.

`.agent/design/adapter-supervisor-wiring.md` (sections 3 and 6) decides how an
adapter-generated invocation reaches the ``Scheduler``:

* the normalized :class:`~sentinelmesh.adapter_protocol.Invocation` is turned
  into an :class:`~sentinelmesh.runtime.InvocationSpec` and handed to
  :func:`~sentinelmesh.supervised_operation.run_supervised_operation`, which owns
  the handoff, the supervised run and the settlement of the operation from its
  two records on disk; this module adds no scheduler transition of its own;
* the vendor's stdout -- ``stdout.log`` in the evidence directory, written by
  the supervisor -- is read back the way ``supervised_record`` reads a record:
  ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK``, a regular file, within a byte ceiling;
* each line is one normalized event object, reduced by the adapter into an
  ``AdapterResult``;
* the usage of that result is recorded in the Scheduler **while the operation is
  still RUNNING**, which is the one moment the bridge offers: ``Scheduler.finish``
  enforces the budget when it moves the operation to review, so a reservation
  dimension that is not final at that point stops the whole request with
  ``USAGE_UNKNOWN``.

The recording rule is fail-closed: only a result that is both ``settled`` and
``raw_complete`` is observed, one all-or-nothing ``settle`` call per dimension
reserved by the caller whose payload value is an exact non-negative integer, with
the SHA-256 of the whole stdout stream as the evidence.  Anything else -- an
unreadable, oversized or invalid stream, a stream the adapter rejects, a run that
never reached the review path -- records nothing and leaves the usage unknown,
which is what keeps the request from claiming again.  Getting it wrong in the
other direction (recording a number nobody can prove) is not available here.

``dispatches`` is a local, non-model metric recorded by the handoff itself, so it
is refused as a usage dimension rather than recorded twice.

Importing this module has no side effect: no ``ctypes`` import, no file read, no
environment read and no process start.  Both of those happen only inside
``run_adapter_operation``, once the caller's arguments have been checked.
"""
from __future__ import annotations

import hashlib
import os
import stat
from typing import Any, Mapping, Optional

from .adapter_protocol import AdapterError, Invocation, OfflineAdapter
from .core import ProtocolError, strict_json_loads
from .runtime import InvocationSpec
from .supervised_operation import run_supervised_operation

__all__ = ["DEFAULT_MAX_STREAM_BYTES", "MAX_STREAM_BYTES", "STREAM_NAME",
           "read_stream", "run_adapter_operation", "supervised_spec"]

# The supervisor's stdout evidence file, created ``O_EXCL`` inside the caller's
# evidence directory.
STREAM_NAME = "stdout.log"
# Byte ceiling for one stdout stream by default, and the largest ceiling a
# caller may ask for.  The upper bound is what keeps a caller from asking this
# process to read an unbounded amount of a child's output into memory.
DEFAULT_MAX_STREAM_BYTES = 1_048_576
MAX_STREAM_BYTES = 16 * 1_048_576

# Reading is chunked so a stream that grows past the ceiling while it is being
# read is not pulled in whole first.
_CHUNK_BYTES = 1 << 16

# The dimension the handoff records itself; recording it here would double count
# one local dispatch.
_RESERVED_DIMENSION = "dispatches"


def _check_max_bytes(max_bytes: Any) -> int:
    """Require an exact ``int`` ceiling inside ``1..MAX_STREAM_BYTES``."""
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_STREAM_BYTES:
        raise AdapterError("E_ADAPTER_SCHEMA",
                           "max_stream_bytes must be an integer in 1..%d" % MAX_STREAM_BYTES)
    return max_bytes


def supervised_spec(invocation: Any, *, cwd: Any, timeout_seconds: Any) -> InvocationSpec:
    """Return the ``InvocationSpec`` that locks ``invocation`` for the supervisor.

    Only the fields the runtime needs are carried over: request id, run id, argv,
    cwd, environment digest and timeout.  Everything else stays in the adapter's
    own contract.  ``InvocationSpec``'s own errors (a relative ``cwd`` being
    settled by the supervisor, a timeout that is not a positive integer,
    an argv the runtime refuses) are the runtime's to raise and are not
    translated here.
    """
    if type(invocation) is not Invocation:
        raise AdapterError("E_ADAPTER_INVOCATION", "invocation must be an adapter_protocol.Invocation")
    return InvocationSpec(invocation.request_id, invocation.run_id, invocation.command, cwd,
                          invocation.environment_sha256, timeout_seconds)


def _read_at_most(descriptor: int, limit: int) -> bytes:
    """Read until EOF or until ``limit`` bytes are in hand, whichever comes first."""
    chunks = []
    total = 0
    while total < limit:
        chunk = os.read(descriptor, min(_CHUNK_BYTES, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _empty(status: str) -> dict[str, Any]:
    return {"status": status, "sha256": None, "bytes": None, "events": None}


def read_stream(evidence_dir: Any, *, max_bytes: int = DEFAULT_MAX_STREAM_BYTES) -> dict[str, Any]:
    """Read the supervised run's stdout stream and parse its normalized events.

    The stream is the vendor's own output, so it is evidence and not input: a
    file state this process cannot account for is reported, never raised, and
    never read from a link (a symlinked stream is a forged one -- the child can
    swap it between the check and the read -- so ``O_NOFOLLOW`` refuses it).

    ``missing`` and ``unreadable`` are answers, not errors: the first is how a
    run that never started looks, the second is a stream that cannot be used as
    evidence at all.  ``too_large`` and ``invalid`` name the two ways a stream
    that was read cannot be reduced; ``invalid`` carries the 1-based line and the
    protocol's own ``code`` (``E_JSON_SYNTAX``, ``E_JSON_LIMIT``, ...) or
    ``E_ADAPTER_STREAM`` when a line parsed to something that is not an event
    object.  In every case nothing is raised except an out-of-range
    ``max_bytes``: what is on disk is a fact about the run, and the decision
    (record no usage) belongs to the caller.
    """
    _check_max_bytes(max_bytes)
    path = os.path.join(evidence_dir, STREAM_NAME)
    try:
        # ``O_NOFOLLOW``: a stream reached through a link is refused rather than
        # followed.  ``O_NONBLOCK``: opening a fifo for reading would otherwise
        # wait for a writer forever, before the regular-file check could refuse
        # it; it changes nothing for a regular file.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return _empty("missing")
    except OSError:
        return _empty("unreadable")
    try:
        info = os.fstat(descriptor)
        # ``fstat`` on the descriptor is used because a second ``stat`` on the
        # path could race with what was really opened.
        if not stat.S_ISREG(info.st_mode):
            return _empty("unreadable")
        if info.st_size > max_bytes:
            return _empty("too_large")
        raw = _read_at_most(descriptor, max_bytes + 1)
        if len(raw) > max_bytes:
            # The size above is only an early refusal: a file that grew after
            # the ``fstat`` is read no further than one byte past the ceiling.
            return _empty("too_large")
    except OSError:
        return _empty("unreadable")
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    return _parse_stream(raw)


def _parse_stream(raw: bytes) -> dict[str, Any]:
    """Return the digest, the byte count and the events, or the line that failed."""
    sha256 = hashlib.sha256(raw).hexdigest()
    # One event object per line; a missing final newline is still a line, an
    # empty one after it is not.
    segments = raw.split(b"\n")
    if segments and segments[-1] == b"":
        segments.pop()
    events: list[dict[str, Any]] = []
    for line, segment in enumerate(segments, start=1):
        try:
            parsed = strict_json_loads(segment)
        except ProtocolError as exc:
            return {"status": "invalid", "sha256": sha256, "bytes": len(raw), "events": None,
                    "line": line, "code": exc.code}
        if type(parsed) is not dict:
            return {"status": "invalid", "sha256": sha256, "bytes": len(raw), "events": None,
                    "line": line, "code": "E_ADAPTER_STREAM"}
        events.append(parsed)
    return {"status": "ok", "sha256": sha256, "bytes": len(raw), "events": events}


def _check_dimensions(usage_dimensions: Any) -> tuple[str, ...]:
    """Require an exact tuple of distinct, non-empty dimensions the handoff does not own."""
    if type(usage_dimensions) is not tuple:
        raise AdapterError("E_ADAPTER_SCHEMA", "usage_dimensions must be a tuple of strings")
    seen: set[str] = set()
    for index, dimension in enumerate(usage_dimensions):
        if type(dimension) is not str or not dimension:
            raise AdapterError("E_ADAPTER_SCHEMA", "usage_dimensions[%d] must be a non-empty string" % index)
        if dimension in seen:
            raise AdapterError("E_ADAPTER_SCHEMA", "usage_dimensions contains a duplicate entry")
        if dimension == _RESERVED_DIMENSION:
            raise AdapterError("E_ADAPTER_SCHEMA", "usage_dimensions must not contain %r" % _RESERVED_DIMENSION)
        seen.add(dimension)
    return usage_dimensions


def _reduce(adapter: OfflineAdapter, invocation: Invocation, evidence_dir: str,
            max_bytes: int) -> tuple[dict[str, Any], Optional[Any], Optional[str]]:
    """Read the stream and reduce it; return ``(stream, result, result_error)``.

    A stream that is not ``ok`` never reaches the adapter: there is no event
    sequence to reduce, and the reason is already named by ``stream``.  An
    ``AdapterError`` from the adapter is a report about the stream, not a
    failure of this call, so its code is carried back and the result stays
    ``None`` -- nothing is recorded from a stream the adapter rejected.
    """
    stream = read_stream(evidence_dir, max_bytes=max_bytes)
    if stream["status"] != "ok":
        return stream, None, None
    try:
        return stream, adapter.run(invocation, stream["events"]), None
    except AdapterError as exc:
        return stream, None, exc.code


def run_adapter_operation(
    scheduler: Any,
    adapter: Any,
    invocation: Any,
    *,
    cwd: Any,
    timeout_seconds: Any,
    environment: Mapping[str, str],
    owner: str,
    generation: int,
    evidence_dir: str,
    worktree_id: str,
    candidate_hash: Any,
    usage_dimensions: tuple[str, ...] = (),
    allowed_secret_names: frozenset[str] = frozenset(),
    now: int = 0,
    max_stream_bytes: int = DEFAULT_MAX_STREAM_BYTES,
) -> dict[str, Any]:
    """Run one adapter invocation under the supervisor and reduce what it wrote.

    Every check on the caller's arguments -- the invocation, the adapter, the
    adapter id bound into the invocation, the usage dimensions, the stream
    ceiling and the runtime specification -- runs *before* the handoff, so a
    refused call starts nothing, leaves the operation in ``RESERVED`` and writes
    no evidence.  Their errors are raised unchanged: ``AdapterError`` for this
    module's own contract, ``RuntimeErrorCode`` for the runtime's.

    ``candidate_hash`` is wrapped.  The bridge calls the wrapper on the one path
    that reaches the end review -- after ``mark_running``, while the operation is
    still RUNNING -- which is the only point where ``Scheduler.finish`` has not
    yet enforced the budget.  There the stream is read and reduced, and the usage
    of a result that is both settled and complete is settled against the stream's
    digest; the caller's own ``candidate_hash(evidence_dir)`` is then evaluated,
    and its exception rises through the bridge, which moves the operation to
    ``START_UNKNOWN``.  On every other path (a handoff proven not to have
    started, an unconfirmed cleanup, a run that never came back) the wrapper is
    never called, no usage is recorded, and the stream is reduced once more,
    afterwards, only so the outcome can report what the run wrote.

    Returns the bridge's own outcome extended with ``stream``, ``result``,
    ``result_error``, ``usage_recorded`` and ``usage_skipped``.
    """
    if type(invocation) is not Invocation:
        raise AdapterError("E_ADAPTER_INVOCATION", "invocation must be an adapter_protocol.Invocation")
    if not isinstance(adapter, OfflineAdapter):
        raise AdapterError("E_ADAPTER_INVOCATION", "adapter must be an OfflineAdapter")
    if invocation.adapter_id is not None and invocation.adapter_id != adapter.adapter_id:
        raise AdapterError("E_ADAPTER_IDENTITY", "invocation adapter does not match the adapter")
    dimensions = _check_dimensions(usage_dimensions)
    _check_max_bytes(max_stream_bytes)
    spec = supervised_spec(invocation, cwd=cwd, timeout_seconds=timeout_seconds)

    # The one reduction on the review path and what it recorded are kept here,
    # so the outcome can report them without reading the stream twice.
    report: dict[str, Optional[tuple[dict[str, Any], Optional[Any], Optional[str], Mapping[str, str], list[str]]]] = {"value": None}

    def wrapped_candidate_hash(directory: str) -> Any:
        stream, result, result_error = _reduce(adapter, invocation, directory, max_stream_bytes)
        if result is not None and result.settled is True and result.raw_complete is True:
            usage = result.usage
            mapping = {dimension: usage[dimension] for dimension in dimensions
                       if dimension in usage and type(usage[dimension]) is int and usage[dimension] >= 0}
            skipped = [dimension for dimension in dimensions if dimension not in mapping]
            recorded = ({}
                        if not mapping
                        else dict(scheduler.settle(invocation.run_id, mapping,
                                                   evidence_hash=stream["sha256"])))
        else:
            skipped = list(dimensions)
            recorded = {}
        report["value"] = (stream, result, result_error, recorded, skipped)
        return candidate_hash(directory)

    outcome = run_supervised_operation(
        scheduler,
        operation_id=invocation.run_id,
        owner=owner,
        generation=generation,
        spec=spec,
        environment=environment,
        evidence_dir=evidence_dir,
        worktree_id=worktree_id,
        candidate_hash=wrapped_candidate_hash,
        allowed_secret_names=allowed_secret_names,
        now=now,
    )
    if report["value"] is None:
        # Any path that did not reach the end review: the stream is read for the
        # report only, and nothing is recorded from it.
        stream, result, result_error = _reduce(adapter, invocation, evidence_dir, max_stream_bytes)
        recorded, skipped = {}, list(dimensions)
    else:
        stream, result, result_error, recorded, skipped = report["value"]
    outcome["stream"] = _stream_summary(stream)
    outcome["result"] = None if result is None else {
        "settled": result.settled,
        "status": result.status,
        "raw_complete": result.raw_complete,
        "usage": dict(result.usage),
        "invocation_sha256": result.invocation_sha256,
    }
    outcome["result_error"] = result_error
    outcome["usage_recorded"] = dict(recorded)
    outcome["usage_skipped"] = list(skipped)
    return outcome


def _stream_summary(stream: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stream's reportable fields: what it is, plus how it failed."""
    summary = {"status": stream["status"], "sha256": stream["sha256"], "bytes": stream["bytes"]}
    if stream["status"] == "invalid":
        summary["line"] = stream["line"]
        summary["code"] = stream["code"]
    return summary
