"""Cases for the supervisor transitions that the planner oracle does not cover.

Every rejection case first proves a legal baseline, then changes exactly one
condition, and asserts the concrete error code together with an unchanged
ledger (events, states/owners/generations/candidate hashes, resource holds and
request stop flags all frozen).
"""
from __future__ import annotations

import unittest

from sentinelmesh.scheduler import ContractError, Scheduler

H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64
IDENTITY = {"pid": 4242, "boot_id": "12345678-1234-1234-1234-1234567890AB", "executable_sha256": "a" * 64,
            "worktree_id": "wt-1"}


class SupervisedTransitionsTest(unittest.TestCase):
    def setUp(self):
        self.scheduler = Scheduler()
        self.scheduler.seed_request("req-1", "repo-1", {"dispatches": 5, "tokens": 100}, concurrency=4)
        self.scheduler.seed_task("req-1", "task-1", {"dispatches": 5, "tokens": 100}, max_attempts=3)
        self.scheduler.seed_task("req-1", "task-2", {"dispatches": 5, "tokens": 100}, max_attempts=3)

    # helpers ---------------------------------------------------------------

    def claim(self, task="task-1", key="idem-1", owner="owner-1", generation=1,
              resources=("src/a.py",)):
        result = self.scheduler.claim("req-1", task, 1, key, owner, generation, 1,
                                      {"dispatches": 1, "tokens": 40}, list(resources))
        return result["operation_id"]

    def handed_off(self, task="task-1", key="idem-1", resources=("src/a.py",)):
        op = self.claim(task=task, key=key, resources=resources)
        self.scheduler.handoff(op, "owner-1", 1)
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
        before = (self.scheduler.events,
                  {op: (o.state, o.owner, o.generation, o.candidate_hash)
                   for op, o in self.scheduler._operations.items()},
                  dict(self.scheduler._resource_holds),
                  {r: q.stopped for r, q in self.scheduler._requests.items()},
                  {op: dict(o.usage) for op, o in self.scheduler._operations.items()})
        self.assertEqual(self.code_of(func, *args, **kwargs), code)
        after = (self.scheduler.events,
                 {op: (o.state, o.owner, o.generation, o.candidate_hash)
                  for op, o in self.scheduler._operations.items()},
                 dict(self.scheduler._resource_holds),
                 {r: q.stopped for r, q in self.scheduler._requests.items()},
                 {op: dict(o.usage) for op, o in self.scheduler._operations.items()})
        self.assertEqual(after, before, "a rejected call changed the ledger")

    # release_proven_unstarted ---------------------------------------------

    def test_release_proven_unstarted_rejects_an_older_generation(self):
        op = self.handed_off()
        self.scheduler.rollover()
        self.assertEqual(self.scheduler.generation, 2)
        self.assert_unchanged(self.scheduler.release_proven_unstarted, "E_FENCED", op, "owner-1", 1,
                              evidence_hash=H1)
        self.assertEqual(self.state_of(op), "HANDOFF_ACCEPTED")
        self.assertIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        # The new generation may not use the release path either: it is not the owner.
        self.assert_unchanged(self.scheduler.release_proven_unstarted, "E_FENCED", op, "owner-2", 2,
                              evidence_hash=H1)

    def test_release_proven_unstarted_rejects_state_conflicts(self):
        running = self.handed_off()
        self.scheduler.mark_running(running, "owner-1", 1)
        self.assert_unchanged(self.scheduler.release_proven_unstarted, "E_NOT_STARTED_STATE", running,
                              "owner-1", 1, evidence_hash=H1)
        other = self.handed_off(task="task-2", key="idem-2", resources=("src/b.py",))
        self.scheduler.unknown(other, "owner-1", 1)
        self.assert_unchanged(self.scheduler.release_proven_unstarted, "E_NOT_STARTED_STATE", other,
                              "owner-1", 1, evidence_hash=H1)
        # Already in the terminal NOT_STARTED state.
        self.assert_unchanged(self.scheduler.release_proven_unstarted, "E_NOT_STARTED_STATE",
                              running, "owner-1", 1, evidence_hash=H1)

    def test_not_started_counts_only_observed_usage(self):
        op = self.handed_off()
        self.scheduler.settle(op, {"tokens": 5}, revision=1, final=True)
        self.scheduler.release_proven_unstarted(op, "owner-1", 1, evidence_hash=H1)
        snapshot = self.scheduler.snapshot("req-1")
        self.assertEqual(snapshot["dispatches"],
                         {"limit": 5, "known": 1, "held": 0, "unknown_operations": 0, "exposure": 1})
        self.assertEqual(snapshot["tokens"],
                         {"limit": 100, "known": 5, "held": 0, "unknown_operations": 0, "exposure": 5})

    def test_not_started_frees_the_concurrency_slot(self):
        self.scheduler.seed_request("req-2", "repo-2", {"dispatches": 2}, concurrency=1)
        self.scheduler.seed_task("req-2", "task-1", {"dispatches": 2}, max_attempts=5)
        self.scheduler.seed_task("req-2", "task-2", {"dispatches": 2}, max_attempts=5)
        first = self.scheduler.claim("req-2", "task-1", 1, "idem-1", "owner-1", 1, 1,
                                      {"dispatches": 1}, [])["operation_id"]
        self.scheduler.handoff(first, "owner-1", 1)
        self.assertEqual(self.code_of(self.scheduler.claim, "req-2", "task-2", 1, "idem-2", "owner-1", 1, 1,
                                      {"dispatches": 1}, []), "E_CONCURRENCY")
        self.scheduler.release_proven_unstarted(first, "owner-1", 1, evidence_hash=H1)
        second = self.scheduler.claim("req-2", "task-2", 1, "idem-2", "owner-1", 1, 1,
                                      {"dispatches": 1}, [])
        self.assertEqual(second["state"], "RESERVED")

    def test_not_started_releases_a_shared_resource_key(self):
        first = self.handed_off(task="task-1", key="idem-1", resources=("src/shared.py",))
        self.assertEqual(self.code_of(self.claim, task="task-2", key="idem-2",
                                      resources=("src/shared.py",)), "E_RESOURCE_BUSY")
        self.scheduler.release_proven_unstarted(first, "owner-1", 1, evidence_hash=H1)
        self.assertEqual(self.state_of(self.claim(task="task-2", key="idem-2",
                                                   resources=("src/shared.py",))), "RESERVED")

    # mark_running / unknown ------------------------------------------------

    def test_mark_running_stores_a_copy_of_the_identity(self):
        op = self.handed_off()
        identity = dict(IDENTITY)
        self.scheduler.mark_running(op, "owner-1", 1, process_identity=identity)
        identity["pid"] = 99
        identity["worktree_id"] = "wt-changed"
        self.assertEqual(self.last_event("run.started")["payload"], {"process_identity": IDENTITY})
        self.assertEqual(self.state_of(op), "RUNNING")

    def test_mark_running_rejects_identity_conflicts_on_state(self):
        op = self.handed_off(task="task-1", key="idem-1", resources=("src/a.py",))
        running = self.handed_off(task="task-2", key="idem-2", resources=("src/b.py",))
        self.scheduler.mark_running(running, "owner-1", 1)
        self.assert_unchanged(self.scheduler.mark_running, "E_START_STATE", running, "owner-1", 1,
                              process_identity=dict(IDENTITY))
        # A wrong owner/generation is fenced even with a well-formed identity.
        self.assert_unchanged(self.scheduler.mark_running, "E_FENCED", op, "owner-2", 1,
                              process_identity=dict(IDENTITY))
        self.assertEqual(self.state_of(op), "HANDOFF_ACCEPTED")

    def test_unknown_rejects_a_none_reason(self):
        op = self.handed_off()
        self.assert_unchanged(self.scheduler.unknown, "E_UNKNOWN_REASON", op, "owner-1", 1, reason=None)
        self.assertEqual(self.state_of(op), "HANDOFF_ACCEPTED")
        self.assertIsNone(self.scheduler._requests["req-1"].stopped)
        for bad in ("start_unknown", "CLEANUP UNKNOWN", "", "CLEANUP_UNCONFIRMED ", 5, True):
            with self.subTest(reason=bad):
                self.assert_unchanged(self.scheduler.unknown, "E_UNKNOWN_REASON", op, "owner-1", 1,
                                      reason=bad)

    def test_unknown_rejects_a_state_conflict_regardless_of_reason(self):
        reserved = self.claim(task="task-2", key="idem-2", resources=("src/b.py",))
        self.assert_unchanged(self.scheduler.unknown, "E_UNKNOWN_STATE", reserved, "owner-1", 1,
                              reason="START_UNKNOWN")
        self.assert_unchanged(self.scheduler.unknown, "E_UNKNOWN_STATE", reserved, "owner-1", 1,
                              reason="CLEANUP_UNCONFIRMED")
        self.assertEqual(self.state_of(reserved), "RESERVED")

    def test_unknown_cleanup_unconfirmed_from_handoff_accepted(self):
        op = self.handed_off()
        self.scheduler.unknown(op, "owner-1", 1, reason="CLEANUP_UNCONFIRMED")
        self.assertEqual(self.state_of(op), "START_UNKNOWN")
        self.assertEqual(self.last_event("scheduler.start_unknown")["payload"],
                         {"reason": "CLEANUP_UNCONFIRMED"})
        # Holds and the reservation stay; the request is stopped.
        self.assertIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        self.assertEqual(self.scheduler.snapshot("req-1")["tokens"]["held"], 40)
        self.assertTrue(self.scheduler._requests["req-1"].stopped)

    # recover ---------------------------------------------------------------

    def test_recover_rejects_a_request_that_is_not_stopped(self):
        op = self.handed_off()
        self.scheduler.rollover()
        # Reconstruct the premise by hand: fenced operation, unstopped request.
        self.scheduler._requests["req-1"].stopped = None
        self.assertEqual(self.scheduler._operations[op].generation, 1)
        self.assert_unchanged(self.scheduler.recover, "E_RECOVERY_NOT_STOPPED", op, "owner-2", 2,
                              outcome="NOT_STARTED", evidence_hash=H1)
        self.assertEqual(self.state_of(op), "HANDOFF_ACCEPTED")
        # With the request stopped again the same call succeeds.
        self.scheduler.stop("req-1", "RECOVERY_REQUIRED")
        self.scheduler.recover(op, "owner-2", 2, outcome="NOT_STARTED", evidence_hash=H1)
        self.assertEqual(self.state_of(op), "NOT_STARTED")

    def test_recover_from_start_unknown_after_rollover(self):
        op = self.handed_off()
        self.scheduler.unknown(op, "owner-1", 1, reason="CLEANUP_UNCONFIRMED")
        self.scheduler.rollover()
        self.scheduler.recover(op, "owner-2", 2, outcome="START_UNKNOWN", evidence_hash=H1)
        self.assertEqual(self.state_of(op), "START_UNKNOWN")
        self.assertEqual(self.scheduler._operations[op].owner, "owner-2")
        self.assertIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        self.assertEqual(self.scheduler.snapshot("req-1")["tokens"]["unknown_operations"], 1)

    def test_recover_start_unknown_to_finished(self):
        op = self.handed_off()
        self.scheduler.unknown(op, "owner-1", 1)
        self.scheduler.rollover()
        self.scheduler.recover(op, "owner-2", 2, outcome="FINISHED", evidence_hash=H1, candidate_hash=H2)
        self.assertEqual(self.state_of(op), "REVIEW_PENDING")
        self.assertEqual(self.scheduler._operations[op].candidate_hash, H2)
        self.assertNotIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        self.assertEqual(self.last_event("scheduler.recovered")["payload"],
                         {"from_generation": 1, "outcome": "FINISHED", "evidence": H1,
                          "state": "REVIEW_PENDING", "candidate": H2})
        self.scheduler.fixture_verdict(op, H2, True, H3)
        self.assertEqual(self.state_of(op), "ACCEPTED")

    def test_recover_start_unknown_to_not_started(self):
        op = self.handed_off()
        self.scheduler.unknown(op, "owner-1", 1)
        self.scheduler.rollover()
        self.scheduler.recover(op, "owner-2", 2, outcome="NOT_STARTED", evidence_hash=H2)
        self.assertEqual(self.state_of(op), "NOT_STARTED")
        self.assertEqual(self.last_event("scheduler.recovered")["payload"],
                         {"from_generation": 1, "outcome": "NOT_STARTED", "evidence": H2,
                          "state": "NOT_STARTED"})
        self.assertNotIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        # The recovered NOT_STARTED operation is not unresolved any more.
        self.assertEqual(self.scheduler.snapshot("req-1")["tokens"]["unknown_operations"], 0)
        self.assertEqual(self.scheduler.snapshot("req-1")["tokens"]["held"], 0)

    def test_recovered_event_carries_the_new_generation(self):
        op = self.handed_off()
        self.scheduler.rollover()
        self.scheduler.recover(op, "owner-2", 2, outcome="NOT_STARTED", evidence_hash=H1)
        event = self.last_event("scheduler.recovered")
        self.assertEqual(event["generation"], 2)
        self.assertEqual(event["operation_id"], op)
        self.assertEqual(event["payload"]["from_generation"], 1)
        # The takeover is recorded under the new generation; older and newer
        # generations never touch the recovered operation again.
        self.scheduler.rollover()
        self.assert_unchanged(self.scheduler.recover, "E_FENCED", op, "owner-2", 1,
                              outcome="NOT_STARTED", evidence_hash=H1)
        self.assert_unchanged(self.scheduler.recover, "E_FENCED", op, "owner-2", 2,
                              outcome="NOT_STARTED", evidence_hash=H1)
        self.assert_unchanged(self.scheduler.recover, "E_RECOVERY_STATE", op, "owner-3", 3,
                              outcome="NOT_STARTED", evidence_hash=H1)
        self.assertEqual(self.state_of(op), "NOT_STARTED")

    def test_recover_requires_a_candidate_hash_only_for_finished(self):
        op = self.handed_off()
        self.scheduler.rollover()
        self.assert_unchanged(self.scheduler.recover, "E_CLEANUP_OR_CANDIDATE", op, "owner-2", 2,
                              outcome="FINISHED", evidence_hash=H1)
        self.assert_unchanged(self.scheduler.recover, "E_CLEANUP_OR_CANDIDATE", op, "owner-2", 2,
                              outcome="FINISHED", evidence_hash=H1, candidate_hash="z" * 64)
        self.assert_unchanged(self.scheduler.recover, "E_CLEANUP_OR_CANDIDATE", op, "owner-2", 2,
                              outcome="FINISHED", evidence_hash=H1, candidate_hash="g" * 64)
        for outcome in ("NOT_STARTED", "START_UNKNOWN"):
            with self.subTest(outcome=outcome):
                self.assert_unchanged(self.scheduler.recover, "E_CLEANUP_OR_CANDIDATE", op, "owner-2", 2,
                                      outcome=outcome, evidence_hash=H1, candidate_hash=H2)
        self.assertEqual(self.state_of(op), "HANDOFF_ACCEPTED")

    def test_recover_rejects_bad_shapes_before_touching_the_request(self):
        op = self.handed_off()
        self.scheduler.rollover()
        for outcome in ("DONE", "not_started", None, 3, ["NOT_STARTED"]):
            with self.subTest(outcome=outcome):
                self.assert_unchanged(self.scheduler.recover, "E_RECOVERY_OUTCOME", op, "owner-2", 2,
                                      outcome=outcome, evidence_hash=H1)
        for outcome in ("NOT_STARTED", "START_UNKNOWN", "FINISHED"):
            with self.subTest(evidence=outcome):
                self.assert_unchanged(self.scheduler.recover, "E_EVIDENCE_HASH", op, "owner-2", 2,
                                      outcome=outcome, evidence_hash=None)
        # A missing operation is reported after the shape checks.
        self.assert_unchanged(self.scheduler.recover, "E_OPERATION_UNKNOWN", "op-missing", "owner-2", 2,
                              outcome="START_UNKNOWN", evidence_hash=H1)

    def test_recover_rejects_terminal_and_unstarted_operations(self):
        reserved = self.claim(task="task-2", key="idem-2", resources=("src/b.py",))
        reviewed = self.handed_off()
        self.scheduler.mark_running(reviewed, "owner-1", 1)
        self.scheduler.finish(reviewed, "owner-1", 1, H2, True)
        self.scheduler.rollover()
        self.assert_unchanged(self.scheduler.recover, "E_RECOVERY_STATE", reviewed, "owner-2", 2,
                              outcome="NOT_STARTED", evidence_hash=H1)
        self.assert_unchanged(self.scheduler.recover, "E_RECOVERY_STATE", reserved, "owner-2", 2,
                              outcome="FINISHED", evidence_hash=H1, candidate_hash=H2)
        self.assertEqual((self.state_of(reviewed), self.state_of(reserved)),
                         ("REVIEW_PENDING", "RESERVED"))

    def test_recover_requires_the_current_generation(self):
        op = self.handed_off()
        self.scheduler.rollover()
        self.assert_unchanged(self.scheduler.recover, "E_FENCED", op, "owner-2", 1,
                              outcome="NOT_STARTED", evidence_hash=H1)
        self.assert_unchanged(self.scheduler.recover, "E_FENCED", op, "owner-2", 3,
                              outcome="NOT_STARTED", evidence_hash=H1)


if __name__ == "__main__":
    unittest.main()
