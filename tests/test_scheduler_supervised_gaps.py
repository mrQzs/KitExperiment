"""Planner follow-up cases for scheduler-supervised-transitions (review of attempt-002).

Two rules the accepted attempt implements had no test that could fail:

* ``recover`` takes only an exact ``int`` generation.  The attempt's ``True``
  case ran at generation 2, where ``True == 1`` is refused by any equality test;
  here ``True`` is offered at generation 1 and an ``int`` subclass at 2;
* the process identity is checked before the owner and the generation, not
  only before the state.

Every rejection names its contract code and leaves the ledger unchanged.
"""
from __future__ import annotations

import unittest

from sentinelmesh.scheduler import ContractError, Scheduler

H1 = "1" * 64


class Generation(int):
    pass


class SchedulerSupervisedGaps(unittest.TestCase):
    def setUp(self):
        self.scheduler = Scheduler()
        self.scheduler.seed_request("req-1", "repo-1", {"dispatches": 5, "tokens": 100}, concurrency=4)
        self.scheduler.seed_task("req-1", "task-1", {"dispatches": 5, "tokens": 100}, max_attempts=3)
        claim = self.scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 40},
                                     ["src/a.py"])
        self.op = claim["operation_id"]
        self.scheduler.handoff(self.op, "owner-1", 1)

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

    def test_recover_refuses_bool_and_int_subclass_generations(self):
        # At generation 1, True == 1: only the exact-int rule refuses it (an
        # equality test would accept it and report E_RECOVERY_NOT_FENCED).
        self.scheduler.stop("req-1")
        self.assert_refused("E_FENCED", self.scheduler.recover, self.op, "owner-2", True, outcome="START_UNKNOWN",
                            evidence_hash=H1)
        self.scheduler.rollover()
        self.assert_refused("E_FENCED", self.scheduler.recover, self.op, "owner-2", Generation(2),
                            outcome="START_UNKNOWN", evidence_hash=H1)
        self.assertIs(type(self.scheduler._operations[self.op].generation), int)

    def test_identity_is_checked_before_the_owner_and_the_generation(self):
        self.assert_refused("E_IDENTITY_SHAPE", self.scheduler.mark_running, self.op, "owner-2", 1,
                            process_identity={"pid": 0})
        self.scheduler.rollover()
        self.assert_refused("E_IDENTITY_SHAPE", self.scheduler.mark_running, self.op, "owner-1", 1,
                            process_identity={"pid": 0})


if __name__ == "__main__":
    unittest.main()
