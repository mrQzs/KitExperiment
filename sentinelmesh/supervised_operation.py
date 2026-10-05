"""Bridge between the native POSIX supervisor and the ``Scheduler`` ledger.

`.agent/design/scheduler-supervisor-wiring.md` (sections 5 and 6) decides what a
supervised run means for the single-host scheduler:

* a handoff proven not to have started closes the operation as ``NOT_STARTED``,
  giving the unused reservation and the resource holds back while keeping the
  dispatch already recorded at handoff;
* a run that ended -- whether it succeeded, failed or was killed on timeout --
  with cleanup confirmed ends in ``REVIEW_PENDING``, with the process identity
  the supervisor observed recorded on the ``run.started`` event;
* a run that ended with cleanup *unconfirmed* is ``START_UNKNOWN`` with reason
  ``CLEANUP_UNCONFIRMED``: holds are kept, the request is stopped, and nothing is
  replayed;
* anything that cannot be classified from the two records on disk is
  ``START_UNKNOWN`` with reason ``START_UNKNOWN``.

This module is the only caller that puts those two halves together.  It owns no
scheduler transition of its own: every state change is one of ``Scheduler``'s
migrations (:meth:`handoff`, :meth:`mark_running`, :meth:`finish`,
:meth:`unknown`, :meth:`release_proven_unstarted`, :meth:`recover`), so the
ledger stays the single authority for what an operation's state is.

Two rules hold everywhere here:

* **the records decide, not the outcome of the call**.  The state an operation
  moves to comes from ``replay_decision()``, which reads ``handoff.json`` and
  ``observation.json`` and nothing else.  A run whose outcome could not be
  recorded is never reported as finished: the conservative ``START_UNKNOWN`` is
  the only answer available to a caller that lost the records.
* **an operation never stays ``RUNNING``**.  ``mark_running`` is a retroactive
  registration of a ``STARTED`` fact the supervisor already observed, so
  ``finish`` can accept it.  If the candidate hash or ``finish`` refuses, the
  operation is moved to ``START_UNKNOWN`` and the original exception is re-raised
  unchanged.

Importing this module has no side effect: no ``ctypes`` import, no file read, no
environment read and no process start.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from typing import Any, Mapping, Optional

from .runtime import InvocationSpec, RuntimeErrorCode
from .supervised_record import (
    HANDOFF_NAME,
    OBSERVATION_NAME,
    record_supervised_run,
    replay_decision,
)

__all__ = ["evidence_digest", "run_supervised_operation", "recover_supervised_operation"]

# Byte ceiling for one record when digesting: the same bound
# ``supervised_record`` reads a record with, so a record that could never be
# read back is not silently digested here either.
_MAX_RECORD_BYTES = 65536

# Replay statuses.  ``STOPPED`` and ``FAILED`` are the two outcomes of a run
# whose process demonstrably existed; ``replay_allowed`` is what keeps an
# unconfirmed cleanup from being read as one of them.
_STOPPED_OR_FAILED = frozenset({"STOPPED", "FAILED"})

# The three scheduler states this module can leave an operation in.
_NOT_STARTED = "NOT_STARTED"
_START_UNKNOWN = "START_UNKNOWN"
_REVIEW_PENDING = "REVIEW_PENDING"
# The state a ``Scheduler.recover`` outcome leaves the operation in.
_RECOVERED_STATES = {"NOT_STARTED": _NOT_STARTED, "START_UNKNOWN": _START_UNKNOWN, "FINISHED": _REVIEW_PENDING}


def _record_digest(path: str) -> Optional[str]:
    """Return the SHA-256 of one record file, ``None`` when absent, ``"unreadable"`` otherwise.

    Absence is how an interrupted run looks, so it is an answer rather than an
    error.  Everything else about the file -- a symlink (tampering, and a link
    can be swapped between the check and the read), a directory, a fifo, a read
    that fails, a file that does not fit the record ceiling, or a file that grows
    past it while being read -- makes this record unusable as evidence and is
    reported as ``"unreadable"``.  Nothing is raised: a digest is a summary of
    whatever is on disk, and the caller's decision comes from
    ``replay_decision``, which fails closed on its own.
    """
    try:
        # ``O_NOFOLLOW``: a record reached through a link is refused rather than
        # followed.  ``O_NONBLOCK``: opening a fifo for reading would otherwise
        # wait for a writer forever, before the regular-file check could refuse
        # it; it changes nothing for a regular file.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"
    try:
        info = os.fstat(descriptor)
        # A directory (or a device, or a fifo) is not a record.  ``fstat`` on
        # the descriptor is used because a second ``stat`` on the path could
        # race with what was really opened.
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RECORD_BYTES:
            return "unreadable"
        raw = _read_at_most(descriptor, _MAX_RECORD_BYTES + 1)
        if len(raw) > _MAX_RECORD_BYTES:
            # The size above is only an early refusal: a file that grew after
            # the ``fstat`` is read no further than one byte past the ceiling.
            return "unreadable"
        return hashlib.sha256(raw).hexdigest()
    except OSError:
        return "unreadable"
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


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


def evidence_digest(evidence_dir: str) -> str:
    """Return the SHA-256 of the two replay records in ``evidence_dir``.

    The value names the pair of files ``replay_decision`` decided from, so a
    scheduler event carrying it can be checked against the same directory later:
    the same records produce the same digest, and any change to either one does
    not.  A record that is missing contributes ``None``; one that cannot be read
    as a regular file within the record ceiling contributes ``"unreadable"``.
    No file state raises: the digest is a summary, never a gate.
    """
    values = {"handoff": _record_digest(os.path.join(evidence_dir, HANDOFF_NAME)),
              "observation": _record_digest(os.path.join(evidence_dir, OBSERVATION_NAME))}
    payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _checked_spec(spec: Any, operation_id: Any) -> None:
    """Refuse a specification that does not belong to this operation.

    The check runs before the handoff, so an invocation for another run starts
    nothing, leaves the operation in ``RESERVED`` and writes no evidence.  A
    subclass is not an ``InvocationSpec``: it could carry a different ``run_id``
    resolution or digest than the record the replay is checked against.
    """
    if type(spec) is not InvocationSpec:
        raise RuntimeErrorCode("E_RUNTIME_INVOCATION", "spec must be an InvocationSpec")
    if spec.run_id != operation_id:
        raise RuntimeErrorCode("E_RUNTIME_INVOCATION", "spec.run_id must equal the operation id")


def _decision_of(spec: InvocationSpec, evidence_dir: str) -> Optional[dict[str, Any]]:
    """Return the replay record, or ``None`` when the records cannot be read.

    Damaged evidence is not a crash for the caller: it is the case where the
    outcome genuinely is unknown, which is exactly what the conservative
    ``START_UNKNOWN`` branch is for.
    """
    try:
        decision = replay_decision(spec, evidence_dir)
    except RuntimeErrorCode:
        return None
    return dict(decision)


def _settle(
    scheduler: Any,
    *,
    operation_id: str,
    owner: str,
    generation: int,
    run: Any,
    decision: Optional[Mapping[str, Any]],
    evidence_dir: str,
    candidate_hash: Any,
) -> str:
    """Move the operation to the state its evidence proves, and return that state.

    Exactly one of the three branches runs, and each names the scheduler
    migration it uses.  The ``mark_running`` before a ``finish`` is the
    retroactive registration of a start the supervisor already observed: it is
    recorded only once a run is known to have existed, and only for a run whose
    outcome is replayable.
    """
    status = None if decision is None else decision.get("status")
    if status == "NOT_STARTED":
        # Proven not to have started: give the unused reservation and the holds
        # back, keep the dispatch recorded at handoff, do not stop the request.
        scheduler.release_proven_unstarted(operation_id, owner, generation,
                                          evidence_hash=evidence_digest(evidence_dir))
        return _NOT_STARTED
    if (run is not None and run.identity is not None and status in _STOPPED_OR_FAILED):
        scheduler.mark_running(operation_id, owner, generation, process_identity=run.identity.to_dict())
        if decision.get("replay_allowed") is True:
            # ``cleanup_verified`` is the replay decision's ``replay_allowed``,
            # never a report from this module: the run ended, its group was
            # confirmed empty, so the candidate may be reviewed.
            try:
                candidate = candidate_hash(evidence_dir)
                scheduler.finish(operation_id, owner, generation, candidate, True)
            except BaseException:
                # A refused candidate still leaves a running operation behind;
                # that is the "did it start?" case, not a review.
                scheduler.unknown(operation_id, owner, generation, reason="START_UNKNOWN")
                raise
            return _REVIEW_PENDING
        # The group may still hold a live descendant, so nothing is replayed and
        # the holds stay until an operator has looked.
        scheduler.unknown(operation_id, owner, generation, reason="CLEANUP_UNCONFIRMED")
        return _START_UNKNOWN
    # No usable evidence (nothing recorded, damaged records, an identity nobody
    # can prove, an outcome that is not a run that ended): investigate, do not
    # re-dispatch.
    scheduler.unknown(operation_id, owner, generation, reason="START_UNKNOWN")
    return _START_UNKNOWN


def run_supervised_operation(
    scheduler: Any,
    *,
    operation_id: str,
    owner: str,
    generation: int,
    spec: InvocationSpec,
    environment: Mapping[str, str],
    evidence_dir: str,
    worktree_id: str,
    candidate_hash: Any,
    allowed_secret_names: frozenset[str] = frozenset(),
    now: int = 0,
) -> dict[str, Any]:
    """Run one supervised invocation and settle the operation from its records.

    The order is fixed: the invocation is checked against the operation, the
    handoff is accepted, the run happens, and only then is the operation's state
    decided -- from ``replay_decision``, so a run whose observation was never
    written is reported as unknown rather than finished.

    ``candidate_hash`` is called with ``evidence_dir`` and must return the
    candidate's hash; it is the caller that knows what the candidate is, not this
    module.  ``record_supervised_run`` is called through this module's own name so
    a caller (or a test) can see the whole call sequence.

    Returns the operation id, the state it ended in, the replay record it was
    decided from, the runtime code ``record_supervised_run`` refused with (if any)
    and the run's stop reason and exit code.  A ``KeyboardInterrupt`` that
    reached the run still settles the operation from the records and is then
    re-raised unchanged.
    """
    _checked_spec(spec, operation_id)
    # The handoff's guards (owner, generation, stopped request, budget) are
    # re-checked here and their errors are the scheduler's to raise: a refused
    # handoff starts nothing and leaves no record.
    scheduler.handoff(operation_id, owner, generation, now)
    error: Optional[str] = None
    run: Any = None
    try:
        run = record_supervised_run(
            spec,
            environment=environment,
            evidence_dir=evidence_dir,
            worktree_id=worktree_id,
            allowed_secret_names=allowed_secret_names,
        )
    except RuntimeErrorCode as exc:
        # A refused input, an unreadable evidence directory, records that already
        # exist: nothing started, so the records alone decide the state.
        error = exc.code
    except BaseException:
        # An interrupt is the caller's to see, but the handoff it follows is
        # already on disk: the operation is settled from those records first.
        decision = _decision_of(spec, evidence_dir)
        _settle(scheduler, operation_id=operation_id, owner=owner, generation=generation, run=None,
                decision=decision, evidence_dir=evidence_dir, candidate_hash=candidate_hash)
        raise
    decision = _decision_of(spec, evidence_dir)
    state = _settle(scheduler, operation_id=operation_id, owner=owner, generation=generation, run=run,
                    decision=decision, evidence_dir=evidence_dir, candidate_hash=candidate_hash)
    return {
        "operation_id": operation_id,
        "state": state,
        "decision": decision,
        "error": error,
        "stop_reason": None if run is None else run.stop_reason,
        "exit_code": None if run is None else run.exit_code,
    }


def recover_supervised_operation(
    scheduler: Any,
    *,
    operation_id: str,
    owner: str,
    generation: int,
    spec: InvocationSpec,
    evidence_dir: str,
    candidate_hash: Any,
) -> dict[str, Any]:
    """Take over an in-flight operation of a fenced generation from its records.

    After a controller rollover the new generation cannot re-derive what became
    of a run it did not start, and the old generation can no longer act: the two
    records on disk are the whole evidence.  This is the only caller that maps a
    replay record onto ``Scheduler.recover``:

    * ``NOT_STARTED`` releases the operation (the run provably never happened);
    * a run that ended with cleanup confirmed goes to ``REVIEW_PENDING`` with the
      candidate hash of the *new* generation's caller;
    * everything else -- an unconfirmed cleanup, damaged records, no records at
      all -- stays ``START_UNKNOWN``.

    The callback is evaluated before ``recover`` is called, so a candidate hash
    that cannot be produced leaves the ledger untouched and the exception reaches
    the caller unchanged; ``recover``'s own errors (a generation that is not
    fenced out, a request that was never stopped) are likewise the scheduler's to
    raise and are not retried or translated here.
    """
    _checked_spec(spec, operation_id)
    decision = _decision_of(spec, evidence_dir)
    digest = evidence_digest(evidence_dir)
    status = None if decision is None else decision.get("status")
    candidate: Optional[str] = None
    if status == "NOT_STARTED":
        outcome = "NOT_STARTED"
    elif status in _STOPPED_OR_FAILED and decision.get("replay_allowed") is True:
        outcome = "FINISHED"
        # Only a finished run carries a candidate, and ``recover`` refuses one
        # for any other outcome, so the hash is computed only on this branch.
        candidate = candidate_hash(evidence_dir)
    else:
        outcome = "START_UNKNOWN"
    scheduler.recover(operation_id, owner, generation, outcome=outcome, evidence_hash=digest,
                      candidate_hash=candidate)
    return {"operation_id": operation_id, "state": _RECOVERED_STATES[outcome], "decision": decision}
