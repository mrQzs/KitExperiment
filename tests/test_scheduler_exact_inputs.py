"""Executor tests for exact Scheduler inputs (scheduler-exact-inputs).

The planner oracle covers the same rules with ``str``/``int`` subclasses.  These
tests cover values that are not ``str`` at all (``None``, ``bytes``, ``int``,
``list``) plus the ``settle`` mapping behaviour the oracle does not exercise: a
replayed first entry, a ``dict`` subclass, an empty mapping, and a mapping whose
second entry cannot be canonically encoded.

Every rejection below first verifies the legal baseline on its own ledger, then
changes exactly one input on a fresh ledger and asserts both the contract code
and an unchanged ledger (values *and* their exact types).
"""
from __future__ import annotations

import dataclasses
import unittest

from sentinelmesh.scheduler import ContractError, Scheduler, digest

H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64
CAPS = {"dispatches": 5, "tokens": 100, "cost": 50}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
STATES = ("RESERVED", "HANDOFF_ACCEPTED", "RUNNING", "REVIEW_PENDING")
_MISSING = object()


class Mapping(dict):
    """A ``dict`` subclass: ``isinstance`` accepts it, exact typing does not."""


def exact_shape(value):
    """A comparable copy that also records the exact type of every part."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (type(value).__qualname__,
                {name: exact_shape(item) for name, item in vars(value).items()})
    if isinstance(value, dict):
        return ("dict", type(value).__qualname__,
                sorted([(exact_shape(k), exact_shape(v)) for k, v in value.items()], key=repr))
    if isinstance(value, (list, tuple)):
        return (type(value).__qualname__, [exact_shape(item) for item in value])
    return (type(value).__qualname__, value)


def ledger(scheduler):
    """Every piece of ledger state, compared together with each stored type."""
    return exact_shape({"generation": scheduler._generation, "requests": scheduler._requests,
                        "tasks": scheduler._tasks, "operations": scheduler._operations,
                        "idempotency": scheduler._idempotency, "holds": scheduler._resource_holds,
                        "events": scheduler._events})


def seeded(state="SEEDED"):
    """A scheduler with one request, two tasks and (from ``RESERVED`` on) one operation in ``state``."""
    scheduler = Scheduler()
    scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
    scheduler.seed_task("req-1", "task-1", CAPS, max_attempts=3)
    scheduler.seed_task("req-1", "task-2", CAPS, max_attempts=3)
    if state == "SEEDED":
        return scheduler, None
    op = scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, RESERVATION,
                         ["src/a.py"])["operation_id"]
    step = STATES.index(state)
    if step >= 1:
        scheduler.handoff(op, "owner-1", 1)
    if step >= 2:
        scheduler.mark_running(op, "owner-1", 1)
    if step >= 3:
        scheduler.finish(op, "owner-1", 1, H2, True)
    return scheduler, op


def state_and_last_event(scheduler, op):
    return (scheduler._operations[op].state, scheduler.events[-1]["type"],
            scheduler.events[-1]["payload"])


def usage_of(scheduler, op, dimension):
    usage = scheduler._operations[op].usage.get(dimension)
    return None if usage is None else (usage.call_key, usage.revision, usage.amount, usage.final,
                                       usage.evidence_hash)


def claim_effect(scheduler, op):
    keys = sorted(scheduler._operations)
    return (len(keys), scheduler._operations[keys[0]].state, sorted(scheduler._resource_holds),
            scheduler._events[-1]["type"], scheduler._idempotency[("req-1", "idem-1")] == keys[0])


def event_of(scheduler, event_type):
    return [event for event in scheduler.events if event["type"] == event_type][-1]


def finish_effect(scheduler, op):
    return (scheduler._operations[op].state, scheduler._operations[op].candidate_hash,
            event_of(scheduler, "run.finished_for_review")["payload"])


def seeded_task_effect(scheduler, task):
    return (("req-1", task) in scheduler._tasks, scheduler._tasks[("req-1", task)].caps)


CASES = (
    {"name": "handoff_owner_none", "state": "RESERVED", "code": "E_FENCED",
     "bad": [None, 5, ["owner-1"], b"owner-1"], "legal": "owner-1",
     "call": lambda s, op, v: s.handoff(op, v, 1),
     "effect": state_and_last_event,
     "effect_is": ("HANDOFF_ACCEPTED", "scheduler.handoff_accepted", {"generation": 1})},
    {"name": "finish_owner_none", "state": "RUNNING", "code": "E_FENCED",
     "bad": [None, 5, ["owner-1"]], "legal": "owner-1",
     "call": lambda s, op, v: s.finish(op, v, 1, H2, True),
     "effect": finish_effect,
     "effect_is": ("REVIEW_PENDING", H2, {"candidate": H2})},
    {"name": "claim_reservation_key_five", "state": "SEEDED", "code": "E_BUDGET_DIMENSION",
     "bad": [5], "legal": "tokens",
     "call": lambda s, op, v: s.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1,
                                      {"dispatches": 1, v: 40, "cost": 20}, ["src/a.py"]),
     "effect": claim_effect,
     "effect_is": (1, "RESERVED", [("repo-1", "src/a.py")], "scheduler.claimed", True)},
    {"name": "claim_resource_key_five", "state": "SEEDED", "code": "E_RESOURCE_KEYS",
     "bad": [5, ["src/a.py"]], "legal": "src/a.py",
     "call": lambda s, op, v: s.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1,
                                      RESERVATION, [v]),
     "effect": claim_effect,
     "effect_is": (1, "RESERVED", [("repo-1", "src/a.py")], "scheduler.claimed", True)},
    {"name": "seed_task_caps_key_none", "state": "SEEDED", "code": "E_BUDGET_DIMENSION",
     "bad": [None, 5], "legal": "tokens",
     "call": lambda s, op, v: s.seed_task("req-1", "task-3", {"dispatches": 5, v: 100, "cost": 50}),
     "effect": lambda s, op: seeded_task_effect(s, "task-3"),
     "effect_is": (True, CAPS)},
    {"name": "observe_call_key_five", "state": "RUNNING", "code": "E_OBSERVATION_SHAPE",
     "bad": [5, None, b"call-1"], "legal": "call-1",
     "call": lambda s, op, v: s.observe(op, "tokens", v, 1, 5, True, H1),
     "effect": lambda s, op: usage_of(s, op, "tokens"),
     "effect_is": ("call-1", 1, 5, True, H1), "returns": "APPLIED"},
    {"name": "observe_evidence_hash_none", "state": "RUNNING", "code": "E_OBSERVATION_SHAPE",
     "bad": [None, 5, H1.encode()], "legal": H1,
     "call": lambda s, op, v: s.observe(op, "tokens", "call-2", 1, 5, True, v),
     "effect": lambda s, op: usage_of(s, op, "tokens"),
     "effect_is": ("call-2", 1, 5, True, H1), "returns": "APPLIED"},
    {"name": "settle_mapping_key_five", "state": "RUNNING", "code": "E_OBSERVATION_SHAPE",
     "bad": [5, None], "legal": "tokens",
     "call": lambda s, op, v: s.settle(op, {v: 5}),
     "effect": lambda s, op: usage_of(s, op, "tokens"),
     "effect_is": (lambda op: (f"local:{op}:tokens", 1, 5, True,
                              digest({"operation": op, "dimension": "tokens", "value": 5}))),
     "returns": {"tokens": "APPLIED"}},
    {"name": "finish_candidate_hash_none", "state": "RUNNING", "code": "E_CLEANUP_OR_CANDIDATE",
     "bad": [None, 5, H2.encode()], "legal": H2,
     "call": lambda s, op, v: s.finish(op, "owner-1", 1, v, True),
     "effect": finish_effect,
     "effect_is": ("REVIEW_PENDING", H2, {"candidate": H2})},
    {"name": "fixture_verdict_candidate_hash_none", "state": "REVIEW_PENDING", "code": "E_VERDICT_CANDIDATE",
     "bad": [None, 5], "legal": H2,
     "call": lambda s, op, v: s.fixture_verdict(op, v, True, H3),
     "effect": state_and_last_event,
     "effect_is": ("ACCEPTED", "run.verdict", {"state": "ACCEPTED", "evidence": H3})},
    {"name": "stop_request_not_a_str", "state": "RESERVED", "code": "E_REQUEST_UNKNOWN",
     "bad": [["req-1"], 5, None], "legal": "req-1",
     "call": lambda s, op, v: s.stop(v),
     "effect": lambda s, op: (s._requests["req-1"].stopped, s.events[-1]["type"]),
     "effect_is": ("OPERATOR_STOP", "request.stopped")},
    {"name": "stop_reason_not_a_text", "state": "RESERVED", "code": "E_STOP_REASON",
     "bad": [None, 5, "", b"STOP", ["STOP"]], "legal": "OPERATOR_STOP",
     "call": lambda s, op, v: s.stop("req-1", v),
     "effect": lambda s, op: (s._requests["req-1"].stopped, s.events[-1]["type"]),
     "effect_is": ("OPERATOR_STOP", "request.stopped")},
)


class SchedulerExactInputsTest(unittest.TestCase):
    def assert_legal_baseline(self, case):
        scheduler, op = seeded(case["state"])
        result = case["call"](scheduler, op, case["legal"])
        if case.get("returns", _MISSING) is not _MISSING:
            self.assertEqual(result, case["returns"], "the legal baseline call returned something else")
        expected = case["effect_is"]
        if callable(expected):
            expected = expected(op)
        self.assertEqual(case["effect"](scheduler, op), expected,
                         "the legal baseline call did not take effect")

    def assert_rejected_unchanged(self, case, value):
        scheduler, op = seeded(case["state"])
        before = ledger(scheduler)
        with self.assertRaises(ContractError) as raised:
            case["call"](scheduler, op, value)
        self.assertEqual(raised.exception.code, case["code"])
        self.assertEqual(ledger(scheduler), before, "a rejected call changed the ledger")
        # The same ledger still takes the legal call.
        case["call"](scheduler, op, case["legal"])

    def test_exact_types_of_foreign_values(self):
        for case in CASES:
            with self.subTest(case=case["name"]):
                self.assert_legal_baseline(case)
                for value in case["bad"]:
                    with self.subTest(value=repr(value), type=type(value).__name__):
                        self.assert_rejected_unchanged(case, value)

    def test_unhashable_values_raise_the_contract_code(self):
        """An unhashable input reports the table's code, never a ``TypeError``."""
        for case in CASES:
            for value in case["bad"]:
                if isinstance(value, list):
                    with self.subTest(case=case["name"], value=repr(value)):
                        self.assert_rejected_unchanged(case, value)

    # settle() with a mapping ------------------------------------------------

    def test_settle_mapping_replays_the_first_entry(self):
        scheduler, op = seeded("RUNNING")
        self.assertEqual(scheduler.settle(op, {"tokens": 7}), {"tokens": "APPLIED"})
        replayed = usage_of(scheduler, op, "tokens")
        events = len(scheduler.events)
        self.assertEqual(scheduler.settle(op, {"tokens": 7, "cost": 2}),
                         {"tokens": "REPLAY", "cost": "APPLIED"})
        # The replayed dimension is kept verbatim, the new one is recorded once.
        self.assertEqual(usage_of(scheduler, op, "tokens"), replayed)
        self.assertEqual(usage_of(scheduler, op, "cost"),
                         (f"local:{op}:cost", 1, 2, True,
                          digest({"operation": op, "dimension": "cost", "value": 2})))
        # Exactly one new event: the replayed dimension emits nothing.
        new_events = scheduler.events[events:]
        self.assertEqual([event["type"] for event in new_events], ["usage.observed"])
        self.assertEqual(new_events[0]["payload"],
                         {"dimension": "cost", "call_key": f"local:{op}:cost", "revision": 1, "amount": 2,
                          "final": True, "correction": False,
                          "evidence_hash": digest({"operation": op, "dimension": "cost", "value": 2})})
        self.assertEqual(scheduler.settle(op, {"cost": 2}), {"cost": "REPLAY"})

    def test_settle_mapping_accepts_a_dict_subclass(self):
        scheduler, op = seeded("RUNNING")
        before = len(scheduler.events)
        self.assertEqual(scheduler.settle(op, Mapping({"tokens": 3, "cost": 4})),
                         {"tokens": "APPLIED", "cost": "APPLIED"})
        self.assertEqual([event["type"] for event in scheduler.events[before:]],
                         ["usage.observed", "usage.observed"])
        self.assertEqual(usage_of(scheduler, op, "tokens"),
                         (f"local:{op}:tokens", 1, 3, True,
                          digest({"operation": op, "dimension": "tokens", "value": 3})))
        self.assertEqual(usage_of(scheduler, op, "cost"),
                         (f"local:{op}:cost", 1, 4, True,
                          digest({"operation": op, "dimension": "cost", "value": 4})))

    def test_settle_mapping_empty(self):
        scheduler, op = seeded("RUNNING")
        before = ledger(scheduler)
        self.assertEqual(scheduler.settle(op, {}), {})
        self.assertEqual(ledger(scheduler), before, "an empty mapping changed the ledger")
        self.assertEqual(scheduler.settle(op, Mapping({})), {})
        self.assertEqual(ledger(scheduler), before, "an empty mapping changed the ledger")
        # An empty mapping still requires the operation to exist.
        with self.assertRaises(ContractError) as raised:
            scheduler.settle("op-missing", {})
        self.assertEqual(raised.exception.code, "E_OPERATION_UNKNOWN")
        self.assertEqual(ledger(scheduler), before, "a rejected settle changed the ledger")

    def test_settle_mapping_rejects_an_unencodable_later_entry(self):
        scheduler, op = seeded("RUNNING")
        before = ledger(scheduler)
        with self.assertRaises(ContractError) as raised:
            scheduler.settle(op, {"tokens": 5, "cost": 2**53})
        self.assertEqual(raised.exception.code, "E_JSON_INTEGER")
        self.assertEqual(ledger(scheduler), before, "settle recorded part of a rejected mapping")
        self.assertIsNone(usage_of(scheduler, op, "tokens"))
        self.assertIsNone(usage_of(scheduler, op, "cost"))
        # The very same call on a clean ledger still records both dimensions.
        self.assertEqual(scheduler.settle(op, {"tokens": 5, "cost": 2}),
                         {"tokens": "APPLIED", "cost": "APPLIED"})

    # stop() ----------------------------------------------------------------

    def test_stop_refuses_an_empty_reason_and_keeps_the_request_running(self):
        scheduler, op = seeded("RESERVED")
        # Legal baseline: a non-empty reason stops the request.
        self.assertIsNone(scheduler.stop("req-1"))
        self.assertEqual(scheduler._requests["req-1"].stopped, "OPERATOR_STOP")
        self.assertEqual(scheduler.events[-1]["payload"], {"request_id": "req-1", "reason": "OPERATOR_STOP"})

    def test_stop_with_an_empty_reason_changes_nothing(self):
        scheduler, op = seeded("RESERVED")
        before = ledger(scheduler)
        for reason in ("", None, 5, b"OPERATOR_STOP"):
            with self.subTest(reason=repr(reason)):
                with self.assertRaises(ContractError) as raised:
                    scheduler.stop("req-1", reason)
                self.assertEqual(raised.exception.code, "E_STOP_REASON")
                self.assertEqual(ledger(scheduler), before, "a rejected stop changed the ledger")
        self.assertIsNone(scheduler._requests["req-1"].stopped)
        # The request is still claimable, and the very same call with a non-empty
        # reason stops it afterwards.
        claimed = scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, RESERVATION,
                                  ["src/a.py"])
        self.assertEqual(claimed["replayed"], True)
        scheduler.stop("req-1", "OPERATOR_STOP")
        self.assertEqual(scheduler._requests["req-1"].stopped, "OPERATOR_STOP")
        with self.assertRaises(ContractError) as raised:
            scheduler.claim("req-1", "task-1", 1, "idem-2", "owner-1", 1, 1, RESERVATION, ["src/a.py"])
        self.assertEqual(raised.exception.code, "E_STOPPED")

    def test_stop_with_a_reason_stops_the_request(self):
        scheduler, op = seeded("RESERVED")
        scheduler.stop("req-1", "OPERATOR_STOP")
        self.assertEqual(scheduler._requests["req-1"].stopped, "OPERATOR_STOP")
        self.assertEqual(scheduler.events[-1]["payload"], {"request_id": "req-1", "reason": "OPERATOR_STOP"})
        with self.assertRaises(ContractError) as raised:
            scheduler.claim("req-1", "task-1", 1, "idem-2", "owner-1", 1, 1, RESERVATION, ["src/a.py"])
        self.assertEqual(raised.exception.code, "E_STOPPED")

    def test_stop_does_not_overwrite_an_existing_reason(self):
        scheduler, op = seeded("RUNNING")
        scheduler.observe(op, "tokens", "call-1", 1, 200, True, H1)  # exceeds the limit
        self.assertEqual(scheduler._requests["req-1"].stopped, "BUDGET_EXCEEDED")
        scheduler.stop("req-1", "OPERATOR_STOP")
        self.assertEqual(scheduler._requests["req-1"].stopped, "BUDGET_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
