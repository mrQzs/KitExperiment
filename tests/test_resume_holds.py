"""Executor tests for resume-holds: stacked stop reasons and started runs.

These cover what the planner oracle (``tests/test_resume_holds_oracle.py``)
leaves out, following one rule throughout: every rejection case first proves a
legal baseline -- the very same call with nothing but the value under test
changed -- and then asserts the specific error, so a different rule refusing the
call first cannot hide a gap.  Every refusal is checked against the state it
found, events included.

The behaviour under test comes from `.agent/design/request-resume.md` sections
9 and 10: every stop reason since the last resume is kept and ``resume`` must
name exactly that list, ``stop_reasons`` reads it, every explicit ``stop`` is an
entry of its own (the same reason twice is two holds, the automatic reasons stay
once), and an operation the ledger saw running -- or one a supervised run was
handed to -- may not be decided as one that never started.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from sentinelmesh.durable_ledger import DurableScheduler, LedgerError, create_ledger, open_ledger
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

    def unknown_after(self, scheduler, *, running, task="task-1", request="req-1"):
        """A START_UNKNOWN operation; ``running`` says whether its start was registered first."""
        op = self.claim(scheduler, task, request=request)
        scheduler.handoff(op, "owner-1", 1)
        if running:
            scheduler.mark_running(op, "owner-1", 1)
        scheduler.unknown(op, "owner-1", 1)
        return op

    def resolve(self, scheduler, op, resolution="ABANDONED", *, generation=None, **changes):
        arguments = dict(resolution=resolution, operator=OPERATOR, evidence_hash=EVIDENCE, note=NOTE,
                         candidate_hash=CANDIDATE if resolution == "FINISHED" else None)
        arguments.update(changes)
        if generation is None:
            generation = scheduler.generation
        return scheduler.resolve_unknown(op, generation, **arguments)

    def resume(self, scheduler, reasons, *, generation=None, request="req-1", **changes):
        arguments = dict(expected_reasons=reasons, operator=OPERATOR, note=NOTE)
        arguments.update(changes)
        if generation is None:
            generation = scheduler.generation
        return scheduler.resume(request, generation, **arguments)

    def inner(self, scheduler):
        """``DurableScheduler`` keeps the ``Scheduler`` it wraps one layer down."""
        return scheduler._scheduler if isinstance(scheduler, DurableScheduler) else scheduler

    def state_of(self, scheduler, request="req-1"):
        """Everything a refused call must leave exactly as it found it."""
        inner = self.inner(scheduler)
        return (scheduler.events, scheduler.snapshot(request),
                tuple(inner._requests[name].stop_reasons for name in sorted(inner._requests)),
                inner._requests[request].stopped,
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


class StopReasonListCase(HoldsCase):
    """What is recorded, in which order, and by which write point."""

    def test_the_reasons_are_kept_in_recorded_order_when_they_interleave(self):
        scheduler = self.scheduler()
        reviewed = self.claim(scheduler, "task-1")
        scheduler.handoff(reviewed, "owner-1", 1)
        scheduler.mark_running(reviewed, "owner-1", 1)
        scheduler.settle(reviewed, {"tokens": 5})
        scheduler.finish(reviewed, "owner-1", 1, CANDIDATE, True)
        handed_off = self.claim(scheduler, "task-2")
        scheduler.handoff(handed_off, "owner-1", 1)
        scheduler.stop("req-1", "SECURITY_HOLD")
        # The same hold again: an explicit stop is its own entry, and it does not
        # move ``stopped``.
        scheduler.stop("req-1", "SECURITY_HOLD")
        # The unknown start does not replace the operator's hold, and neither
        # does the unknown usage or the over-cap amount stacked on top of it.
        scheduler.unknown(handed_off, "owner-1", 1)
        scheduler.observe(handed_off, "tokens", "call-1", 1, 5, False, EVIDENCE)
        scheduler.observe(handed_off, "tokens", "call-1", 2, 600, True, EVIDENCE)
        scheduler.rollover()
        scheduler.stop("req-1", "SECURITY_HOLD")
        self.assertEqual(scheduler.stop_reasons("req-1"),
                         ("SECURITY_HOLD", "SECURITY_HOLD", "START_UNKNOWN", "USAGE_UNKNOWN",
                          "BUDGET_EXCEEDED", "RECOVERY_REQUIRED", "SECURITY_HOLD"))
        self.assertEqual(scheduler._requests["req-1"].stopped, "SECURITY_HOLD",
                         "the first reason moved")
        self.assertEqual([e["type"] for e in scheduler.events].count("request.stopped"), 3,
                         "a repeated stop was not recorded as an event")
        self.assertEqual([e["type"] for e in scheduler.events].count("request.stop_observed"), 2)
        # Every one of them has to be named, and nothing else may be in the way:
        # the run is decided and its usage brought back under the cap first.
        self.resolve(scheduler, handed_off, "NOT_STARTED")
        scheduler.observe(handed_off, "tokens", "call-1", 3, 5, True, EVIDENCE, correction=True)
        self.resume(scheduler, ["SECURITY_HOLD", "SECURITY_HOLD", "START_UNKNOWN", "USAGE_UNKNOWN",
                                "BUDGET_EXCEEDED", "RECOVERY_REQUIRED", "SECURITY_HOLD"])
        self.assertEqual(scheduler.stop_reasons("req-1"), ())
        self.assertIsNone(scheduler._requests["req-1"].stopped)

    def test_every_write_point_records_its_reason_on_an_already_stopped_request(self):
        def stopped_running():
            """A stopped request holding one running operation with settled usage."""
            scheduler = self.scheduler()
            op = self.claim(scheduler)
            scheduler.handoff(op, "owner-1", 1)
            scheduler.mark_running(op, "owner-1", 1)
            scheduler.settle(op, {"tokens": 5})
            scheduler.stop("req-1")
            return scheduler, op

        def recovered(scheduler, op):
            scheduler.recover(op, "owner-2", 2, outcome="FINISHED", evidence_hash=EVIDENCE,
                              candidate_hash=CANDIDATE)

        #: The reason each write point adds, what has to be decided before the
        #: request may resume at all (the rollover fences the run out, the
        #: unknown start and the over-budget usage are preconditions of their
        #: own -- none of them is the reason comparison) and how many events the
        #: write point itself appends.
        cases = {
            "an operator hold": ("SECURITY_HOLD",
                                 lambda scheduler, op: scheduler.stop("req-1", "SECURITY_HOLD"), None, 1),
            "a rollover": ("RECOVERY_REQUIRED", lambda scheduler, op: scheduler.rollover(), recovered, 1),
            "an unknown start": ("START_UNKNOWN", lambda scheduler, op: scheduler.unknown(op, "owner-1", 1),
                                 lambda scheduler, op: self.resolve(scheduler, op), 1),
            "the budget guard": ("BUDGET_EXCEEDED",
                                 lambda scheduler, op: scheduler.settle(op, {"tokens": 600}, revision=2,
                                                                       correction=True),
                                 lambda scheduler, op: scheduler.settle(op, {"tokens": 5}, revision=3,
                                                                       correction=True), 2),
        }
        for label, (reason, stop_again, clear, appended) in cases.items():
            with self.subTest(case=label):
                scheduler, op = stopped_running()
                before = self.state_of(scheduler)
                stop_again(scheduler, op)
                self.assertEqual(scheduler._requests["req-1"].stopped, "OPERATOR_STOP",
                                 "the first reason moved")
                self.assertEqual(scheduler.stop_reasons("req-1"), ("OPERATOR_STOP", reason),
                                 "the stacked stop was not recorded")
                self.assertEqual(len(scheduler.events), len(before[0]) + appended,
                                 "the stop recorded unexpected events")
                # The stacked reason is what the compare-and-swap has to name.
                self.refused(scheduler, "E_STOP_REASON_CHANGED",
                             lambda: self.resume(scheduler, ["OPERATOR_STOP"]))
                if clear is not None:
                    clear(scheduler, op)
                self.resume(scheduler, ["OPERATOR_STOP", reason])
                self.assertEqual(scheduler.stop_reasons("req-1"), ())
                self.assertIsNone(scheduler._requests["req-1"].stopped)

    def test_the_reasons_read_back_without_touching_the_ledger(self):
        scheduler = self.scheduler()
        self.assertEqual(scheduler.stop_reasons("req-1"), (), "a running request carries reasons")
        scheduler.stop("req-1", "SECURITY_HOLD")
        events = scheduler.events
        reasons = scheduler.stop_reasons("req-1")
        self.assertEqual((type(reasons), reasons), (tuple, ("SECURITY_HOLD",)))
        self.assertEqual(scheduler.events, events, "a read recorded an event")
        for value in ("req-9", None, ["req-1"], Key("req-1")):
            with self.subTest(request=repr(value)):
                self.refused(scheduler, "E_REQUEST_UNKNOWN", lambda: scheduler.stop_reasons(value))

    def test_a_resume_leaves_the_list_it_was_given_alone(self):
        scheduler = self.scheduler()
        scheduler.stop("req-1", "SECURITY_HOLD")
        reasons = ["SECURITY_HOLD"]
        self.resume(scheduler, reasons)
        reasons.append("LATER")
        self.assertEqual(scheduler.stop_reasons("req-1"), (), "the caller's list was kept")
        self.assertEqual(scheduler.events[-1]["payload"]["previous_reasons"], ["SECURITY_HOLD"])

    def test_a_second_stop_after_the_resume_starts_a_new_list(self):
        scheduler = self.scheduler()
        op = self.claim(scheduler)
        scheduler.cancel_unstarted(op, 1)
        scheduler.stop("req-1", "SECURITY_HOLD")
        scheduler.rollover()
        self.resume(scheduler, ["SECURITY_HOLD", "RECOVERY_REQUIRED"])
        self.assertEqual(scheduler.stop_reasons("req-1"), ())
        # A hold stacked on a *resumed* request is a list of its own.
        scheduler.stop("req-1", "OPERATOR_STOP")
        scheduler.stop("req-1", "SECURITY_HOLD")
        scheduler.stop("req-1", "OPERATOR_STOP")
        # Every explicit stop is its own entry: the repeated default reason is
        # named twice, and it does not move ``stopped``.
        self.assertEqual(scheduler.stop_reasons("req-1"),
                         ("OPERATOR_STOP", "SECURITY_HOLD", "OPERATOR_STOP"))
        self.assertEqual(scheduler._requests["req-1"].stopped, "OPERATOR_STOP")
        self.resume(scheduler, ["OPERATOR_STOP", "SECURITY_HOLD", "OPERATOR_STOP"])
        resumed = [e["payload"] for e in scheduler.events if e["type"] == "request.resumed"]
        self.assertEqual(resumed, [{"request_id": "req-1",
                                    "previous_reasons": ["SECURITY_HOLD", "RECOVERY_REQUIRED"],
                                    "operator": OPERATOR, "note": NOTE},
                                   {"request_id": "req-1",
                                    "previous_reasons": ["OPERATOR_STOP", "SECURITY_HOLD", "OPERATOR_STOP"],
                                    "operator": OPERATOR, "note": NOTE}])
        self.assertEqual(scheduler.stop_reasons("req-1"), ())
        self.assertIsNone(scheduler._requests["req-1"].stopped)

    def test_resuming_one_request_leaves_the_other_reasons_alone(self):
        scheduler = self.scheduler(requests=("req-1", "req-2"))
        scheduler.stop("req-1", "SECURITY_HOLD")
        scheduler.rollover()
        self.assertEqual(scheduler.stop_reasons("req-1"), ("SECURITY_HOLD", "RECOVERY_REQUIRED"))
        self.assertEqual(scheduler.stop_reasons("req-2"), ("RECOVERY_REQUIRED",))
        self.assertEqual((scheduler._requests["req-1"].stopped, scheduler._requests["req-2"].stopped),
                         ("SECURITY_HOLD", "RECOVERY_REQUIRED"))
        self.resume(scheduler, ["SECURITY_HOLD", "RECOVERY_REQUIRED"], request="req-1")
        self.assertEqual(scheduler.stop_reasons("req-1"), ())
        self.assertEqual(scheduler.stop_reasons("req-2"), ("RECOVERY_REQUIRED",),
                         "the other request's reasons changed")
        self.assertEqual(scheduler._requests["req-2"].stopped, "RECOVERY_REQUIRED")
        # The other request keeps its own compare-and-swap.
        self.code(scheduler, "E_STOP_REASON_CHANGED",
                  lambda: self.resume(scheduler, ["SECURITY_HOLD", "RECOVERY_REQUIRED"], request="req-2"),
                  request="req-2")
        self.resume(scheduler, ["RECOVERY_REQUIRED"], request="req-2")
        self.assertEqual(scheduler.stop_reasons("req-2"), ())


class ExpectedReasonsCase(HoldsCase):
    """The shape of ``expected_reasons`` and the comparison against the list."""

    BAD = {
        "a tuple": ("SECURITY_HOLD",),
        "a single text": "SECURITY_HOLD",
        "nothing": None,
        "an empty list": [],
        "an empty reason": [""],
        "a reason that is not text": [None],
        "a reason that is not exact text": [Key("SECURITY_HOLD")],
        "an empty reason after a repeated one": ["SECURITY_HOLD", "SECURITY_HOLD", ""],
    }

    def test_the_expected_reasons_must_be_a_list_of_reasons(self):
        # A repeated reason is well formed: every explicit stop is its own hold,
        # so naming the same reason twice is naming two holds.
        for label, reasons in self.BAD.items():
            with self.subTest(case=label):
                scheduler = self.scheduler()
                scheduler.stop("req-1", "SECURITY_HOLD")
                before = self.state_of(scheduler)
                self.refused(scheduler, "E_STOP_REASON", lambda: self.resume(scheduler, reasons))
                self.assertEqual(self.state_of(scheduler), before)
                # Legal baseline: the same call naming the one reason.
                self.resume(scheduler, ["SECURITY_HOLD"])
                self.assertEqual(scheduler.stop_reasons("req-1"), ())

    def test_the_expected_reasons_are_compared_against_the_whole_list(self):
        scheduler = self.scheduler()
        scheduler.stop("req-1", "SECURITY_HOLD")
        scheduler.rollover()
        self.assertEqual(scheduler.stop_reasons("req-1"), ("SECURITY_HOLD", "RECOVERY_REQUIRED"))
        for label, reasons in (("only the first reason", ["SECURITY_HOLD"]),
                               ("only the last reason", ["RECOVERY_REQUIRED"]),
                               ("the reasons in another order", ["RECOVERY_REQUIRED", "SECURITY_HOLD"]),
                               ("one reason too many", ["SECURITY_HOLD", "RECOVERY_REQUIRED", "OPERATOR_STOP"]),
                               ("a reason of its own", ["SECURITY_HOLD"])):
            with self.subTest(case=label):
                self.refused(scheduler, "E_STOP_REASON_CHANGED", lambda: self.resume(scheduler, reasons))
        self.resume(scheduler, ["SECURITY_HOLD", "RECOVERY_REQUIRED"])

    def test_the_reasons_check_comes_after_the_shape_and_before_the_rest(self):
        scheduler = self.scheduler(requests=("req-1", "req-2"))
        self.unknown_after(scheduler, running=False)
        self.assertEqual(scheduler.stop_reasons("req-1"), ("START_UNKNOWN",))
        self.assertEqual(scheduler.stop_reasons("req-2"), ())
        # The shape of the list is checked before the generation and the request.
        self.refused(scheduler, "E_NOTE", lambda: self.resume(scheduler, None, note=""))
        self.refused(scheduler, "E_STOP_REASON", lambda: self.resume(scheduler, None, generation=0))
        self.refused(scheduler, "E_FENCED", lambda: self.resume(scheduler, ["X"], generation=0))
        self.refused(scheduler, "E_STOP_REASON_CHANGED", lambda: self.resume(scheduler, ["X"]))
        # Naming the recorded reason moves the check on to the next precondition.
        self.refused(scheduler, "E_RESUME_UNKNOWN", lambda: self.resume(scheduler, ["START_UNKNOWN"]))
        # A request that is not stopped is refused whatever the list is.
        self.refused(scheduler, "E_NOT_STOPPED", lambda: self.resume(scheduler, ["X"], request="req-2"),
                     request="req-2")


class StartedRunCase(HoldsCase):
    """A run the ledger saw start is not one that never started."""

    def test_a_started_run_cannot_be_resolved_not_started(self):
        for running in (True, False):
            with self.subTest(case="the start was registered" if running else "the start was never registered"):
                scheduler = self.scheduler()
                op = self.unknown_after(scheduler, running=running)
                code = "E_RESOLVE_CONFLICT" if running else None
                if running:
                    self.refused(scheduler, code, lambda: self.resolve(scheduler, op, "NOT_STARTED"))
                else:
                    # The legal baseline: the same call on a run whose start the
                    # ledger never saw.
                    self.resolve(scheduler, op, "NOT_STARTED")
                    self.assertEqual(scheduler.operation(op)["state"], "NOT_STARTED")
                # The other two resolutions are unaffected either way.
                for resolution, state in (("ABANDONED", "ABANDONED"), ("FINISHED", "REVIEW_PENDING")):
                    other = self.scheduler()
                    started = self.unknown_after(other, running=running)
                    self.resolve(other, started, resolution)
                    self.assertEqual(other.operation(started)["state"], state)

    def test_recover_refuses_not_started_for_a_started_run_it_took_over(self):
        scheduler = self.scheduler()
        started = self.unknown_after(scheduler, running=True)
        scheduler.rollover()
        self.refused(scheduler, "E_RECOVERY_CONFLICT",
                     lambda: scheduler.recover(started, "owner-2", 2, outcome="NOT_STARTED",
                                               evidence_hash=EVIDENCE))
        # The legal baseline: the same run taken over as an unknown start.
        scheduler.recover(started, "owner-2", 2, outcome="START_UNKNOWN", evidence_hash=EVIDENCE)
        self.assertEqual(scheduler.operation(started)["state"], "START_UNKNOWN")
        self.assertEqual(scheduler.operation(started)["generation"], 2)
        # A handoff whose start was never registered may still be recovered as
        # not started, and it is the only difference from the case above.
        other = self.scheduler()
        op = self.unknown_after(other, running=False)
        other.rollover()
        other.recover(op, "owner-2", 2, outcome="NOT_STARTED", evidence_hash=EVIDENCE)
        self.assertEqual(other.operation(op)["state"], "NOT_STARTED")

    def test_a_start_that_was_taken_over_still_cannot_be_resolved_not_started(self):
        scheduler = self.scheduler()
        op = self.unknown_after(scheduler, running=True)
        scheduler.rollover()
        scheduler.recover(op, "owner-2", 2, outcome="START_UNKNOWN", evidence_hash=EVIDENCE)
        self.assertEqual(scheduler.operation(op)["state"], "START_UNKNOWN")
        self.refused(scheduler, "E_RESOLVE_CONFLICT",
                     lambda: self.resolve(scheduler, op, "NOT_STARTED"))
        self.refused(scheduler, "E_FENCED",
                     lambda: self.resolve(scheduler, op, "NOT_STARTED", generation=1))
        # The legal baseline: the very same operation decided as abandoned.
        self.resolve(scheduler, op, "ABANDONED")
        self.assertEqual(scheduler.operation(op)["state"], "ABANDONED")

    def test_an_abandoned_started_run_resumes_when_every_reason_is_named(self):
        scheduler = self.scheduler()
        op = self.unknown_after(scheduler, running=True)
        self.assertEqual(scheduler.stop_reasons("req-1"), ("START_UNKNOWN",))
        self.resolve(scheduler, op, "ABANDONED")
        scheduler.settle(op, {"tokens": 9})
        # A hold stacked on the request the unknown start already stopped.
        scheduler.stop("req-1", "SECURITY_HOLD")
        self.assertEqual(scheduler.stop_reasons("req-1"), ("START_UNKNOWN", "SECURITY_HOLD"))
        self.refused(scheduler, "E_STOP_REASON_CHANGED", lambda: self.resume(scheduler, ["START_UNKNOWN"]))
        self.refused(scheduler, "E_STOP_REASON_CHANGED", lambda: self.resume(scheduler, ["SECURITY_HOLD"]))
        self.resume(scheduler, ["START_UNKNOWN", "SECURITY_HOLD"])
        self.assertEqual(scheduler.stop_reasons("req-1"), ())
        self.assertIsNone(scheduler._requests["req-1"].stopped)
        event = scheduler.events[-1]
        self.assertEqual((event["type"], event["payload"]),
                         ("request.resumed", {"request_id": "req-1",
                                              "previous_reasons": ["START_UNKNOWN", "SECURITY_HOLD"],
                                              "operator": OPERATOR, "note": NOTE}))


class DurableHoldsCase(HoldsCase):
    """The durable ledger's read accessor and the replay of both rules."""

    def ledger(self, name):
        return self.scheduler(create_ledger(str(self.work / name)))

    def test_the_durable_ledger_reads_the_reasons_back(self):
        path = str(self.work / "ledger.db")
        ledger = self.ledger(path)
        op = self.unknown_after(ledger, running=True)
        ledger.stop("req-1", "SECURITY_HOLD")
        self.assertEqual(ledger.stop_reasons("req-1"), ("START_UNKNOWN", "SECURITY_HOLD"))
        before = ledger.seq
        ledger.close()
        with self.assertRaises(LedgerError) as ctx:
            ledger.stop_reasons("req-1")
        self.assertEqual(ctx.exception.code, "E_LEDGER_UNAVAILABLE")
        with open_ledger(path) as reopened:
            self.assertEqual(reopened.stop_reasons("req-1"), ("START_UNKNOWN", "SECURITY_HOLD"))
            self.assertEqual(reopened.seq, before, "a read was appended to the log")
            with self.assertRaises(ContractError) as ctx:
                reopened.stop_reasons("req-9")
            self.assertEqual(ctx.exception.code, "E_REQUEST_UNKNOWN")
            with self.assertRaises(ContractError) as ctx:
                self.resolve(reopened, op, "NOT_STARTED")
            self.assertEqual(ctx.exception.code, "E_RESOLVE_CONFLICT", "the replay lost the start")
            self.resolve(reopened, op, "ABANDONED")
            reopened.settle(op, {"tokens": 9})
            self.refused(reopened, "E_STOP_REASON_CHANGED", lambda: self.resume(reopened, ["START_UNKNOWN"]))
            self.resume(reopened, ["START_UNKNOWN", "SECURITY_HOLD"])
        with open_ledger(path) as reopened:
            self.assertEqual(reopened.stop_reasons("req-1"), ())
            self.claim(reopened, "task-2", key="idem-again")


if __name__ == "__main__":
    unittest.main()
