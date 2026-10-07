"""Executor tests for ``sentinelmesh.durable_ledger``.

These cases complement the planner oracle (``tests/test_durable_ledger_oracle.py``)
instead of repeating it: the exact shape and chain of one log entry, a rejected
call that leaves neither the state nor the log behind, concurrent claims on a
single instance, the ``reserve`` alias, a non-canonical generation, a generation
that cannot be encoded, and the context manager.  Every rejection starts from a
verified legal baseline and then changes exactly one thing.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from sentinelmesh.durable_ledger import (
    LEDGER_FORMAT,
    MUTATING_METHODS,
    DurableScheduler,
    LedgerError,
    create_ledger,
    open_ledger,
)
from sentinelmesh.scheduler import ContractError, Scheduler

CAPS = {"dispatches": 9, "tokens": 900, "cost": 900}
RESERVATION = {"dispatches": 1, "tokens": 10, "cost": 10}
H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64


def digest_of(previous: str, command: str) -> str:
    return hashlib.sha256(previous.encode("ascii") + b"\n" + command.encode("utf-8")).hexdigest()


def rows_of(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT seq, command, sha256, previous FROM commands ORDER BY seq").fetchall()
    finally:
        db.close()


def meta_of(path):
    db = sqlite3.connect(path)
    try:
        return dict(db.execute("SELECT key, value FROM meta").fetchall())
    finally:
        db.close()


def pairs(text):
    """A JSON text with every object as an ordered list of key/value pairs."""
    return json.loads(text, object_pairs_hook=lambda items: items)


class DurableLedgerCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="durable-ledger-exec-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.addCleanup(os.chmod, self.work, 0o700)
        self.path = str(self.work / "ledger.db")

    def code_of(self, error_type, func, *args, **kwargs):
        with self.assertRaises(error_type) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def seeded(self, **kwargs):
        ledger = create_ledger(self.path, **kwargs)
        self.addCleanup(ledger.close)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        for task in ("task-1", "task-2"):
            ledger.seed_task("req-1", task, CAPS, max_attempts=5)
        return ledger

    def claim(self, ledger, task="task-1", key="idem-1", resource="src/a.py"):
        return ledger.claim("req-1", task, 1, key, "owner-1", 1, 1, RESERVATION,
                            [resource])["operation_id"]


class LogShape(DurableLedgerCase):
    def test_one_entry_per_successful_call_in_the_right_shape(self):
        ledger = self.seeded()
        op = self.claim(ledger)
        ledger.handoff(op, "owner-1", 1, 5)
        ledger.mark_running(op, "owner-1", 1)
        ledger.settle(op, {"tokens": 12, "cost": 3}, evidence_hash=H1)
        other = self.claim(ledger, task="task-2", key="idem-2", resource="src/b.py")
        ledger.handoff(other, "owner-1", 1, 6)
        ledger.observe(other, "tokens", "call-2", 1, 7, False, H2)
        ledger.renew(op, "owner-1", 1, 9, 30)
        ledger.stop("req-1", "OPERATOR_STOP")
        # 1 request + 2 tasks + 2 claims + 2 handoffs + mark_running + settle
        # + observe + renew + stop
        self.assertEqual(ledger.seq, 12)
        rows = rows_of(self.path)
        self.assertEqual([row[0] for row in rows], list(range(1, 13)))
        previous = ""
        for seq, command, digest, row_previous in rows:
            self.assertEqual(row_previous, previous, f"entry {seq} points at the wrong predecessor")
            self.assertEqual(digest, digest_of(previous, command), f"entry {seq} has the wrong digest")
            previous = digest
        self.assertEqual(meta_of(self.path), {"format": LEDGER_FORMAT, "generation": "1"})
        # Every argument is recorded by name, defaults included.
        for seq, command, _digest, _previous in rows:
            entry = pairs(command)
            keys = [key for key, _value in entry]
            self.assertEqual(keys[0], "method", f"entry {seq} starts with {keys[:2]}")
            self.assertEqual(keys[:2], ["method", "arguments"], f"entry {seq} starts with {keys[:2]}")
            method = dict(entry)["method"]
            self.assertIn(method, MUTATING_METHODS)
            expected = list(inspect.signature(getattr(Scheduler, method)).parameters)[1:]
            recorded = [key for key, _value in dict(entry)["arguments"]]
            self.assertEqual(recorded, expected, f"entry {seq} recorded {recorded}")
        claim_entry = pairs(rows[4 - 1][1])
        self.assertEqual([key for key, _value in claim_entry], ["method", "arguments", "operation_id"])
        self.assertEqual(dict(claim_entry)["operation_id"], op)
        handoff_entry = pairs(rows[5 - 1][1])
        self.assertEqual([key for key, _value in handoff_entry], ["method", "arguments"])
        self.assertEqual(dict(handoff_entry)["method"], "handoff")
        # ``settle`` records a mapping entry by entry, in the mapping's own order.
        settle_arguments = dict(dict(pairs(rows[7 - 1][1]))["arguments"])
        self.assertEqual([key for key, _value in settle_arguments["dimension"]], ["tokens", "cost"])
        self.assertEqual(dict(settle_arguments["dimension"]), {"tokens": 12, "cost": 3})
        # A default that the caller did not pass is still recorded, by name.
        renew_arguments = dict(dict(pairs(rows[11 - 1][1]))["arguments"])
        self.assertEqual(dict(renew_arguments)["extension"], 30)

    def test_a_reopened_ledger_continues_the_chain_where_it_stopped(self):
        ledger = self.seeded()
        self.claim(ledger)
        head = rows_of(self.path)[-1]
        ledger.close()
        reopened = open_ledger(self.path)
        self.addCleanup(reopened.close)
        self.claim(reopened, task="task-2", key="idem-2", resource="src/b.py")
        rows = rows_of(self.path)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[-1][3], head[2], "the reopened instance did not chain onto the last digest")

    def test_a_rejected_call_changes_neither_the_state_nor_the_log(self):
        ledger = self.seeded()
        op = self.claim(ledger)
        before = (ledger.seq, ledger.events, ledger.snapshot("req-1"), rows_of(self.path))
        rejections = (
            (ContractError, "E_CLAIM_SHAPE",
             lambda: ledger.claim("req-1", "task-2", 1, "idem-2", "owner-1", 1, 1, {"dispatches": 2}, ["src/b.py"])),
            (ContractError, "E_ACTIVE_TASK",
             lambda: ledger.claim("req-1", "task-1", 1, "idem-3", "owner-1", 1, 1, RESERVATION, ["src/z.py"])),
            (ContractError, "E_HANDOFF_STATE", lambda: ledger.handoff(op, "owner-1", 1, 10**6)),
            (ContractError, "E_REQUEST_SHAPE", lambda: ledger.seed_request(1, "repo-1", CAPS)),
            (TypeError, None, lambda: ledger.finish(op, "owner-1")),
            (TypeError, None, lambda: ledger.renew(op, "owner-1")),
            (ContractError, "E_UNKNOWN_REASON", lambda: ledger.unknown(op, "owner-1", 1, reason="NOPE")),
        )
        for index, (error_type, code, call) in enumerate(rejections):
            with self.subTest(rejection=index):
                if code is None:
                    with self.assertRaises(error_type):
                        call()
                else:
                    self.assertEqual(self.code_of(error_type, call), code)
                self.assertEqual((ledger.seq, ledger.events, ledger.snapshot("req-1"), rows_of(self.path)), before,
                                 "a rejected call left a trace")
        # The instance is still usable, and only the successful call is logged.
        self.claim(ledger, task="task-2", key="idem-4", resource="src/b.py")
        self.assertEqual(ledger.seq, before[0] + 1)
        self.assertEqual(len(rows_of(self.path)), before[0] + 1)
        expected_events = ledger.events
        ledger.close()
        reopened = open_ledger(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.events, expected_events)


class ConcurrentClaims(DurableLedgerCase):
    def test_threads_claim_different_tasks_on_one_instance(self):
        ledger = self.seeded()
        tasks = ("task-1", "task-2", "task-3", "task-4")
        for task in ("task-3", "task-4"):
            ledger.seed_task("req-1", task, CAPS, max_attempts=5)
        results: dict[str, dict[str, object]] = {}
        errors: list[BaseException] = []
        start = threading.Event()

        def claim(task):
            try:
                start.wait(5)
                results[task] = ledger.claim("req-1", task, 1, "idem-" + task, "owner-" + task, 1, 1,
                                             RESERVATION, ["src/" + task])
            except BaseException as exc:  # reported below, never swallowed
                errors.append(exc)

        threads = [threading.Thread(target=claim, args=(task,)) for task in tasks]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), sorted(tasks))
        self.assertEqual(len({result["operation_id"] for result in results.values()}), len(tasks))
        self.assertTrue(all(result["replayed"] is False for result in results.values()))
        self.assertEqual(ledger.seq, 1 + len(tasks) + len(tasks))
        self.assertEqual(len(rows_of(self.path)), 1 + len(tasks) + len(tasks))
        expected = ledger.events
        claimed = [event["operation_id"] for event in ledger.events if event["type"] == "scheduler.claimed"]
        self.assertEqual(sorted(claimed), sorted(result["operation_id"] for result in results.values()))
        ledger.close()
        reopened = open_ledger(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.events, expected, "the replay reordered the concurrent claims")
        self.assertEqual(reopened.seq, 1 + len(tasks) + len(tasks))
        for result in results.values():
            self.assertEqual(reopened.operation(result["operation_id"])["state"], "RESERVED")


class StaleDetection(DurableLedgerCase):
    def test_a_log_head_that_keeps_the_sequence_but_not_the_digest_is_stale(self):
        ledger = self.seeded()
        op = self.claim(ledger)
        self.assertEqual(ledger.seq, 4)
        rows = rows_of(self.path)
        # A legal baseline: the log as written is exactly what this instance applied.
        self.assertEqual(rows[-1][0], ledger.seq)
        # Only the head entry is replaced, with another command of the same method:
        # the sequence number stays 4, so only the digest proves the log moved on.
        edited = json.loads(rows[-1][1])
        edited["arguments"]["now"] = 1
        replacement = json.dumps(edited, separators=(",", ":"), ensure_ascii=False)
        db = sqlite3.connect(self.path, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE commands SET command=?, sha256=? WHERE seq=?",
                       (replacement, digest_of(rows[-1][3], replacement), rows[-1][0]))
            db.execute("COMMIT")
        finally:
            db.close()
        self.assertEqual(self.code_of(LedgerError, ledger.seed_task, "req-1", "task-3", CAPS), "E_LEDGER_STALE")
        self.assertEqual(self.code_of(LedgerError, lambda: ledger.events), "E_LEDGER_UNAVAILABLE")
        ledger.close()
        # The rewritten log is intact and replays on its own.
        reopened = open_ledger(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.seq, 4)
        self.assertEqual(reopened.operation(op)["state"], "RESERVED")


class ReserveAlias(DurableLedgerCase):
    def test_a_reserved_claim_is_recorded_as_a_claim(self):
        ledger = self.seeded()
        op = ledger.reserve("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, RESERVATION, ["src/a.py"])["operation_id"]
        expected_view = ledger.operation(op)
        expected_events = ledger.events
        self.assertEqual(expected_view["state"], "RESERVED")
        entry = dict(pairs(rows_of(self.path)[-1][1]))
        self.assertEqual(dict(entry)["method"], "claim")
        self.assertEqual(dict(entry)["operation_id"], op)
        ledger.close()
        reopened = open_ledger(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.operation(op), expected_view)
        self.assertEqual(reopened.events, expected_events)


class Refusals(DurableLedgerCase):
    def test_a_generation_with_a_leading_zero_is_refused(self):
        ledger = self.seeded()
        ledger.close()
        reopened = open_ledger(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(meta_of(self.path)["generation"], "1")
        copy = str(self.work / "zero-padded.db")
        shutil.copyfile(self.path, copy)
        db = sqlite3.connect(copy)
        db.execute("UPDATE meta SET value='007' WHERE key='generation'")
        db.commit()
        db.close()
        before = Path(copy).read_bytes()
        self.assertEqual(self.code_of(LedgerError, open_ledger, copy), "E_LEDGER_FORMAT")
        self.assertEqual(Path(copy).read_bytes(), before, "a refused open changed the file")

    def test_create_refuses_a_generation_that_cannot_be_encoded(self):
        ledger = create_ledger(self.path, generation=2**53 - 1)
        self.addCleanup(ledger.close)
        self.assertEqual(ledger.generation, 2**53 - 1)
        ledger.close()
        other = str(self.work / "too-big.db")
        self.assertEqual(self.code_of(ContractError, create_ledger, other, generation=2**53), "E_JSON_INTEGER")
        self.assertFalse(Path(other).exists(), "a refused create left a file")
        # The legal baseline is untouched.
        self.assertEqual(open_ledger(self.path).generation, 2**53 - 1)


class Lifecycle(DurableLedgerCase):
    def test_a_context_manager_closes_the_instance(self):
        with create_ledger(self.path) as ledger:
            ledger.seed_request("req-1", "repo-1", CAPS)
            ledger.seed_task("req-1", "task-1", CAPS)
            self.claim(ledger)
            self.assertEqual(ledger.seq, 3)
        for label, call in (("a mutation", lambda: ledger.seed_task("req-1", "task-1", CAPS)),
                            ("a read", lambda: ledger.snapshot("req-1")),
                            ("the generation", lambda: ledger.generation),
                            ("the events", lambda: ledger.events),
                            ("the sequence", lambda: ledger.seq)):
            with self.subTest(call=label):
                self.assertEqual(self.code_of(LedgerError, call), "E_LEDGER_UNAVAILABLE")
        # Closing again is harmless, and the log still opens.
        ledger.close()
        with open_ledger(self.path) as reopened:
            self.assertEqual(reopened.seq, 3)

    def test_a_failed_append_invalidates_the_instance(self):
        ledger = self.seeded()
        # The rollback journal cannot be created in a read-only directory, so the
        # append fails after the in-memory Scheduler already accepted the call.
        os.chmod(self.work, stat.S_IRUSR | stat.S_IXUSR)
        try:
            self.assertEqual(self.code_of(LedgerError, ledger.seed_task, "req-1", "task-3", CAPS), "E_LEDGER_WRITE")
            self.assertEqual(self.code_of(LedgerError, lambda: ledger.generation), "E_LEDGER_UNAVAILABLE")
        finally:
            os.chmod(self.work, 0o700)
        ledger.close()
        reopened = open_ledger(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.seq, 3)
        self.assertEqual(len(rows_of(self.path)), 3)
        self.assertEqual(self.code_of(ContractError, reopened.snapshot, "req-1", "task-3"), "E_SCOPE_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
