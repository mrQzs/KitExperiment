"""Planner oracle for scheduler-operation-view (`.agent/design/stream-trust-boundary.md`, section 3 item 6).

Planner-provided; executors must not modify this file.

``Scheduler.operation(operation_id)`` is a read-only view of one operation --
its request, task, state, owner, generation and reservation -- returned as a
fresh copy.  It records nothing and changes nothing, whatever generation the
caller belongs to.  The bridge and the adapter use it to check an invocation
against the operation it claims to be before anything is handed off.

``rollover()`` and the constructor refuse a generation that cannot be encoded
in an event before anything changes: a failed rollover leaves the generation,
every request's stop flag and the events exactly as they were.
"""
from __future__ import annotations

import dataclasses
import unittest
try:
    from enum import StrEnum
except ImportError:  # Python 3.10: a (str, Enum) member is a str subclass all the same
    from enum import Enum

    class StrEnum(str, Enum):
        pass

from sentinelmesh.scheduler import ContractError, Scheduler

H1, H2 = "1" * 64, "2" * 64
CAPS = {"dispatches": 5, "tokens": 100, "cost": 50}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
VIEW_KEYS = {"operation_id", "request_id", "task_id", "state", "owner", "generation", "reservation"}
MAX_SAFE = 2**53 - 1


class Text(str):
    pass


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


def seeded(generation=1, request="req-1"):
    scheduler = Scheduler(generation=generation)
    scheduler.seed_request(request, "repo-1", CAPS, concurrency=4)
    for task in ("task-1", "task-2"):
        scheduler.seed_task(request, task, CAPS, max_attempts=5)
    return scheduler


def claim(scheduler, key="idem-1", request="req-1", generation=1, resource="src/a.py", reservation=RESERVATION,
          task="task-1"):
    return scheduler.claim(request, task, 1, key, "owner-1", generation, 1, reservation, [resource])["operation_id"]


def view_of(op, state, *, owner="owner-1", generation=1, reservation=RESERVATION, request="req-1", task="task-1"):
    return {"operation_id": op, "request_id": request, "task_id": task, "state": state, "owner": owner,
            "generation": generation, "reservation": dict(reservation)}


