"""Operator entry point for a durable ledger: ``smctl ledger``.

After a crash the request has to be taken back by hand -- look at what the
ledger holds, enter the usage that is known, decide the run nobody could
classify, resume the request -- and every one of those steps used to mean writing
Python: open the ledger, read the stop reasons, find the operation that needs a
decision, call one controlled method, close the ledger.  This module puts exactly
those steps behind ``smctl`` and nothing else.

What it deliberately does *not* do is widen the guards.  Every command opens the
ledger (:func:`~sentinelmesh.durable_ledger.open_ledger`, which replays and never
creates), performs at most one mutation through the ledger's own methods -- so
every refusal, every fence and the fail-closed behaviour of the command log stay
in force -- and closes the instance in a ``finally``, a refusal included.  No
command raises a cap, resumes a request without naming every stop reason back, or
turns an unknown amount into a zero one: an operator's decisions are recorded,
never verified, and what cannot be proven stays unknown.

The output is one JSON object per call on stdout (``sort_keys``, non-ASCII kept as
it was written), and a ledger refusal is one JSON object on stderr with the
ledger's own code and detail.  A command that the ledger refuses changes nothing
and exits 3; an argument argparse refuses exits 2.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from .durable_ledger import LedgerError, open_ledger
from .scheduler import RESOLUTIONS, UNRESOLVED, ContractError

#: The states a run of an older generation was left undecided in.  A run of the
#: *current* generation is not something to attend to: it may still finish on its
#: own, which is what ``resume`` and ``_snapshot`` say about the same states.
_UNDECIDED = frozenset({"RESERVED", "HANDOFF_ACCEPTED", "RUNNING"})
#: The resolutions ``resolve`` offers, in a stable order for the usage message.
#: The scheduler owns the set: what it refuses, this never offers.
_RESOLUTIONS = tuple(sorted(RESOLUTIONS))


def _emit(payload: Any) -> None:
    """One line of canonical JSON on stdout, non-ASCII left as it was written."""
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _refuse(exc: ContractError | LedgerError) -> int:
    """One line of JSON on stderr with the ledger's own code and detail."""
    print(json.dumps({"error": exc.code, "detail": exc.detail}, ensure_ascii=False, sort_keys=True),
          file=sys.stderr)
    return 3


def _with_ledger(args: argparse.Namespace, work: Callable[[Any], dict[str, Any]]) -> int:
    """Open the ledger named on the command line, run one command, close it.

    The ledger is opened first, so a path that cannot be opened at all -- missing,
    damaged, a chain that does not verify -- is reported the same way a refused
    command is, and nothing is written anywhere.  The instance is closed in the
    ``finally`` whatever happens: a command that leaves an instance behind would
    keep a file descriptor and a claim on the log open, and the fail-closed rule
    of the ledger is that a stale instance has to be reopened, not reused.
    """
    try:
        ledger = open_ledger(args.ledger)
    except (ContractError, LedgerError) as exc:
        return _refuse(exc)
    try:
        try:
            result = work(ledger)
        except (ContractError, LedgerError) as exc:
            # A refusal by the ledger or the scheduler: the ledger is unchanged,
            # so closing it is all that is left to do.
            return _refuse(exc)
    finally:
        ledger.close()
    _emit(result)
    return 0


def _usage_incomplete(reservation: dict[str, int], usage: dict[str, dict[str, Any]]) -> bool:
    """Whether one reserved dimension has no usage of a known, final amount.

    This is the rule ``Scheduler._snapshot`` uses for ``unknown_operations``: a
    dimension counts as known only when an observation recorded an amount and
    marked it final.  A dimension with no observation at all, an amount of
    ``None`` or a non-final one keeps its reservation held and the operation
    unknown -- nothing is taken as zero.
    """
    for dimension in reservation:
        record = usage.get(dimension)
        if record is None or record["amount"] is None or record["final"] is not True:
            return True
    return False


