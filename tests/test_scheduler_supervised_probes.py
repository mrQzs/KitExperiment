"""Planner probes for scheduler-supervised-transitions v2 (review of attempt-001).

Planner-provided; executors must not modify this file.  The review found that
str subclasses (including ``StrEnum``) passed the new methods' ``isinstance``
checks, changed the ledger and only then failed in ``_emit``; that ``recover``
stored a float generation; and that several rules had no test.  Every
rejection below must name its contract code and leave the ledger unchanged.
"""
from __future__ import annotations

import unittest
try:
    from enum import StrEnum
except ImportError:  # Python 3.10: a (str, Enum) member is a str subclass all the same
    from enum import Enum

    class StrEnum(str, Enum):
        pass

from sentinelmesh.scheduler import ContractError, Scheduler

H1, H2 = "1" * 64, "2" * 64
IDENTITY = {"pid": 4242, "boot_id": "12345678-1234-1234-1234-1234567890AB", "executable_sha256": "a" * 64,
            "worktree_id": "wt-1"}


class Text(str):
    pass


class Reason(StrEnum):
    CLEANUP = "CLEANUP_UNCONFIRMED"
    NOT_STARTED = "NOT_STARTED"


class IdentityDict(dict):
    pass


class SchedulerSupervisedProbes(unittest.TestCase):
    def setUp(self):
        self.scheduler = Scheduler()
        self.scheduler.seed_request("req-1", "repo-1", {"dispatches": 5, "tokens": 100}, concurrency=4)
        self.count = 0

    def handed_off(self, running=False, finished=False):
        self.count += 1
        task = "task-%d" % self.count  # one active attempt per task
        self.scheduler.seed_task("req-1", task, {"dispatches": 5, "tokens": 100}, max_attempts=3)
        op = self.scheduler.claim("req-1", task, 1, "idem-%d" % self.count, "owner-1", 1, 1,
                                  {"dispatches": 1, "tokens": 40}, ["src/%d.py" % self.count])["operation_id"]
        self.scheduler.handoff(op, "owner-1", 1)
        if running or finished:
            self.scheduler.mark_running(op, "owner-1", 1)
        if finished:
            self.scheduler.finish(op, "owner-1", 1, H2, True)
        return op

    def ledger(self):
        return (self.scheduler.events,
                {op: (o.state, o.owner, o.generation, o.candidate_hash) for op, o in self.scheduler._operations.items()},
                dict(self.scheduler._resource_holds), {r: q.stopped for r, q in self.scheduler._requests.items()})

    def assert_refused(self, code, func, *args, **kwargs):
        before = self.ledger()
        with self.assertRaises(ContractError) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(self.ledger(), before, "a rejected call changed the ledger")

    def test_str_subclasses_are_refused_before_anything_changes(self):
        op = self.handed_off()
        release = self.scheduler.release_proven_unstarted
        self.assert_refused("E_EVIDENCE_HASH", release, op, "owner-1", 1, evidence_hash=Text(H1))
        self.assert_refused("E_UNKNOWN_REASON", self.scheduler.unknown, op, "owner-1", 1, reason=Text("START_UNKNOWN"))
        self.assert_refused("E_UNKNOWN_REASON", self.scheduler.unknown, op, "owner-1", 1, reason=Reason.CLEANUP)
        keys = {Text(key): value for key, value in IDENTITY.items()}
        self.assert_refused("E_IDENTITY_SHAPE", self.scheduler.mark_running, op, "owner-1", 1, process_identity=keys)
        self.assert_refused("E_IDENTITY_SHAPE", self.scheduler.mark_running, op, "owner-1", 1,
                            process_identity=IdentityDict(IDENTITY))
        self.scheduler.rollover()
        recover = self.scheduler.recover
        self.assert_refused("E_RECOVERY_OUTCOME", recover, op, "owner-2", 2, outcome=Reason.NOT_STARTED, evidence_hash=H1)
        self.assert_refused("E_RECOVERY_OUTCOME", recover, op, "owner-2", 2, outcome=Text("START_UNKNOWN"),
                            evidence_hash=H1)
        self.assert_refused("E_EVIDENCE_HASH", recover, op, "owner-2", 2, outcome="START_UNKNOWN", evidence_hash=Text(H1))
        self.assert_refused("E_CLEANUP_OR_CANDIDATE", recover, op, "owner-2", 2, outcome="FINISHED", evidence_hash=H1,
                            candidate_hash=Text(H2))

    def test_recover_requires_an_exact_int_generation(self):
        op = self.handed_off()
        self.scheduler.rollover()
        for generation in (2.0, True):
            with self.subTest(generation=generation):
                self.assert_refused("E_FENCED", self.scheduler.recover, op, "owner-2", generation,
                                    outcome="START_UNKNOWN", evidence_hash=H1)

    def test_usage_recorded_after_not_started_counts_as_known(self):
        op = self.handed_off()
        self.scheduler.release_proven_unstarted(op, "owner-1", 1, evidence_hash=H1)
        self.assertEqual(self.scheduler.settle(op, {"tokens": 5}, revision=1, final=True), {"tokens": "APPLIED"})
        tokens = self.scheduler.snapshot("req-1")["tokens"]
        self.assertEqual((tokens["known"], tokens["held"], tokens["unknown_operations"]), (5, 0, 0))
        self.assertIsNone(self.scheduler._requests["req-1"].stopped)

    def test_a_second_release_is_refused(self):
        op = self.handed_off()
        self.scheduler.release_proven_unstarted(op, "owner-1", 1, evidence_hash=H1)
        self.assert_refused("E_NOT_STARTED_STATE", self.scheduler.release_proven_unstarted, op, "owner-1", 1,
                            evidence_hash=H1)

    def test_an_invalid_identity_is_reported_before_the_state(self):
        op = self.handed_off(running=True)
        self.assert_refused("E_IDENTITY_SHAPE", self.scheduler.mark_running, op, "owner-1", 1,
                            process_identity={"pid": 0})

    def test_recover_success_paths_append_exactly_one_event_and_keep_the_stop(self):
        handed = self.handed_off()
        running = self.handed_off(running=True)
        self.scheduler.rollover()
        stopped = self.scheduler._requests["req-1"].stopped
        for op, outcome, kwargs, state in ((handed, "FINISHED", {"candidate_hash": H2}, "REVIEW_PENDING"),
                                           (running, "START_UNKNOWN", {}, "START_UNKNOWN")):
            with self.subTest(outcome=outcome):
                before = len(self.scheduler.events)
                self.scheduler.recover(op, "owner-2", 2, outcome=outcome, evidence_hash=H1, **kwargs)
                self.assertEqual(len(self.scheduler.events), before + 1)
                self.assertEqual(self.scheduler.events[-1]["type"], "scheduler.recovered")
                self.assertEqual(self.scheduler._operations[op].state, state)
                self.assertEqual(self.scheduler._requests["req-1"].stopped, stopped)

    def test_recover_checks_run_in_the_contract_order(self):
        running = self.handed_off(running=True)
        finished = self.handed_off(finished=True)
        self.scheduler.rollover()
        recover = self.scheduler.recover
        # Steps 1-3 come before the generation (step 4), and each other in order.
        self.assert_refused("E_RECOVERY_OUTCOME", recover, running, "owner-2", 1, outcome="DONE", evidence_hash="x")
        self.assert_refused("E_EVIDENCE_HASH", recover, running, "owner-2", 1, outcome="FINISHED", evidence_hash="x")
        self.assert_refused("E_CLEANUP_OR_CANDIDATE", recover, running, "owner-2", 1, outcome="FINISHED",
                            evidence_hash=H1)
        self.assert_refused("E_FENCED", recover, "op-missing", "owner-2", 1, outcome="NOT_STARTED", evidence_hash=H1)
        # With the request no longer stopped: state (7) before stop (8), stop (8) before conflict (9).
        self.scheduler._requests["req-1"].stopped = None
        self.assert_refused("E_RECOVERY_STATE", recover, finished, "owner-2", 2, outcome="START_UNKNOWN",
                            evidence_hash=H1)
        self.assert_refused("E_RECOVERY_NOT_STOPPED", recover, running, "owner-2", 2, outcome="NOT_STARTED",
                            evidence_hash=H1)


if __name__ == "__main__":
    unittest.main()
