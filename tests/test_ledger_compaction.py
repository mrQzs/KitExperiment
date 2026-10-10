"""Executor tests for ledger-compaction (`.agent/design/ledger-compaction.md`).

The planner oracle compacts at every prefix of a scenario and compares the
result with a ledger that was never compacted; what that comparison cannot see
is everything only a *damaged* or *forged* ledger can show, and it is what this
file covers:

* the surface of the feature -- the format, the schema, the digest function --
  checked against values computed here with ``hashlib``, never against the
  module's own;
* the shape of the recorded state: exactly the seven keys, the field sets of the
  four private dataclasses, the order of every mapping, the run descriptor a
  handoff recorded, and the fact that a compaction loses no order;
* one rejection per rule of the state check and of the checkpoint row, each on
  its own copy of a ledger that opens, with only the one condition under test
  changed and the checkpoint digest recomputed, so that no other rule refuses
  the file first;
* the tail that follows a checkpoint, the log head an append and a compaction
  compare themselves against, and the failures of the compaction transaction;
* the command line, and a ledger reopened in another process.
"""
from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import itertools
import inspect
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh import cli, durable_ledger
from sentinelmesh.durable_ledger import (
    LEDGER_FORMAT,
    MUTATING_METHODS,
    DurableScheduler,
    LedgerError,
    checkpoint_sha256,
    create_ledger,
    open_ledger,
)
from sentinelmesh.scheduler import Scheduler, _Operation, _Request, _Task, _Usage

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = os.path.realpath(sys.executable)
NL = bytes([10])
STATE_KEYS = {"generation", "requests", "tasks", "operations", "idempotency", "resource_holds", "events"}
COMPACT_KEYS = {"seq", "last_sha", "sha256", "removed"}
CLI_KEYS = {"command", "ledger", "generation", "seq", "last_sha", "sha256", "removed"}
REQUEST_FIELDS = {field.name for field in dataclasses.fields(_Request)}
TASK_FIELDS = {field.name for field in dataclasses.fields(_Task)}
OPERATION_FIELDS = {field.name for field in dataclasses.fields(_Operation)}
USAGE_FIELDS = {field.name for field in dataclasses.fields(_Usage)}
CAPS = {"dispatches": 9, "tokens": 500, "cost": 200}
#: A reservation whose order is neither alphabetical nor the request's own: the
#: checkpoint has to keep it, and the read-only views have to show it again.
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
H1, H2 = "1" * 64, "2" * 64
#: How many calls the scenario makes: three requests, five tasks, three claims
#: and the calls in between.
CALLS = 18


class Descending:
    """Operation ids that come out in *descending* order.

    The Scheduler generates random ones, so only a patched generator can prove
    that a recorded order is not an alphabetical one.
    """

    def __init__(self):
        self.counter = itertools.count(0x1000 - 1, -1)

    def __call__(self) -> str:
        return "op-%032x" % next(self.counter)


def descending_ids():
    return mock.patch.object(Scheduler, "_new_operation_id", Descending())


def descriptor(operation: str, evidence_dir: str) -> dict:
    return {"spec": {"request_id": "req-c", "run_id": operation, "command": ["pi", "--mode", "json"], "cwd": "/w",
                     "environment_sha256": "a" * 64, "timeout_seconds": 1800},
            "evidence_dir": evidence_dir, "worktree_id": "wt-1"}


def scenario(ledger, compact_at=None):
    """Three requests seeded out of alphabetical order, three operations in claim order.

    ``compact_at`` compacts right after that call, the way the oracle's scenario
    does.  ``CALLS`` calls in all.
    """
    operations = []
    calls = [0]

    def call(method, *args, **kwargs):
        result = getattr(ledger, method)(*args, **kwargs)
        calls[0] += 1
        if calls[0] == compact_at:
            ledger.compact()
        return result

    call("seed_request", "req-c", "repo-c", CAPS, concurrency=5)
    call("seed_request", "req-a", "repo-a", CAPS, concurrency=2)
    call("seed_request", "req-b", "repo-b", CAPS)
    call("seed_task", "req-c", "task-z", CAPS, max_attempts=3)
    call("seed_task", "req-c", "task-m", CAPS, max_attempts=2)
    call("seed_task", "req-a", "task-k", CAPS, max_attempts=1)
    call("seed_task", "req-a", "task-b", CAPS, max_attempts=4)
    call("seed_task", "req-a", "task-n", CAPS, max_attempts=4)
    first = call("claim", "req-c", "task-z", 1, "idem-c1", "owner-1", 1, 1, RESERVATION,
                 ["src/z.py", "src/shared.py"])["operation_id"]
    operations.append(first)
    call("handoff", first, "owner-1", 1, 1, run=descriptor(first, "/evidence/z"))
    call("settle", first, {"tokens": 12, "cost": 3}, evidence_hash=H1)
    second = call("claim", "req-a", "task-k", 1, "idem-a1", "owner-1", 1, 1, RESERVATION,
                  ["src/k.py"])["operation_id"]
    operations.append(second)
    call("observe", second, "tokens", "call-k", 1, 7, False, H1)
    call("stop", "req-b", "OPERATOR_STOP")
    third = call("claim", "req-a", "task-b", 1, "idem-a2", "owner-1", 1, 1,
                 {"cost": 5, "dispatches": 1, "tokens": 10}, ["src/b.py"])["operation_id"]
    operations.append(third)
    call("handoff", third, "owner-1", 1, 2)
    call("mark_running", third, "owner-1", 1)
    call("finish", third, "owner-1", 1, H2, True)
    return operations