def _attention(ledger: Any, request_id: str) -> list[dict[str, Any]]:
    """The operations of one request an operator has to look at, in claim order.

    An operation is listed when at least one of three things holds, and the
    reasons are reported in this order so the same operation always reads the same
    way:

    * ``start_unknown`` -- the controller could not classify the run at all, so
      only an operator can decide it;
    * ``undecided`` -- a live run of an older generation, left undecided by the
      controller that is gone;
    * ``usage_unknown`` -- the operation is unresolved and a reserved dimension
      still has no known final amount, which is what keeps its reservation held.

    Everything else -- a run of the current generation, an operation whose usage
    is complete -- is the controller's own business and is left out.
    """
    generation = ledger.generation
    entries: list[dict[str, Any]] = []
    for operation_id in ledger.operation_ids():
        view = ledger.operation(operation_id)
        if view["request_id"] != request_id:
            continue
        state = view["state"]
        usage = ledger.operation_usage(operation_id)
        reasons: list[str] = []
        if state == "START_UNKNOWN":
            reasons.append("start_unknown")
        if state in _UNDECIDED and view["generation"] < generation:
            reasons.append("undecided")
        if state in UNRESOLVED and _usage_incomplete(view["reservation"], usage):
            reasons.append("usage_unknown")
        if not reasons:
            continue
        entries.append({"operation_id": operation_id, "task_id": view["task_id"], "state": state,
                        "generation": view["generation"],
                        "supervised": ledger.run_descriptor(operation_id) is not None,
                        "usage": usage, "reasons": reasons})
    return entries


def _status(ledger: Any, path: str) -> dict[str, Any]:
    """The read-only view: what the ledger holds, and what needs an operator."""
    requests = []
    for request_id in ledger.request_ids():
        # A request is stopped exactly while it carries stop reasons: both are
        # set together by every reason and cleared together by a resume.
        reasons = ledger.stop_reasons(request_id)
        requests.append({"request_id": request_id, "stopped": bool(reasons),
                         "stop_reasons": list(reasons), "snapshot": ledger.snapshot(request_id),
                         "attention": _attention(ledger, request_id)})
    return {"ledger": path, "generation": ledger.generation, "seq": ledger.seq, "requests": requests}


def cmd_status(args: argparse.Namespace) -> int:
    return _with_ledger(args, lambda ledger: _status(ledger, args.ledger))


def cmd_stop(args: argparse.Namespace) -> int:
    """Put one explicit hold on a request; the ledger keeps every hold."""
    def work(ledger: Any) -> dict[str, Any]:
        ledger.stop(args.request, args.reason)
        return {"command": "stop", "request_id": args.request,
                "stop_reasons": list(ledger.stop_reasons(args.request))}
    return _with_ledger(args, work)


def cmd_settle(args: argparse.Namespace) -> int:
    """Record one final usage dimension of an operation.

    The single-dimension form of the ledger's ``settle``: ``final`` is always
    true, because an amount that is not final is not something an operator can
    enter from the outside, and the call key is the default the ledger derives
    from the operation and the dimension.  ``--correction`` is the ledger's own
    escape hatch for lowering an amount that was already recorded.
    """
    def work(ledger: Any) -> dict[str, Any]:
        result = ledger.settle(args.operation, args.dimension, call_key=None, revision=args.revision,
                               amount=args.amount, final=True, evidence_hash=args.evidence,
                               correction=args.correction)
        return {"command": "settle", "operation_id": args.operation, "dimension": args.dimension,
                "result": result, "usage": ledger.operation_usage(args.operation)}
    return _with_ledger(args, work)


