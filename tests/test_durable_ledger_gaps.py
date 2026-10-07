"""Planner follow-up for durable-ledger (review of attempt-001).

Rules the oracle and the executor tests left unguarded, and what the review
found:

* a refused open closes its connection on every way out -- a broken chain, a
  command refused on replay, a damaged format, a stored generation the
  Scheduler refuses -- so a process that keeps retrying a damaged ledger does
  not leak a descriptor per attempt (contract item 5);
* a stored generation the Scheduler refuses is a damaged ledger
  (``E_LEDGER_FORMAT``), and a command that breaks the parser in any way, a
  nesting too deep for it included, is ``E_LEDGER_REPLAY``: opening raises
  only ledger errors;
* the chain links each entry to the one before it: an entry whose own digest
  was recomputed over a wrong ``previous`` is still refused;
* creating never takes over or removes a file it did not create: a file that
  appears between the existence check and the creation is ``E_LEDGER_EXISTS``
  and stays as it was, even an empty one, and a failed create removes only
  the file it created itself; opening never creates a file, even when the
  path vanishes between the check and the connection.

Version 2 of the contract adds what the independent review found:

* any exception from the in-memory call other than the Scheduler's own
  ``ContractError`` -- an interrupt above all -- makes the instance unusable,
  because the in-memory state may already have changed while the log has
  nothing: appending later commands on top of it would write a log that no
  longer replays;
* a forged log cannot give two claims the same operation id, nor record an id
  that is not one the Scheduler would generate.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh import durable_ledger
from sentinelmesh.durable_ledger import LedgerError, create_ledger, open_ledger
from sentinelmesh.scheduler import ContractError, Scheduler

CAPS = {"dispatches": 9, "tokens": 500}


def entry_sha256(previous: str, raw: bytes) -> str:
    return hashlib.sha256(previous.encode("ascii") + b"\n" + raw).hexdigest()


def fd_count() -> int:
    return len(os.listdir("/dev/fd"))


class DurableLedgerGaps(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="durable-ledger-gaps-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.path = str(self.work / "ledger.db")
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=2)
        ledger.seed_task("req-1", "task-1", CAPS)
        ledger.close()

    def code_of(self, func, *args, **kwargs):
        with self.assertRaises(LedgerError) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def copy(self, name):
        target = str(self.work / name)
        shutil.copyfile(self.path, target)
        return target

    def execute(self, path, *statements):
        db = sqlite3.connect(path, isolation_level=None)
        try:
            for statement, params in statements:
                db.execute(statement, params)
        finally:
            db.close()

    def rows(self, path):
        db = sqlite3.connect(path)
        try:
            return db.execute("SELECT seq, command, sha256, previous FROM commands ORDER BY seq").fetchall()
        finally:
            db.close()

    def append_rechained(self, path, command):
        """Append ``command`` with a correct chain link."""
        last = self.rows(path)[-1]
        digest = entry_sha256(last[2], command.encode("utf-8"))
        self.execute(path, ("INSERT INTO commands(seq, command, sha256, previous) VALUES (?, ?, ?, ?)",
                            (last[0] + 1, command, digest, last[2])))

    def test_a_refused_open_leaks_no_descriptor(self):
        nested = '{"method":"rollover","arguments":{"x":' + "[" * 200000 + "]" * 200000 + "}}"
        damaged = {}
        damaged["a broken chain"] = self.copy("chain.db")
        self.execute(damaged["a broken chain"], ("UPDATE commands SET sha256=? WHERE seq=1", ("0" * 64,)))
        damaged["a command refused on replay"] = self.copy("replay.db")
        self.append_rechained(damaged["a command refused on replay"],
                              '{"method":"seed_task","arguments":{"request":"req-9","task":"t","caps":{"dispatches":1},'
                              '"max_attempts":1}}')
        damaged["a nesting too deep to parse"] = self.copy("nested.db")
        self.append_rechained(damaged["a nesting too deep to parse"], nested)
        damaged["another format"] = self.copy("format.db")
        self.execute(damaged["another format"], ("UPDATE meta SET value='x' WHERE key='format'", ()))
        damaged["a generation the Scheduler refuses"] = self.copy("generation.db")
        self.execute(damaged["a generation the Scheduler refuses"], ("UPDATE meta SET value='0' WHERE key='generation'", ()))
        expected = {"a broken chain": "E_LEDGER_CHAIN", "a command refused on replay": "E_LEDGER_REPLAY",
                    "a nesting too deep to parse": "E_LEDGER_REPLAY", "another format": "E_LEDGER_FORMAT",
                    "a generation the Scheduler refuses": "E_LEDGER_FORMAT"}
        open_ledger(self.path).close()
        before = fd_count()
        for label, path in damaged.items():
            with self.subTest(case=label):
                for _ in range(3):
                    self.assertEqual(self.code_of(open_ledger, path), expected[label])
                self.assertEqual(fd_count(), before, "a refused open left its connection open")

    def test_a_generation_beyond_the_json_range_is_a_damaged_ledger(self):
        path = self.copy("big.db")
        self.execute(path, ("UPDATE meta SET value=? WHERE key='generation'", (str(2**53),)))
        self.assertEqual(self.code_of(open_ledger, path), "E_LEDGER_FORMAT")

    def test_an_entry_rehashed_over_a_wrong_previous_is_refused(self):
        # The last entry's digest is recomputed over a ``previous`` that is not
        # the digest of the entry before it: only the link check can refuse it.
        path = self.copy("relinked.db")
        rows = self.rows(path)
        last = rows[-1]
        wrong = "0" * 64
        self.execute(path, ("UPDATE commands SET sha256=?, previous=? WHERE seq=?",
                            (entry_sha256(wrong, last[1].encode("utf-8")), wrong, last[0])))
        self.assertEqual(self.code_of(open_ledger, path), "E_LEDGER_CHAIN")

    def test_a_file_that_appears_before_the_creation_is_left_alone(self):
        real_lstat = os.lstat
        for label, content in (("an empty file", b""), ("a file with content", b"someone else's\n")):
            with self.subTest(case=label):
                target = self.work / ("raced-%d.db" % len(content))
                target.write_bytes(content)

                def hidden(path, *args, **kwargs):
                    # The existence check misses the file, as it would if another
                    # process created it right after the check.
                    if os.fsdecode(path) == str(target):
                        raise FileNotFoundError(path)
                    return real_lstat(path, *args, **kwargs)

                with mock.patch.object(os, "lstat", hidden):
                    self.assertEqual(self.code_of(create_ledger, str(target)), "E_LEDGER_EXISTS")
                self.assertEqual(target.read_bytes(), content, "create took over a file it did not create")

    def test_a_failed_create_removes_only_its_own_file(self):
        target = self.work / "new.db"
        real_connect = sqlite3.connect

        class FailingAtTheTransaction:
            """A real connection whose first transaction fails after another
            process moved the new file away and put its own empty file there."""

            def __init__(self, real):
                self.real = real

            def execute(self, sql, *args):
                if sql.lstrip().upper().startswith("BEGIN"):
                    os.rename(target, str(target) + ".moved")
                    target.write_bytes(b"")
                    raise sqlite3.OperationalError("disk I/O error")
                return self.real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.real, name)

        def connect(path, *args, **kwargs):
            return FailingAtTheTransaction(real_connect(path, *args, **kwargs))

        with mock.patch.object(durable_ledger.sqlite3, "connect", connect):
            self.assertEqual(self.code_of(create_ledger, str(target)), "E_LEDGER_OPEN")
        self.assertTrue(target.exists(), "a failed create removed another process's file")
        self.assertEqual(target.read_bytes(), b"")

    def test_opening_never_creates_a_file(self):
        target = self.work / "vanished.db"
        real_lstat = os.lstat

        def present(path, *args, **kwargs):
            # The existence check sees a ledger that is gone by the time the
            # connection is made.
            if os.fsdecode(path) == str(target):
                return real_lstat(self.path)
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(os, "lstat", present):
            with self.assertRaises(LedgerError) as ctx:
                open_ledger(str(target))
        self.assertIn(ctx.exception.code, ("E_LEDGER_OPEN", "E_LEDGER_MISSING"))
        self.assertFalse(target.exists(), "opening created a file")

    def test_a_generation_too_long_to_convert_is_a_damaged_ledger(self):
        path = self.copy("long.db")
        self.execute(path, ("UPDATE meta SET value=? WHERE key='generation'", ("1" * 5000,)))
        self.assertEqual(self.code_of(open_ledger, path), "E_LEDGER_FORMAT")

    def test_an_entry_removed_and_the_rest_renumbered_is_refused(self):
        # Every remaining entry is still consistent with its own previous, but
        # the entry after the gap no longer links to the one before it.
        path = self.copy("dropped.db")
        ledger = open_ledger(path)
        ledger.seed_task("req-1", "task-2", CAPS)
        ledger.seed_task("req-1", "task-3", CAPS)
        ledger.close()
        rows = self.rows(path)
        self.assertEqual(len(rows), 4)
        kept = [rows[0], rows[1]] + [(row[0] - 1,) + tuple(row[1:]) for row in rows[3:]]
        self.execute(path, ("DELETE FROM commands", ()))
        for row in kept:
            self.execute(path, ("INSERT INTO commands(seq, command, sha256, previous) VALUES (?, ?, ?, ?)", row))
        self.assertEqual(self.code_of(open_ledger, path), "E_LEDGER_CHAIN")

    def test_an_interrupt_inside_the_in_memory_call_makes_the_instance_unusable(self):
        real_emit = Scheduler._emit

        def interrupted(scheduler, operation_id, event_type, payload, **kwargs):
            if event_type == "scheduler.claimed":
                raise KeyboardInterrupt
            return real_emit(scheduler, operation_id, event_type, payload, **kwargs)

        for label, failure in (("an interrupt", KeyboardInterrupt), ("any other error", RuntimeError)):
            with self.subTest(case=label):
                path = self.copy("interrupted-%s.db" % failure.__name__)
                ledger = open_ledger(path)

                def failing(scheduler, operation_id, event_type, payload, **kwargs):
                    if event_type == "scheduler.claimed":
                        raise failure("inside the claim")
                    return real_emit(scheduler, operation_id, event_type, payload, **kwargs)

                with mock.patch.object(Scheduler, "_emit", failing):
                    with self.assertRaises(failure):
                        ledger.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 1},
                                     ["src/a.py"])
                self.assertEqual(self.code_of(ledger.seed_task, "req-1", "task-2", CAPS), "E_LEDGER_UNAVAILABLE")
                self.assertEqual(self.code_of(ledger.snapshot, "req-1"), "E_LEDGER_UNAVAILABLE")
                ledger.close()
                reopened = open_ledger(path)
                self.assertEqual(reopened.seq, 2, "the interrupted call was recorded")
                self.assertEqual(reopened.snapshot("req-1")["dispatches"]["held"], 0)
                reopened.close()
        # A refusal by the Scheduler itself still leaves the instance usable.
        ledger = open_ledger(self.path)
        with self.assertRaises(ContractError):
            ledger.claim("req-1", "task-9", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 1}, ["src/a.py"])
        ledger.seed_task("req-1", "task-2", CAPS)
        ledger.close()
        self.assertIs(Scheduler._emit, real_emit)
        del interrupted

    def test_a_forged_log_cannot_reuse_or_invent_an_operation_id(self):
        ledger = open_ledger(self.copy("claims.db"))
        ledger.seed_task("req-1", "task-2", CAPS)
        op = ledger.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 1},
                          ["src/a.py"])["operation_id"]
        path = ledger.path
        ledger.close()
        rows = self.rows(path)
        claim = json.loads(rows[-1][1])
        second = dict(claim, arguments=dict(claim["arguments"], task="task-2", key="idem-2",
                                            resource_keys=["src/b.py"]))
        cases = {
            "a second claim recorded with the first claim's id": json.dumps(second, separators=(",", ":")),
            "a claim recorded with an id the Scheduler never generates": json.dumps(
                dict(second, operation_id="op-NOT-HEX"), separators=(",", ":")),
        }
        for label, command in cases.items():
            with self.subTest(case=label):
                target = str(self.work / ("forged-%d.db" % len(label)))
                shutil.copyfile(path, target)
                self.append_rechained(target, command)
                self.assertEqual(self.code_of(open_ledger, target), "E_LEDGER_REPLAY")
        # Legal baseline: the same second claim with a fresh id replays.
        target = str(self.work / "fresh-id.db")
        shutil.copyfile(path, target)
        self.append_rechained(target, json.dumps(dict(second, operation_id="op-" + "e" * 32), separators=(",", ":")))
        reopened = open_ledger(target)
        self.assertEqual(reopened.operation("op-" + "e" * 32)["task_id"], "task-2")
        self.assertEqual(reopened.operation(op)["task_id"], "task-1")
        reopened.close()


if __name__ == "__main__":
    unittest.main()
