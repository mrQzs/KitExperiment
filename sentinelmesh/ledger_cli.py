"""Operator entry point for a durable ledger: ``smctl ledger``.

After a crash the request has to be taken back by hand -- look at what the
ledger holds, enter the usage that is known, decide the run nobody could
classify, decide the candidate one put up for review, resume the request -- and
every one of those steps used to mean writing
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
import re
import sys
from typing import Any, Callable

from .adapter_run import MAX_LINE_BYTES, MAX_STREAM_BYTES, read_stream
from .claude_events import DIALECT as CLAUDE_DIALECT
from .claude_events import translate as claude_translate
from .durable_ledger import LedgerError, open_ledger
from .pi_events import DIALECT, translate
from .scheduler import RESOLUTIONS, UNRESOLVED, ContractError

#: The states a run of an older generation was left undecided in.  A run of the
#: *current* generation is not something to attend to: it may still finish on its
#: own, which is what ``resume`` and ``_snapshot`` say about the same states.
_UNDECIDED = frozenset({"RESERVED", "HANDOFF_ACCEPTED", "RUNNING"})
#: The resolutions ``resolve`` offers, in a stable order for the usage message.
#: The scheduler owns the set: what it refuses, this never offers.
_RESOLUTIONS = tuple(sorted(RESOLUTIONS))
#: The output-stream dialects ``reference`` understands, and nothing else: the
#: ledger records no dialect, so the operator names the one the run wrote and a
#: dialect nobody implemented is refused by argparse instead of being guessed at.
#: pi's ``pi-session-v3`` first, then Claude Code's ``claude-stream-json``.
_DIALECTS = (DIALECT, CLAUDE_DIALECT)

#: The event types the Scheduler emits, and the only ones ``events`` offers.  The
#: set is the Scheduler's own: an operator selecting a type nobody emits is a
#: usage error (exit 2) rather than a filter that silently matches nothing.
_EVENT_TYPES = (
    "controller.fenced",
    "request.created",
    "request.resumed",
    "request.stop_observed",
    "request.stopped",
    "run.finished_for_review",
    "run.started",
    "run.verdict",
    "scheduler.cancelled_before_handoff",
    "scheduler.claimed",
    "scheduler.handoff_accepted",
    "scheduler.not_started",
    "scheduler.recovered",
    "scheduler.start_unknown",
    "scheduler.unknown_resolved",
    "usage.observed",
)
#: The page size ``events`` uses when the operator names none.
_DEFAULT_EVENTS = 100
#: The largest page ``events`` returns: a long ledger's output stays bounded.
_MAX_EVENTS = 10000


def _digits(text: str) -> int:
    """One non-negative integer written in decimal digits, and nothing else.

    ``int`` itself would take a sign, underscores, surrounding whitespace or a
    decimal point, and a value that reads as a number is not the same as a
    sequence number the operator meant: ``--after 1.5`` is a typo, not the first
    event after one.  A refusal here is argparse's own, so the ledger is not even
    opened for a value it would have to guess at.
    """
    if re.fullmatch("[0-9]+", text) is None:
        raise argparse.ArgumentTypeError("%r is not a sequence number written in decimal digits" % text)
    return int(text)


def _page_size(text: str) -> int:
    """The ``--limit`` value: decimal digits, and at least one event."""
    value = _digits(text)
    if not 1 <= value <= _MAX_EVENTS:
        raise argparse.ArgumentTypeError("the page size must be between 1 and %d" % _MAX_EVENTS)
    return value


def _write_line(stream: Any, payload: Any) -> None:
    """One line of canonical JSON, written as UTF-8 whatever the terminal's encoding.

    ``ensure_ascii=False`` keeps non-ASCII text as it was written.  Writing the
    UTF-8 bytes to the stream's buffer means a terminal that cannot represent
    them (``PYTHONIOENCODING=ascii``) no longer turns a command that already
    committed into an ``error:`` exit after the fact -- an operator retrying a
    "failed" ``stop`` would add a second hold (review of attempt-001, L3).  A lone
    surrogate (an argument decoded from invalid UTF-8) is written as its JSON
    escape, so the line still parses.  A stream without a byte buffer (an
    in-process ``StringIO``) gets the text.
    """
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(line)
        return
    stream.flush()
    buffer.write(line.encode("utf-8", "backslashreplace"))
    buffer.flush()


def _emit(payload: Any) -> None:
    """One line of canonical JSON on stdout, non-ASCII left as it was written."""
    _write_line(sys.stdout, payload)


def _refuse(exc: ContractError | LedgerError) -> int:
    """One line of JSON on stderr with the ledger's own code and detail."""
    _write_line(sys.stderr, {"error": exc.code, "detail": exc.detail})
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