def cmd_resolve(args: argparse.Namespace) -> int:
    """Decide a run the controller could not classify, from the operator's evidence."""
    def work(ledger: Any) -> dict[str, Any]:
        # The current generation of *this* ledger, never one the caller names: an
        # older generation may not decide a run it no longer owns.
        ledger.resolve_unknown(args.operation, ledger.generation, resolution=args.resolution,
                               operator=args.operator, evidence_hash=args.evidence, note=args.note,
                               candidate_hash=args.candidate)
        return {"command": "resolve", "operation": ledger.operation(args.operation)}
    return _with_ledger(args, work)


def cmd_resume(args: argparse.Namespace) -> int:
    """Re-open a stopped request, naming every stop reason by hand.

    There is no shortcut that copies the current reasons: the whole point of the
    comparison is that the operator reads what the ledger holds -- ``status``
    shows it -- and names it back, so a hold stacked on an already stopped request
    cannot be cleared by one call.  The reasons are passed in the order they were
    given, one ``--reason`` per hold.
    """
    def work(ledger: Any) -> dict[str, Any]:
        previous = list(ledger.stop_reasons(args.request))
        ledger.resume(args.request, ledger.generation, expected_reasons=list(args.reason),
                      operator=args.operator, note=args.note)
        return {"command": "resume", "request_id": args.request, "previous_reasons": previous}
    return _with_ledger(args, work)


def register(subparsers: Any) -> None:
    """Add the ``ledger`` command group to ``smctl``'s subparsers.

    Every command takes the ledger path as its first positional argument and
    refuses an unknown combination through argparse, which is what a usage error
    is: the ledger is not even opened for one.
    """
    group = subparsers.add_parser("ledger", help="operator entry point for a durable ledger")
    commands = group.add_subparsers(dest="ledger_command", required=True)

    p = commands.add_parser("status", help="read-only view of a ledger and of what needs an operator")
    p.add_argument("ledger", help="path of the ledger file")
    p.set_defaults(func=cmd_status)

    p = commands.add_parser("stop", help="put an explicit hold on a request")
    p.add_argument("ledger", help="path of the ledger file")
    p.add_argument("--request", required=True, help="request to hold")
    p.add_argument("--reason", required=True, help="the hold, in the operator's own words")
    p.set_defaults(func=cmd_stop)

    p = commands.add_parser("settle", help="record one final usage dimension of an operation")
    p.add_argument("ledger", help="path of the ledger file")
    p.add_argument("--operation", required=True, help="operation the usage belongs to")
    p.add_argument("--dimension", required=True, help="reserved dimension to record")
    p.add_argument("--amount", required=True, type=int, help="the known amount")
    p.add_argument("--evidence", required=True, help="sha256 of the evidence for the amount")
    p.add_argument("--revision", type=int, default=1, help="revision of the observation (default 1)")
    p.add_argument("--correction", action="store_true", help="lower an amount already recorded")
    p.set_defaults(func=cmd_settle)

    p = commands.add_parser("resolve", help="decide an operation the controller could not classify")
    p.add_argument("ledger", help="path of the ledger file")
    p.add_argument("--operation", required=True, help="the START_UNKNOWN operation to decide")
    p.add_argument("--resolution", required=True, choices=_RESOLUTIONS,
                   help="NOT_STARTED, ABANDONED or FINISHED")
    p.add_argument("--operator", required=True, help="who decided")
    p.add_argument("--evidence", required=True, help="sha256 of the evidence for the decision")
    p.add_argument("--note", required=True, help="what the operator established")
    p.add_argument("--candidate", help="sha256 of the candidate, for FINISHED")
    p.set_defaults(func=cmd_resolve)

    p = commands.add_parser("resume", help="re-open a stopped request, naming every hold back")
    p.add_argument("ledger", help="path of the ledger file")
    p.add_argument("--request", required=True, help="request to resume")
    p.add_argument("--reason", required=True, action="append",
                   help="one stop reason, in the order the ledger recorded them; repeat per hold")
    p.add_argument("--operator", required=True, help="who resumed")
    p.add_argument("--note", required=True, help="what the operator established")
    p.set_defaults(func=cmd_resume)
