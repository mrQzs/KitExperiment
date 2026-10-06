"""Planner probes for scheduler-exact-inputs v2 (review of attempt-001).

Planner-provided; executors must not modify this file.

* Rule 2 of the contract: an unhashable value reports the table's code and
  never escapes as ``TypeError``.  The oracle offered unhashable values only as
  operation ids, and ``observe`` looked the recorded usage up by the dimension
  before it checked the dimension's type, so a list, a set or a dict escaped as
  ``TypeError``.  This probe fails on attempt-001.
* Rule 3: with an explicit ``evidence_hash`` no default ``digest()`` runs, so
  only the event payload encoded before anything is recorded keeps a mapping
  whose later entry is ``2**53`` from recording the earlier one.  Attempt-001
  already does this; the probe guards it.

Every rejection names its contract code and leaves the whole ledger unchanged
(values and exact types); the legal call still succeeds on the same ledger.
"""
from __future__ import annotations

import dataclasses
import unittest

from sentinelmesh.scheduler import ContractError, Scheduler

H1 = "1" * 64
CAPS = {"dispatches": 5, "tokens": 100, "cost": 50}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}


def shape(value):
    """A comparable copy of ``value`` that also records the exact type of every part."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (type(value).__qualname__, shape(vars(value)))
    if isinstance(value, dict):
        return ("dict", type(value).__qualname__, sorted((shape(k), shape(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return (type(value).__qualname__, [shape(item) for item in value])
    return (type(value).__qualname__, value)


def ledger(scheduler):
    return shape({"generation": scheduler._generation, "requests": scheduler._requests,
                  "tasks": scheduler._tasks, "operations": scheduler._operations,
                  "idempotency": scheduler._idempotency, "holds": scheduler._resource_holds,
                  "events": scheduler._events})


def running():
    scheduler = Scheduler()
    scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
    scheduler.seed_task("req-1", "task-1", CAPS, max_attempts=3)
    op = scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, RESERVATION, ["src/a.py"])["operation_id"]
    scheduler.handoff(op, "owner-1", 1)
    scheduler.mark_running(op, "owner-1", 1)
    return scheduler, op


class SchedulerExactInputsProbes(unittest.TestCase):
    def test_observe_rejects_an_unhashable_dimension(self):
        for dimension in (["tokens"], {"tokens"}, {"tokens": 1}):
            with self.subTest(dimension=repr(dimension)):
                scheduler, op = running()
                before = ledger(scheduler)
                with self.assertRaises(ContractError) as raised:
                    scheduler.observe(op, dimension, "call-1", 1, 5, True, H1)
                self.assertEqual(raised.exception.code, "E_OBSERVATION_SHAPE")
                self.assertEqual(ledger(scheduler), before, "a rejected call changed the ledger")
                self.assertEqual(scheduler.observe(op, "tokens", "call-1", 1, 5, True, H1), "APPLIED")

    def test_settle_mapping_with_explicit_evidence_encodes_before_recording(self):
        scheduler, op = running()
        before = ledger(scheduler)
        with self.assertRaises(ContractError) as raised:
            scheduler.settle(op, {"tokens": 5, "cost": 2**53}, evidence_hash=H1)
        self.assertEqual(raised.exception.code, "E_JSON_INTEGER")
        self.assertEqual(ledger(scheduler), before, "settle recorded part of a rejected mapping")
        self.assertEqual(scheduler.settle(op, {"tokens": 5, "cost": 2}, evidence_hash=H1),
                         {"tokens": "APPLIED", "cost": "APPLIED"})


if __name__ == "__main__":
    unittest.main()
