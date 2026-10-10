"""Planner oracle for ledger-compaction (`.agent/design/ledger-compaction.md`).

Planner-provided; executors must not modify this file.

Opening a durable ledger replayed every command since its creation, and the
replay of a ``claim`` walks every operation, so opening grew faster than the
log.  The user decided (2026-10-09): a *checkpoint* holds the whole in-memory
state -- events included -- at one sequence number, so that the checkpoint plus
the commands after it rebuilds exactly what a full replay would; compaction
writes the checkpoint and removes the commands it covers; a compaction by
another process leaves a live instance current, because the identity
``(seq, sha)`` of the log does not change; the format is
``sentinelmesh-ledger-v2`` and a format 1 file is refused.

The cases run one scenario (every mutating method at least once, ``CALLS``
calls) and compact at every prefix of it: whatever the point, the live
instance and the reopened ledger have to equal a ledger that was never
compacted -- every read-only view, every event and the private state of the
in-memory Scheduler -- and the commands after the checkpoint have to be the
same commands, chained to it.  Real child processes compact a ledger the
parent holds open, and one is killed right after compacting.  Damaged and
forged checkpoints are refused without the file being changed.
"""
from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import os
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
    create_ledger,
    open_ledger,
)
from sentinelmesh.scheduler import ContractError, Scheduler

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = os.path.realpath(sys.executable)
NL = bytes([10])
STATE_KEYS = {"generation", "requests", "tasks", "operations", "idempotency", "resource_holds", "events"}
COMPACT_KEYS = {"seq", "last_sha", "sha256", "removed"}
CLI_KEYS = {"command", "ledger", "generation", "seq", "last_sha", "sha256", "removed"}