# --------------------------------------------------------------------------- #
# the file, read and written directly: nothing here calls the module under test
# --------------------------------------------------------------------------- #
def checkpoint_digest(seq: int, last_sha: str, state: str) -> str:
    return hashlib.sha256(str(seq).encode("ascii") + NL + last_sha.encode("ascii") + NL
                          + state.encode("utf-8")).hexdigest()


def entry_digest(previous: str, command: str) -> str:
    return hashlib.sha256(previous.encode("ascii") + NL + command.encode("utf-8")).hexdigest()


def query(path: str, sql: str, params=()) -> list:
    db = sqlite3.connect(path)
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


def checkpoint_of(path: str) -> list:
    return query(path, "SELECT seq, last_sha, state, sha256 FROM checkpoint ORDER BY seq")


def rows_of(path: str) -> list:
    return query(path, "SELECT seq, command, sha256, previous FROM commands ORDER BY seq")


def state_of(path: str) -> dict:
    (_seq, _last_sha, state, _digest), = checkpoint_of(path)
    return json.loads(state)


def execute(path: str, sql: str, params=()) -> None:
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(sql, params)
        db.execute("COMMIT")
    finally:
        db.close()


def rewrite_checkpoint(path: str, seq, last_sha, state, digest=None) -> None:
    """Replace the checkpoint row; the digest is recomputed unless one is given."""
    execute(path, "DELETE FROM checkpoint")
    execute(path, "INSERT INTO checkpoint(seq, last_sha, state, sha256) VALUES (?, ?, ?, ?)",
            (seq, last_sha, state, checkpoint_digest(seq, last_sha, state) if digest is None else digest))


