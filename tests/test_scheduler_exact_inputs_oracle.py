"""Planner oracle for scheduler-exact-inputs.

Planner-provided; executors must not modify this file.

The supervised transitions already take exact types.  The older ``Scheduler``
methods did not:

* they accepted ``str`` subclasses (including ``StrEnum`` members), changed the
  ledger and only then failed in ``canonical()`` with ``E_JSON_TYPE`` /
  ``E_JSON_KEY``, or stored the foreign value silently;
* ``_epoch`` accepted any generation that merely *equals* the current one
  (``True``, an ``int`` subclass, ``1.0``);
* an exact ``int`` beyond the JSON-safe range was recorded before ``_emit``
  failed with ``E_JSON_INTEGER``;
* ``settle()`` with a mapping recorded the leading dimensions before a later
  one was rejected;
* ``stop()`` with an empty reason left the request running;
* ``scheduler.recovered`` did not name the new owner.

Every rejection below names its contract code and leaves the whole ledger
unchanged -- requests, tasks, operations (usage included), idempotency keys,
resource holds and events, compared together with the exact type of every
stored value -- and the legal call still succeeds on the same ledger afterwards.
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

from sentinelmesh.scheduler import ContractError, Scheduler, digest

H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64
CAPS = {"dispatches": 5, "tokens": 100, "cost": 50}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
STATES = ("RESERVED", "HANDOFF_ACCEPTED", "RUNNING", "REVIEW_PENDING")


class Text(str):
    pass


class Generation(int):
    pass


def foreign(value):
    """Values equal to ``value`` that are not an exact ``str``."""
    return [Text(value), StrEnum("Member", [("V", value)]).V]


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


def ledger_at(state):
    """A ledger with one operation of ``task-1`` in ``state`` (``SEEDED``: none; ``FENCED``: handed off, then rollover)."""
    scheduler = Scheduler()
    scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
    scheduler.seed_task("req-1", "task-1", CAPS, max_attempts=3)
    if state == "SEEDED":
        return scheduler, None
    op = scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, RESERVATION, ["src/a.py"])["operation_id"]
    step = STATES.index("HANDOFF_ACCEPTED" if state == "FENCED" else state)
    if step >= 1:
        scheduler.handoff(op, "owner-1", 1)
    if step >= 2:
        scheduler.mark_running(op, "owner-1", 1)
    if step >= 3:
        scheduler.finish(op, "owner-1", 1, H2, True)
    if state == "FENCED":
        scheduler.rollover()
    return scheduler, op


GENERATIONS = [True, Generation(1), 1.0]
CASES = []


def case(name, state, code, call, bad, legal):
    """``call(scheduler, op, value)`` with the input under test set to ``value``.

    ``bad(op)`` lists the rejected values and ``legal(op)`` the accepted one.
    """
    CASES.append((name, state, code, call, bad, legal))


def claim_with(**changes):
    args = {"request": "req-1", "task": "task-1", "key": "idem-1", "owner": "owner-1",
            "reservation": RESERVATION, "resources": ["src/a.py"]}
    args.update(changes)
    return lambda s: s.claim(args["request"], args["task"], 1, args["key"], args["owner"], 1, 1,
                             args["reservation"], args["resources"])


one, same = (lambda op: 1), (lambda op: op)

# 1. The generation of every fenced method: an exact int equal to the current one, else E_FENCED.
for name, state, call in (
        ("handoff", "RESERVED", lambda s, op, g: s.handoff(op, "owner-1", g)),
        ("mark_running", "HANDOFF_ACCEPTED", lambda s, op, g: s.mark_running(op, "owner-1", g)),
        ("renew", "RESERVED", lambda s, op, g: s.renew(op, "owner-1", g, 0, 10)),
        ("finish", "RUNNING", lambda s, op, g: s.finish(op, "owner-1", g, H2, True)),
        ("unknown", "RUNNING", lambda s, op, g: s.unknown(op, "owner-1", g)),
        ("release_proven_unstarted", "HANDOFF_ACCEPTED",
         lambda s, op, g: s.release_proven_unstarted(op, "owner-1", g, evidence_hash=H1)),
        ("cancel_unstarted", "RESERVED", lambda s, op, g: s.cancel_unstarted(op, g))):
    case(name + "_generation", state, "E_FENCED", call, lambda op: GENERATIONS, one)

# 2. Operation ids: an exact str, else E_OPERATION_UNKNOWN (fixture_verdict: E_VERDICT_CANDIDATE).
for name, state, code, call in (
        ("handoff", "RESERVED", "E_OPERATION_UNKNOWN", lambda s, op, v: s.handoff(v, "owner-1", 1)),
        ("mark_running", "HANDOFF_ACCEPTED", "E_OPERATION_UNKNOWN", lambda s, op, v: s.mark_running(v, "owner-1", 1)),
        ("renew", "RESERVED", "E_OPERATION_UNKNOWN", lambda s, op, v: s.renew(v, "owner-1", 1, 0, 10)),
        ("finish", "RUNNING", "E_OPERATION_UNKNOWN", lambda s, op, v: s.finish(v, "owner-1", 1, H2, True)),
        ("unknown", "RUNNING", "E_OPERATION_UNKNOWN", lambda s, op, v: s.unknown(v, "owner-1", 1)),
        ("release_proven_unstarted", "HANDOFF_ACCEPTED", "E_OPERATION_UNKNOWN",
         lambda s, op, v: s.release_proven_unstarted(v, "owner-1", 1, evidence_hash=H1)),
        ("cancel_unstarted", "RESERVED", "E_OPERATION_UNKNOWN", lambda s, op, v: s.cancel_unstarted(v, 1)),
        ("observe", "RUNNING", "E_OPERATION_UNKNOWN", lambda s, op, v: s.observe(v, "tokens", "call-1", 1, 5, True, H1)),
        ("settle", "RUNNING", "E_OPERATION_UNKNOWN", lambda s, op, v: s.settle(v, "tokens", amount=5)),
        ("settle_mapping", "RUNNING", "E_OPERATION_UNKNOWN", lambda s, op, v: s.settle(v, {"tokens": 5})),
        ("recover", "FENCED", "E_OPERATION_UNKNOWN",
         lambda s, op, v: s.recover(v, "owner-2", 2, outcome="NOT_STARTED", evidence_hash=H1)),
        ("fixture_verdict", "REVIEW_PENDING", "E_VERDICT_CANDIDATE", lambda s, op, v: s.fixture_verdict(v, H2, True, H3))):
    case(name + "_operation", state, code, call, lambda op: foreign(op) + [[op]], same)

# 3. Owners: an exact str, else E_FENCED.
for name, state, call in (
        ("handoff", "RESERVED", lambda s, op, w: s.handoff(op, w, 1)),
        ("mark_running", "HANDOFF_ACCEPTED", lambda s, op, w: s.mark_running(op, w, 1)),
        ("renew", "RESERVED", lambda s, op, w: s.renew(op, w, 1, 0, 10)),
        ("finish", "RUNNING", lambda s, op, w: s.finish(op, w, 1, H2, True)),
        ("unknown", "RUNNING", lambda s, op, w: s.unknown(op, w, 1)),
        ("release_proven_unstarted", "HANDOFF_ACCEPTED",
         lambda s, op, w: s.release_proven_unstarted(op, w, 1, evidence_hash=H1))):
    case(name + "_owner", state, "E_FENCED", call, lambda op: foreign("owner-1"), lambda op: "owner-1")
case("recover_owner", "FENCED", "E_FENCED",
     lambda s, op, w: s.recover(op, w, 2, outcome="NOT_STARTED", evidence_hash=H1),
     lambda op: foreign("owner-2") + [5, None], lambda op: "owner-2")

# 4. claim: request and task (E_SCOPE_UNKNOWN), key and owner (E_CLAIM_SHAPE),
#    reservation keys (E_BUDGET_DIMENSION) and resource keys (E_RESOURCE_KEYS).
case("claim_request", "SEEDED", "E_SCOPE_UNKNOWN", lambda s, op, v: claim_with(request=v)(s),
     lambda op: foreign("req-1") + [["req-1"]], lambda op: "req-1")
case("claim_task", "SEEDED", "E_SCOPE_UNKNOWN", lambda s, op, v: claim_with(task=v)(s),
     lambda op: foreign("task-1") + [5], lambda op: "task-1")
case("claim_key", "SEEDED", "E_CLAIM_SHAPE", lambda s, op, v: claim_with(key=v)(s),
     lambda op: foreign("idem-1") + [5, None], lambda op: "idem-1")
case("claim_owner", "SEEDED", "E_CLAIM_SHAPE", lambda s, op, v: claim_with(owner=v)(s),
     lambda op: foreign("owner-1") + [5, None], lambda op: "owner-1")
case("claim_reservation_key", "SEEDED", "E_BUDGET_DIMENSION",
     lambda s, op, v: claim_with(reservation={"dispatches": 1, v: 40, "cost": 20})(s),
     lambda op: foreign("tokens"), lambda op: "tokens")
case("claim_resource_key", "SEEDED", "E_RESOURCE_KEYS", lambda s, op, v: claim_with(resources=[v])(s),
     lambda op: foreign("src/a.py"), lambda op: "src/a.py")

# 5. Seeding: request and repo (E_REQUEST_SHAPE), task (E_TASK_SHAPE), the request a task
#    belongs to (E_REQUEST_UNKNOWN), and budget keys (E_BUDGET_DIMENSION).
case("seed_request_request", "SEEDED", "E_REQUEST_SHAPE", lambda s, op, v: s.seed_request(v, "repo-2", CAPS),
     lambda op: foreign("req-2") + [5, None], lambda op: "req-2")
case("seed_request_repo", "SEEDED", "E_REQUEST_SHAPE", lambda s, op, v: s.seed_request("req-2", v, CAPS),
     lambda op: foreign("repo-2") + [None], lambda op: "repo-2")
case("seed_request_caps_key", "SEEDED", "E_BUDGET_DIMENSION",
     lambda s, op, v: s.seed_request("req-2", "repo-2", {"dispatches": 5, v: 100}),
     lambda op: foreign("tokens"), lambda op: "tokens")
case("seed_task_task", "SEEDED", "E_TASK_SHAPE", lambda s, op, v: s.seed_task("req-1", v, CAPS),
     lambda op: foreign("task-2") + [5, None], lambda op: "task-2")
case("seed_task_request", "SEEDED", "E_REQUEST_UNKNOWN", lambda s, op, v: s.seed_task(v, "task-2", CAPS),
     lambda op: foreign("req-1") + [["req-1"]], lambda op: "req-1")
case("seed_task_caps_key", "SEEDED", "E_BUDGET_DIMENSION",
     lambda s, op, v: s.seed_task("req-1", "task-2", {"dispatches": 5, v: 100, "cost": 50}),
     lambda op: foreign("tokens"), lambda op: "tokens")

# 6. Usage: dimension, call key and evidence hash are exact str, else E_OBSERVATION_SHAPE.
case("observe_dimension", "RUNNING", "E_OBSERVATION_SHAPE",
     lambda s, op, v: s.observe(op, v, "call-1", 1, 5, True, H1), lambda op: foreign("tokens") + [None],
     lambda op: "tokens")
case("observe_call_key", "RUNNING", "E_OBSERVATION_SHAPE",
     lambda s, op, v: s.observe(op, "tokens", v, 1, 5, True, H1), lambda op: foreign("call-1"), lambda op: "call-1")
case("observe_evidence_hash", "RUNNING", "E_OBSERVATION_SHAPE",
     lambda s, op, v: s.observe(op, "tokens", "call-1", 1, 5, True, v), lambda op: foreign(H1), lambda op: H1)
case("settle_dimension", "RUNNING", "E_OBSERVATION_SHAPE", lambda s, op, v: s.settle(op, v, amount=5),
     lambda op: foreign("tokens") + [None], lambda op: "tokens")
case("settle_call_key", "RUNNING", "E_OBSERVATION_SHAPE", lambda s, op, v: s.settle(op, "tokens", v, amount=5),
     lambda op: foreign("call-1"), lambda op: "call-1")
case("settle_evidence_hash", "RUNNING", "E_OBSERVATION_SHAPE",
     lambda s, op, v: s.settle(op, "tokens", amount=5, evidence_hash=v), lambda op: foreign(H1), lambda op: H1)
case("settle_mapping_key", "RUNNING", "E_OBSERVATION_SHAPE", lambda s, op, v: s.settle(op, {v: 5}),
     lambda op: foreign("tokens"), lambda op: "tokens")

# 6b. Exact ints beyond the JSON-safe range pass the type checks, but the event payload cannot be encoded:
#     canonical()'s E_JSON_INTEGER, raised before anything is recorded.
UNSAFE = lambda op: [2**53, 2**60]  # noqa: E731
case("observe_amount_unsafe", "RUNNING", "E_JSON_INTEGER",
     lambda s, op, v: s.observe(op, "tokens", "call-1", 1, v, True, H1), UNSAFE, lambda op: 5)
case("observe_revision_unsafe", "RUNNING", "E_JSON_INTEGER",
     lambda s, op, v: s.observe(op, "tokens", "call-1", v, 5, True, H1), UNSAFE, lambda op: 1)
case("seed_request_caps_unsafe", "SEEDED", "E_JSON_INTEGER",
     lambda s, op, v: s.seed_request("req-2", "repo-2", {"dispatches": v}), UNSAFE, lambda op: 5)

# 7. Candidates and verdict evidence.
case("finish_candidate_hash", "RUNNING", "E_CLEANUP_OR_CANDIDATE",
     lambda s, op, v: s.finish(op, "owner-1", 1, v, True), lambda op: foreign(H2), lambda op: H2)
case("fixture_verdict_candidate_hash", "REVIEW_PENDING", "E_VERDICT_CANDIDATE",
     lambda s, op, v: s.fixture_verdict(op, v, True, H3), lambda op: foreign(H2), lambda op: H2)
case("fixture_verdict_evidence_hash", "REVIEW_PENDING", "E_VERDICT_SHAPE",
     lambda s, op, v: s.fixture_verdict(op, H2, True, v), lambda op: foreign(H3) + [None, H3.encode()],
     lambda op: H3)

# 8. stop: the request (E_REQUEST_UNKNOWN) and a non-empty exact str reason (E_STOP_REASON).
case("stop_request", "RESERVED", "E_REQUEST_UNKNOWN", lambda s, op, v: s.stop(v),
     lambda op: foreign("req-1") + [["req-1"]], lambda op: "req-1")
case("stop_reason", "RESERVED", "E_STOP_REASON", lambda s, op, v: s.stop("req-1", v),
     lambda op: foreign("OPERATOR_STOP") + ["", None, 5], lambda op: "OPERATOR_STOP")


class SchedulerExactInputsOracle(unittest.TestCase):
    def assert_rejected_unchanged(self, name, state, code, call, bad, legal):
        _sample, sample_op = ledger_at(state)
        for index in range(len(bad(sample_op))):
            scheduler, op = ledger_at(state)  # a fresh ledger for every rejected value
            value = bad(op)[index]
            with self.subTest(value=repr(value), type=type(value).__name__):
                before = ledger(scheduler)
                with self.assertRaises(ContractError) as raised:
                    call(scheduler, op, value)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(ledger(scheduler), before, "a rejected call changed the ledger")
                # The same ledger still takes the legal call.
                call(scheduler, op, legal(op))

    def test_settle_mapping_records_all_or_nothing(self):
        for mapping, code in (({"tokens": 5, "cost": -1}, "E_OBSERVATION_SHAPE"),
                              ({"tokens": 5, "gpu": 1}, "E_OBSERVATION_SCOPE"),
                              ({"tokens": 5, "dispatches": 1}, "E_LOCAL_METRIC_RESERVED"),
                              ({"tokens": 5, Text("cost"): 2}, "E_OBSERVATION_SHAPE")):
            with self.subTest(mapping=repr(mapping)):
                scheduler, op = ledger_at("RUNNING")
                before = ledger(scheduler)
                with self.assertRaises(ContractError) as raised:
                    scheduler.settle(op, mapping)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(ledger(scheduler), before, "settle recorded part of a rejected mapping")
                self.assertEqual(scheduler.settle(op, {"tokens": 5, "cost": 2}), {"tokens": "APPLIED", "cost": "APPLIED"})
        # A later entry conflicting with usage recorded earlier rejects the whole mapping.
        scheduler, op = ledger_at("RUNNING")
        self.assertEqual(scheduler.settle(op, {"cost": 2}), {"cost": "APPLIED"})
        before = ledger(scheduler)
        with self.assertRaises(ContractError) as raised:
            scheduler.settle(op, {"tokens": 5, "cost": 3})
        self.assertEqual(raised.exception.code, "E_USAGE_CONFLICT")
        self.assertEqual(ledger(scheduler), before, "settle recorded part of a rejected mapping")

    def test_settle_mapping_success_is_observe_in_order(self):
        # tokens 120 exceeds the request limit: as with observe, the budget check runs after every entry.
        scheduler, op = ledger_at("RUNNING")
        start = len(scheduler.events)
        self.assertEqual(scheduler.settle(op, {"tokens": 120, "cost": 2}), {"tokens": "APPLIED", "cost": "APPLIED"})
        expected = []
        for dimension, value in (("tokens", 120), ("cost", 2)):
            expected.append(("usage.observed", op, {
                "dimension": dimension, "call_key": "local:%s:%s" % (op, dimension), "revision": 1, "amount": value,
                "final": True, "correction": False,
                "evidence_hash": digest({"operation": op, "dimension": dimension, "value": value})}))
            expected.append(("request.stop_observed", None, {"request_id": "req-1", "reason": "BUDGET_EXCEEDED"}))
        self.assertEqual([(e["type"], e["operation_id"], e["payload"]) for e in scheduler.events[start:]], expected)

    def test_recovered_event_names_the_new_owner(self):
        for outcome, extra, state in (("NOT_STARTED", {}, "NOT_STARTED"), ("START_UNKNOWN", {}, "START_UNKNOWN"),
                                      ("FINISHED", {"candidate_hash": H2}, "REVIEW_PENDING")):
            with self.subTest(outcome=outcome):
                scheduler, op = ledger_at("FENCED")
                scheduler.recover(op, "owner-2", 2, outcome=outcome, evidence_hash=H1, **extra)
                event = scheduler.events[-1]
                expected = {"from_generation": 1, "outcome": outcome, "evidence": H1, "state": state,
                            "owner": "owner-2"}
                if outcome == "FINISHED":
                    expected["candidate"] = H2
                self.assertEqual((event["type"], event["operation_id"], event["payload"]),
                                 ("scheduler.recovered", op, expected))
                self.assertEqual(scheduler._operations[op].owner, "owner-2")


def _make(name, state, code, call, bad, legal):
    def test(self):
        self.assert_rejected_unchanged(name, state, code, call, bad, legal)
    test.__name__ = "test_" + name
    return test


for _entry in CASES:
    setattr(SchedulerExactInputsOracle, "test_" + _entry[0], _make(*_entry))


if __name__ == "__main__":
    unittest.main()