# Shared by this process and the child processes: the same scenario, the same views.
HELPERS = r'''
import dataclasses
import itertools
import json
from unittest import mock

from sentinelmesh.scheduler import Scheduler

CAPS = {"dispatches": 9, "tokens": 500, "cost": 200}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
IDENTITY = {"pid": 4242, "boot_id": "12345678-1234-1234-1234-1234567890AB", "executable_sha256": "a" * 64,
            "worktree_id": "wt-1"}
H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64
TASKS = ("task-1", "task-2", "task-3", "task-4", "task-5")
CALLS = 33


def descriptor(op, evidence_dir):
    return {"spec": {"request_id": "req-1", "run_id": op, "command": ["pi", "--mode", "json"], "cwd": "/w",
                     "environment_sha256": "a" * 64, "timeout_seconds": 1800},
            "evidence_dir": evidence_dir, "worktree_id": "wt-1"}


class Done(Exception):
    pass


class Steps:
    """Call the ledger method by method: compact right after call number ``compact_at``,
    stop right after call number ``stop_after``."""

    def __init__(self, ledger, compact_at=None, stop_after=None):
        self.ledger, self.compact_at, self.stop_after = ledger, compact_at, stop_after
        self.count, self.compactions = 0, []
        if compact_at == 0:
            self.compactions.append(ledger.compact())

    def __call__(self, method, *args, **kwargs):
        result = getattr(self.ledger, method)(*args, **kwargs)
        self.count += 1
        if self.count == self.compact_at:
            self.compactions.append(self.ledger.compact())
        if self.count == self.stop_after:
            raise Done()
        return result


class Ids:
    """Operation ids that are the same in every run: the derived evidence hash of a
    handoff's dispatch depends on the id, so two runs compare whole only with equal ids."""

    def __init__(self):
        self.counter = itertools.count(1)

    def __call__(self):
        return "op-%032x" % next(self.counter)


def deterministic_ids():
    return mock.patch.object(Scheduler, "_new_operation_id", Ids())


def scenario(ledger, compact_at=None, stop_after=None):
    """Every mutating method at least once (CALLS calls); (operation ids in claim order, compactions)."""
    with deterministic_ids():
        return _scenario(ledger, compact_at, stop_after)


def _scenario(ledger, compact_at, stop_after):
    ops = []
    step = Steps(ledger, compact_at, stop_after)
    if stop_after == 0:
        return ops, step.compactions
    try:
        step("seed_request", "req-1", "repo-1", CAPS, concurrency=5)
        for task in TASKS:
            step("seed_task", "req-1", task, CAPS, max_attempts=3)
        a = step("claim", "req-1", "task-1", 1, "idem-a", "owner-1", 1, 1, RESERVATION, ["src/a.py"])["operation_id"]
        ops.append(a)
        step("handoff", a, "owner-1", 1, 1, run=descriptor(a, "/evidence/a"))
        step("mark_running", a, "owner-1", 1, process_identity=dict(IDENTITY))
        step("settle", a, {"tokens": 12, "cost": 3}, evidence_hash=H1)
        step("finish", a, "owner-1", 1, H2, True)
        step("fixture_verdict", a, H2, True, H1)
        b = step("claim", "req-1", "task-2", 1, "idem-b", "owner-1", 1, 1, RESERVATION, ["src/b.py"], now=5,
                 lease=60)["operation_id"]
        ops.append(b)
        step("renew", b, "owner-1", 1, 10, 100)
        step("handoff", b, "owner-1", 1, 20)
        step("release_proven_unstarted", b, "owner-1", 1, evidence_hash=H3)
        c = step("claim", "req-1", "task-3", 1, "idem-c", "owner-1", 1, 1, RESERVATION, ["src/c.py"])["operation_id"]
        ops.append(c)
        step("cancel_unstarted", c, 1)
        d = step("claim", "req-1", "task-4", 1, "idem-d", "owner-1", 1, 1, RESERVATION, ["src/d.py"])["operation_id"]
        ops.append(d)
        step("handoff", d, "owner-1", 1, 2)
        step("observe", d, "tokens", "call-d", 1, 7, False, H1)
        e = step("claim", "req-1", "task-5", 1, "idem-e", "owner-1", 1, 1, RESERVATION, ["src/e.py"])["operation_id"]
        ops.append(e)
        step("handoff", e, "owner-1", 1, 3)
        step("mark_running", e, "owner-1", 1)
        step("unknown", e, "owner-1", 1, reason="CLEANUP_UNCONFIRMED")
        step("rollover")
        step("recover", d, "owner-2", 2, outcome="START_UNKNOWN", evidence_hash=H2)
        step("stop", "req-1", "OPERATOR_STOP")
        step("resolve_unknown", e, 2, resolution="ABANDONED", operator="op-1", evidence_hash=H2, note="process gone")
        step("resolve_unknown", d, 2, resolution="FINISHED", operator="op-1", evidence_hash=H2, note="finished",
             candidate_hash=H3)
        step("seed_request", "req-2", "repo-2", CAPS)
        step("stop", "req-2", "OPERATOR_STOP")
        step("resume", "req-2", 2, expected_reasons=["OPERATOR_STOP"], operator="op-1", note="resumed")
    except Done:
        pass
    return ops, step.compactions


def view_or_code(call):
    try:
        return call()
    except Exception as exc:
        return {"error": getattr(exc, "code", type(exc).__name__)}


def views(ledger, ops):
    """Everything the read-only surface says, operations in claim order."""
    requests = list(ledger.request_ids())
    return {"generation": ledger.generation, "seq": getattr(ledger, "seq", None), "events": list(ledger.events),
            "request_ids": requests, "operation_ids": list(ledger.operation_ids()),
            "operations": [ledger.operation(op) for op in ops],
            "usage": [ledger.operation_usage(op) for op in ops],
            "descriptors": [ledger.run_descriptor(op) for op in ops],
            "stop_reasons": [list(ledger.stop_reasons(request)) for request in requests],
            "budgets": [ledger.snapshot(request) for request in requests],
            "tasks": [view_or_code(lambda: ledger.snapshot("req-1", task)) for task in TASKS]}


def private_state(ledger):
    """The in-memory Scheduler's own state, read through its private attributes, order kept."""
    s = getattr(ledger, "_scheduler", ledger)
    return json.dumps({"generation": s._generation,
                       "requests": {k: dataclasses.asdict(v) for k, v in s._requests.items()},
                       "tasks": [[k[0], k[1], dataclasses.asdict(v)] for k, v in s._tasks.items()],
                       "operations": {k: dataclasses.asdict(v) for k, v in s._operations.items()},
                       "idempotency": [[k[0], k[1], v] for k, v in s._idempotency.items()],
                       "resource_holds": [[k[0], k[1], v] for k, v in s._resource_holds.items()],
                       "events": s._events}, sort_keys=False)
'''
NAMESPACE: dict = {}
exec(HELPERS, NAMESPACE)
scenario, views, private_state = NAMESPACE["scenario"], NAMESPACE["views"], NAMESPACE["private_state"]
CAPS, RESERVATION, CALLS, TASKS = NAMESPACE["CAPS"], NAMESPACE["RESERVATION"], NAMESPACE["CALLS"], NAMESPACE["TASKS"]


def normalized(value, ops):
    """``value`` as text with each operation id replaced by its claim position."""
    text = value if type(value) is str else json.dumps(value, sort_keys=True)
    for index, op in enumerate(ops):
        text = text.replace(op, "OP%d" % index)
    return text


def without_seq(view):
    return {key: value for key, value in view.items() if key != "seq"}


def checkpoint_digest(seq, last_sha, state):
    """The checkpoint digest, computed here and not by the module under test."""
    return hashlib.sha256(str(seq).encode("ascii") + NL + last_sha.encode("ascii") + NL
                          + state.encode("utf-8")).hexdigest()


def entry_digest(previous, command):
    return hashlib.sha256(previous.encode("ascii") + NL + command.encode("utf-8")).hexdigest()


