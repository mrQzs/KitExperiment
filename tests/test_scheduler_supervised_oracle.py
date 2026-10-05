"""Acceptance oracle for the Scheduler transitions the supervisor needs.

Planner-provided; executors must not modify this file.  It checks the user's
decisions recorded in `.agent/design/scheduler-supervisor-wiring.md` (sections
5 and 6) on the in-memory Scheduler:

* a handoff proven not started reaches NOT_STARTED: resources and reservations
  are released, the dispatch stays counted and so does the attempt;
* an unconfirmed cleanup is START_UNKNOWN with its reason recorded;
* the start event can carry the process identity;
* after a controller rollover the new generation recovers in-flight operations
  from evidence, and every rejected input leaves the ledger unchanged.
"""
from __future__ import annotations

import unittest

from sentinelmesh.scheduler import ContractError, Scheduler

H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64
IDENTITY = {"pid": 4242, "boot_id": "12345678-1234-1234-1234-1234567890AB", "executable_sha256": "a" * 64,
            "worktree_id": "wt-1"}


class SchedulerSupervisedOracle(unittest.TestCase):
    def setUp(self):
        self.scheduler = Scheduler()
        self.scheduler.seed_request("req-1", "repo-1", {"dispatches": 5, "tokens": 100}, concurrency=4)
        self.scheduler.seed_task("req-1", "task-1", {"dispatches": 5, "tokens": 100}, max_attempts=1)
        self.scheduler.seed_task("req-1", "task-2", {"dispatches": 5, "tokens": 100}, max_attempts=2)

    def claim(self, task="task-1", key="idem-1", owner="owner-1", generation=1, resources=("src/a.py",)):
        result = self.scheduler.claim("req-1", task, 1, key, owner, generation, 1, {"dispatches": 1, "tokens": 40},
                                      list(resources))
        return result["operation_id"]

    def handed_off(self, **kwargs):
        op = self.claim(**kwargs)
        self.scheduler.handoff(op, kwargs.get("owner", "owner-1"), kwargs.get("generation", 1))
        return op

    def state_of(self, op):
        return self.scheduler._operations[op].state

    def code_of(self, func, *args, **kwargs):
        with self.assertRaises(ContractError) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def last_event(self, event_type):
        return [event for event in self.scheduler.events if event["type"] == event_type][-1]

    def assert_unchanged(self, func, code, *args, **kwargs):
        before = (self.scheduler.events, {op: (o.state, o.owner, o.generation, o.candidate_hash)
                                          for op, o in self.scheduler._operations.items()},
                  dict(self.scheduler._resource_holds), {r: q.stopped for r, q in self.scheduler._requests.items()})
        self.assertEqual(self.code_of(func, *args, **kwargs), code)
        after = (self.scheduler.events, {op: (o.state, o.owner, o.generation, o.candidate_hash)
                                         for op, o in self.scheduler._operations.items()},
                 dict(self.scheduler._resource_holds), {r: q.stopped for r, q in self.scheduler._requests.items()})
        self.assertEqual(after, before, "a rejected call changed the ledger")

    # proven not started -----------------------------------------------------

    def test_proven_unstarted_releases_holds_and_keeps_the_dispatch_and_the_attempt(self):
        op = self.handed_off()
        self.scheduler.release_proven_unstarted(op, "owner-1", 1, evidence_hash=H1)
        self.assertEqual(self.state_of(op), "NOT_STARTED")
        self.assertEqual(self.last_event("scheduler.not_started")["payload"], {"evidence": H1})
        snapshot = self.scheduler.snapshot("req-1")
        self.assertEqual(snapshot["dispatches"], {"limit": 5, "known": 1, "held": 0, "unknown_operations": 0,
                                                  "exposure": 1})
        self.assertEqual(snapshot["tokens"], {"limit": 100, "known": 0, "held": 0, "unknown_operations": 0,
                                              "exposure": 0})
        self.assertIsNone(self.scheduler._requests["req-1"].stopped)
        # The resource is free again, and the attempt still counts (max_attempts=1).
        other = self.claim(task="task-2", key="idem-2")
        self.assertEqual(self.state_of(other), "RESERVED")
        self.assertEqual(self.code_of(self.claim, key="idem-3", resources=("src/b.py",)), "E_ATTEMPTS")

    def test_proven_unstarted_rejections_change_nothing(self):
        reserved = self.claim(task="task-2", key="idem-r", resources=("src/r.py",))
        self.assert_unchanged(self.scheduler.release_proven_unstarted, "E_NOT_STARTED_STATE", reserved, "owner-1", 1,
                              evidence_hash=H1)
        op = self.handed_off()
        for bad in ("A" * 64, "1" * 63, None, 7):
            with self.subTest(evidence=bad):
                self.assert_unchanged(self.scheduler.release_proven_unstarted, "E_EVIDENCE_HASH", op, "owner-1", 1,
                                      evidence_hash=bad)
        self.assert_unchanged(self.scheduler.release_proven_unstarted, "E_FENCED", op, "owner-2", 1, evidence_hash=H1)
        self.scheduler.mark_running(op, "owner-1", 1)
        self.assert_unchanged(self.scheduler.release_proven_unstarted, "E_NOT_STARTED_STATE", op, "owner-1", 1,
                              evidence_hash=H1)

    # start event and unknown reason ----------------------------------------

    def test_running_can_record_the_process_identity(self):
        op = self.handed_off()
        self.scheduler.mark_running(op, "owner-1", 1, process_identity=dict(IDENTITY))
        self.assertEqual(self.state_of(op), "RUNNING")
        self.assertEqual(self.last_event("run.started")["payload"], {"process_identity": IDENTITY})
        plain = self.handed_off(task="task-2", key="idem-2", resources=("src/b.py",))
        self.scheduler.mark_running(plain, "owner-1", 1)
        self.assertEqual(self.last_event("run.started")["payload"], {})

    def test_an_invalid_identity_leaves_the_operation_handed_off(self):
        op = self.handed_off()
        bad_identities = [dict(IDENTITY, pid=0), dict(IDENTITY, executable_sha256="z" * 64),
                          {k: v for k, v in IDENTITY.items() if k != "worktree_id"}, dict(IDENTITY, extra=1), "id"]
        for bad in bad_identities:
            with self.subTest(identity=bad):
                self.assert_unchanged(self.scheduler.mark_running, "E_IDENTITY_SHAPE", op, "owner-1", 1,
                                      process_identity=bad)
        self.assertEqual(self.state_of(op), "HANDOFF_ACCEPTED")

    def test_an_unconfirmed_cleanup_is_start_unknown_with_its_reason(self):
        op = self.handed_off()
        self.scheduler.mark_running(op, "owner-1", 1)
        self.assert_unchanged(self.scheduler.unknown, "E_UNKNOWN_REASON", op, "owner-1", 1, reason="DONE")
        self.scheduler.unknown(op, "owner-1", 1, reason="CLEANUP_UNCONFIRMED")
        self.assertEqual(self.state_of(op), "START_UNKNOWN")
        self.assertEqual(self.last_event("scheduler.start_unknown")["payload"], {"reason": "CLEANUP_UNCONFIRMED"})
        self.assertTrue(self.scheduler._requests["req-1"].stopped)
        # Resources and the token reservation stay held.
        self.assertEqual(self.scheduler.snapshot("req-1")["tokens"]["held"], 40)
        self.assertIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        # The request is stopped: no new claim.
        self.assertEqual(self.code_of(self.claim, task="task-2", key="idem-2", resources=("src/z.py",)), "E_STOPPED")

    def test_the_default_unknown_reason_is_start_unknown(self):
        op = self.handed_off()
        self.scheduler.unknown(op, "owner-1", 1)
        self.assertEqual(self.last_event("scheduler.start_unknown")["payload"], {"reason": "START_UNKNOWN"})

    # recovery after a rollover ---------------------------------------------

    def test_recovery_after_a_rollover_follows_the_evidence(self):
        started_unknown = self.handed_off(task="task-1", key="idem-1", resources=("src/a.py",))
        not_started = self.handed_off(task="task-2", key="idem-2", resources=("src/b.py",))
        generation = self.scheduler.rollover()
        self.assertEqual(generation, 2)
        self.scheduler.recover(started_unknown, "owner-2", 2, outcome="START_UNKNOWN", evidence_hash=H1)
        self.scheduler.recover(not_started, "owner-2", 2, outcome="NOT_STARTED", evidence_hash=H2)
        ops = self.scheduler._operations
        self.assertEqual((ops[started_unknown].state, ops[started_unknown].owner, ops[started_unknown].generation),
                         ("START_UNKNOWN", "owner-2", 2))
        self.assertEqual((ops[not_started].state, ops[not_started].owner, ops[not_started].generation),
                         ("NOT_STARTED", "owner-2", 2))
        self.assertIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        self.assertNotIn(("repo-1", "src/b.py"), self.scheduler._resource_holds)
        self.assertTrue(self.scheduler._requests["req-1"].stopped)
        events = [event for event in self.scheduler.events if event["type"] == "scheduler.recovered"]
        self.assertEqual([event["payload"] for event in events], [
            {"from_generation": 1, "outcome": "START_UNKNOWN", "evidence": H1, "state": "START_UNKNOWN"},
            {"from_generation": 1, "outcome": "NOT_STARTED", "evidence": H2, "state": "NOT_STARTED"}])

    def test_a_finished_run_recovers_into_review(self):
        op = self.handed_off()
        self.scheduler.mark_running(op, "owner-1", 1)
        self.scheduler.rollover()
        self.scheduler.recover(op, "owner-2", 2, outcome="FINISHED", evidence_hash=H1, candidate_hash=H2)
        self.assertEqual(self.state_of(op), "REVIEW_PENDING")
        self.assertNotIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        self.assertEqual(self.last_event("scheduler.recovered")["payload"],
                         {"from_generation": 1, "outcome": "FINISHED", "evidence": H1, "state": "REVIEW_PENDING",
                          "candidate": H2})
        self.scheduler.fixture_verdict(op, H2, True, H3)
        self.assertEqual(self.state_of(op), "ACCEPTED")

    def test_recovery_rejections_change_nothing(self):
        running = self.handed_off()
        self.scheduler.mark_running(running, "owner-1", 1)
        reserved = self.claim(task="task-2", key="idem-2", resources=("src/b.py",))
        # Not fenced yet: the current generation uses the normal paths.
        self.assert_unchanged(self.scheduler.recover, "E_RECOVERY_NOT_FENCED", running, "owner-1", 1,
                              outcome="START_UNKNOWN", evidence_hash=H1)
        self.scheduler.rollover()
        cases = [
            ("E_FENCED", (running, "owner-2", 1), {"outcome": "START_UNKNOWN", "evidence_hash": H1}),
            ("E_RECOVERY_CONFLICT", (running, "owner-2", 2), {"outcome": "NOT_STARTED", "evidence_hash": H1}),
            ("E_RECOVERY_STATE", (reserved, "owner-2", 2), {"outcome": "NOT_STARTED", "evidence_hash": H1}),
            ("E_RECOVERY_OUTCOME", (running, "owner-2", 2), {"outcome": "DONE", "evidence_hash": H1}),
            ("E_EVIDENCE_HASH", (running, "owner-2", 2), {"outcome": "START_UNKNOWN", "evidence_hash": "x"}),
            ("E_CLEANUP_OR_CANDIDATE", (running, "owner-2", 2), {"outcome": "FINISHED", "evidence_hash": H1}),
            ("E_CLEANUP_OR_CANDIDATE", (running, "owner-2", 2),
             {"outcome": "START_UNKNOWN", "evidence_hash": H1, "candidate_hash": H2}),
            ("E_OPERATION_UNKNOWN", ("op-missing", "owner-2", 2), {"outcome": "START_UNKNOWN", "evidence_hash": H1}),
        ]
        for code, args, kwargs in cases:
            with self.subTest(code=code, kwargs=kwargs):
                self.assert_unchanged(self.scheduler.recover, code, *args, **kwargs)
        # A RESERVED operation of the old generation is still cancelled the usual way.
        self.scheduler.cancel_unstarted(reserved, 2)
        self.assertEqual(self.state_of(reserved), "CANCELLED_NOT_STARTED")

    def test_recovered_not_started_counts_like_a_proven_unstarted(self):
        op = self.handed_off()
        self.scheduler.rollover()
        self.scheduler.recover(op, "owner-2", 2, outcome="NOT_STARTED", evidence_hash=H1)
        snapshot = self.scheduler.snapshot("req-1", "task-1")
        self.assertEqual((snapshot["dispatches"]["known"], snapshot["dispatches"]["held"]), (1, 0))
        self.assertEqual((snapshot["tokens"]["known"], snapshot["tokens"]["held"]), (0, 0))
        self.assertEqual(snapshot["tokens"]["unknown_operations"], 0)


if __name__ == "__main__":
    unittest.main()
