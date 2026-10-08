"""Planner oracle for resume-holds (`.agent/design/request-resume.md`, section 9).

Planner-provided; executors must not modify this file.

The review of request-resume attempt-001 found that ``resume`` compared only
the first stop reason, so a hold put on an already stopped request (an
operator's ``stop(..., "SECURITY_HOLD")``) was cleared with it, and that an
operation the ledger saw running could still be resolved ``NOT_STARTED`` with
its unrecorded usage taken as zero.  The user decided (2026-10-08):

* every stop reason since the last resume is kept, and ``resume`` must name
  exactly that list (``expected_reasons``); ``stop_reasons(request)`` reads it;
* an operation the ledger saw running (``run.started``) cannot be resolved
  ``NOT_STARTED`` -- only ``ABANDONED`` or ``FINISHED``.

Planner decisions P8-P12: the reasons are kept in first-seen order by every
stop (``stop``, ``rollover``, ``unknown``, the budget guard); the first stays
in ``stopped``; ``mark_running`` marks the operation started, and ``recover``
refuses ``NOT_STARTED`` for it as well.  Section 10 (2026-10-09) then made
every explicit ``stop`` its own entry -- the same reason twice is named twice
-- while the automatic reasons stay once (``tests/test_explicit_holds_oracle.py``).
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from sentinelmesh.durable_ledger import LedgerError, create_ledger, open_ledger
from sentinelmesh.scheduler import ContractError, Scheduler

CAPS = {"dispatches": 9, "tokens": 500}
RESERVATION = {"dispatches": 1, "tokens": 40}
EVIDENCE, CANDIDATE = "e" * 64, "d" * 64
OPERATOR, NOTE = "ops-1", "checked the process table and the worktree"


class Key(str):
    """A ``str`` subclass: equal to the text it spells, but not exact text."""


class HoldsCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="resume-holds-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)

    def scheduler(self, ledger=None, *, requests=("req-1",)):
        scheduler = ledger or Scheduler()
        for request in requests:
            scheduler.seed_request(request, "repo-" + request, CAPS, concurrency=4)
            for task in ("task-1", "task-2"):
                scheduler.seed_task(request, task, CAPS)
        return scheduler

    def claim(self, scheduler, task="task-1", *, request="req-1", key=None):
        return scheduler.claim(request, task, 1, key or "idem-%s-%s" % (request, task), "owner-1", 1, 1,
                               RESERVATION, ["src/%s.py" % task])["operation_id"]

    def unknown_after(self, scheduler, *, running, task="task-1"):
        """A START_UNKNOWN operation; ``running`` says whether its start was registered first."""
        op = self.claim(scheduler, task)
        scheduler.handoff(op, "owner-1", 1)
        if running:
            scheduler.mark_running(op, "owner-1", 1)
        scheduler.unknown(op, "owner-1", 1)
        return op

    def resolve(self, scheduler, op, resolution, *, generation=1):
        return scheduler.resolve_unknown(op, generation, resolution=resolution, operator=OPERATOR,
                                         evidence_hash=EVIDENCE, note=NOTE,
                                         candidate_hash=CANDIDATE if resolution == "FINISHED" else None)

    def resume(self, scheduler, reasons, *, generation=1, request="req-1", **changes):
        arguments = dict(expected_reasons=reasons, operator=OPERATOR, note=NOTE)
        arguments.update(changes)
        return scheduler.resume(request, generation, **arguments)

    def refused(self, scheduler, code, call, *, op=None):
        before = (scheduler.events, scheduler.stop_reasons("req-1"), scheduler.snapshot("req-1"),
                  None if op is None else scheduler.operation(op))
        with self.assertRaises(ContractError) as ctx:
            call()
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual((scheduler.events, scheduler.stop_reasons("req-1"), scheduler.snapshot("req-1"),
                          None if op is None else scheduler.operation(op)), before,
                         "a refused call changed the ledger")


class StackedHoldsOracle(HoldsCase):
    def test_a_hold_stacked_on_a_stopped_request_must_be_named(self):
        scheduler = self.scheduler()
        scheduler.rollover()
        scheduler.stop("req-1", "SECURITY_HOLD")
        self.assertEqual(scheduler.stop_reasons("req-1"), ("RECOVERY_REQUIRED", "SECURITY_HOLD"))
        self.assertEqual(scheduler._requests["req-1"].stopped, "RECOVERY_REQUIRED", "the first reason moved")
        for label, reasons in (("only the first reason", ["RECOVERY_REQUIRED"]),
                               ("the reasons in another order", ["SECURITY_HOLD", "RECOVERY_REQUIRED"]),
                               ("one reason too many", ["RECOVERY_REQUIRED", "SECURITY_HOLD", "OPERATOR_STOP"])):
            with self.subTest(case=label):
                self.refused(scheduler, "E_STOP_REASON_CHANGED", lambda: self.resume(scheduler, reasons, generation=2))
        self.resume(scheduler, ["RECOVERY_REQUIRED", "SECURITY_HOLD"], generation=2)
        self.assertEqual((scheduler._requests["req-1"].stopped, scheduler.stop_reasons("req-1")), (None, ()))
        event = scheduler.events[-1]
        self.assertEqual((event["type"], event["payload"]),
                         ("request.resumed", {"request_id": "req-1",
                                              "previous_reasons": ["RECOVERY_REQUIRED", "SECURITY_HOLD"],
                                              "operator": OPERATOR, "note": NOTE}))
        # A new stop after the resume starts a new list.
        scheduler.stop("req-1", "SECURITY_HOLD")
        self.assertEqual(scheduler.stop_reasons("req-1"), ("SECURITY_HOLD",))

    def test_every_stop_records_its_reason_once(self):
        with self.subTest(case="the same explicit reason twice"):
            # Section 10, decision 2: every explicit stop is its own hold.
            scheduler = self.scheduler()
            scheduler.stop("req-1", "SECURITY_HOLD")
            scheduler.stop("req-1", "SECURITY_HOLD")
            self.assertEqual(scheduler.stop_reasons("req-1"), ("SECURITY_HOLD", "SECURITY_HOLD"))
            self.assertEqual(sum(e["type"] == "request.stopped" for e in scheduler.events), 2)
        with self.subTest(case="the budget guard on a stopped request"):
            scheduler = self.scheduler()
            op = self.claim(scheduler)
            scheduler.handoff(op, "owner-1", 1)
            scheduler.mark_running(op, "owner-1", 1)
            scheduler.stop("req-1")
            scheduler.finish(op, "owner-1", 1, CANDIDATE, True)
            self.assertEqual(scheduler.stop_reasons("req-1"), ("OPERATOR_STOP", "USAGE_UNKNOWN"))
            scheduler.settle(op, {"tokens": 600})
            self.assertEqual(scheduler.stop_reasons("req-1"), ("OPERATOR_STOP", "USAGE_UNKNOWN", "BUDGET_EXCEEDED"))
        with self.subTest(case="an unknown start on a stopped request"):
            scheduler = self.scheduler()
            op = self.claim(scheduler)
            scheduler.handoff(op, "owner-1", 1)
            scheduler.stop("req-1")
            scheduler.unknown(op, "owner-1", 1)
            self.assertEqual(scheduler.stop_reasons("req-1"), ("OPERATOR_STOP", "START_UNKNOWN"))
        with self.subTest(case="a rollover over a running and a stopped request"):
            scheduler = self.scheduler(requests=("req-1", "req-2"))
            scheduler.stop("req-2")
            scheduler.rollover()
            self.assertEqual((scheduler.stop_reasons("req-1"), scheduler.stop_reasons("req-2")),
                             (("RECOVERY_REQUIRED",), ("OPERATOR_STOP", "RECOVERY_REQUIRED")))
            self.assertEqual(scheduler._requests["req-2"].stopped, "OPERATOR_STOP")

    def test_the_reasons_read_back(self):
        scheduler = self.scheduler()
        self.assertEqual(scheduler.stop_reasons("req-1"), ())
        scheduler.stop("req-1", "SECURITY_HOLD")
        events = scheduler.events
        reasons = scheduler.stop_reasons("req-1")
        self.assertEqual((type(reasons), reasons), (tuple, ("SECURITY_HOLD",)))
        self.assertEqual(scheduler.events, events, "a read recorded an event")
        for value in ("req-9", None, ["req-1"], Key("req-1")):
            with self.subTest(request=repr(value)):
                with self.assertRaises(ContractError) as ctx:
                    scheduler.stop_reasons(value)
                self.assertEqual(ctx.exception.code, "E_REQUEST_UNKNOWN")

    def test_the_expected_reasons_are_a_list_of_reasons(self):
        bad = {
            "a tuple": ("SECURITY_HOLD",),
            "a single text": "SECURITY_HOLD",
            "nothing": None,
            "an empty list": [],
            "an empty reason": [""],
            "a reason that is not text": [None],
            "a reason that is not exact text": [Key("SECURITY_HOLD")],
        }
        for label, reasons in bad.items():
            with self.subTest(case=label):
                scheduler = self.scheduler()
                scheduler.stop("req-1", "SECURITY_HOLD")
                self.refused(scheduler, "E_STOP_REASON", lambda: self.resume(scheduler, reasons))
                # Legal baseline: the same call naming the one reason.
                self.resume(scheduler, ["SECURITY_HOLD"])
                self.assertEqual(scheduler.stop_reasons("req-1"), ())

    def test_the_reason_checks_come_in_their_order(self):
        scheduler = self.scheduler()
        self.unknown_after(scheduler, running=False)
        self.assertEqual(scheduler.stop_reasons("req-1"), ("START_UNKNOWN",))
        self.refused(scheduler, "E_NOTE", lambda: self.resume(scheduler, None, note=""))
        self.refused(scheduler, "E_STOP_REASON", lambda: self.resume(scheduler, None, generation=0))
        self.refused(scheduler, "E_FENCED", lambda: self.resume(scheduler, ["X"], generation=0))
        self.refused(scheduler, "E_STOP_REASON_CHANGED", lambda: self.resume(scheduler, ["X"]))
        self.refused(scheduler, "E_RESUME_UNKNOWN", lambda: self.resume(scheduler, ["START_UNKNOWN"]))


class StartedRunOracle(HoldsCase):
    def test_a_run_the_ledger_saw_start_cannot_be_resolved_not_started(self):
        scheduler = self.scheduler()
        op = self.unknown_after(scheduler, running=True)
        self.refused(scheduler, "E_RESOLVE_CONFLICT", lambda: self.resolve(scheduler, op, "NOT_STARTED"), op=op)
        for resolution, state in (("ABANDONED", "ABANDONED"), ("FINISHED", "REVIEW_PENDING")):
            with self.subTest(resolution=resolution):
                other = self.scheduler()
                started = self.unknown_after(other, running=True)
                self.resolve(other, started, resolution)
                self.assertEqual(other.operation(started)["state"], state)
        with self.subTest(case="a handoff whose start was never registered"):
            other = self.scheduler()
            unstarted = self.unknown_after(other, running=False)
            self.resolve(other, unstarted, "NOT_STARTED")
            self.assertEqual(other.operation(unstarted)["state"], "NOT_STARTED")

    def test_the_conflict_is_checked_after_the_state_and_the_generation(self):
        scheduler = self.scheduler()
        running = self.claim(scheduler, "task-2")
        scheduler.handoff(running, "owner-1", 1)
        scheduler.mark_running(running, "owner-1", 1)
        started = self.unknown_after(scheduler, running=True)
        self.refused(scheduler, "E_RESOLVE_STATE", lambda: self.resolve(scheduler, running, "NOT_STARTED"), op=running)
        self.refused(scheduler, "E_FENCED", lambda: self.resolve(scheduler, started, "NOT_STARTED", generation=0),
                     op=started)
        self.refused(scheduler, "E_RESOLVE_CONFLICT", lambda: self.resolve(scheduler, started, "NOT_STARTED"),
                     op=started)

    def test_recover_refuses_not_started_for_a_run_the_ledger_saw_start(self):
        scheduler = self.scheduler()
        started = self.unknown_after(scheduler, running=True)
        scheduler.rollover()
        self.refused(scheduler, "E_RECOVERY_CONFLICT",
                     lambda: scheduler.recover(started, "owner-2", 2, outcome="NOT_STARTED", evidence_hash=EVIDENCE),
                     op=started)
        scheduler.recover(started, "owner-2", 2, outcome="START_UNKNOWN", evidence_hash=EVIDENCE)
        self.assertEqual(scheduler.operation(started)["generation"], 2)
        # A handoff whose start was never registered may still be recovered as not started.
        other = self.scheduler()
        op = self.claim(other)
        other.handoff(op, "owner-1", 1)
        other.unknown(op, "owner-1", 1)
        other.rollover()
        other.recover(op, "owner-2", 2, outcome="NOT_STARTED", evidence_hash=EVIDENCE)
        self.assertEqual(other.operation(op)["state"], "NOT_STARTED")


class DurableHoldsOracle(HoldsCase):
    def test_the_durable_ledger_keeps_the_reasons_and_the_start(self):
        path = str(self.work / "ledger.db")
        ledger = self.scheduler(create_ledger(path))
        op = self.unknown_after(ledger, running=True)
        ledger.stop("req-1", "SECURITY_HOLD")
        ledger.close()
        with self.assertRaises(LedgerError) as ctx:
            ledger.stop_reasons("req-1")
        self.assertEqual(ctx.exception.code, "E_LEDGER_UNAVAILABLE")
        with open_ledger(path) as reopened:
            self.assertEqual(reopened.stop_reasons("req-1"), ("START_UNKNOWN", "SECURITY_HOLD"))
            with self.assertRaises(ContractError) as ctx:
                self.resolve(reopened, op, "NOT_STARTED")
            self.assertEqual(ctx.exception.code, "E_RESOLVE_CONFLICT", "the replay lost the start")
            self.resolve(reopened, op, "ABANDONED")
            reopened.settle(op, {"tokens": 9})
            with self.assertRaises(ContractError) as ctx:
                self.resume(reopened, ["START_UNKNOWN"])
            self.assertEqual(ctx.exception.code, "E_STOP_REASON_CHANGED")
            self.resume(reopened, ["START_UNKNOWN", "SECURITY_HOLD"])
        with open_ledger(path) as reopened:
            self.assertEqual(reopened.stop_reasons("req-1"), ())
            self.claim(reopened, "task-1", key="idem-again")


if __name__ == "__main__":
    unittest.main()
