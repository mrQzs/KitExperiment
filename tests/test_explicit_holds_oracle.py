"""Planner oracle for explicit-holds (`.agent/design/request-resume.md`, section 10).

Planner-provided; executors must not modify this file.

The review of resume-holds attempt-001 found two gaps in the user's decisions
of section 9, and the user decided (2026-10-09):

* an operation handed to a supervised run (a run descriptor recorded at its
  handoff) can never be resolved ``NOT_STARTED`` by an operator -- the bridge
  registers the start only after the run, so a controller killed mid-run left
  a run the ledger never saw start; what can be proven unstarted, the bridge
  and the restart recovery already decide from the records;
* every explicit ``stop`` is its own hold: the same reason twice is named twice
  in ``resume``, while the automatic reasons (the budget guard, a rollover, an
  unknown start) stay once.

Planner decisions P13-P15: the resolve conflict covers operations with a run
descriptor (``recover`` is unchanged), explicit stops always append, and
``expected_reasons`` may repeat a reason.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from sentinelmesh.durable_ledger import create_ledger, open_ledger
from sentinelmesh.scheduler import ContractError, Scheduler

CAPS = {"dispatches": 9, "tokens": 500}
RESERVATION = {"dispatches": 1, "tokens": 40}
EVIDENCE, CANDIDATE = "e" * 64, "d" * 64
OPERATOR, NOTE = "ops-1", "checked the process table and the worktree"
ENV_DIGEST = "a" * 64


class ExplicitCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="explicit-holds-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)

    def scheduler(self, ledger=None):
        scheduler = ledger or Scheduler()
        scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        for task in ("task-1", "task-2", "task-3"):
            scheduler.seed_task("req-1", task, CAPS)
        return scheduler

    def claim(self, scheduler, task="task-1"):
        return scheduler.claim("req-1", task, 1, "idem-" + task, "owner-1", 1, 1, RESERVATION,
                               ["src/%s.py" % task])["operation_id"]

    def descriptor(self, op):
        return {"spec": {"request_id": "req-1", "run_id": op, "command": ["/usr/bin/true"], "cwd": str(self.work),
                         "environment_sha256": ENV_DIGEST, "timeout_seconds": 30},
                "evidence_dir": str(self.work), "worktree_id": "wt-1"}

    def unknown(self, scheduler, task="task-1", *, supervised):
        """A START_UNKNOWN handoff whose start the ledger never saw."""
        op = self.claim(scheduler, task)
        scheduler.handoff(op, "owner-1", 1, run=self.descriptor(op) if supervised else None)
        scheduler.unknown(op, "owner-1", 1)
        return op

    def resolve(self, scheduler, op, resolution, *, generation=1):
        return scheduler.resolve_unknown(op, generation, resolution=resolution, operator=OPERATOR,
                                         evidence_hash=EVIDENCE, note=NOTE,
                                         candidate_hash=CANDIDATE if resolution == "FINISHED" else None)

    def resume(self, scheduler, reasons, *, generation=1):
        return scheduler.resume("req-1", generation, expected_reasons=reasons, operator=OPERATOR, note=NOTE)

    def refused(self, scheduler, code, call, *, op=None):
        before = (scheduler.events, scheduler.stop_reasons("req-1"), scheduler.snapshot("req-1"),
                  None if op is None else scheduler.operation(op))
        with self.assertRaises(ContractError) as ctx:
            call()
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual((scheduler.events, scheduler.stop_reasons("req-1"), scheduler.snapshot("req-1"),
                          None if op is None else scheduler.operation(op)), before,
                         "a refused call changed the ledger")


class ExplicitHoldsOracle(ExplicitCase):
    def test_every_explicit_stop_is_its_own_hold(self):
        scheduler = self.scheduler()
        scheduler.stop("req-1", "OPERATOR_STOP")
        scheduler.stop("req-1", "SECURITY_HOLD")
        scheduler.stop("req-1", "OPERATOR_STOP")
        self.assertEqual(scheduler.stop_reasons("req-1"), ("OPERATOR_STOP", "SECURITY_HOLD", "OPERATOR_STOP"))
        self.assertEqual(scheduler._requests["req-1"].stopped, "OPERATOR_STOP")
        for label, reasons in (("each reason once", ["OPERATOR_STOP", "SECURITY_HOLD"]),
                               ("the repeat in another place", ["OPERATOR_STOP", "OPERATOR_STOP", "SECURITY_HOLD"]),
                               ("one hold too many", ["OPERATOR_STOP", "SECURITY_HOLD", "OPERATOR_STOP",
                                                      "OPERATOR_STOP"])):
            with self.subTest(case=label):
                self.refused(scheduler, "E_STOP_REASON_CHANGED", lambda: self.resume(scheduler, reasons))
        self.resume(scheduler, ["OPERATOR_STOP", "SECURITY_HOLD", "OPERATOR_STOP"])
        self.assertEqual(scheduler.stop_reasons("req-1"), ())
        self.assertEqual(scheduler.events[-1]["payload"]["previous_reasons"],
                         ["OPERATOR_STOP", "SECURITY_HOLD", "OPERATOR_STOP"])

    def test_two_operators_with_the_default_reason_are_two_holds(self):
        scheduler = self.scheduler()
        scheduler.stop("req-1")
        scheduler.stop("req-1")
        self.refused(scheduler, "E_STOP_REASON_CHANGED", lambda: self.resume(scheduler, ["OPERATOR_STOP"]))
        self.resume(scheduler, ["OPERATOR_STOP", "OPERATOR_STOP"])
        self.assertIsNone(scheduler._requests["req-1"].stopped)

    def test_automatic_reasons_stay_once(self):
        scheduler = self.scheduler()
        first = self.claim(scheduler, "task-1")
        second = self.claim(scheduler, "task-2")
        for op in (first, second):
            scheduler.handoff(op, "owner-1", 1)
        scheduler.unknown(first, "owner-1", 1)
        scheduler.unknown(second, "owner-1", 1)
        scheduler.rollover()
        scheduler.rollover()
        scheduler.settle(first, {"tokens": 600})
        scheduler.settle(second, {"tokens": 1})
        self.assertEqual(scheduler.stop_reasons("req-1"), ("START_UNKNOWN", "RECOVERY_REQUIRED", "BUDGET_EXCEEDED"))
        # An explicit stop after them is still its own entry, even with a reason
        # an automatic stop already recorded.
        scheduler.stop("req-1", "RECOVERY_REQUIRED")
        self.assertEqual(scheduler.stop_reasons("req-1"),
                         ("START_UNKNOWN", "RECOVERY_REQUIRED", "BUDGET_EXCEEDED", "RECOVERY_REQUIRED"))

    def test_a_repeated_reason_is_a_well_formed_expectation(self):
        scheduler = self.scheduler()
        scheduler.stop("req-1", "SECURITY_HOLD")
        # Well formed, so the comparison is what refuses it.
        self.refused(scheduler, "E_STOP_REASON_CHANGED", lambda: self.resume(scheduler, ["SECURITY_HOLD", "SECURITY_HOLD"]))
        for label, reasons in (("an empty reason among them", ["SECURITY_HOLD", ""]),
                               ("a reason that is not text", ["SECURITY_HOLD", None])):
            with self.subTest(case=label):
                self.refused(scheduler, "E_STOP_REASON", lambda: self.resume(scheduler, reasons))
        self.resume(scheduler, ["SECURITY_HOLD"])


class SupervisedRunOracle(ExplicitCase):
    def test_a_supervised_run_cannot_be_resolved_not_started(self):
        scheduler = self.scheduler()
        op = self.unknown(scheduler, supervised=True)
        self.assertIsNotNone(scheduler.run_descriptor(op))
        self.refused(scheduler, "E_RESOLVE_CONFLICT", lambda: self.resolve(scheduler, op, "NOT_STARTED"), op=op)
        for resolution, state in (("ABANDONED", "ABANDONED"), ("FINISHED", "REVIEW_PENDING")):
            with self.subTest(resolution=resolution):
                other = self.scheduler()
                supervised = self.unknown(other, supervised=True)
                self.resolve(other, supervised, resolution)
                self.assertEqual(other.operation(supervised)["state"], state)
        with self.subTest(case="a handoff nobody described"):
            other = self.scheduler()
            bare = self.unknown(other, supervised=False)
            self.resolve(other, bare, "NOT_STARTED")
            self.assertEqual(other.operation(bare)["state"], "NOT_STARTED")

    def test_an_abandoned_supervised_run_names_its_usage(self):
        scheduler = self.scheduler()
        op = self.unknown(scheduler, supervised=True)
        self.resolve(scheduler, op, "ABANDONED")
        self.refused(scheduler, "E_RESUME_USAGE_UNKNOWN", lambda: self.resume(scheduler, ["START_UNKNOWN"]))
        # Zero has to be said, it is never assumed.
        scheduler.settle(op, {"tokens": 0})
        self.resume(scheduler, ["START_UNKNOWN"])
        tokens = scheduler.snapshot("req-1")["tokens"]
        self.assertEqual((tokens["known"], tokens["held"], tokens["unknown_operations"]), (0, 0, 0))

    def test_the_conflict_comes_after_the_state(self):
        scheduler = self.scheduler()
        handed_off = self.claim(scheduler, "task-2")
        scheduler.handoff(handed_off, "owner-1", 1, run=self.descriptor(handed_off))
        op = self.unknown(scheduler, supervised=True)
        self.refused(scheduler, "E_RESOLVE_STATE", lambda: self.resolve(scheduler, handed_off, "NOT_STARTED"),
                     op=handed_off)
        self.refused(scheduler, "E_FENCED", lambda: self.resolve(scheduler, op, "NOT_STARTED", generation=0), op=op)
        self.refused(scheduler, "E_RESOLVE_CONFLICT", lambda: self.resolve(scheduler, op, "NOT_STARTED"), op=op)

    def test_recover_still_decides_not_started_from_the_records(self):
        scheduler = self.scheduler()
        op = self.claim(scheduler)
        scheduler.handoff(op, "owner-1", 1, run=self.descriptor(op))
        scheduler.rollover()
        scheduler.recover(op, "owner-2", 2, outcome="NOT_STARTED", evidence_hash=EVIDENCE)
        self.assertEqual(scheduler.operation(op)["state"], "NOT_STARTED")


class DurableExplicitOracle(ExplicitCase):
    def test_the_durable_ledger_keeps_every_explicit_hold(self):
        path = str(self.work / "ledger.db")
        ledger = self.scheduler(create_ledger(path))
        op = self.unknown(ledger, supervised=True)
        ledger.stop("req-1")
        ledger.stop("req-1")
        ledger.close()
        with open_ledger(path) as reopened:
            self.assertEqual(reopened.stop_reasons("req-1"), ("START_UNKNOWN", "OPERATOR_STOP", "OPERATOR_STOP"))
            with self.assertRaises(ContractError) as ctx:
                self.resolve(reopened, op, "NOT_STARTED")
            self.assertEqual(ctx.exception.code, "E_RESOLVE_CONFLICT")
            self.resolve(reopened, op, "ABANDONED")
            reopened.settle(op, {"tokens": 0})
            self.resume(reopened, ["START_UNKNOWN", "OPERATOR_STOP", "OPERATOR_STOP"])
        with open_ledger(path) as reopened:
            self.assertEqual(reopened.stop_reasons("req-1"), ())


if __name__ == "__main__":
    unittest.main()