def with_state(path: str, edit) -> None:
    """Re-encode the checkpoint's state after ``edit`` changed it, with a digest that verifies."""
    (seq, last_sha, state, _digest), = checkpoint_of(path)
    value = json.loads(state)
    edit(value)
    rewrite_checkpoint(path, seq, last_sha, json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def with_state_text(path: str, edit) -> None:
    """Rewrite the checkpoint's state *text* after ``edit``, with a digest that verifies."""
    (seq, last_sha, state, _digest), = checkpoint_of(path)
    rewrite_checkpoint(path, seq, last_sha, edit(state))


def append_rechained(path: str, command: str) -> None:
    """Append one command row after the current head, correctly chained."""
    rows = rows_of(path)
    if rows:
        seq, previous = rows[-1][0], rows[-1][2]
    else:
        (seq, last_sha, _state, _digest), = checkpoint_of(path)
        previous = last_sha
    execute(path, "INSERT INTO commands(seq, command, sha256, previous) VALUES (?, ?, ?, ?)",
            (seq + 1, command, entry_digest(previous, command), previous))


def rename_operation(value: dict, old_key: str, new_key: str, inner: bool) -> None:
    """Record one operation under ``new_key``; ``inner`` renames its own id too.

    The entries that name it follow, so only the check under test is left to fire.
    """
    value["operations"] = {(new_key if key == old_key else key): fields
                           for key, fields in value["operations"].items()}
    if inner:
        value["operations"][new_key]["operation_id"] = new_key
    for entry in value["idempotency"] + value["resource_holds"]:
        if entry[2] == old_key:
            entry[2] = new_key


def edit_field(path):
    """An edit that sets one field of the state, addressed by a path of keys and indices."""

    def edit(value):
        target = value
        for part in path[:-1]:
            target = target[part]
        target[path[-1][0]] = path[-1][1]

    return edit


def smctl(*argv):
    """Run ``smctl`` in this process: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main([str(argument) for argument in argv])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class CompactionCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="ledger-compaction-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.path = str(self.work / "ledger.db")

    def code_of(self, error_type, func, *args, **kwargs):
        with self.assertRaises(error_type) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def copy(self, name, source=None):
        target = str(self.work / name)
        shutil.copyfile(source or self.path, target)
        return target

    def refuses(self, source, label, code, damage):
        """A copy of ``source`` that opens, is damaged in one place, and is then refused.

        The legal baseline comes first: a copy nobody damaged has to open, so the
        refusal that follows is the damage's own and not the file's.  The bytes
        are compared as well: a refused open repairs nothing.
        """
        path = self.copy("refused-%s.db" % re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-"), source)
        open_ledger(path).close()
        damage(path)
        before = Path(path).read_bytes()
        self.assertEqual(self.code_of(LedgerError, open_ledger, path), code, label)
        self.assertEqual(Path(path).read_bytes(), before, "a refused open changed the file")

    def compacted(self, name, at=None, generation=1):
        """A ledger that ran the whole scenario, compacted right after call ``at``."""
        path = str(self.work / name)
        ledger = create_ledger(path, generation=generation)
        operations = scenario(ledger, compact_at=at)
        ledger.close()
        return path, operations


class CompactionSurface(CompactionCase):
    def test_the_format_the_schema_and_the_digest(self):
        self.assertEqual(LEDGER_FORMAT, "sentinelmesh-ledger-v2")
        self.assertNotIn("compact", MUTATING_METHODS)
        self.assertEqual(list(inspect.signature(DurableScheduler.compact).parameters), ["self"])
        ledger = create_ledger(self.path)
        ledger.close()
        self.assertEqual([row[0] for row in query(self.path, "SELECT name FROM sqlite_master WHERE type='table'"
                                                             " ORDER BY name")],
                         ["checkpoint", "commands", "meta"])
        self.assertEqual([row[1] for row in query(self.path, "PRAGMA table_info(checkpoint)")],
                         ["seq", "last_sha", "state", "sha256"])
        self.assertEqual(query(self.path, "SELECT key, value FROM meta"),
                         [("format", "sentinelmesh-ledger-v2"), ("generation", "1")])
        self.assertEqual(checkpoint_of(self.path), [])

    def test_the_checkpoint_digest_covers_every_other_column(self):
        # Computed here, not by the module under test.
        self.assertEqual(checkpoint_sha256(17, "a" * 64, '{"generation":1}'),
                         hashlib.sha256(b"17\n" + b"a" * 64 + b"\n" + b'{"generation":1}').hexdigest())
        self.assertEqual(checkpoint_sha256(0, "", "[]"), hashlib.sha256(b"0\n\n[]").hexdigest())
        self.assertNotEqual(checkpoint_sha256(17, "a" * 64, '{"generation":1}'),
                            checkpoint_sha256(18, "a" * 64, '{"generation":1}'))
        self.assertNotEqual(checkpoint_sha256(17, "a" * 64, '{"generation":1}'),
                            checkpoint_sha256(17, "b" * 64, '{"generation":1}'))
        self.assertNotEqual(checkpoint_sha256(17, "a" * 64, '{"generation":1}'),
                            checkpoint_sha256(17, "a" * 64, '{"generation":2}'))


class StateShape(CompactionCase):
    def test_the_recorded_state_has_exactly_the_seven_keys_and_keeps_every_order(self):
        path = str(self.work / "shape.db")
        with descending_ids():
            ledger = create_ledger(path)
            operations = scenario(ledger)
            ledger.compact()
            ledger.close()
        recorded = state_of(path)
        self.assertEqual(set(recorded), STATE_KEYS)
        self.assertEqual(list(recorded), ["generation", "requests", "tasks", "operations", "idempotency",
                                          "resource_holds", "events"])
        # Seeding order, not an alphabetical one.
        self.assertEqual(list(recorded["requests"]), ["req-c", "req-a", "req-b"])
        self.assertEqual([set(fields) for fields in recorded["requests"].values()], [REQUEST_FIELDS] * 3)
        # Claim order, and the ids are not sorted.
        self.assertEqual(list(recorded["operations"]), operations)
        self.assertNotEqual(list(recorded["operations"]), sorted(operations))
        self.assertEqual(sorted(recorded["operations"]), sorted(operations))
        for fields in recorded["operations"].values():
            self.assertEqual(set(fields), OPERATION_FIELDS)
            self.assertEqual(len(fields), 17)
            for observed in fields["usage"].values():
                self.assertEqual(set(observed), USAGE_FIELDS)
                self.assertEqual(len(observed), 5)
        self.assertEqual([len(entry) for entry in recorded["tasks"]], [3, 3, 3, 3, 3])
        self.assertEqual([len(entry) for entry in recorded["idempotency"]], [3, 3, 3])
        self.assertEqual([len(entry) for entry in recorded["resource_holds"]], [3, 3, 3])
        # The three triples, the way the state writes them.
        self.assertEqual([entry[:2] for entry in recorded["tasks"]],
                         [["req-c", "task-z"], ["req-c", "task-m"], ["req-a", "task-k"],
                          ["req-a", "task-b"], ["req-a", "task-n"]])
        self.assertEqual([entry[0] for entry in recorded["idempotency"]], ["req-c", "req-a", "req-a"])
        self.assertEqual([entry[0] for entry in recorded["resource_holds"]], ["repo-c", "repo-c", "repo-a"])
        # The Scheduler keeps the resource keys sorted, and the holds follow.
        self.assertEqual([entry[1] for entry in recorded["resource_holds"]],
                         ["src/shared.py", "src/z.py", "src/k.py"])
        self.assertEqual([entry[2] for entry in recorded["resource_holds"]],
                         [operations[0], operations[0], operations[1]])

    def test_the_events_and_the_run_descriptor_travel_with_the_checkpoint(self):
        ledger = create_ledger(self.path)
        operations = scenario(ledger)
        expected_events = list(ledger.events)
        expected_run = ledger.run_descriptor(operations[0])
        ledger.compact()
        ledger.close()
        recorded = state_of(self.path)
        self.assertEqual(recorded["events"], expected_events)
        self.assertEqual(recorded["operations"][operations[0]]["run"], expected_run)
        self.assertIsNone(recorded["operations"][operations[1]]["run"])
        # The dispatch the handoff recorded is usage like any other.
        self.assertEqual(list(recorded["operations"][operations[0]]["usage"]),
                         ["dispatches", "tokens", "cost"])

    def test_no_order_is_lost_through_a_checkpoint(self):
        ledger = create_ledger(self.path)
        operations = scenario(ledger)
        before = {"requests": list(ledger.request_ids()), "operations": list(ledger.operation_ids()),
                  "reservations": [list(ledger.operation(op)["reservation"]) for op in operations],
                  "usage": [list(ledger.operation_usage(op)) for op in operations],
                  "reasons": list(ledger.stop_reasons("req-b")), "events": len(ledger.events),
                  "snapshot": [list(ledger.snapshot(request)) for request in ledger.request_ids()]}
        self.assertEqual(before["reservations"][0], ["dispatches", "tokens", "cost"])
        ledger.compact()
        self.assertEqual((ledger.seq, len(ledger.events), ledger.generation), (CALLS, before["events"], 1))
        self.assertEqual(list(ledger.stop_reasons("req-b")), before["reasons"])
        ledger.close()
        reopened = open_ledger(self.path)
        after = {"requests": list(reopened.request_ids()), "operations": list(reopened.operation_ids()),
                 "reservations": [list(reopened.operation(op)["reservation"]) for op in operations],
                 "usage": [list(reopened.operation_usage(op)) for op in operations],
                 "reasons": list(reopened.stop_reasons("req-b")), "events": len(reopened.events),
                 "snapshot": [list(reopened.snapshot(request)) for request in reopened.request_ids()]}
        self.assertEqual(after, before)
        self.assertEqual(reopened.seq, CALLS)
        reopened.close()


class CompactReturns(CompactionCase):
    def test_the_result_and_what_it_leaves_alone(self):
        ledger = create_ledger(self.path)
        operations = scenario(ledger)
        events, generation = len(ledger.events), ledger.generation
        last_sha = rows_of(self.path)[-1][2]
        result = ledger.compact()
        self.assertEqual(set(result), COMPACT_KEYS)
        self.assertEqual((result["seq"], result["last_sha"], result["removed"]), (CALLS, last_sha, CALLS))
        self.assertRegex(result["last_sha"], r"\A[0-9a-f]{64}\Z")
        # A second compaction of the same state records the same digest and removes nothing.
        again = ledger.compact()
        self.assertEqual(again, {**result, "removed": 0})
        self.assertEqual(len(checkpoint_of(self.path)), 1)
        # Three more commands, and the next compaction removes exactly those three.  A
        # ``seed_task`` records no event, so only the stop adds one.
        ledger.seed_task("req-c", "task-x", CAPS)
        ledger.seed_task("req-c", "task-y", CAPS)
        ledger.stop("req-c", "OPERATOR_STOP")
        self.assertEqual(ledger.compact()["removed"], 3)
        self.assertEqual((ledger.seq, len(ledger.events), ledger.generation), (CALLS + 3, events + 1, generation))
        self.assertEqual(ledger.operation_ids(), tuple(operations))
        ledger.close()

    def test_an_empty_ledger_compacts_to_sequence_zero(self):
        ledger = create_ledger(self.path)
        result = ledger.compact()
        self.assertEqual(set(result), COMPACT_KEYS)
        self.assertEqual((result["seq"], result["last_sha"], result["removed"]), (0, "", 0))
        (seq, last_sha, _state, digest), = checkpoint_of(self.path)
        self.assertEqual((seq, last_sha, digest), (0, "", result["sha256"]))
        self.assertEqual(state_of(self.path)["events"], [])
        self.assertEqual(rows_of(self.path), [])
        ledger.seed_request("req-1", "repo-1", CAPS)
        self.assertEqual(ledger.compact()["removed"], 1)
        ledger.close()
        reopened = open_ledger(self.path)
        self.assertEqual((reopened.seq, reopened.request_ids()), (1, ("req-1",)))
        reopened.close()


class CheckpointRefusals(CompactionCase):
    """Every rule of the checkpoint row itself."""

    def test_a_second_row_a_wrong_digest_and_a_bad_shape_are_refused(self):
        path, _operations = self.compacted("row.db", at=CALLS)
        (seq, last_sha, state, _digest), = checkpoint_of(path)
        self.refuses(path, "two checkpoints", "E_LEDGER_CHECKPOINT",
                     lambda p: execute(p, "INSERT INTO checkpoint SELECT seq + 1, last_sha, state, sha256 "
                                          "FROM checkpoint"))
        self.refuses(path, "a digest that does not verify", "E_LEDGER_CHECKPOINT",
                     lambda p: execute(p, "UPDATE checkpoint SET sha256=?", ("0" * 64,)))
        self.refuses(path, "a state touched after the digest", "E_LEDGER_CHECKPOINT",
                     lambda p: execute(p, "UPDATE checkpoint SET state = state || ' '"))
        self.refuses(path, "a negative sequence number", "E_LEDGER_CHECKPOINT",
                     lambda p: rewrite_checkpoint(p, -1, last_sha, state))
        self.refuses(path, "a link at sequence zero", "E_LEDGER_CHECKPOINT",
                     lambda p: rewrite_checkpoint(p, 0, "a" * 64, state))
        self.refuses(path, "an empty link above sequence zero", "E_LEDGER_CHECKPOINT",
                     lambda p: rewrite_checkpoint(p, seq, "", state))
        self.refuses(path, "a link that is not a digest", "E_LEDGER_CHECKPOINT",
                     lambda p: rewrite_checkpoint(p, seq, "z" * 64, state))
        # A TEXT column turns the integer into text, so it is the digest that
        # refuses this one; a blob is the other way into a non-text state, and it
        # has no digest that could verify at all.
        self.refuses(path, "a state that is not text", "E_LEDGER_CHECKPOINT",
                     lambda p: execute(p, "UPDATE checkpoint SET state=1"))
        self.refuses(path, "a state that is a blob", "E_LEDGER_CHECKPOINT",
                     lambda p: execute(p, "UPDATE checkpoint SET state=x'0000'"))


class StateRefusals(CompactionCase):
    """One rejection per rule of the state check, each on its own copy."""

    def setUp(self):
        super().setUp()
        self.path, self.operations = self.compacted("state.db", at=CALLS)
        self.first, self.second, self.third = self.operations

    def test_the_state_itself(self):
        source = self.path
        self.refuses(source, "a state that is not an object", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state_text(p, lambda text: "[]"))
        self.refuses(source, "a key the state does not have", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field([("extra", 1)])))
        self.refuses(source, "a key missing from the state", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: value.pop("events")))
        self.refuses(source, "a duplicate key", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state_text(p, lambda text: text.replace('{"generation":',
                                                                             '{"generation":2,"generation":', 1)))
        self.refuses(source, "a generation below the stored one", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field([("generation", 0)])))

    def test_the_generation(self):
        # A ledger of a later generation: the checkpoint may not name an older one.
        path = str(self.work / "later.db")
        ledger = create_ledger(path, generation=5)
        ledger.seed_request("req-1", "repo-1", CAPS)
        ledger.compact()
        ledger.close()
        self.refuses(path, "a generation below the stored one", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field([("generation", 4)])))
        source = self.path
        self.refuses(source, "a generation the Scheduler refuses", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field([("generation", 2 ** 53)])))
        self.refuses(source, "a generation that is a boolean", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field([("generation", True)])))

    def test_every_dataclass_keeps_its_own_fields(self):
        source = self.path
        self.refuses(source, "a request with a field it does not have", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["requests", "req-c", ("extra", 1)])))
        self.refuses(source, "a request missing a field", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: value["requests"]["req-c"].pop("max_concurrency")))
        self.refuses(source, "a task with a field it does not have", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["tasks", 0, 2, ("extra", 1)])))
        self.refuses(source, "a task missing a field", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: value["tasks"][0][2].pop("guard_version")))
        self.refuses(source, "an operation with a field it does not have", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["operations", self.first, ("extra", 1)])))
        self.refuses(source, "an operation missing a field", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: value["operations"][self.first].pop("started")))
        self.refuses(source, "an observation with a field it does not have", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["operations", self.first, "usage", "tokens",
                                                         ("extra", 1)])))
        self.refuses(source, "an observation missing a field", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: value["operations"][self.first]["usage"]
                                          ["tokens"].pop("final")))

    def test_the_types_of_every_field(self):
        source = self.path
        cases = [
            ("a cap recorded as a float", ["requests", "req-c", "caps", ("tokens", 1.0)]),
            ("a max concurrency that is a boolean", ["requests", "req-c", ("max_concurrency", True)]),
            ("a stop recorded as an integer", ["requests", "req-c", ("stopped", 0)]),
            ("a stop reason that is null", ["requests", "req-b", "stop_reasons", (0, None)]),
            ("a max attempts that is a boolean", ["tasks", 0, 2, ("max_attempts", True)]),
            ("a fixture eligibility recorded as an integer", ["tasks", 0, 2, ("fixture_eligible", 1)]),
            ("a plan version that is a boolean", ["operations", self.first, ("plan_version", True)]),
            ("a lease that is a float", ["operations", self.first, ("lease_until", 1.5)]),
            ("an owner that is an integer", ["operations", self.first, ("owner", 7)]),
            ("a reservation that is a float", ["operations", self.first, "reservation", ("tokens", 40.5)]),
            ("a resource key that is an integer", ["operations", self.first, "resource_keys", (0, 1)]),
            ("a candidate hash recorded as an integer", ["operations", self.third, ("candidate_hash", 0)]),
            ("a started recorded as an integer", ["operations", self.first, ("started", 1)]),
            ("a run descriptor recorded as an array", ["operations", self.first, ("run", [])]),
            ("a finality recorded as an integer", ["operations", self.first, "usage", "tokens", ("final", 1)]),
            ("an amount recorded as text", ["operations", self.first, "usage", "tokens", ("amount", "12")]),
            ("a revision that is a boolean", ["operations", self.first, "usage", "tokens", ("revision", True)]),
            ("a call key that is null", ["operations", self.first, "usage", "tokens", ("call_key", None)]),
            ("a request that is not an object", ["requests", ("req-c", 1)]),
            ("a task list that is an object", [("tasks", {})]),
            ("an operation list that is an array", [("operations", [])]),
            ("an idempotency list that is an object", [("idempotency", {})]),
            ("a resource hold list that is an object", [("resource_holds", {})]),
            ("an event list that is an object", [("events", {})]),
            ("a usage mapping that is an array", ["operations", self.first, ("usage", [])]),
        ]
        for label, path_in_state in cases:
            with self.subTest(case=label):
                self.refuses(source, label, "E_LEDGER_CHECKPOINT",
                             lambda path: with_state(path, edit_field(path_in_state)))

    def test_the_operation_itself(self):
        source = self.path
        self.refuses(source, "a state the Scheduler never records", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["operations", self.first, ("state", "DONE")])))
        self.refuses(source, "an operation under another id", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: rename_operation(value, self.first,
                                                                            "op-" + "e" * 32, inner=False)))
        self.refuses(source, "an operation id of another shape", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: rename_operation(value, self.first, "op-x", inner=True)))
        self.refuses(source, "an operation naming an unknown task", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["operations", self.first, ("task_id", "task-9")])))

    def test_the_tasks(self):
        source = self.path
        self.refuses(source, "a task naming an unknown request", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["tasks", 0, (0, "req-9")])))
        self.refuses(source, "a task recorded twice", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: value["tasks"].append(list(value["tasks"][0]))))
        self.refuses(source, "a task entry that is not a triple", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: value["tasks"][0].pop()))

    def test_the_idempotency_entries(self):
        source = self.path
        self.refuses(source, "an idempotency entry naming an unknown operation", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["idempotency", 0, (2, "op-" + "f" * 32)])))
        self.refuses(source, "an idempotency entry with another key", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["idempotency", 0, (1, "idem-z")])))
        self.refuses(source, "an idempotency entry of another request", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["idempotency", 0, (0, "req-a")])))
        self.refuses(source, "an idempotency key recorded twice", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: value["idempotency"].append(list(value["idempotency"][0]))))
        self.refuses(source, "an operation without an idempotency entry", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: value["idempotency"].pop(0)))
        self.refuses(source, "an idempotency entry that is not a triple", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: value["idempotency"][0].pop()))
        self.refuses(source, "an idempotency entry that is not text", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["idempotency", 0, (1, 7)])))

    def test_the_resource_holds(self):
        source = self.path
        self.refuses(source, "a resource hold naming an unknown operation", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["resource_holds", 0, (2, "op-" + "f" * 32)])))
        self.refuses(source, "a resource hold its operation does not hold", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["resource_holds", 0, (1, "src/q.py")])))
        self.refuses(source, "a resource hold of another repository", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["resource_holds", 0, (0, "repo-a")])))
        self.refuses(source, "a resource held twice", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: value["resource_holds"].append(
                         list(value["resource_holds"][0]))))
        self.refuses(source, "a resource hold that is not a triple", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: value["resource_holds"][0].pop()))
        self.refuses(source, "a resource that is not text", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["resource_holds", 0, (1, 7)])))

    def test_the_events(self):
        source = self.path
        self.refuses(source, "events numbered from two", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["events", 0, ("seq", 2)])))
        self.refuses(source, "an event without its keys", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, lambda value: value["events"][0].pop("payload")))
        self.refuses(source, "an event with a key it does not have", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["events", 0, ("extra", 1)])))
        self.refuses(source, "an event type that is not text", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["events", 0, ("type", 7)])))
        self.refuses(source, "an event generation that is a boolean", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["events", 0, ("generation", True)])))
        self.refuses(source, "an event operation id that is an integer", "E_LEDGER_CHECKPOINT",
                     lambda p: with_state(p, edit_field(["events", 1, ("operation_id", 7)])))


class TheTail(CompactionCase):
    """The commands after a checkpoint, and what the log head is."""

    def test_the_tail_has_to_start_where_the_checkpoint_ends(self):
        # Compacted at the call before the last, so exactly one command follows.
        path, _operations = self.compacted("tail.db", at=CALLS - 1)
        (seq, last_sha, _state, _digest), = checkpoint_of(path)
        self.assertEqual((seq, [row[0] for row in rows_of(path)]), (CALLS - 1, [CALLS]))
        self.refuses(path, "a tail that starts one command late", "E_LEDGER_CHAIN",
                     lambda p: execute(p, "UPDATE commands SET seq=? WHERE seq=?", (seq + 2, seq + 1)))
        self.refuses(path, "a tail that starts one command early", "E_LEDGER_CHAIN",
                     lambda p: execute(p, "UPDATE commands SET seq=? WHERE seq=?", (seq, seq + 1)))
        # Relinked on purpose: the row is consistent with itself, and only the
        # link to the checkpoint is wrong.
        self.refuses(path, "a tail that does not link to the checkpoint", "E_LEDGER_CHAIN",
                     lambda p: execute(p, "UPDATE commands SET previous=?, sha256=? WHERE seq=?",
                                       ("f" * 64, entry_digest("f" * 64, rows_of(p)[0][1]), seq + 1)))
        self.refuses(path, "a tail command the checkpoint already covers", "E_LEDGER_CHAIN",
                     lambda p: execute(p, "INSERT INTO commands(seq, command, sha256, previous) "
                                          "VALUES (?, '{}', '', '')", (seq,)))

    def test_a_chain_that_breaks_in_the_middle_of_the_tail(self):
        path, _operations = self.compacted("broken.db", at=10)
        (seq, _last_sha, _state, _digest), = checkpoint_of(path)
        self.assertGreater(len(rows_of(path)), 2)
        # The row's own digest is replaced: it no longer is the digest of itself,
        # and the row after it no longer links to it.
        self.refuses(path, "a tail command with another digest", "E_LEDGER_CHAIN",
                     lambda p: execute(p, "UPDATE commands SET sha256=? WHERE seq=?", ("0" * 64, seq + 2)))

    def test_a_stored_generation_the_scheduler_refuses_is_a_damaged_ledger(self):
        path, _operations = self.compacted("generation.db", at=6)
        # Not E_LEDGER_CHECKPOINT: the meta generation is checked before the
        # checkpoint is even looked at, and it is the file that is wrong.
        self.refuses(path, "a stored generation of zero", "E_LEDGER_FORMAT",
                     lambda p: execute(p, "UPDATE meta SET value='0' WHERE key='generation'"))

    def test_a_tail_claim_reusing_a_checkpointed_operation_id_is_a_forgery(self):
        path, operations = self.compacted("forged.db", at=CALLS)
        # A claim that is legal on its own -- an unclaimed task of a request that is
        # not stopped, with a fresh idempotency key -- but for the operation id it
        # carries, which the checkpoint already holds.
        claim = json.dumps({"method": "claim",
                            "arguments": {"request": "req-c", "task": "task-m", "plan": 1, "key": "idem-c9",
                                          "owner": "owner-1", "generation": 1, "guard_version": 1,
                                          "reservation": {"cost": 1, "dispatches": 1, "tokens": 1},
                                          "resource_keys": ["src/m.py"],
                                          "now": 0, "lease": 30, "fail_before_commit": False},
                            "operation_id": operations[0]}, separators=(",", ":"))
        self.refuses(path, "a tail claim reusing a checkpointed operation id", "E_LEDGER_REPLAY",
                     lambda p: append_rechained(p, claim))
        # The control: only the reused id made it a forgery.
        control = self.copy("forged-control.db", path)
        append_rechained(control, claim.replace(operations[0], "op-" + "f" * 32))
        reopened = open_ledger(control)
        self.assertEqual(reopened.operation_ids(), (*operations, "op-" + "f" * 32))
        self.assertEqual((reopened.operation("op-" + "f" * 32)["task_id"], reopened.seq), ("task-m", CALLS + 1))
        reopened.close()


class TheLogHead(CompactionCase):
    """What an append and a compaction compare themselves against."""

    def test_another_instance_compacting_leaves_this_one_current(self):
        ledger = create_ledger(self.path)
        operations = scenario(ledger)
        other = open_ledger(self.path)
        self.assertEqual(other.seq, CALLS)
        result = other.compact()
        self.assertEqual(result["removed"], CALLS)
        (seq, last_sha, _state, _digest), = checkpoint_of(self.path)
        self.assertEqual((seq, last_sha), (CALLS, result["last_sha"]))
        self.assertEqual(rows_of(self.path), [])
        # The identity of the log did not move, so this instance is still current.
        ledger.stop("req-c", "OPERATOR_STOP")
        self.assertEqual(ledger.seq, CALLS + 1)
        self.assertEqual([(row[0], row[3]) for row in rows_of(self.path)], [(CALLS + 1, last_sha)])
        other.close()
        ledger.close()
        reopened = open_ledger(self.path)
        self.assertEqual((reopened.seq, reopened.stop_reasons("req-c")), (CALLS + 1, ("OPERATOR_STOP",)))
        self.assertEqual(reopened.operation_ids(), tuple(operations))
        reopened.close()

    def test_another_instance_appending_makes_this_one_stale(self):
        ledger = create_ledger(self.path)
        scenario(ledger)
        other = open_ledger(self.path)
        other.compact()
        other.stop("req-c", "OPERATOR_STOP")
        other.close()
        self.assertEqual(self.code_of(LedgerError, ledger.stop, "req-c", "another"), "E_LEDGER_STALE")
        self.assertEqual(self.code_of(LedgerError, lambda: ledger.seq), "E_LEDGER_UNAVAILABLE")
        ledger.close()

    def test_a_stale_instance_may_not_compact(self):
        first = create_ledger(self.path)
        first.seed_request("req-1", "repo-1", CAPS)
        second = open_ledger(self.path)
        second.seed_task("req-1", "task-1", CAPS)
        self.assertEqual(self.code_of(LedgerError, first.compact), "E_LEDGER_STALE")
        self.assertEqual(self.code_of(LedgerError, lambda: first.seq), "E_LEDGER_UNAVAILABLE")
        self.assertEqual(checkpoint_of(self.path), [], "a stale instance wrote a checkpoint")
        self.assertEqual(len(rows_of(self.path)), 2)
        # The current instance compacts, and compacts again after it appended.
        self.assertEqual(second.compact()["removed"], 2)
        second.seed_task("req-1", "task-2", CAPS)
        self.assertEqual(second.compact()["removed"], 1)
        self.assertEqual(checkpoint_of(self.path)[0][0], 3)
        first.close()
        second.close()


class FailedCompactions(CompactionCase):
    def test_a_closed_instance_refuses_compaction(self):
        ledger = create_ledger(self.path)
        scenario(ledger)
        ledger.close()
        self.assertEqual(self.code_of(LedgerError, ledger.compact), "E_LEDGER_UNAVAILABLE")
        self.assertEqual(checkpoint_of(self.path), [])

    def test_a_failing_statement_invalidates_the_instance_and_changes_nothing(self):
        real_connect = sqlite3.connect

        class FailingAt:
            """A real connection whose statement starting with ``prefix`` fails, once armed."""

            def __init__(self, real, prefix, armed):
                self.real, self.prefix, self.armed = real, prefix, armed

            def execute(self, sql, *args):
                if self.armed and sql.lstrip().upper().startswith(self.prefix):
                    raise sqlite3.OperationalError("disk I/O error")
                return self.real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.real, name)

        for index, prefix in enumerate(("INSERT INTO CHECKPOINT", "DELETE FROM COMMANDS", "COMMIT")):
            with self.subTest(failing=prefix):
                path = str(self.work / ("failing-%d.db" % index))
                armed = []

                def connect(location, *args, **kwargs):
                    return FailingAt(real_connect(location, *args, **kwargs), prefix, armed)

                with mock.patch.object(durable_ledger.sqlite3, "connect", connect):
                    ledger = create_ledger(path)
                    operations = scenario(ledger)
                    armed.append(True)
                    self.assertEqual(self.code_of(LedgerError, ledger.compact), "E_LEDGER_WRITE")
                self.assertEqual(self.code_of(LedgerError, lambda: ledger.seq), "E_LEDGER_UNAVAILABLE")
                self.assertEqual(self.code_of(LedgerError, ledger.compact), "E_LEDGER_UNAVAILABLE")
                ledger.close()
                self.assertEqual(checkpoint_of(path), [], "a failed compaction left a checkpoint")
                self.assertEqual(len(rows_of(path)), CALLS, "a failed compaction removed commands")
                reopened = open_ledger(path)
                self.assertEqual(reopened.operation_ids(), tuple(operations))
                reopened.close()

    def test_an_unencodable_state_invalidates_the_instance_and_changes_nothing(self):
        ledger = create_ledger(self.path)
        operations = scenario(ledger)
        with mock.patch.object(durable_ledger, "_encode_state", side_effect=ValueError("unencodable")):
            self.assertEqual(self.code_of(LedgerError, ledger.compact), "E_LEDGER_WRITE")
        self.assertEqual(self.code_of(LedgerError, lambda: ledger.seq), "E_LEDGER_UNAVAILABLE")
        ledger.close()
        self.assertEqual((checkpoint_of(self.path), len(rows_of(self.path))), ([], CALLS))
        reopened = open_ledger(self.path)
        self.assertEqual(reopened.operation_ids(), tuple(operations))
        reopened.close()


class TheCommandLine(CompactionCase):
    def test_the_compact_command(self):
        ledger = create_ledger(self.path)
        operations = scenario(ledger)
        ledger.close()
        code, status_before, err = smctl("ledger", "status", self.path)
        self.assertEqual((code, err), (0, ""))
        code, out, err = smctl("ledger", "compact", self.path)
        self.assertEqual((code, err), (0, ""), out)
        result = json.loads(out)
        self.assertEqual(set(result), CLI_KEYS)
        (_seq, last_sha, _state, digest), = checkpoint_of(self.path)
        self.assertEqual(result, {"command": "compact", "ledger": self.path, "generation": 1, "seq": CALLS,
                                  "last_sha": last_sha, "sha256": digest, "removed": CALLS})
        self.assertEqual(rows_of(self.path), [])
        code, status_after, err = smctl("ledger", "status", self.path)
        self.assertEqual((code, err, status_after), (0, "", status_before), "status changed with the compaction")
        code, out, err = smctl("ledger", "compact", self.path)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {**result, "removed": 0})
        self.assertEqual(len(operations), 3)

    def test_a_refused_and_a_misused_command(self):
        with self.subTest(case="a missing ledger"):
            code, out, err = smctl("ledger", "compact", str(self.work / "missing.db"))
            self.assertEqual((code, out), (3, ""))
            self.assertEqual(json.loads(err), {"error": "E_LEDGER_MISSING",
                                               "detail": str(self.work / "missing.db")})
        for label, argv in (("no ledger", ("ledger", "compact")),
                            ("an extra argument", ("ledger", "compact", self.path, "--now"))):
            with self.subTest(case=label):
                code, out, _err = smctl(*argv)
                self.assertEqual((code, out), (2, ""))

    def test_the_command_in_another_process(self):
        self.compacted("cli.db", at=CALLS)
        done = subprocess.run([PY, "-m", "sentinelmesh.cli", "ledger", "compact", str(self.work / "cli.db")],
                              cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stdout)
        result = json.loads(done.stdout)
        self.assertEqual(set(result), CLI_KEYS)
        self.assertEqual((result["seq"], result["removed"]), (CALLS, 0))
        self.assertEqual(result["ledger"], str(self.work / "cli.db"))


class AnotherProcess(CompactionCase):
    def test_a_compacted_ledger_reopens_the_same_in_another_process(self):
        path, operations = self.compacted("child.db", at=6)
        body = ("import json, sys\n"
                "from sentinelmesh.durable_ledger import open_ledger\n"
                "ledger = open_ledger(sys.argv[1])\n"
                "print(json.dumps({'operation_ids': list(ledger.operation_ids()), "
                "'events': list(ledger.events), 'seq': ledger.seq}))\n")
        done = subprocess.run([PY, "-c", body, path], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stdout)
        from_child = json.loads(done.stdout)
        reopened = open_ledger(path)
        self.assertEqual(from_child, {"operation_ids": list(reopened.operation_ids()),
                                      "events": list(reopened.events), "seq": reopened.seq})
        self.assertEqual(from_child["operation_ids"], operations)
        self.assertEqual(from_child["seq"], CALLS)
        reopened.close()


if __name__ == "__main__":
    unittest.main()
