"""Executor tests for ``Scheduler.operation`` and the generation boundaries.

These cases complement the planner oracle (``test_scheduler_operation_view_oracle.py``)
instead of repeating it: every rejection starts from a verified legal baseline
and changes exactly one input, recorded usage does not appear in the view, two
operations of one request have independent views, two successful rollovers in a
row are checked event by event, and a release that gives the *budget*
reservation back leaves the operation's own reservation in the view.
"""
from __future__ import annotations

import copy
import unittest

from sentinelmesh.scheduler import ContractError, Scheduler

H1, H2 = "1" * 64, "2" * 64
CAPS = {"dispatches": 5, "tokens": 100, "cost": 50}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
VIEW_KEYS = {"operation_id", "request_id", "task_id", "state", "owner", "generation", "reservation"}
MAX_SAFE = 2**53 - 1


class Text(str):
    """A ``str`` subclass: equal to a stored id, but not that id."""


def ledger(scheduler):
    """A deep copy of every ledger structure, for before/after comparisons."""
    return {"generation": scheduler._generation, "requests": copy.deepcopy(scheduler._requests),
            "tasks": copy.deepcopy(scheduler._tasks), "operations": copy.deepcopy(scheduler._operations),
            "idempotency": copy.deepcopy(scheduler._idempotency),
            "holds": copy.deepcopy(scheduler._resource_holds),
            "events": copy.deepcopy(scheduler._events)}


def seeded(generation=1, request="req-1"):
    scheduler = Scheduler(generation=generation)
    scheduler.seed_request(request, "repo-1", CAPS, concurrency=4)
    for task in ("task-1", "task-2"):
        scheduler.seed_task(request, task, CAPS, max_attempts=5)
    return scheduler


def claim(scheduler, key="idem-1", request="req-1", generation=1, resource="src/a.py",
          reservation=RESERVATION, task="task-1"):
    return scheduler.claim(request, task, 1, key, "owner-1", generation, 1, reservation, [resource])["operation_id"]


def view_of(op, state, *, owner="owner-1", generation=1, reservation=RESERVATION, request="req-1", task="task-1"):
    return {"operation_id": op, "request_id": request, "task_id": task, "state": state, "owner": owner,
            "generation": generation, "reservation": dict(reservation)}


