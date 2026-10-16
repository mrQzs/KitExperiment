"""Planner oracle for durable-ledger (`.agent/design/durable-ledger.md`).

Planner-provided; executors must not modify this file.

The in-memory Scheduler stays the only implementation of the ledger's
semantics.  ``sentinelmesh.durable_ledger`` wraps it: every successful
mutating call is appended to a SQLite command log (a hash chain) before it
returns, and opening the ledger replays the log.  The user's decisions
(2026-10-08): a command log with replay; fail closed when an append fails or
another process moved the log; run descriptors for restart recovery come in
the next contract.

Several cases start real ``python`` processes: a writer killed with SIGKILL,
and a second controller that takes the ledger over with ``rollover()``.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import durable_ledger
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

# Shared by this process and the child processes: the same scenario, the same views.
HELPERS = r'''
CAPS = {"dispatches": 9, "tokens": 500, "cost": 200}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
IDENTITY = {"pid": 4242, "boot_id": "12345678-1234-1234-1234-1234567890AB", "executable_sha256": "a" * 64,
            "worktree_id": "wt-1"}
H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64
TASKS = ("task-1", "task-2", "task-3", "task-4", "task-5")
CALLS = 28


def scenario(ledger):
    """Every mutating method at least once; CALLS calls; returns the operation ids in claim order."""
    ledger.seed_request("req-1", "repo-1", CAPS, concurrency=5)
    for task in TASKS:
        ledger.seed_task("req-1", task, CAPS, max_attempts=3)
    a = ledger.claim("req-1", "task-1", 1, "idem-a", "owner-1", 1, 1, RESERVATION, ["src/a.py"])["operation_id"]
    ledger.handoff(a, "owner-1", 1, 1)
    ledger.mark_running(a, "owner-1", 1, process_identity=dict(IDENTITY))
    ledger.settle(a, {"tokens": 12, "cost": 3}, evidence_hash=H1)
    ledger.finish(a, "owner-1", 1, H2, True)
    ledger.fixture_verdict(a, H2, True, H1)
    b = ledger.claim("req-1", "task-2", 1, "idem-b", "owner-1", 1, 1, RESERVATION, ["src/b.py"], now=5,
                     lease=60)["operation_id"]
    ledger.renew(b, "owner-1", 1, 10, 100)
    ledger.handoff(b, "owner-1", 1, 20)
    ledger.release_proven_unstarted(b, "owner-1", 1, evidence_hash=H3)
    c = ledger.claim("req-1", "task-3", 1, "idem-c", "owner-1", 1, 1, RESERVATION, ["src/c.py"])["operation_id"]
    ledger.cancel_unstarted(c, 1)
    d = ledger.claim("req-1", "task-4", 1, "idem-d", "owner-1", 1, 1, RESERVATION, ["src/d.py"])["operation_id"]
    ledger.handoff(d, "owner-1", 1, 2)
    ledger.observe(d, "tokens", "call-d", 1, 7, False, H1)
    e = ledger.claim("req-1", "task-5", 1, "idem-e", "owner-1", 1, 1, RESERVATION, ["src/e.py"])["operation_id"]
    ledger.handoff(e, "owner-1", 1, 3)
    ledger.mark_running(e, "owner-1", 1)
    ledger.unknown(e, "owner-1", 1, reason="CLEANUP_UNCONFIRMED")
    ledger.rollover()
    ledger.recover(d, "owner-2", 2, outcome="START_UNKNOWN", evidence_hash=H2)
    ledger.stop("req-1", "OPERATOR_STOP")
    return [a, b, c, d, e]


def views(ledger, ops):
    return {"generation": ledger.generation, "events": list(ledger.events),
            "operations": [ledger.operation(op) for op in ops],
            "request": ledger.snapshot("req-1"),
            "tasks": [ledger.snapshot("req-1", task) for task in TASKS]}
'''
NAMESPACE: dict = {}
exec(HELPERS, NAMESPACE)
scenario, views, CAPS, CALLS = NAMESPACE["scenario"], NAMESPACE["views"], NAMESPACE["CAPS"], NAMESPACE["CALLS"]


def normalized(value, ops):
    """``value`` as JSON text with each operation id replaced by its claim position."""
    text = json.dumps(value, sort_keys=True)
    for index, op in enumerate(ops):
        text = text.replace(op, "OP%d" % index)
    return text


def bare(signature):
    """A signature without annotations: names, kinds and defaults are what callers rely on."""
    return signature.replace(parameters=[parameter.replace(annotation=inspect.Parameter.empty)
                                         for parameter in signature.parameters.values()],
                             return_annotation=inspect.Signature.empty)


def entry_sha256(previous: str, raw: bytes) -> str:
    return hashlib.sha256(previous.encode("ascii") + b"\n" + raw).hexdigest()


def rows_of(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT seq, command, sha256, previous FROM commands ORDER BY seq").fetchall()
    finally:
        db.close()


def rewrite(path, rows):
    """Replace the whole log with ``rows`` of (seq, command, sha256, previous)."""
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM commands")
        db.executemany("INSERT INTO commands(seq, command, sha256, previous) VALUES (?, ?, ?, ?)", rows)
        db.execute("COMMIT")
    finally:
        db.close()


def rechained(commands):
    """Rows for ``commands`` (text) with a correct chain."""
    rows, previous = [], ""
    for seq, command in enumerate(commands, start=1):
        digest = entry_sha256(previous, command.encode("utf-8"))
        rows.append((seq, command, digest, previous))
        previous = digest
    return rows


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="durable-ledger-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.addCleanup(os.chmod, self.work, 0o700)
        self.path = str(self.work / "ledger.db")

    def code_of(self, error_type, func, *args, **kwargs):
        with self.assertRaises(error_type) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def child(self, body, *args):
        """Run ``HELPERS`` + ``body`` in a fresh python process; return the completed process."""
        return subprocess.run([PY, "-c", HELPERS + "\n" + body, *args], cwd=str(REPO_ROOT), capture_output=True,
                              text=True, timeout=120)


class DurableLedgerOracle(LedgerCase):
    def test_the_public_surface_matches_the_scheduler(self):
        self.assertEqual(LEDGER_FORMAT, "sentinelmesh-ledger-v2")
        self.assertEqual(MUTATING_METHODS, ("seed_request", "seed_task", "claim", "handoff", "mark_running", "renew",
                                            "observe", "settle", "finish", "fixture_verdict", "verdict", "unknown",
                                            "release_proven_unstarted", "recover", "cancel_unstarted", "stop",
                                            "rollover", "resolve_unknown", "resume"))
        for name in MUTATING_METHODS + ("snapshot", "operation"):
            with self.subTest(method=name):
                self.assertEqual(bare(inspect.signature(getattr(DurableScheduler, name))),
                                 bare(inspect.signature(getattr(Scheduler, name))))
        self.assertTrue(Scheduler()._new_operation_id().startswith("op-"))
        self.assertEqual(len(Scheduler()._new_operation_id()), 35)

    def test_a_reopened_ledger_matches_the_in_memory_scheduler(self):
        ledger = create_ledger(self.path)
        ops = scenario(ledger)
        self.assertEqual(ledger.seq, CALLS)
        memory = Scheduler()
        memory_ops = scenario(memory)
        self.assertEqual(normalized(views(ledger, ops), ops), normalized(views(memory, memory_ops), memory_ops),
                         "the durable ledger behaved differently from the in-memory Scheduler")
        expected = views(ledger, ops)
        ledger.close()
        with self.subTest(case="reopened in this process"):
            reopened = open_ledger(self.path)
            self.assertEqual(reopened.seq, CALLS)
            self.assertEqual(views(reopened, ops), expected, "the replay did not restore the same state and ids")
            reopened.close()
        with self.subTest(case="reopened in another process"):
            done = self.child("import json, sys\n"
                              "from sentinelmesh.durable_ledger import open_ledger\n"
                              "ledger = open_ledger(sys.argv[1])\n"
                              "print(json.dumps(views(ledger, json.loads(sys.argv[2]))))\n",
                              self.path, json.dumps(ops))
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout), json.loads(json.dumps(expected)))

    @unittest.skipIf(os.name != "posix", "needs SIGKILL; Windows stays NOT_RUN")
    def test_every_call_that_returned_survives_a_kill(self):
        create_ledger(self.path).close()
        done = self.child("import json, os, signal, sys\n"
                          "from sentinelmesh.durable_ledger import open_ledger\n"
                          "ledger = open_ledger(sys.argv[1])\n"
                          "ops = scenario(ledger)\n"
                          "print(json.dumps(ops), flush=True)\n"
                          "os.kill(os.getpid(), signal.SIGKILL)\n", self.path)
        self.assertEqual(done.returncode, -signal.SIGKILL, done.stderr)
        ops = json.loads(done.stdout)
        reopened = open_ledger(self.path)
        self.assertEqual(reopened.seq, CALLS)
        memory = Scheduler()
        memory_ops = scenario(memory)
        self.assertEqual(normalized(views(reopened, ops), ops), normalized(views(memory, memory_ops), memory_ops))
        reopened.close()

    def test_rejected_calls_change_neither_the_ledger_nor_the_log(self):
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=2)
        ledger.seed_task("req-1", "task-1", CAPS)
        before = (ledger.seq, ledger.events)
        self.assertEqual(self.code_of(ContractError, ledger.claim, "req-1", "task-9", 1, "idem-1", "owner-1", 1, 1,
                                      {"dispatches": 1, "tokens": 1, "cost": 1}, ["src/a.py"]), "E_SCOPE_UNKNOWN")
        self.assertEqual(self.code_of(ContractError, ledger.stop, "req-1", ""), "E_STOP_REASON")
        with self.assertRaises(TypeError):
            ledger.handoff("op-x")
        self.assertEqual((ledger.seq, ledger.events), before)
        # The instance is still usable, and the rejected calls left no trace in the log.
        ledger.seed_task("req-1", "task-2", CAPS)
        self.assertEqual(ledger.seq, before[0] + 1)
        expected = (ledger.seq, ledger.events, ledger.snapshot("req-1"))
        ledger.close()
        reopened = open_ledger(self.path)
        self.assertEqual((reopened.seq, reopened.events, reopened.snapshot("req-1")), expected)
        reopened.close()

    def test_an_idempotent_claim_is_recorded_and_replays_to_the_same_operation(self):
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS)
        ledger.seed_task("req-1", "task-1", CAPS)
        args = ("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 1, "cost": 1}, ["src/a.py"])
        first = ledger.claim(*args)
        second = ledger.claim(*args)
        self.assertEqual((second["operation_id"], second["replayed"]), (first["operation_id"], True))
        self.assertEqual(ledger.seq, 4)
        expected = (ledger.events, ledger.operation(first["operation_id"]))
        ledger.close()
        reopened = open_ledger(self.path)
        self.assertEqual((reopened.events, reopened.operation(first["operation_id"])), expected)
        reopened.close()

    def test_a_mapping_keeps_its_order_through_the_log(self):
        # ``settle`` records a mapping entry by entry, in the mapping's own order:
        # a log that sorted the keys would replay the events in another order.
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=2)
        ops = []
        for task, mapping in (("task-1", {"tokens": 12, "cost": 3}), ("task-2", {"cost": 4, "tokens": 9})):
            ledger.seed_task("req-1", task, CAPS)
            op = ledger.claim("req-1", task, 1, "idem-" + task, "owner-1", 1, 1, {"dispatches": 1, "tokens": 40,
                                                                                    "cost": 20}, ["src/" + task])["operation_id"]
            ledger.handoff(op, "owner-1", 1)
            ledger.mark_running(op, "owner-1", 1)
            ledger.settle(op, mapping, evidence_hash="1" * 64)
            ops.append(op)
        observed = [(e["operation_id"], e["payload"]["dimension"]) for e in ledger.events if e["type"] == "usage.observed"]
        self.assertEqual(observed, [(ops[0], "tokens"), (ops[0], "cost"), (ops[1], "cost"), (ops[1], "tokens")])
        expected = ledger.events
        ledger.close()
        reopened = open_ledger(self.path)
        self.assertEqual(reopened.events, expected)
        reopened.close()

    def test_integers_that_never_reach_an_event_are_recorded(self):
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=2**60)
        ledger.seed_task("req-1", "task-1", CAPS, max_attempts=2**60)
        op = ledger.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 1, "cost": 1},
                          ["src/a.py"], now=2**60)["operation_id"]
        ledger.renew(op, "owner-1", 1, 2**60 + 1, 2**61)
        ledger.handoff(op, "owner-1", 1, 2**60 + 2)
        expected = (ledger.seq, ledger.events, ledger.operation(op))
        ledger.close()
        reopened = open_ledger(self.path)
        self.assertEqual((reopened.seq, reopened.events, reopened.operation(op)), expected)
        reopened.close()

    def test_create_and_open_refuse_the_wrong_file(self):
        create_ledger(self.path).close()
        original = Path(self.path).read_bytes()
        with self.subTest(case="create over an existing ledger"):
            self.assertEqual(self.code_of(LedgerError, create_ledger, self.path), "E_LEDGER_EXISTS")
            self.assertEqual(Path(self.path).read_bytes(), original)
        other = self.work / "notes.txt"
        other.write_text("not a ledger\n")
        with self.subTest(case="create over another file"):
            self.assertEqual(self.code_of(LedgerError, create_ledger, str(other)), "E_LEDGER_EXISTS")
            self.assertEqual(other.read_text(), "not a ledger\n")
        missing = self.work / "missing.db"
        with self.subTest(case="open a missing ledger"):
            self.assertEqual(self.code_of(LedgerError, open_ledger, str(missing)), "E_LEDGER_MISSING")
            self.assertFalse(missing.exists(), "opening created a ledger")
        with self.subTest(case="open a file that is not a database"):
            self.assertEqual(self.code_of(LedgerError, open_ledger, str(other)), "E_LEDGER_OPEN")
        foreign = self.work / "foreign.db"
        db = sqlite3.connect(foreign)
        db.execute("CREATE TABLE things (x)")
        db.commit()
        db.close()
        with self.subTest(case="open a database that is not a ledger"):
            self.assertEqual(self.code_of(LedgerError, open_ledger, str(foreign)), "E_LEDGER_FORMAT")
        for label, statement in (("another format", "UPDATE meta SET value='sentinelmesh-ledger-v1' WHERE key='format'"),
                                 ("an extra meta key", "INSERT INTO meta(key, value) VALUES ('note', 'x')"),
                                 ("a generation that is not an integer", "UPDATE meta SET value='one' WHERE key='generation'")):
            with self.subTest(case=label):
                copy = self.work / ("copy-%d.db" % len(label))
                shutil.copyfile(self.path, copy)
                db = sqlite3.connect(copy)
                db.execute(statement)
                db.commit()
                db.close()
                self.assertEqual(self.code_of(LedgerError, open_ledger, str(copy)), "E_LEDGER_FORMAT")
        for label, path in (("bytes", os.fsencode(self.path)), ("None", None), ("a Path", Path(self.path)), ("empty", "")):
            with self.subTest(case="a path given as " + label):
                self.assertEqual(self.code_of(LedgerError, open_ledger, path), "E_LEDGER_PATH")
                self.assertEqual(self.code_of(LedgerError, create_ledger, path), "E_LEDGER_PATH")
        fresh = self.work / "fresh.db"
        for generation in (0, True):
            with self.subTest(case="create with generation %r" % generation):
                self.assertEqual(self.code_of(ContractError, create_ledger, str(fresh), generation=generation),
                                 "E_INTEGER")
                self.assertFalse(fresh.exists(), "a refused create left a file")

    def test_a_ledger_created_at_another_generation_reopens_there(self):
        ledger = create_ledger(self.path, generation=7)
        self.assertEqual(ledger.generation, 7)
        ledger.seed_request("req-1", "repo-1", CAPS)
        self.assertEqual(ledger.events[-1]["generation"], 7)
        ledger.close()
        reopened = open_ledger(self.path)
        self.assertEqual((reopened.generation, reopened.events[-1]["generation"]), (7, 7))
        reopened.close()

    def test_a_damaged_log_is_refused_without_being_changed(self):
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS)
        ledger.seed_task("req-1", "task-1", CAPS)
        ledger.seed_task("req-1", "task-2", CAPS)
        op = ledger.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 1, "cost": 1},
                          ["src/a.py"])["operation_id"]
        ledger.handoff(op, "owner-1", 1)
        ledger.close()
        rows = rows_of(self.path)
        self.assertEqual(len(rows), 5)
        commands = [row[1] for row in rows]
        # Legal baseline: the log as written replays, and so does a rewrite of the same chain.
        open_ledger(self.path).close()
        claim = json.loads(commands[3])
        self.assertEqual((claim["method"], claim["operation_id"]), ("claim", op))

        def edited(index, command):
            return commands[:index] + [command] + commands[index + 1:]

        cases = {
            "a command edited without its digest": ("E_LEDGER_CHAIN", [rows[0], (2, commands[1].replace("task-1", "task-9"),
                                                                                rows[1][2], rows[1][3])] + rows[2:]),
            "an entry removed": ("E_LEDGER_CHAIN", [rows[0], rows[1], (3, rows[3][1], rows[3][2], rows[3][3]), rows[4]]),
            "an entry whose previous digest is wrong": ("E_LEDGER_CHAIN", rows[:2] + [(3, rows[2][1], rows[2][2], "0" * 64)] + rows[3:]),
            "a re-chained command the scheduler refuses": (
                "E_LEDGER_REPLAY", rechained(edited(3, commands[3].replace('"task-1"', '"task-9"')))),
            "a re-chained unknown method": (
                "E_LEDGER_REPLAY", rechained(edited(1, commands[1].replace('"seed_task"', '"drop_task"')))),
            "a re-chained claim that replays to another operation": (
                "E_LEDGER_REPLAY", rechained(commands[:4] + [json.dumps(dict(claim, operation_id="op-" + "f" * 32),
                                                                        separators=(",", ":"))])),
            "a re-chained command with a float": (
                "E_LEDGER_REPLAY", rechained(edited(4, commands[4].replace('"now":0', '"now":0.0')))),
            "a re-chained command that is not an object": ("E_LEDGER_REPLAY", rechained(edited(4, "[1,2]"))),
            # Lenient JSON would keep the last of two keys and replay a different,
            # valid command; as the last entry nothing after it fails, so only a
            # strict parser refuses it.
            "a re-chained last command with a duplicate key": (
                "E_LEDGER_REPLAY", rechained(edited(4, '{"method":"handoff","method":"stop",'
                                                       '"arguments":{"request":"req-1","reason":"X"}}'))),
            # The order is intact but the entries are numbered 1, 2, 4, 5, 6.
            "entries renumbered with the chain intact": (
                "E_LEDGER_CHAIN", [row if row[0] < 3 else (row[0] + 1,) + tuple(row[1:]) for row in rows]),
        }
        for label, (code, damaged_rows) in cases.items():
            with self.subTest(case=label):
                copy = str(self.work / ("damaged-%d.db" % len(label)))
                shutil.copyfile(self.path, copy)
                rewrite(copy, damaged_rows)
                before = Path(copy).read_bytes()
                self.assertEqual(self.code_of(LedgerError, open_ledger, copy), code)
                self.assertEqual(Path(copy).read_bytes(), before, "a refused open changed the file")
        with self.subTest(case="the legal chain rewritten as is"):
            copy = str(self.work / "rewritten.db")
            shutil.copyfile(self.path, copy)
            rewrite(copy, rechained(commands))
            reopened = open_ledger(copy)
            self.assertEqual(reopened.operation(op)["state"], "HANDOFF_ACCEPTED")
            reopened.close()

    def test_another_process_that_takes_over_makes_this_instance_stale(self):
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS)
        done = self.child("import sys\n"
                          "from sentinelmesh.durable_ledger import open_ledger\n"
                          "ledger = open_ledger(sys.argv[1])\n"
                          "print(ledger.rollover())\n"
                          "ledger.close()\n", self.path)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "2"), done.stderr)
        self.assertEqual(self.code_of(LedgerError, ledger.seed_task, "req-1", "task-1", CAPS), "E_LEDGER_STALE")
        for label, call in (("a read of the generation", lambda: ledger.generation),
                            ("a snapshot", lambda: ledger.snapshot("req-1")),
                            ("the events", lambda: ledger.events),
                            ("another mutation", lambda: ledger.seed_task("req-1", "task-2", CAPS))):
            with self.subTest(case=label):
                self.assertEqual(self.code_of(LedgerError, call), "E_LEDGER_UNAVAILABLE")
        ledger.close()
        reopened = open_ledger(self.path)
        self.assertEqual((reopened.generation, reopened.seq), (2, 2))
        self.assertEqual(self.code_of(ContractError, reopened.snapshot, "req-1", "task-1"), "E_SCOPE_UNKNOWN")
        reopened.close()

    def test_a_second_instance_in_this_process_is_caught_up(self):
        # ledger-catch-up (2026-10-16): the old assertion -- the instance that did not write last is
        # stale -- is wrong now.  Each write first replays what the other instance appended; only a
        # rollover by another controller still makes an instance stale (the case above).
        first = create_ledger(self.path)
        first.seed_request("req-1", "repo-1", CAPS)
        second = open_ledger(self.path)
        second.seed_task("req-1", "task-1", CAPS)
        first.seed_task("req-1", "task-2", CAPS)
        self.assertEqual(first.seq, 3)
        second.seed_task("req-1", "task-3", CAPS)
        self.assertEqual(second.seq, 4)
        first.refresh()
        self.assertEqual((first.seq, first.snapshot("req-1")), (4, second.snapshot("req-1")))
        first.close()
        second.close()

    @unittest.skipIf(os.name != "posix", "a read-only directory does not refuse a new file on Windows")
    def test_a_failed_append_makes_the_instance_unavailable_and_records_nothing(self):
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS)
        # The rollback journal cannot be created in a read-only directory, so the
        # append fails after the in-memory Scheduler already accepted the call.
        os.chmod(self.work, stat.S_IRUSR | stat.S_IXUSR)
        try:
            self.assertEqual(self.code_of(LedgerError, ledger.seed_task, "req-1", "task-1", CAPS), "E_LEDGER_WRITE")
            self.assertEqual(self.code_of(LedgerError, ledger.snapshot, "req-1"), "E_LEDGER_UNAVAILABLE")
        finally:
            os.chmod(self.work, 0o700)
        ledger.close()
        reopened = open_ledger(self.path)
        self.assertEqual(reopened.seq, 1)
        self.assertEqual(self.code_of(ContractError, reopened.snapshot, "req-1", "task-1"), "E_SCOPE_UNKNOWN")
        reopened.seed_task("req-1", "task-1", CAPS)
        reopened.close()

    def test_a_closed_instance_refuses_everything(self):
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS)
        ledger.close()
        for label, call in (("a mutation", lambda: ledger.seed_task("req-1", "task-1", CAPS)),
                            ("a read", lambda: ledger.snapshot("req-1")),
                            ("the generation", lambda: ledger.generation)):
            with self.subTest(case=label):
                self.assertEqual(self.code_of(LedgerError, call), "E_LEDGER_UNAVAILABLE")
        with open_ledger(self.path) as reopened:
            self.assertEqual(reopened.seq, 1)

    def test_the_log_uses_the_rollback_journal_and_reads_write_nothing(self):
        ledger = create_ledger(self.path)
        ops = scenario(ledger)
        ledger.close()
        db = sqlite3.connect(self.path)
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        db.close()
        self.assertEqual(sorted(os.listdir(self.work)), ["ledger.db"], "a WAL or journal file was left behind")
        before = Path(self.path).read_bytes()
        reopened = open_ledger(self.path)
        views(reopened, ops)
        reopened.close()
        self.assertEqual(Path(self.path).read_bytes(), before, "opening and reading changed the file")

    def test_importing_has_no_side_effect(self):
        done = subprocess.run([PY, "-c", "import sys, sentinelmesh.durable_ledger; "
                                         "assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"],
                              cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "IMPORT_SAFE_OK"), done.stderr)
        self.assertTrue(hasattr(durable_ledger, "open_ledger"))


if __name__ == "__main__":
    unittest.main()
