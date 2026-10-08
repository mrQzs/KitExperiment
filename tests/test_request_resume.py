"""Executor tests for request-resume: ``ABANDONED``, ``resolve_unknown`` and ``resume``.

These cover what the planner oracle (``tests/test_request_resume_oracle.py``)
leaves out, following one rule throughout: every rejection case first proves a
legal baseline -- the very same call with nothing but the value under test
changed -- and then asserts the specific error, so a different rule refusing the
call first cannot hide a gap.  Every refusal is checked against the state it
found, events included.

The behaviour under test comes from `.agent/design/request-resume.md` sections 5
and 6: an operator decides a ``START_UNKNOWN`` operation (``NOT_STARTED``,
``ABANDONED`` or ``FINISHED``), a request resumes only once everything in it is
decided, its usage is known and it is within budget, and the durable ledger
records both new methods without changing its format.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sentinelmesh.durable_ledger import (
    DurableScheduler,
    LEDGER_FORMAT,
    MUTATING_METHODS,
    LedgerError,
    create_ledger,
    open_ledger,
)
from sentinelmesh.scheduler import ABANDONED, LIVE, UNRESOLVED, ContractError, Scheduler

CAPS = {"dispatches": 9, "tokens": 500}
RESERVATION = {"dispatches": 1, "tokens": 40}
EVIDENCE, CANDIDATE = "e" * 64, "d" * 64
OPERATOR, NOTE = "ops-1", "checked the process table and the worktree"
#: The states that keep an operation of an older generation undecided.
UNDECIDED = {"RESERVED", "HANDOFF_ACCEPTED", "RUNNING"}
#: The stop reasons a request stopped by an unknown start alone carries.
DEFAULT_REASONS = ["START_UNKNOWN"]


class Key(str):
    """A ``str`` subclass: equal to the text it spells, but not exact text."""


class ResumeCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="request-resume-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)

    def scheduler(self, ledger=None, *, concurrency=4, request="req-1",
                  tasks=("task-1", "task-2", "task-3")):
        scheduler = ledger or Scheduler()
        scheduler.seed_request(request, "repo-1", CAPS, concurrency=concurrency)
        for task in tasks:
            scheduler.seed_task(request, task, CAPS)
        return scheduler

    def claim(self, scheduler, task, *, request="req-1", key=None, owner="owner-1", generation=1, resources=None):
        return scheduler.claim(request, task, 1, key or "idem-" + task, owner, generation, 1, RESERVATION,
                               resources or ["src/%s.py" % task])["operation_id"]

    def start_unknown(self, scheduler, task="task-1", *, request="req-1", resources=None):
        """A handed-off operation the controller could not decide."""
        op = self.claim(scheduler, task, request=request, resources=resources)
        scheduler.handoff(op, "owner-1", 1)
        scheduler.unknown(op, "owner-1", 1, reason="START_UNKNOWN")
        return op

    def reviewed(self, scheduler, task="task-2", *, request="req-1", tokens=5):
        """A finished run in review, its tokens settled."""
        op = self.claim(scheduler, task, request=request)
        scheduler.handoff(op, "owner-1", 1)
        scheduler.mark_running(op, "owner-1", 1)
        scheduler.settle(op, {"tokens": tokens})
        scheduler.finish(op, "owner-1", 1, CANDIDATE, True)
        return op

    def handed_off(self, scheduler, task, *, request="req-1"):
        op = self.claim(scheduler, task, request=request)
        scheduler.handoff(op, "owner-1", 1)
        return op

    def running(self, scheduler, task, *, request="req-1"):
        op = self.handed_off(scheduler, task, request=request)
        scheduler.mark_running(op, "owner-1", 1)
        return op

    def verdict(self, scheduler, task, accepted, *, request="req-1"):
        op = self.reviewed(scheduler, task, request=request)
        scheduler.fixture_verdict(op, CANDIDATE, accepted, EVIDENCE)
        return op

    def proven_unstarted(self, scheduler, task, *, request="req-1"):
        op = self.handed_off(scheduler, task, request=request)
        scheduler.release_proven_unstarted(op, "owner-1", 1, evidence_hash=EVIDENCE)
        return op

    def cancelled(self, scheduler, task, *, request="req-1"):
        op = self.claim(scheduler, task, request=request)
        scheduler.cancel_unstarted(op, 1)
        return op

    def resolve(self, scheduler, op, resolution="ABANDONED", *, generation=1, **changes):
        arguments = dict(resolution=resolution, operator=OPERATOR, evidence_hash=EVIDENCE, note=NOTE,
                         candidate_hash=CANDIDATE if resolution == "FINISHED" else None)
        arguments.update(changes)
        return scheduler.resolve_unknown(op, generation, **arguments)

    def resume(self, scheduler, request="req-1", *, generation=1, expected_reasons=DEFAULT_REASONS, **changes):
        arguments = dict(expected_reasons=expected_reasons, operator=OPERATOR, note=NOTE)
        arguments.update(changes)
        return scheduler.resume(request, generation, **arguments)

    def state_of(self, scheduler, request="req-1"):
        """Everything a refused call must leave exactly as it found it."""
        # ``DurableScheduler`` keeps the ``Scheduler`` it wraps one layer down.
        inner = scheduler._scheduler if isinstance(scheduler, DurableScheduler) else scheduler
        return (scheduler.events, scheduler.snapshot(request), inner._requests[request].stopped,
                [scheduler.operation(op) for op in scheduler.operation_ids()])

    def refused(self, scheduler, code, call, *, request="req-1"):
        before = self.state_of(scheduler, request)
        with self.assertRaises(ContractError) as ctx:
            call()
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(self.state_of(scheduler, request), before,
                         "a refused call changed the ledger")

    def code(self, scheduler, code, call, *, request="req-1"):
        self.refused(scheduler, code, call, request=request)
        return code

    def ledger(self, name):
        return self.scheduler(create_ledger(str(self.work / name)))

    def log_of(self, path):
        db = sqlite3.connect(path)
        try:
            commands = [row[0] for row in db.execute("SELECT command FROM commands ORDER BY seq")]
            fmt = db.execute("SELECT value FROM meta WHERE key='format'").fetchone()[0]
        finally:
            db.close()
        return [json.loads(command)["method"] for command in commands], fmt


class AbandonedAccountingCase(ResumeCase):
    """The terminal ``ABANDONED`` state: no concurrency slot, usage still counted."""

    def test_abandoned_is_unresolved_but_not_live(self):
        self.assertIn(ABANDONED, UNRESOLVED, "an abandoned run's usage may not be taken as zero")
        self.assertNotIn(ABANDONED, LIVE, "an abandoned run may not hold a concurrency slot")

    def test_an_abandoned_run_whose_usage_is_known_gives_the_reservation_back_at_once(self):
        scheduler = self.scheduler()
        op = self.start_unknown(scheduler)
        scheduler.settle(op, {"tokens": 7})
        self.resolve(scheduler, op, "ABANDONED")
        tokens = scheduler.snapshot("req-1")["tokens"]
        self.assertEqual((tokens["known"], tokens["held"], tokens["unknown_operations"], tokens["exposure"]),
                         (7, 0, 0, 7), "the usage was known before the resolution")
        self.assertEqual(scheduler.snapshot("req-1")["dispatches"]["known"], 1, "the dispatch stopped counting")
        self.assertEqual(scheduler.operation(op)["state"], ABANDONED)

    def test_an_abandoned_run_holds_its_reservation_until_the_usage_is_complete(self):
        scheduler = self.scheduler()
        op = self.start_unknown(scheduler)
        self.resolve(scheduler, op, "ABANDONED")
        tokens = scheduler.snapshot("req-1")["tokens"]
        self.assertEqual((tokens["known"], tokens["held"], tokens["unknown_operations"]), (0, 40, 1))
        # A partial observation keeps the rest of the reservation and the run unknown.
        scheduler.observe(op, "tokens", "call-1", 1, 5, False, EVIDENCE)
        tokens = scheduler.snapshot("req-1")["tokens"]
        self.assertEqual((tokens["known"], tokens["held"], tokens["unknown_operations"]), (5, 35, 1))
        # An unknown usage keeps the request stopped, whatever the state.
        self.code(scheduler, "E_RESUME_USAGE_UNKNOWN",
                  lambda: self.resume(scheduler, expected_reasons=["START_UNKNOWN", "USAGE_UNKNOWN"]))
        # The final amount releases the reservation, and only then may it resume.
        self.assertEqual(scheduler.observe(op, "tokens", "call-1", 2, 7, True, EVIDENCE), "APPLIED")
        tokens = scheduler.snapshot("req-1")["tokens"]
        self.assertEqual((tokens["known"], tokens["held"], tokens["unknown_operations"]), (7, 0, 0))
        self.resume(scheduler, expected_reasons=["START_UNKNOWN", "USAGE_UNKNOWN"])
        self.assertIsNone(scheduler._requests["req-1"].stopped)

    def test_an_abandoned_run_no_longer_holds_a_concurrency_slot(self):
        scheduler = self.scheduler(concurrency=1)
        op = self.start_unknown(scheduler, "task-1")
        scheduler.settle(op, {"tokens": 7})
        self.resolve(scheduler, op, "ABANDONED")
        self.resume(scheduler)
        second = self.claim(scheduler, "task-2", key="idem-task-2")
        self.assertEqual(scheduler.operation(second)["state"], "RESERVED",
                         "the abandoned run still occupied the only slot")

    def test_a_finished_resolution_rejected_in_review_may_be_claimed_again(self):
        scheduler = self.scheduler()
        op = self.start_unknown(scheduler, "task-1")
        scheduler.settle(op, {"tokens": 7})
        self.resolve(scheduler, op, "FINISHED")
        self.assertEqual(scheduler.operation(op)["state"], "REVIEW_PENDING")
        scheduler.fixture_verdict(op, CANDIDATE, False, EVIDENCE)
        self.assertEqual(scheduler.operation(op)["state"], "REJECTED")
        self.resume(scheduler)
        again = self.claim(scheduler, "task-1", key="idem-task-1-again")
        self.assertNotEqual(again, op)
        self.assertEqual(scheduler.operation(again)["state"], "RESERVED")
        # The rejected attempt and its usage still count.
        self.assertEqual(scheduler.snapshot("req-1")["tokens"]["known"], 7)
        scheduler.cancel_unstarted(again, 1)
        third = self.claim(scheduler, "task-1", key="idem-task-1-third")
        self.assertEqual(scheduler.operation(third)["state"], "RESERVED")
        scheduler.cancel_unstarted(third, 1)
        self.code(scheduler, "E_ATTEMPTS", lambda: self.claim(scheduler, "task-1", key="idem-task-1-fourth"))

    def test_every_resolution_keeps_the_operation_and_the_stop(self):
        for resolution, state in (("NOT_STARTED", "NOT_STARTED"), ("ABANDONED", ABANDONED),
                                  ("FINISHED", "REVIEW_PENDING")):
            with self.subTest(resolution=resolution):
                scheduler = self.scheduler()
                op = self.start_unknown(scheduler)
                scheduler.settle(op, {"tokens": 7})
                before = scheduler.operation(op)
                self.resolve(scheduler, op, resolution)
                after = scheduler.operation(op)
                self.assertEqual(after["state"], state)
                self.assertEqual({key: after[key] for key in ("owner", "generation", "reservation", "task_id",
                                                              "request_id", "operation_id")},
                                 {key: before[key] for key in ("owner", "generation", "reservation", "task_id",
                                                               "request_id", "operation_id")},
                                 "a resolution rewrote the operation")
                self.assertEqual(scheduler._requests["req-1"].stopped, "START_UNKNOWN",
                                 "a resolution resumed the request")
                event = scheduler.events[-1]
                self.assertEqual((event["type"], event["operation_id"]),
                                 ("scheduler.unknown_resolved", op))
                self.assertEqual(set(event["payload"]),
                                 {"resolution", "state", "operator", "evidence", "note"}
                                 | ({"candidate"} if resolution == "FINISHED" else set()))
                self.assertEqual(event["payload"]["state"], state)
                # The resource holds are given back for all three.  A confirmed
                # finish is in review, so its task stays taken until the verdict;
                # the other two leave the task free for a new attempt.
                self.resume(scheduler)
                if resolution == "FINISHED":
                    self.code(scheduler, "E_ACTIVE_TASK",
                              lambda: self.claim(scheduler, "task-1", key="idem-task-1-again"))
                else:
                    again = self.claim(scheduler, "task-1", key="idem-task-1-again")
                    self.assertEqual(scheduler.operation(again)["state"], "RESERVED")


class DurableResolutionCase(ResumeCase):
    """The two new methods on the command log: same state after a reopen."""

    def test_each_resolution_and_the_resume_replay_identically(self):
        for resolution in ("NOT_STARTED", "ABANDONED", "FINISHED"):
            with self.subTest(resolution=resolution):
                path = str(self.work / ("ledger-%s.db" % resolution))
                ledger = self.ledger(path)
                op = self.start_unknown(ledger, "task-1")
                ledger.settle(op, {"tokens": 7})
                self.resolve(ledger, op, resolution)
                self.resume(ledger)
                expected = self.state_of(ledger)
                ledger.close()
                for _ in range(2):
                    with open_ledger(path) as reopened:
                        self.assertEqual(self.state_of(reopened), expected,
                                         "the replay restored a different state")
                        self.assertIsNone(reopened._scheduler._requests["req-1"].stopped)
                methods, fmt = self.log_of(path)
                self.assertEqual(fmt, LEDGER_FORMAT)
                self.assertEqual(MUTATING_METHODS[-2:], ("resolve_unknown", "resume"))
                self.assertIn("resolve_unknown", methods)
                self.assertIn("resume", methods)

    def test_a_refused_call_records_nothing_and_keeps_the_instance_usable(self):
        path = str(self.work / "ledger.db")
        ledger = self.ledger(path)
        op = self.start_unknown(ledger, "task-1")
        before = ledger.seq
        # A note the log could not encode is refused by the in-memory check, so
        # nothing is appended and the instance stays usable.
        self.refused(ledger, "E_NOTE", lambda: self.resolve(ledger, op, note="checked \udcff"))
        self.refused(ledger, "E_RESUME_UNKNOWN", lambda: self.resume(ledger))
        self.assertEqual(ledger.seq, before, "a refused call was appended")
        ledger.settle(op, {"tokens": 7})
        self.resolve(ledger, op, "ABANDONED")
        self.resume(ledger)
        ledger.close()
        with open_ledger(path) as reopened:
            self.assertEqual(reopened.seq, before + 3)
            self.assertIsNone(reopened._scheduler._requests["req-1"].stopped)
        methods, _ = self.log_of(path)
        self.assertEqual(methods[-2:], ["resolve_unknown", "resume"])
        ledger.close()
        with self.assertRaises(LedgerError):
            ledger.operation(op)


class TwoRequestsCase(ResumeCase):
    """A resume is per request: the other stopped request keeps its guards."""

    def test_resuming_one_request_leaves_the_other_stopped(self):
        scheduler = Scheduler()
        for request in ("req-1", "req-2"):
            scheduler.seed_request(request, "repo-1", CAPS, concurrency=4)
            for task in ("task-1", "task-2"):
                scheduler.seed_task(request, task, CAPS)
        first = self.start_unknown(scheduler, "task-1", request="req-1", resources=["src/a.py"])
        second = self.start_unknown(scheduler, "task-1", request="req-2", resources=["src/b.py"])
        scheduler.settle(first, {"tokens": 7})
        scheduler.settle(second, {"tokens": 9})
        self.resolve(scheduler, first, "ABANDONED")
        self.resolve(scheduler, second, "ABANDONED")
        scheduler.stop("req-2", "OPERATOR_STOP")
        # ``unknown`` recorded the first reason, and ``stop`` did not replace it.
        self.assertEqual((scheduler._requests["req-1"].stopped, scheduler._requests["req-2"].stopped),
                         ("START_UNKNOWN", "START_UNKNOWN"))
        self.resume(scheduler, "req-1")
        self.assertIsNone(scheduler._requests["req-1"].stopped)
        self.assertEqual(scheduler._requests["req-2"].stopped, "START_UNKNOWN")
        # The other request's operations and usage are untouched.
        self.assertEqual(scheduler.operation(second)["state"], ABANDONED)
        self.assertEqual(scheduler.snapshot("req-2")["tokens"]["known"], 9)
        self.code(scheduler, "E_STOPPED", lambda: self.claim(scheduler, "task-2", request="req-2"),
                  request="req-2")
        again = self.claim(scheduler, "task-1", request="req-1", key="idem-req-1-again")
        self.assertEqual(scheduler.operation(again)["state"], "RESERVED")
        # Resuming it too is a separate decision, and its compare-and-swap names
        # every reason the ledger recorded for it -- the unknown start *and* the
        # operator stop that was stacked on it -- not just one of them.
        self.code(scheduler, "E_STOP_REASON_CHANGED",
                  lambda: self.resume(scheduler, "req-2", expected_reasons=["OPERATOR_STOP"]), request="req-2")
        self.code(scheduler, "E_STOP_REASON_CHANGED",
                  lambda: self.resume(scheduler, "req-2", expected_reasons=["START_UNKNOWN"]), request="req-2")
        self.resume(scheduler, "req-2", expected_reasons=["START_UNKNOWN", "OPERATOR_STOP"])
        self.assertIsNone(scheduler._requests["req-2"].stopped)


class StopAgainCase(ResumeCase):
    """A resume opens the request; the guards apply again from there."""

    def test_a_resumed_request_stops_again_and_resumes_again(self):
        scheduler = self.scheduler()
        op = self.reviewed(scheduler, "task-1")
        scheduler.stop("req-1")
        self.resume(scheduler, expected_reasons=["OPERATOR_STOP"])
        self.assertIsNone(scheduler._requests["req-1"].stopped)
        # A usage over the request cap stops it again, exactly as before.
        scheduler.settle(op, {"tokens": 600}, revision=2, correction=True)
        self.assertEqual(scheduler._requests["req-1"].stopped, "BUDGET_EXCEEDED")
        event = scheduler.events[-1]
        self.assertEqual((event["type"], event["payload"]),
                         ("request.stop_observed", {"request_id": "req-1", "reason": "BUDGET_EXCEEDED"}))
        # The operator stop the resume already cleared is gone from the list: a
        # new list starts at the resume, and only the budget guard is in it.
        self.assertEqual(scheduler.stop_reasons("req-1"), ("BUDGET_EXCEEDED",))
        self.code(scheduler, "E_STOP_REASON_CHANGED",
                  lambda: self.resume(scheduler, expected_reasons=["OPERATOR_STOP"]))
        # No cap is raised: the recorded usage has to come back under it.
        self.code(scheduler, "E_RESUME_BUDGET",
                  lambda: self.resume(scheduler, expected_reasons=["BUDGET_EXCEEDED"]))
        scheduler.settle(op, {"tokens": 5}, revision=3, correction=True)
        self.resume(scheduler, expected_reasons=["BUDGET_EXCEEDED"])
        self.assertIsNone(scheduler._requests["req-1"].stopped)
        again = self.claim(scheduler, "task-2", key="idem-task-2")
        self.assertEqual(scheduler.operation(again)["state"], "RESERVED")

    def test_resuming_a_request_that_is_not_stopped_changes_nothing(self):
        scheduler = self.scheduler()
        self.reviewed(scheduler, "task-1")
        before = self.state_of(scheduler)
        self.refused(scheduler, "E_NOT_STOPPED",
                     lambda: self.resume(scheduler, expected_reasons=["OPERATOR_STOP"]))
        self.assertEqual(self.state_of(scheduler), before)

    def test_resuming_an_already_resumed_request_changes_nothing(self):
        scheduler = self.scheduler()
        self.reviewed(scheduler, "task-1")
        scheduler.stop("req-1")
        self.resume(scheduler, expected_reasons=["OPERATOR_STOP"])
        before = self.state_of(scheduler)
        self.refused(scheduler, "E_NOT_STOPPED",
                     lambda: self.resume(scheduler, expected_reasons=["OPERATOR_STOP"]))
        self.assertEqual(self.state_of(scheduler), before)

    def test_the_resume_names_every_stop_reason_recorded(self):
        scheduler = self.scheduler()
        op = self.start_unknown(scheduler)
        scheduler.settle(op, {"tokens": 7})
        scheduler.stop("req-1", "OPERATOR_STOP")
        self.assertEqual(scheduler.stop_reasons("req-1"), ("START_UNKNOWN", "OPERATOR_STOP"))
        self.assertEqual(scheduler._requests["req-1"].stopped, "START_UNKNOWN",
                         "a later stop replaced the first reason")
        # Naming one of the two reasons is not enough, whichever one it is, and
        # neither is naming them in another order: only the whole list, in the
        # order the ledger recorded it, gets past the comparison.
        for label, reasons in (("only the first reason", ["START_UNKNOWN"]),
                               ("only the later reason", ["OPERATOR_STOP"]),
                               ("the reasons in another order", ["OPERATOR_STOP", "START_UNKNOWN"])):
            with self.subTest(case=label):
                self.code(scheduler, "E_STOP_REASON_CHANGED",
                          lambda: self.resume(scheduler, expected_reasons=reasons))
        # Naming all of them moves the check on to the next precondition.
        self.code(scheduler, "E_RESUME_UNKNOWN",
                  lambda: self.resume(scheduler, expected_reasons=["START_UNKNOWN", "OPERATOR_STOP"]))
        self.resolve(scheduler, op, "ABANDONED")
        self.resume(scheduler, expected_reasons=["START_UNKNOWN", "OPERATOR_STOP"])
        self.assertIsNone(scheduler._requests["req-1"].stopped)
        self.assertEqual(scheduler.stop_reasons("req-1"), ())


class RefusedResolutionCase(ResumeCase):
    """Every refusal of ``resolve_unknown``, each against a legal baseline.

    Each case names the *legal* arguments first and then the single value that is
    changed, so the refused call differs from a call that succeeded in exactly
    one value: no other rule can be the one refusing it.
    """

    #: A legal resolution of an abandoned run, and the two variants of it.
    BASE = dict(resolution="ABANDONED", operator=OPERATOR, evidence_hash=EVIDENCE, note=NOTE, candidate_hash=None)
    NOT_STARTED = dict(BASE, resolution="NOT_STARTED")
    FINISHED = dict(BASE, resolution="FINISHED", candidate_hash=CANDIDATE)

    CASES = [
        ("a resolution in lower case", "E_RESOLUTION", BASE, dict(resolution="abandoned")),
        ("a resolution that is not text", "E_RESOLUTION", BASE, dict(resolution=12)),
        ("an evidence hash one character short", "E_EVIDENCE_HASH", BASE, dict(evidence_hash="e" * 63)),
        ("an evidence hash that is not text", "E_EVIDENCE_HASH", BASE, dict(evidence_hash=b"e" * 64)),
        ("a finished run whose candidate is not exact text", "E_CLEANUP_OR_CANDIDATE", FINISHED,
         dict(candidate_hash=Key(CANDIDATE))),
        ("a finished run whose candidate is upper case", "E_CLEANUP_OR_CANDIDATE", FINISHED,
         dict(candidate_hash="D" * 64)),
        ("a finished run whose candidate is missing", "E_CLEANUP_OR_CANDIDATE", FINISHED,
         dict(candidate_hash=None)),
        ("a not-started run that carries a candidate", "E_CLEANUP_OR_CANDIDATE", NOT_STARTED,
         dict(candidate_hash=CANDIDATE)),
        ("an abandoned run that carries a candidate", "E_CLEANUP_OR_CANDIDATE", BASE,
         dict(candidate_hash=CANDIDATE)),
        ("an operator with a delete character", "E_OPERATOR", BASE, dict(operator="ops\x7f1")),
        ("an operator with a tab", "E_OPERATOR", BASE, dict(operator="ops\t1")),
        ("an operator that is a number", "E_OPERATOR", BASE, dict(operator=7)),
        ("an operator that is not exact text", "E_OPERATOR", BASE, dict(operator=Key("ops-1"))),
        ("an operator that is too long", "E_OPERATOR", BASE, dict(operator="o" * 201)),
        ("a note with a NUL", "E_NOTE", BASE, dict(note="checked\x00")),
        ("a note with a line break", "E_NOTE", BASE, dict(note="checked\ntwice")),
        ("a note that is too long", "E_NOTE", BASE, dict(note="n" * 501)),
        ("a note that is not text", "E_NOTE", BASE, dict(note=["note"])),
        ("a newer generation", "E_FENCED", BASE, dict(generation=2)),
        ("a generation that is a float", "E_FENCED", BASE, dict(generation=1.0)),
    ]

    def test_every_refused_resolution_changes_nothing(self):
        for label, code, legal, changes in self.CASES:
            with self.subTest(case=label):
                # The legal baseline: the same operation, the same call, nothing
                # but the one value under test changed.
                baseline = self.scheduler()
                op = self.start_unknown(baseline)
                self.resolve(baseline, op, **legal)
                self.assertNotEqual(baseline.operation(op)["state"], "START_UNKNOWN")
                scheduler = self.scheduler()
                op = self.start_unknown(scheduler)
                self.refused(scheduler, code, lambda: self.resolve(scheduler, op, **dict(legal, **changes)))

    def test_an_operation_that_is_not_a_known_exact_id_is_refused(self):
        scheduler = self.scheduler()
        op = self.start_unknown(scheduler)
        for value in (Key(op), None, 42, ["op"], b"op"):
            with self.subTest(operation=repr(value)):
                self.refused(scheduler, "E_OPERATION_UNKNOWN",
                             lambda: self.resolve(scheduler, value))
        self.resolve(scheduler, op)
        self.assertEqual(scheduler.operation(op)["state"], ABANDONED)

    def test_only_a_start_unknown_operation_may_be_resolved(self):
        states = {
            "RESERVED": lambda scheduler, task: self.claim(scheduler, task),
            "HANDOFF_ACCEPTED": lambda scheduler, task: self.handed_off(scheduler, task),
            "RUNNING": lambda scheduler, task: self.running(scheduler, task),
            "REVIEW_PENDING": lambda scheduler, task: self.reviewed(scheduler, task),
            "ACCEPTED": lambda scheduler, task: self.verdict(scheduler, task, True),
            "REJECTED": lambda scheduler, task: self.verdict(scheduler, task, False),
            "NOT_STARTED": lambda scheduler, task: self.proven_unstarted(scheduler, task),
            "CANCELLED_NOT_STARTED": lambda scheduler, task: self.cancelled(scheduler, task),
        }
        for state, build in states.items():
            with self.subTest(state=state):
                scheduler = self.scheduler()
                # The operation under test is built first: ``unknown`` stops the
                # request, and nothing else may be claimed after that.
                op = build(scheduler, "task-1")
                # The legal baseline: the very same call on a START_UNKNOWN
                # operation of this very scheduler.
                unknown = self.start_unknown(scheduler, "task-3")
                self.resolve(scheduler, unknown, "ABANDONED")
                self.assertEqual(scheduler.operation(unknown)["state"], ABANDONED)
                self.assertEqual(scheduler.operation(op)["state"], state)
                self.refused(scheduler, "E_RESOLVE_STATE", lambda: self.resolve(scheduler, op))

    def test_an_already_resolved_operation_is_not_resolved_again(self):
        for resolution in ("NOT_STARTED", "ABANDONED", "FINISHED"):
            with self.subTest(resolution=resolution):
                scheduler = self.scheduler()
                op = self.start_unknown(scheduler)
                self.resolve(scheduler, op, resolution)
                before = self.state_of(scheduler)
                self.refused(scheduler, "E_RESOLVE_STATE",
                             lambda: self.resolve(scheduler, op, resolution))
                self.assertEqual(self.state_of(scheduler), before)

    def test_the_longest_operator_and_note_are_accepted(self):
        scheduler = self.scheduler()
        op = self.start_unknown(scheduler)
        self.resolve(scheduler, op, operator="o" * 200, note="n" * 500)
        payload = scheduler.events[-1]["payload"]
        self.assertEqual((payload["operator"], payload["note"]), ("o" * 200, "n" * 500))


class RefusedResumeCase(ResumeCase):
    """Every refusal of ``resume``, each against a legal baseline.

    As for the resolution: each case names the legal arguments first and then the
    single value that is changed, so no other rule can be the one refusing it.
    """

    #: A legal resume of a request an operator stopped, and nothing else.
    LEGAL = dict(request="req-1", generation=1, expected_reasons=["OPERATOR_STOP"], operator=OPERATOR, note=NOTE)

    CASES = [
        ("expected reasons that are not a list", "E_STOP_REASON", dict(expected_reasons=12)),
        ("a reason that is not exact text", "E_STOP_REASON",
         dict(expected_reasons=[Key("OPERATOR_STOP")])),
        ("an operator with a line break", "E_OPERATOR", dict(operator="ops\n1")),
        ("an operator that is not exact text", "E_OPERATOR", dict(operator=Key("ops-1"))),
        ("an operator that is too long", "E_OPERATOR", dict(operator="o" * 201)),
        ("a note that is too long", "E_NOTE", dict(note="n" * 501)),
        ("a note with a control character", "E_NOTE", dict(note="checked\x00")),
        ("a newer generation", "E_FENCED", dict(generation=2)),
        ("a generation that is a float", "E_FENCED", dict(generation=1.0)),
        ("a request that is not exact text", "E_REQUEST_UNKNOWN", dict(request=Key("req-1"))),
    ]

    def stopped(self, *, shape="reserved", tokens=5):
        """A stopped request holding one operation, and the reason it stopped with."""
        scheduler = self.scheduler()
        if shape == "unknown_resolved":
            op = self.start_unknown(scheduler, "task-1")
            scheduler.settle(op, {"tokens": tokens})
            self.resolve(scheduler, op, "ABANDONED")
            return scheduler, "START_UNKNOWN"
        op = self.claim(scheduler, "task-1")
        if shape != "reserved":
            scheduler.handoff(op, "owner-1", 1)
        if shape in ("running", "review"):
            scheduler.mark_running(op, "owner-1", 1)
        if shape == "review":
            scheduler.settle(op, {"tokens": tokens})
            scheduler.finish(op, "owner-1", 1, CANDIDATE, True)
        scheduler.stop("req-1", "OPERATOR_STOP")
        return scheduler, "OPERATOR_STOP"

    def test_a_legal_resume_of_every_stopped_shape(self):
        for shape in ("reserved", "running", "review", "unknown_resolved"):
            with self.subTest(shape=shape):
                scheduler, reason = self.stopped(shape=shape)
                self.assertEqual(scheduler._requests["req-1"].stopped, reason)
                self.resume(scheduler, expected_reasons=[reason])
                self.assertIsNone(scheduler._requests["req-1"].stopped)
                event = scheduler.events[-1]
                self.assertEqual((event["type"], event["operation_id"]),
                                 ("request.resumed", None))
                self.assertEqual(set(event["payload"]), {"request_id", "previous_reasons", "operator", "note"})
                self.assertEqual(event["payload"]["previous_reasons"], [reason])

    def test_every_refused_resume_changes_nothing(self):
        for label, code, changes in self.CASES:
            with self.subTest(case=label):
                # The legal baseline: the same request, the same call, nothing but
                # the one value under test changed.
                baseline, _ = self.stopped()
                self.resume(baseline, expected_reasons=["OPERATOR_STOP"])
                self.assertIsNone(baseline._requests["req-1"].stopped)
                scheduler = self.stopped()[0]
                self.refused(scheduler, code,
                             lambda: scheduler.resume(**dict(self.LEGAL, **changes)))

    def test_a_request_that_is_not_a_known_exact_id_is_refused(self):
        scheduler, reason = self.stopped()
        for value in ("req-9", None, 42, ["req-1"], Key("req-1")):
            with self.subTest(request=repr(value)):
                self.refused(scheduler, "E_REQUEST_UNKNOWN",
                             lambda: scheduler.resume(value, 1, expected_reasons=[reason],
                                                      operator=OPERATOR, note=NOTE))

    def test_an_older_generation_undecided_operation_blocks_the_resume(self):
        for state in sorted(UNDECIDED):
            with self.subTest(state=state):
                scheduler = self.scheduler()
                op = self.claim(scheduler, "task-1")
                if state != "RESERVED":
                    scheduler.handoff(op, "owner-1", 1)
                if state == "RUNNING":
                    scheduler.mark_running(op, "owner-1", 1)
                scheduler.rollover()
                self.assertEqual(scheduler.generation, 2)
                self.code(scheduler, "E_RESUME_UNDECIDED",
                          lambda: self.resume(scheduler, generation=2,
                                              expected_reasons=["RECOVERY_REQUIRED"]))

    def test_an_older_generation_that_was_decided_does_not_block_the_resume(self):
        for outcome, settle in (("NOT_STARTED", False), ("FINISHED", True), ("START_UNKNOWN", True)):
            with self.subTest(outcome=outcome):
                scheduler = self.scheduler()
                op = self.claim(scheduler, "task-1")
                scheduler.handoff(op, "owner-1", 1)
                scheduler.rollover()
                scheduler.recover(op, "owner-2", 2, outcome=outcome, evidence_hash=EVIDENCE,
                                  candidate_hash=CANDIDATE if outcome == "FINISHED" else None)
                if settle:
                    scheduler.settle(op, {"tokens": 3})
                if outcome == "START_UNKNOWN":
                    self.code(scheduler, "E_RESUME_UNKNOWN",
                              lambda: self.resume(scheduler, generation=2,
                                                  expected_reasons=["RECOVERY_REQUIRED"]))
                    self.resolve(scheduler, op, "ABANDONED", generation=2)
                self.resume(scheduler, generation=2, expected_reasons=["RECOVERY_REQUIRED"])
                self.assertIsNone(scheduler._requests["req-1"].stopped)

    def test_a_run_of_the_current_generation_does_not_block_the_resume(self):
        scheduler = self.scheduler()
        op = self.running(scheduler, "task-1")
        scheduler.stop("req-1")
        self.resume(scheduler, expected_reasons=["OPERATOR_STOP"])
        self.assertEqual(scheduler.operation(op)["state"], "RUNNING")
        scheduler.settle(op, {"tokens": 5})
        scheduler.finish(op, "owner-1", 1, CANDIDATE, True)
        self.assertEqual(scheduler.operation(op)["state"], "REVIEW_PENDING")
        self.assertIsNone(scheduler._requests["req-1"].stopped)

    def test_the_longest_operator_and_note_are_accepted(self):
        scheduler = self.scheduler()
        self.reviewed(scheduler, "task-1")
        scheduler.stop("req-1")
        self.resume(scheduler, expected_reasons=["OPERATOR_STOP"], operator="o" * 200, note="n" * 500)
        payload = scheduler.events[-1]["payload"]
        self.assertEqual((payload["operator"], payload["note"]), ("o" * 200, "n" * 500))


if __name__ == "__main__":
    unittest.main()