class OperationViewOracle(unittest.TestCase):
    def assert_view(self, scheduler, op, expected):
        view = scheduler.operation(op)
        self.assertIs(type(view), dict)
        self.assertEqual(set(view), VIEW_KEYS)
        self.assertEqual(view, expected)
        self.assertEqual(shape(view), shape(expected), "a value is not of the exact type")

    def test_the_view_follows_an_operation_to_review(self):
        scheduler = seeded()
        op = claim(scheduler)
        self.assert_view(scheduler, op, view_of(op, "RESERVED"))
        scheduler.handoff(op, "owner-1", 1)
        self.assert_view(scheduler, op, view_of(op, "HANDOFF_ACCEPTED"))
        scheduler.mark_running(op, "owner-1", 1)
        self.assert_view(scheduler, op, view_of(op, "RUNNING"))
        scheduler.settle(op, {"tokens": 12, "cost": 3}, evidence_hash=H1)
        scheduler.finish(op, "owner-1", 1, H2, True)
        self.assert_view(scheduler, op, view_of(op, "REVIEW_PENDING"))
        scheduler.fixture_verdict(op, H2, True, H1)
        self.assert_view(scheduler, op, view_of(op, "ACCEPTED"))

    def test_the_view_of_the_other_end_states(self):
        scheduler = seeded()
        unknown = claim(scheduler, "idem-1", resource="src/a.py")
        scheduler.handoff(unknown, "owner-1", 1)
        scheduler.unknown(unknown, "owner-1", 1, reason="CLEANUP_UNCONFIRMED")
        self.assert_view(scheduler, unknown, view_of(unknown, "START_UNKNOWN"))
        other = seeded()
        released = claim(other, "idem-2", resource="src/b.py")
        other.handoff(released, "owner-1", 1)
        other.release_proven_unstarted(released, "owner-1", 1, evidence_hash=H1)
        self.assert_view(other, released, view_of(released, "NOT_STARTED"))
        cancelled = claim(other, "idem-3", resource="src/c.py", task="task-2")
        other.cancel_unstarted(cancelled, 1)
        self.assert_view(other, cancelled, view_of(cancelled, "CANCELLED_NOT_STARTED", task="task-2"))

    def test_the_view_is_the_operations_own_reservation(self):
        # The task budget and the reservation differ in every dimension, so a
        # view built from the caps (or from another operation) cannot pass.
        scheduler = seeded()
        first = claim(scheduler, "idem-1", resource="src/a.py")
        smaller = {"dispatches": 1, "tokens": 7, "cost": 0}
        second = claim(scheduler, "idem-2", resource="src/b.py", reservation=smaller, task="task-2")
        self.assert_view(scheduler, first, view_of(first, "RESERVED"))
        self.assert_view(scheduler, second, view_of(second, "RESERVED", reservation=smaller, task="task-2"))

    def test_the_view_is_a_copy_and_records_nothing(self):
        scheduler = seeded()
        op = claim(scheduler)
        scheduler.handoff(op, "owner-1", 1)
        before = ledger(scheduler)
        view = scheduler.operation(op)
        self.assertEqual(ledger(scheduler), before, "reading the view changed the ledger")
        view["state"] = "ACCEPTED"
        view["reservation"]["tokens"] = 0
        view["reservation"]["extra"] = 1
        view["extra"] = 1
        self.assertEqual(ledger(scheduler), before, "the view shares state with the ledger")
        self.assert_view(scheduler, op, view_of(op, "HANDOFF_ACCEPTED"))
        # The ledger still works as before: the operation runs and finishes.
        scheduler.mark_running(op, "owner-1", 1)
        scheduler.settle(op, {"tokens": 12, "cost": 3}, evidence_hash=H1)
        scheduler.finish(op, "owner-1", 1, H2, True)
        self.assert_view(scheduler, op, view_of(op, "REVIEW_PENDING"))

    def test_the_view_after_a_rollover_and_a_recovery(self):
        scheduler = seeded()
        op = claim(scheduler)
        scheduler.handoff(op, "owner-1", 1)
        self.assertEqual(scheduler.rollover(), 2)
        # Reading is not fenced: the new generation sees the old owner and generation.
        self.assert_view(scheduler, op, view_of(op, "HANDOFF_ACCEPTED"))
        scheduler.recover(op, "owner-2", 2, outcome="START_UNKNOWN", evidence_hash=H1)
        self.assert_view(scheduler, op, view_of(op, "START_UNKNOWN", owner="owner-2", generation=2))

    def test_an_unknown_or_foreign_operation_id_is_refused(self):
        scheduler = seeded()
        op = claim(scheduler)
        self.assert_view(scheduler, op, view_of(op, "RESERVED"))
        before = ledger(scheduler)
        bad = {"an unknown id": "op-missing", "None": None, "an int": 5, "a list": [op], "a dict": {op: 1},
               "a str subclass equal to the id": Text(op),
               "a StrEnum member equal to the id": StrEnum("Member", [("V", op)]).V}
        for label, value in bad.items():
            with self.subTest(case=label):
                with self.assertRaises(ContractError) as ctx:
                    scheduler.operation(value)
                self.assertEqual(ctx.exception.code, "E_OPERATION_UNKNOWN")
                self.assertEqual(ledger(scheduler), before)
        self.assert_view(scheduler, op, view_of(op, "RESERVED"))

    # generations that cannot be encoded ----------------------------------------

    def test_a_rollover_past_the_json_safe_range_changes_nothing(self):
        scheduler = seeded(generation=MAX_SAFE - 1)
        claim(scheduler, generation=MAX_SAFE - 1)
        # Legal boundary: the last encodable generation is still reached.
        self.assertEqual(scheduler.rollover(), MAX_SAFE)
        self.assertEqual(scheduler.events[-1]["payload"], {"generation": MAX_SAFE})
        # A request seeded after that rollover is running, so a failed rollover
        # that stopped it would show in the ledger.
        scheduler.seed_request("req-2", "repo-2", CAPS, concurrency=4)
        before = ledger(scheduler)
        with self.assertRaises(ContractError) as ctx:
            scheduler.rollover()
        self.assertEqual(ctx.exception.code, "E_JSON_INTEGER")
        self.assertEqual(ledger(scheduler), before, "a refused rollover changed the ledger")
        self.assertEqual(scheduler.generation, MAX_SAFE)
        self.assertIsNone(scheduler._requests["req-2"].stopped)

    def test_a_generation_that_cannot_be_encoded_is_refused_at_construction(self):
        legal = Scheduler(generation=MAX_SAFE)
        self.assertEqual(legal.generation, MAX_SAFE)
        legal.seed_request("req-1", "repo-1", CAPS)
        self.assertEqual(legal.events[-1]["generation"], MAX_SAFE)
        for value in (MAX_SAFE + 1, 2**60):
            with self.subTest(generation=value):
                with self.assertRaises(ContractError) as ctx:
                    Scheduler(generation=value)
                self.assertEqual(ctx.exception.code, "E_JSON_INTEGER")
        # The existing integer rule is unchanged and comes first.
        for value in (0, -1, True, 1.0, "1"):
            with self.subTest(generation=repr(value)):
                with self.assertRaises(ContractError) as ctx:
                    Scheduler(generation=value)
                self.assertEqual(ctx.exception.code, "E_INTEGER")


if __name__ == "__main__":
    unittest.main()