def query(path, sql):
    db = sqlite3.connect(path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def rows_of(path):
    return query(path, "SELECT seq, command, sha256, previous FROM commands ORDER BY seq")


def checkpoint_of(path):
    return query(path, "SELECT seq, last_sha, state, sha256 FROM checkpoint ORDER BY seq")


def tables_of(path):
    return [row[0] for row in query(path, "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def meta_of(path):
    return dict(query(path, "SELECT key, value FROM meta"))


def execute(path, sql, params=()):
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(sql, params)
        db.execute("COMMIT")
    finally:
        db.close()


def rewrite_checkpoint(path, seq, last_sha, state, digest=None):
    """Replace the checkpoint row; the digest is recomputed unless one is given."""
    digest = checkpoint_digest(seq, last_sha, state) if digest is None else digest
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM checkpoint")
        db.execute("INSERT INTO checkpoint(seq, last_sha, state, sha256) VALUES (?, ?, ?, ?)",
                   (seq, last_sha, state, digest))
        db.execute("COMMIT")
    finally:
        db.close()


def with_state(path, edit):
    """Re-encode the checkpoint's state after ``edit`` changed it, with a digest that verifies."""
    (seq, last_sha, state, _digest), = checkpoint_of(path)
    value = json.loads(state)
    replaced = edit(value)
    value = value if replaced is None else replaced
    rewrite_checkpoint(path, seq, last_sha, json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def with_state_text(path, edit):
    """Rewrite the checkpoint's state *text* after ``edit``, with a digest that verifies."""
    (seq, last_sha, state, _digest), = checkpoint_of(path)
    rewrite_checkpoint(path, seq, last_sha, edit(state))


def append_rechained(path, command):
    """Append one command row after the current head, correctly chained."""
    rows = rows_of(path)
    if rows:
        seq, previous = rows[-1][0], rows[-1][2]
    else:
        checkpoints = checkpoint_of(path)
        seq, previous = (checkpoints[0][0], checkpoints[0][1]) if checkpoints else (0, "")
    execute(path, "INSERT INTO commands(seq, command, sha256, previous) VALUES (?, ?, ?, ?)",
            (seq + 1, command, entry_digest(previous, command), previous))


def smctl(*argv):
    """Run ``smctl`` in this process: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main([str(a) for a in argv])
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

    def child(self, body, *args):
        """Run ``HELPERS`` + ``body`` in a fresh python process; return the completed process."""
        return subprocess.run([PY, "-c", HELPERS + "\n" + body, *args], cwd=str(REPO_ROOT), capture_output=True,
                              text=True, timeout=120)

    def compacted(self, name, compact_at):
        """A ledger at ``name`` that ran the whole scenario, compacted after call ``compact_at``."""
        path = str(self.work / name)
        ledger = create_ledger(path)
        ops, compactions = scenario(ledger, compact_at=compact_at)
        ledger.close()
        self.assertEqual(len(compactions), 1)
        return path, ops


class CompactionOracle(CompactionCase):
    def test_the_format_and_the_surface(self):
        self.assertEqual(LEDGER_FORMAT, "sentinelmesh-ledger-v2")
        self.assertNotIn("compact", MUTATING_METHODS)
        self.assertEqual(list(inspect.signature(DurableScheduler.compact).parameters), ["self"])
        ledger = create_ledger(self.path)
        self.assertEqual(ledger.seq, 0)
        ledger.close()
        self.assertEqual(tables_of(self.path), ["checkpoint", "commands", "meta"])
        self.assertEqual([row[1] for row in query(self.path, "PRAGMA table_info(checkpoint)")],
                         ["seq", "last_sha", "state", "sha256"])
        self.assertEqual(meta_of(self.path), {"format": "sentinelmesh-ledger-v2", "generation": "1"})
        self.assertEqual(checkpoint_of(self.path), [])

    def test_compacting_at_every_prefix_reopens_to_the_same_ledger(self):
        reference = create_ledger(self.path)
        ops, compactions = scenario(reference)
        self.assertEqual((len(compactions), reference.seq), (0, CALLS))
        expected, expected_private = views(reference, ops), private_state(reference)
        reference.close()
        full_rows = rows_of(self.path)
        self.assertEqual([row[0] for row in full_rows], list(range(1, CALLS + 1)))
        memory = Scheduler()
        memory_ops, _ = scenario(memory)
        self.assertEqual(normalized(expected_private, ops), normalized(private_state(memory), memory_ops),
                         "the durable ledger behaved differently from the in-memory Scheduler")
        self.assertEqual(normalized(without_seq(expected), ops), normalized(without_seq(views(memory, memory_ops)),
                                                                            memory_ops))
        for k in range(CALLS + 1):
            with self.subTest(compact_at=k):
                path = str(self.work / ("compact-%02d.db" % k))
                ledger = create_ledger(path)
                ops_k, compactions = scenario(ledger, compact_at=k)
                self.assertEqual(len(compactions), 1)
                self.assertEqual(set(compactions[0]), COMPACT_KEYS)
                self.assertEqual((compactions[0]["seq"], compactions[0]["removed"]), (k, k))
                self.assertEqual(ledger.seq, CALLS)
                # The live instance, compacted mid-way, is the reference.
                self.assertEqual(normalized(views(ledger, ops_k), ops_k), normalized(expected, ops))
                self.assertEqual(normalized(private_state(ledger), ops_k), normalized(expected_private, ops))
                ledger.close()
                # The file: one checkpoint at k that verifies, and only the tail, chained to it.
                (seq, last_sha, state, digest), = checkpoint_of(path)
                self.assertEqual((seq, last_sha, digest), (k, compactions[0]["last_sha"], compactions[0]["sha256"]))
                self.assertEqual(digest, checkpoint_digest(seq, last_sha, state))
                self.assertEqual(set(json.loads(state)), STATE_KEYS)
                if k == 0:
                    self.assertEqual(last_sha, "")
                else:
                    self.assertRegex(last_sha, "^[0-9a-f]{64}$")
                rows = rows_of(path)
                self.assertEqual([row[0] for row in rows], list(range(k + 1, CALLS + 1)))
                previous = last_sha
                for _seq, command, sha, row_previous in rows:
                    self.assertEqual(row_previous, previous)
                    self.assertEqual(sha, entry_digest(previous, command))
                    previous = sha
                self.assertEqual([normalized(row[1], ops_k) for row in rows],
                                 [normalized(row[1], ops) for row in full_rows[k:]],
                                 "the commands after the checkpoint are not the same commands")
                # Reopened: the same ledger, and it keeps appending after CALLS.
                reopened = open_ledger(path)
                self.assertEqual(reopened.seq, CALLS)
                self.assertEqual(normalized(views(reopened, ops_k), ops_k), normalized(expected, ops))
                self.assertEqual(normalized(private_state(reopened), ops_k), normalized(expected_private, ops))
                reopened.seed_task("req-2", "task-x", CAPS)
                self.assertEqual(reopened.seq, CALLS + 1)
                reopened.close()
                self.assertEqual([row[0] for row in rows_of(path)], list(range(k + 1, CALLS + 2)))
                self.assertEqual(rows_of(path)[-1][3], previous)

    def test_a_compacted_ledger_reopens_the_same_in_another_process(self):
        reference = create_ledger(self.path)
        ops, _ = scenario(reference)
        expected, expected_private = views(reference, ops), private_state(reference)
        reference.close()
        for k in (0, 16, CALLS):
            with self.subTest(compact_at=k):
                path, ops_k = self.compacted("compact-%02d.db" % k, k)
                done = self.child("import json, sys\n"
                                  "from sentinelmesh.durable_ledger import open_ledger\n"
                                  "ledger = open_ledger(sys.argv[1])\n"
                                  "ops = json.loads(sys.argv[2])\n"
                                  "print(json.dumps({'views': views(ledger, ops), 'private': private_state(ledger)}))\n",
                                  path, json.dumps(ops_k))
                self.assertEqual(done.returncode, 0, done.stderr)
                result = json.loads(done.stdout)
                self.assertEqual(normalized(result["views"], ops_k), normalized(expected, ops))
                self.assertEqual(normalized(result["private"], ops_k), normalized(expected_private, ops))

    def test_compaction_changes_nothing_the_ledger_holds(self):
        ledger = create_ledger(self.path)
        ops, _ = scenario(ledger, stop_after=20)
        before_rows = rows_of(self.path)
        before = (views(ledger, ops), private_state(ledger))
        result = ledger.compact()
        self.assertEqual(set(result), COMPACT_KEYS)
        self.assertEqual((result["seq"], result["removed"], result["last_sha"]), (20, 20, before_rows[-1][2]))
        self.assertEqual((views(ledger, ops), private_state(ledger)), before, "compaction changed the state")
        self.assertEqual(rows_of(self.path), [])
        (seq, last_sha, state, digest), = checkpoint_of(self.path)
        self.assertEqual((seq, last_sha, digest), (20, result["last_sha"], result["sha256"]))
        self.assertEqual(digest, checkpoint_digest(seq, last_sha, state))
        recorded = json.loads(state)
        self.assertEqual(set(recorded), STATE_KEYS)
        self.assertEqual((recorded["generation"], list(recorded["requests"]), list(recorded["operations"])),
                         (1, ["req-1"], ops))
        self.assertEqual(len(recorded["events"]), len(before[0]["events"]))
        # The instance is still current and usable: the next command is 21, chained to the checkpoint.
        ledger.stop("req-1", "hold")
        self.assertEqual(ledger.seq, 21)
        self.assertEqual([(row[0], row[3]) for row in rows_of(self.path)], [(21, last_sha)])
        live = (views(ledger, ops), private_state(ledger))
        ledger.close()
        reopened = open_ledger(self.path)
        self.assertEqual((views(reopened, ops), private_state(reopened)), live)
        self.assertEqual(reopened.stop_reasons("req-1"), ("hold",))
        reopened.close()

    def test_an_empty_ledger_and_an_already_compacted_ledger_compact_to_nothing(self):
        ledger = create_ledger(self.path)
        first = ledger.compact()
        self.assertEqual((first["seq"], first["last_sha"], first["removed"]), (0, "", 0))
        (seq, last_sha, state, digest), = checkpoint_of(self.path)
        self.assertEqual((seq, last_sha, digest), (0, "", first["sha256"]))
        self.assertEqual(json.loads(state)["events"], [])
        self.assertEqual(ledger.compact(), first, "compacting again recorded something else")
        ledger.seed_request("req-1", "repo-1", CAPS)
        ledger.seed_task("req-1", "task-1", CAPS)
        self.assertEqual([row[0] for row in rows_of(self.path)], [1, 2])
        second = ledger.compact()
        self.assertEqual((second["seq"], second["removed"]), (2, 2))
        self.assertNotEqual(second["sha256"], first["sha256"])
        third = ledger.compact()
        self.assertEqual(third, {**second, "removed": 0})
        self.assertEqual(len(checkpoint_of(self.path)), 1)
        ledger.close()
        reopened = open_ledger(self.path)
        self.assertEqual((reopened.seq, reopened.request_ids()), (2, ("req-1",)))
        reopened.seed_task("req-1", "task-2", CAPS)
        self.assertEqual(reopened.seq, 3)
        reopened.close()
        fresh = open_ledger(self.path)
        self.assertEqual(fresh.seq, 3)
        self.assertEqual(self.code_of(ContractError, fresh.seed_task, "req-1", "task-2", CAPS), "E_TASK_EXISTS")
        fresh.close()

    def test_another_process_compacting_leaves_this_instance_current(self):
        ledger = create_ledger(self.path)
        ops, _ = scenario(ledger, stop_after=20)
        done = self.child("import json, sys\n"
                          "from sentinelmesh.durable_ledger import open_ledger\n"
                          "ledger = open_ledger(sys.argv[1])\n"
                          "print(json.dumps(ledger.compact()))\n"
                          "ledger.close()\n", self.path)
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual((result["seq"], result["removed"]), (20, 20))
        self.assertEqual(rows_of(self.path), [])
        # The identity of the log did not change, so this instance is still current.
        ledger.stop("req-1", "hold")
        self.assertEqual(ledger.seq, 21)
        self.assertEqual([(row[0], row[3]) for row in rows_of(self.path)], [(21, result["last_sha"])])
        live = views(ledger, ops)
        reopened = open_ledger(self.path)
        self.assertEqual(views(reopened, ops), live)
        reopened.close()
        # A process that compacts *and* appends moves the log on: this instance is stale.
        done = self.child("import sys\n"
                          "from sentinelmesh.durable_ledger import open_ledger\n"
                          "ledger = open_ledger(sys.argv[1])\n"
                          "ledger.compact()\n"
                          "ledger.stop('req-1', 'another')\n"
                          "ledger.close()\n", self.path)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.code_of(LedgerError, ledger.stop, "req-1", "third"), "E_LEDGER_STALE")
        self.assertEqual(self.code_of(LedgerError, lambda: ledger.seq), "E_LEDGER_UNAVAILABLE")
        ledger.close()
        reopened = open_ledger(self.path)
        self.assertEqual((reopened.seq, reopened.stop_reasons("req-1")), (22, ("hold", "another")))
        reopened.close()

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

    def test_a_writer_killed_right_after_compacting_leaves_a_ledger_that_reopens(self):
        create_ledger(self.path).close()
        body = ("import json, sys, time\n"
                "from sentinelmesh.durable_ledger import open_ledger\n"
                "ledger = open_ledger(sys.argv[1])\n"
                "ops, compactions = scenario(ledger, compact_at=20)\n"
                "print(json.dumps([ops, compactions]), flush=True)\n"
                "time.sleep(60)\n")
        proc = subprocess.Popen([PY, "-c", HELPERS + "\n" + body, self.path], cwd=str(REPO_ROOT),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline()
        finally:
            proc.kill()
            _out, err = proc.communicate(timeout=60)
        self.assertTrue(line, err)
        ops, compactions = json.loads(line)
        self.assertEqual(compactions[0]["seq"], 20)
        reopened = open_ledger(self.path)
        self.assertEqual(reopened.seq, CALLS)
        self.assertEqual(checkpoint_of(self.path)[0][0], 20)
        self.assertEqual([row[0] for row in rows_of(self.path)], list(range(21, CALLS + 1)))
        memory = Scheduler()
        memory_ops, _ = scenario(memory)
        self.assertEqual(normalized(private_state(reopened), ops), normalized(private_state(memory), memory_ops))
        self.assertEqual(normalized(without_seq(views(reopened, ops)), ops),
                         normalized(without_seq(views(memory, memory_ops)), memory_ops))
        reopened.close()

    def test_damaged_checkpoints_are_refused_without_being_changed(self):
        tail, tail_ops = self.compacted("tail.db", 20)
        head, head_ops = self.compacted("head.db", CALLS)
        a, d = head_ops[0], head_ops[3]
        later = str(self.work / "later.db")
        ledger = create_ledger(later, generation=5)
        ledger.seed_request("req-1", "repo-1", CAPS)
        ledger.seed_task("req-1", "task-1", CAPS)
        ledger.compact()
        ledger.close()
        claim = json.dumps({"method": "claim",
                            "arguments": {"request": "req-1", "task": "task-5", "plan": 1, "key": "idem-z",
                                          "owner": "owner-1", "generation": 1, "guard_version": 1,
                                          "reservation": RESERVATION, "resource_keys": ["src/z.py"], "now": 0,
                                          "lease": 30, "fail_before_commit": False},
                            "operation_id": tail_ops[0]}, separators=(",", ":"))

        def rename_operation(value, new_key, inner):
            """Record operation ``a`` under ``new_key``; ``inner`` renames its own id too.
            The entries that name it follow, so only the check under test is left to fire."""
            value["operations"] = {(new_key if key == a else key): fields
                                   for key, fields in value["operations"].items()}
            if inner:
                value["operations"][new_key]["operation_id"] = new_key
            for entry in value["idempotency"] + value["resource_holds"]:
                if entry[2] == a:
                    entry[2] = new_key

        def edit_field(path_in_state):
            def edit(value):
                target = value
                for part in path_in_state[:-1]:
                    target = target[part]
                target[path_in_state[-1][0]] = path_in_state[-1][1]
            return edit

        cases = [
            ("two checkpoints", tail, "E_LEDGER_CHECKPOINT",
             lambda p: execute(p, "INSERT INTO checkpoint SELECT seq + 1, last_sha, state, sha256 FROM checkpoint")),
            ("a digest that does not verify", tail, "E_LEDGER_CHECKPOINT",
             lambda p: execute(p, "UPDATE checkpoint SET sha256=?", ("0" * 64,))),
            ("a state touched after the digest", tail, "E_LEDGER_CHECKPOINT",
             lambda p: execute(p, "UPDATE checkpoint SET state = state || ' '")),
            ("a link that does not fit the chain", tail, "E_LEDGER_CHAIN",
             lambda p: rewrite_checkpoint(p, 20, "f" * 64, checkpoint_of(p)[0][2])),
            ("a sequence number ahead of the tail", tail, "E_LEDGER_CHAIN",
             lambda p: rewrite_checkpoint(p, 21, checkpoint_of(p)[0][1], checkpoint_of(p)[0][2])),
            ("a sequence number behind the tail", tail, "E_LEDGER_CHAIN",
             lambda p: rewrite_checkpoint(p, 19, checkpoint_of(p)[0][1], checkpoint_of(p)[0][2])),
            ("a negative sequence number", head, "E_LEDGER_CHECKPOINT",
             lambda p: rewrite_checkpoint(p, -1, checkpoint_of(p)[0][1], checkpoint_of(p)[0][2])),
            ("an empty link above sequence 0", head, "E_LEDGER_CHECKPOINT",
             lambda p: rewrite_checkpoint(p, CALLS, "", checkpoint_of(p)[0][2])),
            ("a link at sequence 0", head, "E_LEDGER_CHECKPOINT",
             lambda p: rewrite_checkpoint(p, 0, "a" * 64, checkpoint_of(p)[0][2])),
            ("a tail that skips a command", tail, "E_LEDGER_CHAIN",
             lambda p: execute(p, "DELETE FROM commands WHERE seq=21")),
            ("a tail command the checkpoint already covers", tail, "E_LEDGER_CHAIN",
             lambda p: execute(p, "INSERT INTO commands(seq, command, sha256, previous) VALUES (20, '{}', '', '')")),
            ("a state that is not an object", head, "E_LEDGER_CHECKPOINT", lambda p: with_state(p, lambda v: [])),
            ("a key the state does not have", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field([("extra", 1)]))),
            ("a key missing from the state", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, lambda v: v.pop("events"))),
            ("a duplicate key", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state_text(p, lambda t: t.replace('{"generation":', '{"generation":2,"generation":', 1))),
            ("a generation below the stored one", later, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field([("generation", 4)]))),
            ("a generation the Scheduler refuses", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field([("generation", 2 ** 53)]))),
            ("a generation that is a boolean", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field([("generation", True)]))),
            ("an operation missing a field", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, lambda v: v["operations"][a].pop("started"))),
            ("an operation with a field it does not have", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["operations", a, ("extra", 1)]))),
            ("a boolean recorded as an integer", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["operations", a, ("started", 1)]))),
            ("an amount recorded as text", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["operations", a, "usage", "tokens", ("amount", "12")]))),
            ("a stop recorded as an integer", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["requests", "req-1", ("stopped", 1)]))),
            ("a state the Scheduler never records", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["operations", a, ("state", "DONE")]))),
            ("an operation under another id", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, lambda v: rename_operation(v, "op-" + "e" * 32, inner=False))),
            ("an operation id of another shape", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, lambda v: rename_operation(v, "op-x", inner=True))),
            ("a cap recorded as text", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["requests", "req-1", "caps", ("tokens", "500")]))),
            ("a reservation that is not an integer", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["operations", a, "reservation", ("tokens", 40.5)]))),
            ("a stop reason that is not text", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["requests", "req-1", "stop_reasons", (0, 1)]))),
            ("a resource key that is not text", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["operations", a, "resource_keys", (0, 1)]))),
            ("a task naming an unknown request", later, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["tasks", 0, (0, "req-9")]))),
            ("a task recorded twice", later, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, lambda v: v["tasks"].append(list(v["tasks"][0])))),
            ("an idempotency entry that is not a triple", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, lambda v: v["idempotency"][0].pop())),
            ("an operation naming an unknown task", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["operations", a, ("task_id", "task-9")]))),
            ("an idempotency entry naming an unknown operation", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["idempotency", 0, (2, "op-" + "f" * 32)]))),
            ("an idempotency entry with another key", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["idempotency", 0, (1, "idem-z")]))),
            ("an operation without an idempotency entry", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, lambda v: v["idempotency"].pop(0))),
            ("a resource hold its operation does not hold", tail, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["resource_holds", 0, (1, "src/z.py")]))),
            ("a resource held twice", tail, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, lambda v: v["resource_holds"].append(list(v["resource_holds"][0])))),
            ("events numbered from 2", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, edit_field(["events", 0, ("seq", 2)]))),
            ("an event without its keys", head, "E_LEDGER_CHECKPOINT",
             lambda p: with_state(p, lambda v: v["events"][0].pop("payload"))),
            ("a tail claim reusing a checkpointed operation id", tail, "E_LEDGER_REPLAY",
             lambda p: append_rechained(p, claim)),
            ("a format 1 file", head, "E_LEDGER_FORMAT",
             lambda p: execute(p, "UPDATE meta SET value='sentinelmesh-ledger-v1' WHERE key='format'")),
            ("no checkpoint table", head, "E_LEDGER_FORMAT", lambda p: execute(p, "DROP TABLE checkpoint")),
            ("no checkpoint table in an uncompacted ledger", self.path, "E_LEDGER_FORMAT",
             lambda p: execute(p, "DROP TABLE checkpoint")),
        ]
        create_ledger(self.path).close()
        # The tail ledger's one hold is the handoff of the fourth operation.
        self.assertEqual(json.loads(checkpoint_of(tail)[0][2])["resource_holds"], [["repo-1", "src/d.py", tail_ops[3]]])
        for index, (label, source, code, damage) in enumerate(cases):
            with self.subTest(case=label):
                copy = str(self.work / ("damaged-%02d.db" % index))
                shutil.copyfile(source, copy)
                open_ledger(copy).close()  # legal before the damage
                damage(copy)
                before = Path(copy).read_bytes()
                self.assertEqual(self.code_of(LedgerError, open_ledger, copy), code)
                self.assertEqual(Path(copy).read_bytes(), before, "a refused open changed the file")
        self.assertEqual(d, head_ops[3])

    def test_a_failed_compaction_invalidates_the_instance_and_changes_nothing(self):
        real_connect = sqlite3.connect

        class FailingAt:
            def __init__(self, real, prefix):
                self.real, self.prefix = real, prefix

            def execute(self, sql, *args):
                if sql.lstrip().upper().startswith(self.prefix):
                    raise sqlite3.OperationalError("disk I/O error")
                return self.real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.real, name)

        for index, prefix in enumerate(("DELETE FROM CHECKPOINT", "INSERT INTO CHECKPOINT", "DELETE FROM COMMANDS")):
            with self.subTest(failing=prefix):
                path = str(self.work / ("failing-%d.db" % index))

                def connect(location, *args, **kwargs):
                    return FailingAt(real_connect(location, *args, **kwargs), prefix)

                with mock.patch.object(durable_ledger.sqlite3, "connect", connect):
                    ledger = create_ledger(path)
                ops, _ = scenario(ledger, stop_after=20)
                expected = views(ledger, ops)
                self.assertEqual(self.code_of(LedgerError, ledger.compact), "E_LEDGER_WRITE")
                self.assertEqual(self.code_of(LedgerError, lambda: ledger.seq), "E_LEDGER_UNAVAILABLE")
                self.assertEqual(self.code_of(LedgerError, ledger.compact), "E_LEDGER_UNAVAILABLE")
                ledger.close()
                self.assertEqual(checkpoint_of(path), [], "a failed compaction left a checkpoint")
                self.assertEqual(len(rows_of(path)), 20, "a failed compaction removed commands")
                reopened = open_ledger(path)
                self.assertEqual(views(reopened, ops), expected)
                reopened.close()
        with self.subTest(failing="the encoding of the state"):
            ledger = create_ledger(self.path)
            ops, _ = scenario(ledger, stop_after=20)
            expected = views(ledger, ops)
            with mock.patch.object(durable_ledger, "_encode_state", side_effect=ValueError("unencodable")):
                self.assertEqual(self.code_of(LedgerError, ledger.compact), "E_LEDGER_WRITE")
            self.assertEqual(self.code_of(LedgerError, lambda: ledger.seq), "E_LEDGER_UNAVAILABLE")
            ledger.close()
            self.assertEqual((checkpoint_of(self.path), len(rows_of(self.path))), ([], 20))
            reopened = open_ledger(self.path)
            self.assertEqual(views(reopened, ops), expected)
            reopened.close()

    def test_a_closed_instance_refuses_compaction(self):
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS)
        ledger.close()
        self.assertEqual(self.code_of(LedgerError, ledger.compact), "E_LEDGER_UNAVAILABLE")
        self.assertEqual(checkpoint_of(self.path), [])

    def test_the_cli_compacts_and_everything_else_reads_the_same(self):
        ledger = create_ledger(self.path)
        ops, _ = scenario(ledger)
        ledger.close()
        rows = rows_of(self.path)
        code, status_before, err = smctl("ledger", "status", self.path)
        self.assertEqual((code, err), (0, ""))
        code, out, err = smctl("ledger", "compact", self.path)
        self.assertEqual((code, err), (0, ""), out)
        result = json.loads(out)
        self.assertEqual(set(result), CLI_KEYS)
        (seq, last_sha, _state, digest), = checkpoint_of(self.path)
        self.assertEqual(result, {"command": "compact", "ledger": self.path, "generation": 2, "seq": CALLS,
                                  "last_sha": rows[-1][2], "sha256": digest, "removed": CALLS})
        self.assertEqual((seq, last_sha), (CALLS, rows[-1][2]))
        self.assertEqual(rows_of(self.path), [])
        code, status_after, err = smctl("ledger", "status", self.path)
        self.assertEqual((code, err, status_after), (0, "", status_before), "status changed with the compaction")
        code, out, err = smctl("ledger", "reference", self.path, "--operation", ops[0], "--dialect", "pi-session-v3")
        self.assertEqual((code, err), (0, ""), out)
        reference = json.loads(out)
        self.assertEqual((reference["evidence_dir"], reference["stream"]["status"], reference["reference"],
                          sorted(reference["recorded"])), ("/evidence/a", "missing", None, ["cost", "dispatches", "tokens"]))
        code, out, err = smctl("ledger", "compact", self.path)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {**result, "removed": 0})
        with self.subTest(case="a missing ledger"):
            code, out, err = smctl("ledger", "compact", str(self.work / "missing.db"))
            self.assertEqual((code, out), (3, ""))
            self.assertEqual(json.loads(err)["error"], "E_LEDGER_MISSING")
        for label, argv in (("no ledger", ("ledger", "compact")),
                            ("an extra argument", ("ledger", "compact", self.path, "--now"))):
            with self.subTest(case=label):
                code, out, _err = smctl(*argv)
                self.assertEqual((code, out), (2, ""))
        done = subprocess.run([PY, "-m", "sentinelmesh.cli", "ledger", "compact", "--help"], cwd=str(REPO_ROOT),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ledger", done.stdout)

    def test_a_compaction_leaves_no_journal_behind_and_reads_write_nothing(self):
        ledger = create_ledger(self.path)
        ops, _ = scenario(ledger, compact_at=25)
        ledger.close()
        self.assertEqual(sorted(os.listdir(self.work)), ["ledger.db"], "a journal file was left behind")
        self.assertEqual(query(self.path, "PRAGMA journal_mode")[0][0], "delete")
        before = Path(self.path).read_bytes()
        reopened = open_ledger(self.path)
        views(reopened, ops)
        reopened.close()
        self.assertEqual(Path(self.path).read_bytes(), before, "opening and reading changed the file")

    def test_importing_has_no_side_effect(self):
        done = subprocess.run([PY, "-c", "import sys, sentinelmesh.durable_ledger, sentinelmesh.ledger_cli; "
                                         "assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"],
                              cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "IMPORT_SAFE_OK"), done.stderr)


if __name__ == "__main__":
    unittest.main()