def _missing_dimensions(reservation: dict[str, int], usage: dict[str, dict[str, Any]]) -> list[str]:
    """The reserved dimensions with no usage of a known, final amount.

    The rule of ``_usage_incomplete``, one dimension at a time and in the order
    the operation claimed its reservation in: an operator reads what is missing
    in the order the run holds it, never in an alphabetical one.
    """
    return [dimension for dimension in reservation
            if _usage_incomplete({dimension: reservation[dimension]}, usage)]


def _stream_summary(stream: dict[str, Any]) -> dict[str, Any]:
    """The stream's reportable fields: what it is, plus how it failed.

    ``run_adapter_operation`` reports a stream the same way, so an operator reads
    one shape whatever read it.  The events themselves are the translator's
    input, not something an operator has to look at.
    """
    summary = {"status": stream["status"], "sha256": stream["sha256"], "bytes": stream["bytes"]}
    if stream["status"] == "invalid":
        summary["line"] = stream["line"]
        summary["code"] = stream["code"]
    return summary


def _pi_reference(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``pi-session-v3`` reference value of one translated stream.

    Only :func:`~sentinelmesh.pi_events.translate` decides the numbers -- nothing
    is recounted here, so the pairing, ordering and termination rules stay the
    translator's own.  A stream that cannot be called complete still reports the
    sums of the assistant messages that were legal, with the reasons; they are
    listed in one fixed order, so the same stream always reads the same way.
    """
    translation = translate(events)
    reasons: list[str] = []
    if translation.terminal is None:
        # Only ``agent_settled`` ends a run: an ``agent_end`` is not a terminal.
        reasons.append("no_terminal")
    if any(event["type"] == "assistant" and event["payload"]["usage"] is None
           for event in translation.events):
        # An illegal usage leaves that one message out of the sum.
        reasons.append("illegal_usage")
    if translation.unknown > 0:
        # An unknown or malformed frame, or a frame after the terminal one.
        reasons.append("unknown_frames")
    return {"complete": not reasons, "reasons": reasons, "terminal": translation.terminal,
            "last_stop_reason": translation.last_stop_reason, "usage": dict(translation.usage),
            "assistant_messages": translation.assistant_messages, "tool_calls": translation.tool_calls,
            "tool_errors": translation.tool_errors, "agent_cycles": translation.agent_cycles,
            "frames": translation.frames, "unknown": translation.unknown,
            "models": [[provider, model] for provider, model in translation.models]}


def _claude_reference(events: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``claude-stream-json`` reference value of one translated stream.

    Only :func:`~sentinelmesh.claude_events.translate` decides the numbers --
    nothing is recounted here, so the tool pairing, ordering and termination
    rules stay the translator's own.  When the terminal ``result`` carried a
    legal ``modelUsage`` the reference is that billed total and the only reason
    left is ``unknown_frames``; otherwise the reference is the per-message sum,
    which is only a lower bound on what the run produced, and the reasons are
    listed in one fixed order so the same stream always reads the same way.
    """
    translation = claude_translate(events)
    from_messages = translation.reported_usage is None
    reasons: list[str] = []
    if from_messages and translation.terminal is None:
        # No legal ``result``: the run was cut or killed before it settled.
        reasons.append("no_terminal")
    if from_messages and (translation.terminal is not None or translation.illegal_message_usage > 0):
        # A result whose usage was illegal, or a message whose usage was illegal.
        reasons.append("illegal_usage")
    if from_messages:
        # The per-message sum is only what the frames showed, never the total.
        reasons.append("partial_output")
    if translation.unknown > 0:
        # An unknown or malformed frame, or a frame after the terminal one.
        reasons.append("unknown_frames")
    usage = dict(translation.message_usage if from_messages else translation.reported_usage)
    return {"complete": not reasons, "reasons": reasons, "terminal": translation.terminal,
            "subtype": translation.subtype, "is_error": translation.is_error,
            "num_turns": translation.num_turns, "usage": usage,
            "assistant_messages": translation.assistant_messages, "tool_calls": translation.tool_calls,
            "tool_errors": translation.tool_errors, "permission_denials": translation.permission_denials,
            "fallbacks": translation.fallbacks, "frames": translation.frames,
            "unknown": translation.unknown, "models": list(translation.models)}


#: The reference each dialect computes from the events of a stream that read
#: ``ok``.  Two dialects in this version; a dialect nobody implemented never
#: reaches here, because argparse refuses it before the ledger is opened.
_REFERENCES: dict[str, Callable[[list[dict[str, Any]]], dict[str, Any]]] = {DIALECT: _pi_reference,
                                                                           CLAUDE_DIALECT: _claude_reference}


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


def _candidate_of(ledger: Any, operation_id: str) -> str | None:
    """The candidate the ledger last recorded for one operation, or ``None``.

    An operation's view carries no candidate: the scheduler keeps it as a field
    that only the events ever spell, so the only place to read it is the log.
    Three event types put one up for review -- ``run.finished_for_review``
    (``finish``), ``scheduler.recovered`` (``recover`` with the ``FINISHED``
    outcome) and ``scheduler.unknown_resolved`` (``resolve_unknown`` with the
    ``FINISHED`` resolution) -- and ``run.verdict`` repeats the one that was
    decided; the last of them is the one that decided the run's
    current state, so the last one in the log is what the operator reviews.  An
    operation that never recorded a candidate has nothing to review, which is
    reported as ``None`` rather than guessed at.
    """
    candidate: str | None = None
    for event in ledger.events:
        if event["operation_id"] != operation_id:
            continue
        payload = event["payload"]
        if isinstance(payload, dict) and "candidate" in payload:
            candidate = payload["candidate"]
    return candidate


def _reviews(ledger: Any, request_id: str) -> list[dict[str, Any]]:
    """The runs of one request waiting for an operator's verdict, in claim order.

    A run that finished waits in ``REVIEW_PENDING`` with the candidate it put up
    for review, and only an operator can move it on -- which is why ``status``
    lists them here, in the order the request claimed them, each with the
    candidate ``smctl ledger verdict`` (``Scheduler.verdict``) has to be given.  ``attention`` is untouched: a
    review is not something an operator has to look at *now*, and a run of the
    current generation that may still finish on its own is not something to
    attend to at all.
    """
    entries: list[dict[str, Any]] = []
    for operation_id in ledger.operation_ids():
        view = ledger.operation(operation_id)
        if view["request_id"] != request_id or view["state"] != "REVIEW_PENDING":
            continue
        entries.append({"operation_id": operation_id, "task_id": view["task_id"],
                        "generation": view["generation"],
                        "candidate": _candidate_of(ledger, operation_id)})
    return entries


def _tasks(ledger: Any, request_id: str) -> list[dict[str, Any]]:
    """The tasks of one request, in seeding order, each as the ledger views it.

    Every task the request seeded is listed -- including one that has never been
    claimed -- as ``Scheduler.task`` reports it: ``task_id``, ``max_attempts``,
    ``attempts`` and ``done``.  That is what an operator needs to see which
    tasks are already accepted (closed to further claims with ``E_TASK_DONE``),
    which are still open, and which have used up their attempts, without
    deriving any of it from the event log.  ``attention`` and ``reviews`` are
    untouched: a closed task is not something to attend to.
    """
    return [ledger.task(request_id, task_id) for task_id in ledger.task_ids(request_id)]


def _status(ledger: Any, path: str) -> dict[str, Any]:
    """The read-only view: what the ledger holds, and what needs an operator."""
    requests = []
    for request_id in ledger.request_ids():
        # A request is stopped exactly while it carries stop reasons: both are
        # set together by every reason and cleared together by a resume.
        reasons = ledger.stop_reasons(request_id)
        requests.append({"request_id": request_id, "stopped": bool(reasons),
                         "stop_reasons": list(reasons), "snapshot": ledger.snapshot(request_id),
                         "attention": _attention(ledger, request_id),
                         "reviews": _reviews(ledger, request_id),
                         "tasks": _tasks(ledger, request_id)})
    return {"ledger": path, "generation": ledger.generation, "seq": ledger.seq, "requests": requests}


def cmd_status(args: argparse.Namespace) -> int:
    return _with_ledger(args, lambda ledger: _status(ledger, args.ledger))


def cmd_compact(args: argparse.Namespace) -> int:
    """Write a checkpoint of the whole state and remove the commands it covers.

    The one write command that records no decision: it appends no command, emits
    no event and moves no generation, so the ledger's history is unchanged --
    only the commands the checkpoint already covers stop being replayed one by
    one.  The ledger is opened (replayed) first, so a path that cannot be opened
    at all is reported the same way a refused command is, and the instance is
    closed in the ``finally`` whatever happens.

    It is explicit on purpose: when the commands an audit needs may be dropped is
    an operator's decision, so a ledger that has to keep them has to be backed up
    before this runs.
    """
    def work(ledger: Any) -> dict[str, Any]:
        result = ledger.compact()
        return {"command": "compact", "ledger": args.ledger, "generation": ledger.generation,
                "seq": result["seq"], "last_sha": result["last_sha"], "sha256": result["sha256"],
                "removed": result["removed"]}
    return _with_ledger(args, work)


def _belongs_to_request(ledger: Any, event: dict[str, Any], request_id: str,
                         operations: dict[str, str]) -> bool:
    """Whether one recorded event belongs to the request the operator named.

    An event of an operation belongs to that operation's request -- the claim
    recorded which request it was made for, and the ledger is the only place
    that knows it.  A request-level event carries its own ``request_id`` in the
    payload.  An event with no operation and no such payload -- the fence the
    controller recorded -- belongs to no request at all, which is why a payload
    that is not an object matches nothing instead of being guessed at.

    The operation lookups are cached in ``operations``, because the read is the
    whole event log and one request's history walks every operation id in it.
    """
    operation_id = event["operation_id"]
    if operation_id is not None:
        if operation_id not in operations:
            operations[operation_id] = ledger.operation(operation_id)["request_id"]
        return operations[operation_id] == request_id
    payload = event["payload"]
    return isinstance(payload, dict) and payload.get("request_id") == request_id


def _events(ledger: Any, args: argparse.Namespace) -> dict[str, Any]:
    """The events the filters select, paged, with the ledger's own header.

    Read-only from end to end: the events are read once, filtered in the order
    the ledger recorded them (sequence numbers ascend) and paged, and no
    mutating method is called -- an operator reading the history leaves it
    exactly as it was, checkpoint included.
    """
    if args.request is not None and args.request not in ledger.request_ids():
        raise ContractError("E_REQUEST_UNKNOWN", "request %s is not one this ledger knows" % args.request)
    if args.operation is not None:
        # Unknown or not exactly a string: the ledger's own refusal, before any
        # event is read.
        ledger.operation(args.operation)
    operations: dict[str, str] = {}
    events = ledger.events
    wanted = []
    for event in events:
        if args.request is not None and not _belongs_to_request(ledger, event, args.request, operations):
            continue
        if args.operation is not None and event["operation_id"] != args.operation:
            continue
        if args.type and event["type"] not in args.type:
            continue
        wanted.append(event)
    remaining = [event for event in wanted if event["seq"] > args.after]
    page = remaining[:args.limit]
    return {"command": "events", "ledger": args.ledger, "generation": ledger.generation,
            "seq": ledger.seq, "checkpoint": ledger.checkpoint_seq, "total": len(events),
            "matched": len(wanted), "events": page,
            "next": page[-1]["seq"] if len(remaining) > len(page) else None}


def cmd_events(args: argparse.Namespace) -> int:
    """Read the events the ledger recorded, filtered and paged.

    A compaction drops the commands it covers, so the events are the only
    complete history a ledger keeps -- and this is the operator's way to read
    them: by request (an operation's request, or the payload's for a
    request-level event; the fence belongs to none), by operation, by type, and
    a page at a time with ``--after`` and ``--limit``.  Every event is printed
    exactly as the Scheduler recorded it, with no derived field added: what the
    ledger holds is the audit material, and an operator reading it is not
    offered a second version of it.

    The header reports where the ledger's checkpoint is, because that is the
    command that tells the operator which history is left in the commands and
    which is only here.
    """
    return _with_ledger(args, lambda ledger: _events(ledger, args))


def cmd_reference(args: argparse.Namespace) -> int:
    """Read one supervised run's output stream and print the usage it adds up to.

    The reference is a candidate for the operator's own ``settle`` and nothing
    more: the command opens the ledger, reads the stream the run recorded in its
    evidence directory and closes the ledger again.  It calls no mutating method
    and writes nothing next to the stream, so what it prints is a reading of what
    the run wrote and not a claim the ledger holds -- what is entered afterwards
    stays the operator's decision, with the stream's own digest as its evidence.

    A stream that is missing, unreadable, too large or invalid is an answer and
    not a failure: the command exits 0 and reports the state, with no reference
    value.  That is the crash case the command exists for -- the controller is
    gone, the supervisor's own stream fact was never recorded, and the only
    thing left to read is the file itself.
    """
    def work(ledger: Any) -> dict[str, Any]:
        view = ledger.operation(args.operation)
        run = ledger.run_descriptor(args.operation)
        if run is None:
            # No handoff to a supervised run, so there is no stream to read: the
            # operation's usage is the controller's own business.
            raise ContractError("E_NOT_SUPERVISED",
                                "operation %s was never handed off to a supervised run" % args.operation)
        usage = ledger.operation_usage(args.operation)
        # No ``expected``: the supervisor's stream fact is written when it reaps
        # the group, and a controller that died mid-run left none behind.  The
        # ceilings are the largest ones, because the stream is read once, by hand,
        # and not while the run writes it.
        stream = read_stream(run["evidence_dir"], max_bytes=MAX_STREAM_BYTES,
                             max_line_bytes=MAX_LINE_BYTES)
        return {"command": "reference", "operation_id": view["operation_id"],
                "request_id": view["request_id"], "task_id": view["task_id"],
                "state": view["state"], "generation": view["generation"],
                # The evidence directory exactly as the run recorded it: a
                # relative one is resolved against the operator's own working
                # directory, which is why it is printed unchanged.
                "evidence_dir": run["evidence_dir"], "dialect": args.dialect,
                "reservation": view["reservation"], "recorded": usage,
                "missing": _missing_dimensions(view["reservation"], usage),
                "stream": _stream_summary(stream),
                "reference": (None if stream["status"] != "ok"
                              else _REFERENCES[args.dialect](stream["events"]))}
    return _with_ledger(args, work)


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
        if not args.evidence:
            # The scheduler takes an empty hash as "not given" and derives one
            # from the operation, the dimension and the amount; an operator's
            # entry must carry real evidence (review of attempt-001, L2).
            raise ContractError("E_OBSERVATION_SHAPE", "the evidence hash is empty")
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


def cmd_verdict(args: argparse.Namespace) -> int:
    """Decide a run that finished, from the operator's evidence.

    The entry to the last transition of the chain ``claim -> handoff -> run ->
    finish``: a run that finished waits in ``REVIEW_PENDING`` with the candidate
    it put up for review, and only an operator can accept or reject it.  The
    candidate is named by hand and the decision is taken in the current
    generation of *this* ledger -- an older controller may not decide a run it no
    longer owns -- with the operator, the evidence digest and the note recorded
    in the ``run.verdict`` event, exactly as ``resolve`` records its own
    decision.  The scheduler records them and verifies none of them: the
    operator's authority and what the evidence says stay the operator's business.

    Nothing else moves: the usage, the reservations, the resource holds and the
    request's stop are what the run left them, and the task may be attempted
    again whatever the verdict was, until its attempts run out.
    """
    def work(ledger: Any) -> dict[str, Any]:
        ledger.verdict(args.operation, ledger.generation, candidate_hash=args.candidate,
                       accepted=args.accept, evidence_hash=args.evidence, operator=args.operator,
                       note=args.note)
        return {"command": "verdict", "operation": ledger.operation(args.operation)}
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
        reasons = list(args.reason)
        ledger.resume(args.request, ledger.generation, expected_reasons=reasons,
                      operator=args.operator, note=args.note)
        # A resume succeeds only when the reasons named equal the ledger's list,
        # so they are the previous reasons; reading the list first would only
        # change which error a faulty call reports (review of attempt-001, L1).
        return {"command": "resume", "request_id": args.request, "previous_reasons": reasons}
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

    p = commands.add_parser("reference", help="read one supervised run's stream and print its usage as a reference")
    p.add_argument("ledger", help="path of the ledger file")
    p.add_argument("--operation", required=True, help="operation the reference belongs to")
    p.add_argument("--dialect", required=True, choices=_DIALECTS,
                   help="dialect of the run's output stream")
    p.set_defaults(func=cmd_reference)

    p = commands.add_parser("compact", help="write a checkpoint of the whole state and drop the commands it covers")
    p.add_argument("ledger", help="path of the ledger file")
    p.set_defaults(func=cmd_compact)

    p = commands.add_parser("events", help="read the events the ledger recorded, filtered and paged")
    p.add_argument("ledger", help="path of the ledger file")
    p.add_argument("--request", help="only the events of this request")
    p.add_argument("--operation", help="only the events of this operation")
    p.add_argument("--type", action="append", choices=_EVENT_TYPES,
                   help="only the events of this type; repeat for a union of types")
    p.add_argument("--after", type=_digits, default=0,
                   help="skip the events at or below this sequence number (default 0)")
    p.add_argument("--limit", type=_page_size, default=_DEFAULT_EVENTS,
                   help="how many events to print at most (default %d, at most %d)"
                        % (_DEFAULT_EVENTS, _MAX_EVENTS))
    p.set_defaults(func=cmd_events)

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

    p = commands.add_parser("verdict", help="decide a run that finished, from the operator's evidence")
    p.add_argument("ledger", help="path of the ledger file")
    p.add_argument("--operation", required=True, help="the REVIEW_PENDING operation to decide")
    p.add_argument("--candidate", required=True, help="sha256 of the candidate the run put up for review")
    decision = p.add_mutually_exclusive_group(required=True)
    decision.add_argument("--accept", dest="accept", action="store_true", help="accept the candidate")
    decision.add_argument("--reject", dest="accept", action="store_false", help="reject the candidate")
    p.add_argument("--evidence", required=True, help="sha256 of the evidence for the decision")
    p.add_argument("--operator", required=True, help="who decided")
    p.add_argument("--note", required=True, help="what the operator established")
    p.set_defaults(func=cmd_verdict)

    p = commands.add_parser("resume", help="re-open a stopped request, naming every hold back")
    p.add_argument("ledger", help="path of the ledger file")
    p.add_argument("--request", required=True, help="request to resume")
    p.add_argument("--reason", required=True, action="append",
                   help="one stop reason, in the order the ledger recorded them; repeat per hold")
    p.add_argument("--operator", required=True, help="who resumed")
    p.add_argument("--note", required=True, help="what the operator established")
    p.set_defaults(func=cmd_resume)
