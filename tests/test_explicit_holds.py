"""Executor tests for explicit-holds: every explicit hold, and supervised runs.

These cover what the planner oracle (``tests/test_explicit_holds_oracle.py``)
leaves out, following one rule throughout: every rejection case first proves a
legal baseline -- the very same call with nothing but the value under test
changed -- and then asserts the specific error, so a different rule refusing the
call first cannot hide a gap.  Every refusal is checked against the state it
found, events included.

The behaviour under test comes from `.agent/design/request-resume.md` section 10:
an operation a supervised run was handed to (a run descriptor recorded at its
handoff) can never be resolved ``NOT_STARTED`` by an operator, while every
explicit ``stop`` is a hold of its own -- the same reason twice is named twice,
and the automatic reasons (the budget guard, a rollover, an unknown start) stay
one entry each.  ``recover`` is untouched: it is the path that takes an
operation over from the records.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from sentinelmesh.durable_ledger import DurableScheduler, create_ledger, open_ledger
from sentinelmesh.scheduler import ContractError, Scheduler

CAPS = {"dispatches": 9, "tokens": 500}
RESERVATION = {"dispatches": 1, "tokens": 40}
EVIDENCE, CANDIDATE = "e" * 64, "d" * 64
OPERATOR, NOTE = "ops-1", "checked the process table and the worktree"
ENV_DIGEST = "a" * 64


class Key(str):
    """A ``str`` subclass: equal to the text it spells, but not exact text."""


class ExplicitCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="explicit-holds-")).resolve()
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

    def descriptor(self, op):
        """The run descriptor a bridge records at handoff."""
        return {"spec": {"request_id": "req-1", "run_id": op, "command": ["/usr/bin/true"],
                         "cwd": str(self.work), "environment_sha256": ENV_DIGEST, "timeout_seconds": 30},
                "evidence_dir": str(self.work), "worktree_id": "wt-1"}

    def unknown(self, scheduler, task="task-1", *, supervised=False, running=False):
        """A START_UNKNOWN operation.

        ``supervised`` says whether the handoff recorded a run descriptor, and
        ``running`` whether the ledger saw the run start -- the bridge registers
        the start only after the run, so a supervised run is normally the first
        without the second.
        """
        op = self.claim(scheduler, task)
        scheduler.handoff(op, "owner-1", 1, run=self.descriptor(op) if supervised else None)
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

    def stopped_events(self, scheduler):
        """How many explicit ``stop`` calls the ledger recorded as events."""
        return [e["type"] for e in scheduler.events].count("request.stopped")


class ExplicitHoldRecordingCase(ExplicitCase):
    """What is recorded, in which order, and by which write point."""

    def test_explicit_and_automatic_reasons_interleave_without_merging(self):
        scheduler = self.scheduler()
        first = self.claim(scheduler, "task-1")
        second = self.claim(scheduler, "task-2")
        for op in (first, second):
            scheduler.handoff(op, "owner-1", 1)
        scheduler.stop("req-1", "SECURITY_HOLD")
        scheduler.unknown(first, "owner-1", 1)
        scheduler.stop("req-1", "SECURITY_HOLD")
        scheduler.unknown(second, "owner-1", 1)
        scheduler.rollover()
        scheduler.rollover()
        scheduler.settle(first, {"tokens": 600})
        scheduler.settle(second, {"tokens": 600})
        # Every explicit stop is its own entry, in the order it was taken; the
        # two unknown starts and the two rollovers are one entry each, however
        # often they fired.
        self.assertEqual(scheduler.stop_reasons("req-1"),
                         ("SECURITY_HOLD", "START_UNKNOWN", "SECURITY_HOLD", "RECOVERY_REQUIRED",
                          "BUDGET_EXCEEDED"))
        self.assertEqual(scheduler._requests["req-1"].stopped, "SECURITY_HOLD", "the first reason moved")
        self.assertEqual(self.stopped_events(scheduler), 2)
        # Nothing but naming every one of them opens the request again.
        self.refused(scheduler, "E_STOP_REASON_CHANGED",
                     lambda: self.resume(scheduler, ["SECURITY_HOLD", "START_UNKNOWN", "SECURITY_HOLD",
                                                     "RECOVERY_REQUIRED"]))
        self.refused(scheduler, "E_STOP_REASON_CHANGED",
                     lambda: self.resume(scheduler, ["SECURITY_HOLD", "START_UNKNOWN", "SECURITY_HOLD",
                                                     "RECOVERY_REQUIRED", "BUDGET_EXCEEDED",
                                                     "BUDGET_EXCEEDED"]))
        self.refused(scheduler, "E_STOP_REASON_CHANGED",
                     lambda: self.resume(scheduler, ["SECURITY_HOLD", "SECURITY_HOLD", "START_UNKNOWN",
                                                     "RECOVERY_REQUIRED", "BUDGET_EXCEEDED"]))
        self.resolve(scheduler, first, "ABANDONED")
        self.resolve(scheduler, second, "ABANDONED")
        scheduler.settle(first, {"tokens": 600}, revision=2, correction=True)
        scheduler.settle(second, {"tokens": 0}, revision=2, correction=True)
        # The budget has to come back under the cap before the resume is legal.
        self.refused(scheduler, "E_RESUME_BUDGET",
                     lambda: self.resume(scheduler, ["SECURITY_HOLD", "START_UNKNOWN", "SECURITY_HOLD",
                                                     "RECOVERY_REQUIRED", "BUDGET_EXCEEDED"]))
        scheduler.settle(first, {"tokens": 100}, revision=3, correction=True)
        self.resume(scheduler, ["SECURITY_HOLD", "START_UNKNOWN", "SECURITY_HOLD",
                                "RECOVERY_REQUIRED", "BUDGET_EXCEEDED"])
        self.assertEqual(scheduler.stop_reasons("req-1"), ())
        self.assertIsNone(scheduler._requests["req-1"].stopped)

    def test_a_repeated_automatic_reason_is_still_one_entry(self):
        scheduler = self.scheduler()
        op = self.claim(scheduler)
        other = self.claim(scheduler, "task-2")
        scheduler.handoff(op, "owner-1", 1)
        scheduler.handoff(other, "owner-1", 1)
        scheduler.mark_running(op, "owner-1", 1)
        scheduler.settle(op, {"tokens": 600})
        scheduler.settle(op, {"tokens": 5}, revision=2, correction=True)
        # The guard fires again on the same request with the same reason.
        scheduler.settle(op, {"tokens": 600}, revision=3, correction=True)
        self.assertEqual(scheduler.stop_reasons("req-1"), ("BUDGET_EXCEEDED",))
        self.assertEqual(self.stopped_events(scheduler), 0,
                         "an automatic reason recorded an operator's stop event")
        # The unknown start and the rollover behave the same way, and neither
        # replaces the reason already there.
        scheduler.stop("req-1", "OPERATOR_STOP")
        scheduler.unknown(op, "owner-1", 1)
        scheduler.unknown(other, "owner-1", 1)
        scheduler.rollover()
        scheduler.rollover()
        self.assertEqual(scheduler.stop_reasons("req-1"),
                         ("BUDGET_EXCEEDED", "OPERATOR_STOP", "START_UNKNOWN", "RECOVERY_REQUIRED"))
        self.assertEqual(scheduler._requests["req-1"].stopped, "BUDGET_EXCEEDED", "the first reason moved")

    def test_a_second_round_after_a_resume_starts_a_new_list(self):
        scheduler = self.scheduler()
        scheduler.stop("req-1", "SECURITY_HOLD")
        scheduler.rollover()
        self.assertEqual(scheduler.stop_reasons("req-1"), ("SECURITY_HOLD", "RECOVERY_REQUIRED"))
        self.resume(scheduler, ["SECURITY_HOLD", "RECOVERY_REQUIRED"], generation=2)
        self.assertEqual(scheduler.stop_reasons("req-1"), ())
        # The very same reason twice in the new round: two holds of its own, and
        # the first round's reasons are gone.
        scheduler.stop("req-1", "SECURITY_HOLD")
        scheduler.stop("req-1", "SECURITY_HOLD")
        self.assertEqual(scheduler.stop_reasons("req-1"), ("SECURITY_HOLD", "SECURITY_HOLD"))
        self.assertEqual(scheduler._requests["req-1"].stopped, "SECURITY_HOLD")
        self.assertEqual(self.stopped_events(scheduler), 3,
                         "an explicit stop recorded no event of its own")
        self.refused(scheduler, "E_STOP_REASON_CHANGED", lambda: self.resume(scheduler, ["SECURITY_HOLD"],
                                                                             generation=2))
        self.resume(scheduler, ["SECURITY_HOLD", "SECURITY_HOLD"], generation=2)
        self.assertEqual(scheduler.stop_reasons("req-1"), ())
        resumed = [e["payload"] for e in scheduler.events if e["type"] == "request.resumed"]
        self.assertEqual(resumed, [{"request_id": "req-1",
                                    "previous_reasons": ["SECURITY_HOLD", "RECOVERY_REQUIRED"],
                                    "operator": OPERATOR, "note": NOTE},
                                   {"request_id": "req-1",
                                    "previous_reasons": ["SECURITY_HOLD", "SECURITY_HOLD"],
                                    "operator": OPERATOR, "note": NOTE}])

    def test_a_repeated_hold_on_one_request_leaves_the_other_reasons_alone(self):
        scheduler = self.scheduler(requests=("req-1", "req-2"))
        scheduler.stop("req-1", "SECURITY_HOLD")
        scheduler.stop("req-1", "SECURITY_HOLD")
        scheduler.stop("req-2", "SECURITY_HOLD")
        scheduler.stop("req-2", "SECURITY_HOLD")
        scheduler.rollover()
        self.assertEqual(scheduler.stop_reasons("req-1"),
                         ("SECURITY_HOLD", "SECURITY_HOLD", "RECOVERY_REQUIRED"))
        self.assertEqual(scheduler.stop_reasons("req-2"),
                         ("SECURITY_HOLD", "SECURITY_HOLD", "RECOVERY_REQUIRED"))
        self.assertEqual((scheduler._requests["req-1"].stopped, scheduler._requests["req-2"].stopped),
                         ("SECURITY_HOLD", "SECURITY_HOLD"))
        self.assertEqual(self.stopped_events(scheduler), 4,
                         "an explicit stop recorded no event of its own")
        self.resume(scheduler, ["SECURITY_HOLD", "SECURITY_HOLD", "RECOVERY_REQUIRED"], generation=2)
        self.assertEqual(scheduler.stop_reasons("req-1"), ())
        self.assertEqual(scheduler.stop_reasons("req-2"),
                         ("SECURITY_HOLD", "SECURITY_HOLD", "RECOVERY_REQUIRED"),
                         "the other request's reasons changed")
        self.assertEqual(scheduler._requests["req-2"].stopped, "SECURITY_HOLD")
        # The other request keeps its own compare-and-swap: the first request's
        # holds are not its, and neither of its own two holds is optional.
        self.refused(scheduler, "E_STOP_REASON_CHANGED",
                     lambda: self.resume(scheduler, ["SECURITY_HOLD", "RECOVERY_REQUIRED"], request="req-2"),
                     request="req-2")
        self.refused(scheduler, "E_STOP_REASON_CHANGED",
                     lambda: self.resume(scheduler, ["SECURITY_HOLD", "SECURITY_HOLD", "RECOVERY_REQUIRED",
                                                     "SECURITY_HOLD"], request="req-2"),
                     request="req-2")
        self.resume(scheduler, ["SECURITY_HOLD", "SECURITY_HOLD", "RECOVERY_REQUIRED"], generation=2,
                    request="req-2")
        self.assertEqual(scheduler.stop_reasons("req-2"), ())

    def test_a_repeated_hold_is_read_back_after_a_reopen(self):
        path = str(self.work / "ledger.db")
        ledger = self.scheduler(create_ledger(path))
        op = self.unknown(ledger, supervised=True)
        ledger.stop("req-1", "OPERATOR_STOP")
        ledger.stop("req-1", "OPERATOR_STOP")
        ledger.rollover()
        ledger.close()
        with open_ledger(path) as reopened:
            self.assertEqual(reopened.stop_reasons("req-1"),
                             ("START_UNKNOWN", "OPERATOR_STOP", "OPERATOR_STOP", "RECOVERY_REQUIRED"))
            # The repeated holds survived the replay: naming one of them is not
            # enough, and naming the rollover twice is not enough either.
            self.refused(reopened, "E_STOP_REASON_CHANGED",
                         lambda: self.resume(reopened, ["START_UNKNOWN", "OPERATOR_STOP",
                                                        "RECOVERY_REQUIRED"], generation=2))
            self.refused(reopened, "E_STOP_REASON_CHANGED",
                         lambda: self.resume(reopened, ["START_UNKNOWN", "OPERATOR_STOP", "OPERATOR_STOP",
                                                        "RECOVERY_REQUIRED", "RECOVERY_REQUIRED"],
                                            generation=2))
            self.resolve(reopened, op, "ABANDONED", generation=2)
            reopened.settle(op, {"tokens": 0})
            self.resume(reopened, ["START_UNKNOWN", "OPERATOR_STOP", "OPERATOR_STOP", "RECOVERY_REQUIRED"],
                        generation=2)
        with open_ledger(path) as reopened:
            self.assertEqual(reopened.stop_reasons("req-1"), ())


class ExpectedReasonsShapeCase(ExplicitCase):
    """The shape of ``expected_reasons`` now that a reason may be named twice."""

    BAD = {
        "a tuple": ("SECURITY_HOLD", "SECURITY_HOLD"),
        "a single text": "SECURITY_HOLD",
        "nothing": None,
        "an empty list": [],
        "an empty reason among them": ["SECURITY_HOLD", ""],
        "an empty reason after the repeat": ["SECURITY_HOLD", "SECURITY_HOLD", ""],
        "a reason that is not text": ["SECURITY_HOLD", None],
        "a reason that is not exact text": ["SECURITY_HOLD", Key("SECURITY_HOLD")],
    }

    def test_a_repeated_reason_is_a_well_formed_expectation(self):
        for label, reasons in self.BAD.items():
            with self.subTest(case=label):
                scheduler = self.scheduler()
                scheduler.stop("req-1", "SECURITY_HOLD")
                scheduler.stop("req-1", "SECURITY_HOLD")
                before = self.state_of(scheduler)
                self.refused(scheduler, "E_STOP_REASON", lambda: self.resume(scheduler, reasons))
                self.assertEqual(self.state_of(scheduler), before)
                # Legal baseline: the same call naming both holds.
                self.resume(scheduler, ["SECURITY_HOLD", "SECURITY_HOLD"])
                self.assertEqual(scheduler.stop_reasons("req-1"), ())

    def test_the_repeat_is_compared_like_any_other_reason(self):
        scheduler = self.scheduler()
        scheduler.stop("req-1", "SECURITY_HOLD")
        scheduler.stop("req-1", "OPERATOR_STOP")
        scheduler.stop("req-1", "SECURITY_HOLD")
        self.assertEqual(scheduler.stop_reasons("req-1"), ("SECURITY_HOLD", "OPERATOR_STOP", "SECURITY_HOLD"))
        for label, reasons in (("the repeat dropped", ["SECURITY_HOLD", "OPERATOR_STOP"]),
                               ("the repeat moved", ["SECURITY_HOLD", "SECURITY_HOLD", "OPERATOR_STOP"]),
                               ("the repeat one too many",
                                ["SECURITY_HOLD", "OPERATOR_STOP", "SECURITY_HOLD", "SECURITY_HOLD"]),
                               ("the repeat instead of another reason",
                                ["SECURITY_HOLD", "OPERATOR_STOP", "OPERATOR_STOP"])):
            with self.subTest(case=label):
                self.refused(scheduler, "E_STOP_REASON_CHANGED", lambda: self.resume(scheduler, reasons))
        self.resume(scheduler, ["SECURITY_HOLD", "OPERATOR_STOP", "SECURITY_HOLD"])
        self.assertEqual(scheduler.stop_reasons("req-1"), ())

    def test_the_shape_is_checked_on_a_copy_before_anything_else(self):
        scheduler = self.scheduler()
        scheduler.stop("req-1", "SECURITY_HOLD")
        scheduler.stop("req-1", "SECURITY_HOLD")
        reasons = ["SECURITY_HOLD", "SECURITY_HOLD"]
        self.resume(scheduler, reasons)
        self.assertEqual(scheduler.stop_reasons("req-1"), ())
        # The comparison used the copy: what the caller does to its own list
        # afterwards cannot change what was recorded.
        self.assertEqual(scheduler.events[-1]["payload"]["previous_reasons"],
                         ["SECURITY_HOLD", "SECURITY_HOLD"])
        reasons.append("LATER")
        reasons[0] = ""
        self.assertEqual(scheduler.stop_reasons("req-1"), (), "the caller's list was kept")
        # The shape of the list is still checked before the generation and the
        # request: the repeated reason makes no difference to the order.
        scheduler.stop("req-1", "SECURITY_HOLD")
        self.refused(scheduler, "E_NOTE", lambda: self.resume(scheduler, None, note=""))
        self.refused(scheduler, "E_STOP_REASON", lambda: self.resume(scheduler, None, generation=0))
        self.refused(scheduler, "E_FENCED", lambda: self.resume(scheduler, ["X"], generation=0))
        self.refused(scheduler, "E_STOP_REASON_CHANGED", lambda: self.resume(scheduler, ["X"]))


class SupervisedRunCase(ExplicitCase):
    """A handoff a supervised run was given is not one that never started."""

    def test_a_supervised_run_cannot_be_resolved_not_started(self):
        for label, supervised, running in (("a run descriptor and a start the ledger saw", True, True),
                                           ("a run descriptor only", True, False),
                                           ("a start the ledger saw only", False, True)):
            with self.subTest(case=label):
                scheduler = self.scheduler()
                op = self.unknown(scheduler, supervised=supervised, running=running)
                self.assertEqual(scheduler.run_descriptor(op) is not None, supervised)
                self.assertEqual(scheduler.operation(op)["state"], "START_UNKNOWN")
                self.refused(scheduler, "E_RESOLVE_CONFLICT", lambda: self.resolve(scheduler, op, "NOT_STARTED"))
                # The legal baseline: the very same operation abandoned.
                self.resolve(scheduler, op, "ABANDONED")
                self.assertEqual(scheduler.operation(op)["state"], "ABANDONED")

    def test_a_bare_handoff_is_still_decided_from_the_records(self):
        scheduler = self.scheduler()
        op = self.unknown(scheduler, supervised=False, running=False)
        self.assertIsNone(scheduler.run_descriptor(op))
        # The legal baseline: nothing says this run ever started, so the records
        # decide it.
        self.resolve(scheduler, op, "NOT_STARTED")
        self.assertEqual(scheduler.operation(op)["state"], "NOT_STARTED")

    def test_the_conflict_comes_after_the_state_and_the_generation(self):
        scheduler = self.scheduler()
        handed_off = self.claim(scheduler, "task-2")
        scheduler.handoff(handed_off, "owner-1", 1, run=self.descriptor(handed_off))
        op = self.unknown(scheduler, supervised=True, running=True)
        self.refused(scheduler, "E_RESOLVE_STATE",
                     lambda: self.resolve(scheduler, handed_off, "NOT_STARTED"))
        self.refused(scheduler, "E_FENCED",
                     lambda: self.resolve(scheduler, op, "NOT_STARTED", generation=0))
        self.refused(scheduler, "E_RESOLVE_CONFLICT", lambda: self.resolve(scheduler, op, "NOT_STARTED"))
        # A finished operation is untouched by the conflict, and so is a
        # resolution that carries a candidate.
        other = self.scheduler()
        finished = self.unknown(other, supervised=True, running=True)
        self.resolve(other, finished, "FINISHED")
        self.assertEqual(other.operation(finished)["state"], "REVIEW_PENDING")
        self.refused(other, "E_RESOLVE_STATE",
                     lambda: self.resolve(other, finished, "NOT_STARTED"))

    def test_a_taken_over_supervised_run_still_cannot_be_resolved_not_started(self):
        scheduler = self.scheduler()
        op = self.unknown(scheduler, supervised=True, running=False)
        scheduler.rollover()
        # The restart recovery takes the operation over from the records; the
        # descriptor survives the takeover.
        scheduler.recover(op, "owner-2", 2, outcome="START_UNKNOWN", evidence_hash=EVIDENCE)
        self.assertEqual(scheduler.operation(op)["state"], "START_UNKNOWN")
        self.assertEqual(scheduler.operation(op)["generation"], 2)
        self.assertIsNotNone(scheduler.run_descriptor(op), "the takeover lost the run descriptor")
        self.refused(scheduler, "E_RESOLVE_CONFLICT",
                     lambda: self.resolve(scheduler, op, "NOT_STARTED", generation=2))
        self.resolve(scheduler, op, "ABANDONED", generation=2)
        self.assertEqual(scheduler.operation(op)["state"], "ABANDONED")

    def test_an_abandoned_supervised_run_resumes_once_its_usage_is_named(self):
        scheduler = self.scheduler()
        op = self.unknown(scheduler, supervised=True, running=True)
        self.resolve(scheduler, op, "ABANDONED")
        self.assertEqual(scheduler.stop_reasons("req-1"), ("START_UNKNOWN",))
        # Zero has to be said: the run's usage is unknown until it is recorded.
        self.refused(scheduler, "E_RESUME_USAGE_UNKNOWN", lambda: self.resume(scheduler, ["START_UNKNOWN"]))
        scheduler.settle(op, {"tokens": 0})
        self.resume(scheduler, ["START_UNKNOWN"])
        self.assertEqual(scheduler.stop_reasons("req-1"), ())
        tokens = scheduler.snapshot("req-1")["tokens"]
        self.assertEqual((tokens["known"], tokens["held"], tokens["unknown_operations"]), (0, 0, 0))

    def test_recover_still_decides_a_supervised_handoff_not_started(self):
        scheduler = self.scheduler()
        op = self.claim(scheduler, "task-1")
        scheduler.handoff(op, "owner-1", 1, run=self.descriptor(op))
        scheduler.rollover()
        # An undecided supervised handoff is taken over from its records, and the
        # records say this run never started.
        scheduler.recover(op, "owner-2", 2, outcome="NOT_STARTED", evidence_hash=EVIDENCE)
        self.assertEqual(scheduler.operation(op)["state"], "NOT_STARTED")
        self.assertEqual(scheduler.operation(op)["generation"], 2)
        # A supervised run already decided START_UNKNOWN is not: that would be an
        # operator overturning the decision (review of attempt-001, M).
        decided = self.scheduler()
        unknown = self.unknown(decided, supervised=True, running=False)
        decided.rollover()
        self.refused(decided, "E_RECOVERY_CONFLICT",
                     lambda: decided.recover(unknown, "owner-2", 2, outcome="NOT_STARTED", evidence_hash=EVIDENCE))
        # The difference from the case above is the start the ledger saw: the
        # very same takeover refuses it, exactly as it did before.
        other = self.scheduler()
        started = self.unknown(other, supervised=True, running=True)
        other.rollover()
        self.refused(other, "E_RECOVERY_CONFLICT",
                     lambda: other.recover(started, "owner-2", 2, outcome="NOT_STARTED",
                                           evidence_hash=EVIDENCE))


if __name__ == "__main__":
    unittest.main()
