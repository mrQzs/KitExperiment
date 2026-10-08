"""Recovery driver for a controller that crashed and was restarted.

A controller that dies leaves its operations in the durable ledger: claims that
never reached a handoff, and handoffs whose run it never decided.  The ledger
records the run descriptor of every supervised handoff
(``.agent/design/durable-ledger.md``, section 5 item 3), so a restarted
controller does not need the caller to supply the invocation again -- it reads
the descriptor back and settles the operation from the records on disk with
``recover_supervised_operation``.

This driver is the whole of that restart sequence:

* the ledger is rolled over exactly once, which fences the dead generation out
  and stops every request with ``RECOVERY_REQUIRED`` -- a request is never
  reopened here, it is the operator's to reopen;
* a claim of an older generation that never reached a handoff is cancelled: it
  is provably unstarted, and its reservation and resource holds are given back;
* a handoff of an older generation that was never decided (``HANDOFF_ACCEPTED``
  or ``RUNNING``) is settled from its records, and only when the ledger holds a
  run descriptor for it -- without one there is nothing to rebuild the
  invocation from, so the operation is reported as skipped and left alone;
* every operation is reported, in claim order, and the run continues to the next
  one when one of them fails.

What this driver deliberately does **not** touch is an operation the old
controller already decided -- ``START_UNKNOWN`` above all.  That controller held
the supervisor's in-memory observation of a run that did start, while the
records on disk can be written by the supervised child itself; re-deciding from
the records alone would overturn a judgement made with strictly more
information.  The same restraint covers ``REVIEW_PENDING``, ``ACCEPTED``,
``REJECTED``, ``NOT_STARTED`` and ``CANCELLED_NOT_STARTED``: they are closed, and
closing them again is not a recovery.  Nothing is re-run, no worktree is cleaned
and no request is reopened.

Errors are reported per operation rather than raised, with one exception: a
``LedgerError`` means the durable ledger itself is closed or has failed, so the
recovery stops immediately and passes it on -- continuing would report
operations from a ledger that no longer answers.  An exception that is not an
``Exception`` at all (a ``KeyboardInterrupt`` above all) is likewise passed on
unchanged: the restarted controller is being asked to stop.

Importing this module has no side effect: no ``ctypes`` import, no file read, no
environment read and no process start.
"""
from __future__ import annotations

from typing import Any, Callable

from .durable_ledger import LedgerError
from .runtime import InvocationSpec
from .scheduler import ContractError
from .supervised_operation import recover_supervised_operation

__all__ = ["recover_after_restart"]

#: The two states a handoff the dead controller never decided can be in.  They
#: are exactly the source states ``Scheduler.recover`` accepts together with
#: ``START_UNKNOWN``, and the reason ``START_UNKNOWN`` is missing here is the one
#: the module docstring gives: it was already decided.
_UNDECIDED = frozenset({"HANDOFF_ACCEPTED", "RUNNING"})


def _checked_inputs(owner: Any, candidate_hash: Any) -> None:
    """Refuse a recovery that cannot act, before anything is touched.

    The owner is the identity the recovered operations are handed to, and the
    candidate hash is what a run that finished is reviewed by: neither can be
    guessed.  The check runs before the rollover, so a refused call leaves the
    generation, the requests and the events exactly as they were.
    """
    if type(owner) is not str or not owner or not callable(candidate_hash):
        raise ContractError("E_RECOVERY_INPUT")


def _rebuilt_spec(descriptor: Any) -> InvocationSpec:
    """Rebuild the invocation the recorded descriptor describes.

    Only the descriptor's own fields are used, and the command becomes the tuple
    ``InvocationSpec`` validates and stores canonically: the specification a
    restart decides from has to be the one the run was started with, or the
    records on disk would answer for a different invocation.
    """
    spec = descriptor["spec"]
    return InvocationSpec(spec["request_id"], spec["run_id"], tuple(spec["command"]), spec["cwd"],
                          spec["environment_sha256"], spec["timeout_seconds"])


def _code_of(exc: BaseException) -> str:
    """The stable code of a failure, or its type name when it carries none."""
    code = getattr(exc, "code", None)
    return code if type(code) is str else type(exc).__name__


def recover_after_restart(scheduler: Any, *, owner: str,
                          candidate_hash: Callable[[str], str]) -> dict[str, Any]:
    """Recover a restarted controller's operations from the durable ledger.

    ``scheduler`` is a :class:`~sentinelmesh.scheduler.Scheduler` or a
    :class:`~sentinelmesh.durable_ledger.DurableScheduler`; ``owner`` is the
    identity the recovered operations are handed to and ``candidate_hash`` is
    called with the evidence directory of a run that is found finished, exactly
    as ``recover_supervised_operation`` calls it.

    Returns the generation this recovery rolled over to and one report per
    operation it acted on, in claim order:

    * ``{"operation_id", "action": "cancelled", "state"}`` for a claim of an
      older generation that never reached a handoff, with the state it ended in;
    * ``{"operation_id", "action": "recovered", "state"}`` for a handoff settled
      from its records, with the state ``recover_supervised_operation`` decided;
    * ``{"operation_id", "action": "skipped", "state", "reason"}`` for a handoff
      the ledger holds no run descriptor for;
    * ``{"operation_id", "action": "failed", "state", "error"}`` for an operation
      whose cancellation or recovery raised, with the state it was in before and
      the error's stable code (or its type name).

    Operations the old controller already decided are neither touched nor
    reported.  Every request stays stopped.
    """
    _checked_inputs(owner, candidate_hash)
    generation = scheduler.rollover()
    operations: list[dict[str, Any]] = []
    for operation_id in scheduler.operation_ids():
        state = scheduler.operation(operation_id)["state"]
        if state == "RESERVED":
            # Never handed off, so provably unstarted: cancel it and give the
            # reservation and the holds back.
            try:
                scheduler.cancel_unstarted(operation_id, generation)
            except LedgerError:
                raise
            except Exception as exc:
                operations.append({"operation_id": operation_id, "action": "failed", "state": state,
                                   "error": _code_of(exc)})
            else:
                operations.append({"operation_id": operation_id, "action": "cancelled",
                                   "state": scheduler.operation(operation_id)["state"]})
            continue
        if state in _UNDECIDED:
            descriptor = scheduler.run_descriptor(operation_id)
            if descriptor is None:
                # Without the descriptor there is no invocation to rebuild and
                # no evidence directory to read: the operation is left exactly
                # as the dead controller left it.
                operations.append({"operation_id": operation_id, "action": "skipped", "state": state,
                                   "reason": "no run descriptor"})
                continue
            try:
                result = recover_supervised_operation(
                    scheduler, operation_id=operation_id, owner=owner, generation=generation,
                    spec=_rebuilt_spec(descriptor), evidence_dir=descriptor["evidence_dir"],
                    candidate_hash=candidate_hash)
            except LedgerError:
                # The ledger is closed or has failed: nothing after this point
                # could be reported from it, so the recovery stops here.
                raise
            except Exception as exc:
                operations.append({"operation_id": operation_id, "action": "failed", "state": state,
                                   "error": _code_of(exc)})
            else:
                operations.append({"operation_id": operation_id, "action": "recovered",
                                   "state": result["state"]})
            continue
        # Anything else was decided by a controller that saw more than the
        # records -- ``START_UNKNOWN`` above all -- so it is left as it is.
    return {"generation": generation, "operations": operations}