class OperationViewGaps(unittest.TestCase):
    def assert_view(self, scheduler, op, expected):
        view = scheduler.operation(op)
        self.assertIs(type(view), dict)
        self.assertEqual(set(view), VIEW_KEYS)
        self.assertEqual(view, expected)
        for key in ("operation_id", "request_id", "task_id", "state", "owner"):
            self.assertIs(type(view[key]), str, f"{key} is not an exact str")
        self.assertIs(type(view["generation"]), int)
        self.assertIs(type(view["reservation"]), dict)
        for dimension, amount in view["reservation"].items():
            self.assertIs(type(dimension), str)
            self.assertIs(type(amount), int)

    # the rejections ----------------------------------------------------------

    def test_a_foreign_operation_id_is_refused_after_the_baseline_view(self):
        scheduler = seeded()
        op = claim(scheduler)
        self.assert_view(scheduler, op, view_of(op, "RESERVED"))
        before = ledger(scheduler)
        bad = {"a missing id": "op-missing", "an empty id": "", "None": None, "an int": 5,
               "a bool": True, "a list": [op], "a dict": {op: 1}, "a set": {op},
               "a str subclass equal to the id": Text(op)}
        for label, value in bad.items():
            with self.subTest(case=label):
                with self.assertRaises(ContractError) as ctx:
                    scheduler.operation(value)
                self.assertEqual(ctx.exception.code, "E_OPERATION_UNKNOWN")
                self.assertEqual(ledger(scheduler), before)
        # The legal baseline still reads the same afterwards.
        self.assert_view(scheduler, op, view_of(op, "RESERVED"))

    def test_the_view_of_a_fresh_scheduler_is_refused(self):
        # Legal baseline: an operation exists and is readable.  The only change
        # is that no operation is stored at all.
        scheduler = seeded()
        op = claim(scheduler)
        self.assert_view(scheduler, op, view_of(op, "RESERVED"))
        empty = seeded()
        before = ledger(empty)
        with self.assertRaises(ContractError) as ctx:
            empty.operation(claim(scheduler))
        self.assertEqual(ctx.exception.code, "E_OPERATION_UNKNOWN")
        self.assertEqual(ledger(empty), before)

    def test_the_view_is_readable_after_the_request_is_stopped(self):
        scheduler = seeded()
        op = claim(scheduler)
        scheduler.handoff(op, "owner-1", 1)
        scheduler.stop("req-1", reason="OPERATOR_STOP")
        before = ledger(scheduler)
        view = scheduler.operation(op)
        self.assertEqual(view, view_of(op, "HANDOFF_ACCEPTED"))
        self.assertIs(type(view), dict)
        self.assertEqual(ledger(scheduler), before, "reading the view changed the ledger")

    # no usage, no candidate, no resources -----------------------------------

    def test_recorded_usage_never_changes_the_view(self):
        scheduler = seeded()
        op = claim(scheduler)
        scheduler.handoff(op, "owner-1", 1)
        scheduler.mark_running(op, "owner-1", 1)
        self.assert_view(scheduler, op, view_of(op, "RUNNING"))
        before = scheduler.operation(op)
        # Applied observations, then a correction and a replay of the same one.
        self.assertEqual(scheduler.settle(op, {"tokens": 12, "cost": 3}, evidence_hash=H1),
                         {"tokens": "APPLIED", "cost": "APPLIED"})
        self.assert_view(scheduler, op, view_of(op, "RUNNING"))
        scheduler.settle(op, {"tokens": 20}, revision=2, correction=True, evidence_hash=H2)
        self.assertEqual(scheduler.settle(op, {"tokens": 20}, revision=2, evidence_hash=H2),
                         {"tokens": "REPLAY"})
        self.assertEqual(scheduler.operation(op), before)
        # The view carries no usage, no candidate hash and no resource keys.
        self.assertEqual(set(scheduler.operation(op)), VIEW_KEYS)
        scheduler.finish(op, "owner-1", 1, H2, True)
        self.assertEqual(set(scheduler.operation(op)), VIEW_KEYS)
        self.assert_view(scheduler, op, view_of(op, "REVIEW_PENDING"))

    # one request, two tasks -------------------------------------------------

    def test_two_operations_of_one_request_have_independent_views(self):
        scheduler = seeded()
        first = claim(scheduler, "idem-1", resource="src/a.py")
        smaller = {"dispatches": 1, "tokens": 7, "cost": 0}
        second = claim(scheduler, "idem-2", resource="src/b.py", task="task-2", reservation=smaller)
        # The reservation is the operation's own, neither the request caps
        # (dispatches 5 / tokens 100 / cost 50) nor the task caps.
        self.assertEqual(scheduler.operation(first)["reservation"], dict(RESERVATION))
        self.assertNotEqual(scheduler.operation(first)["reservation"], CAPS)
        # Advancing only the second operation leaves the first view alone.
        scheduler.handoff(second, "owner-1", 1)
        self.assert_view(scheduler, first, view_of(first, "RESERVED"))
        self.assert_view(scheduler, second, view_of(second, "HANDOFF_ACCEPTED",
                                                   reservation=smaller, task="task-2"))
        # Mutating one view does not touch the other operation or the ledger.
        view = scheduler.operation(first)
        view["state"] = "ACCEPTED"
        view["reservation"]["tokens"] = 0
        self.assert_view(scheduler, first, view_of(first, "RESERVED"))
        self.assert_view(scheduler, second, view_of(second, "HANDOFF_ACCEPTED",
                                                   reservation=smaller, task="task-2"))
        # The task budgets in the snapshot are still their own.
        self.assertEqual(scheduler.snapshot("req-1", "task-2")["tokens"]["limit"], 100)

    # rollover ---------------------------------------------------------------

    def test_two_successful_rollovers_in_a_row(self):
        # Legal baseline: from the last encodable-1 generation two rollovers are
        # still legal, and each one is checked on its own boundary.
        scheduler = seeded(generation=MAX_SAFE - 2)
        op = claim(scheduler, generation=MAX_SAFE - 2)
        self.assertEqual(scheduler.rollover(), MAX_SAFE - 1)
        self.assertEqual(scheduler.rollover(), MAX_SAFE)
        self.assertEqual(scheduler.generation, MAX_SAFE)
        fenced = [e for e in scheduler.events if e["type"] == "controller.fenced"]
        self.assertEqual([e["payload"]["generation"] for e in fenced], [MAX_SAFE - 1, MAX_SAFE])
        self.assertEqual([e["generation"] for e in fenced], [MAX_SAFE - 1, MAX_SAFE])
        self.assertEqual([e["seq"] for e in fenced], [3, 4])
        self.assertEqual([e["operation_id"] for e in fenced], [None, None])
        # Every request is marked for recovery exactly once.
        for root in scheduler._requests.values():
            self.assertEqual(root.stopped, "RECOVERY_REQUIRED")
        # Reading still works, and the third rollover cannot be encoded.
        self.assert_view(scheduler, op, view_of(op, "RESERVED", generation=MAX_SAFE - 2))
        before = ledger(scheduler)
        with self.assertRaises(ContractError) as ctx:
            scheduler.rollover()
        self.assertEqual(ctx.exception.code, "E_JSON_INTEGER")
        self.assertEqual(ledger(scheduler), before, "a refused rollover changed the ledger")
        self.assertEqual(scheduler.generation, MAX_SAFE)

    def test_a_rollover_keeps_an_existing_stop_reason(self):
        scheduler = seeded()
        claim(scheduler)
        scheduler.stop("req-1", reason="BUDGET_EXCEEDED")
        self.assertEqual(scheduler.rollover(), 2)
        self.assertEqual(scheduler._requests["req-1"].stopped, "BUDGET_EXCEEDED")

    # release ----------------------------------------------------------------

    def test_a_release_gives_back_the_budget_not_the_own_reservation(self):
        scheduler = seeded()
        op = claim(scheduler)
        scheduler.handoff(op, "owner-1", 1)
        self.assert_view(scheduler, op, view_of(op, "HANDOFF_ACCEPTED"))
        scheduler.settle(op, {"tokens": 5}, final=False, evidence_hash=H1)
        before = scheduler.snapshot("req-1")
        scheduler.release_proven_unstarted(op, "owner-1", 1, evidence_hash=H2)
        self.assert_view(scheduler, op, view_of(op, "NOT_STARTED"))
        # The reservation the operation made is no longer held by the budget...
        self.assertEqual(before["tokens"]["held"], 35)
        self.assertEqual(scheduler.snapshot("req-1")["tokens"]["held"], 0)
        self.assertEqual(scheduler.snapshot("req-1")["tokens"]["known"], 5)
        # ...but the view still shows what the operation itself reserved.
        self.assertEqual(scheduler.operation(op)["reservation"], dict(RESERVATION))


if __name__ == "__main__":
    unittest.main()
