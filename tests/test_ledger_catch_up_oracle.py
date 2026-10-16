"""Planner oracle for ledger-catch-up (`.agent/design/serve-watch.md`, section 3.1 and the user's decisions).

Planner-provided; executors must not modify this file.

A ``DurableScheduler`` used to be the only writer of its log: a command another
instance appended made it stale (``E_LEDGER_STALE``) and unusable, so an
operator's ``smctl ledger stop`` or ``verdict`` from another process broke a
controller with a run in flight.  Now every mutation first takes the write lock
and replays the commands appended after this instance's head -- the same replay
an open does -- and only then runs in memory, appends and commits; ``refresh()``
does the same catch-up for reads, in a read transaction, and writes nothing.
Three things still make an instance stale: a ``rollover`` among the commands to
catch up (another controller took over), a checkpoint past this instance's
head (the commands it would need are gone), and a log whose next command does
not link to this instance's head.  A damaged tail is ``E_LEDGER_CHAIN``, a tail
the Scheduler refuses is ``E_LEDGER_REPLAY``, and any failure while catching up
invalidates the instance.  ``compact`` is unchanged.

Several cases start real ``python`` processes: two writers taking turns on one
ledger, and an operator's ``smctl ledger stop``/``verdict`` while
``smctl run serve`` has a run in flight.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh import durable_ledger
from sentinelmesh.durable_ledger import DurableScheduler, LedgerError, create_ledger, entry_sha256, open_ledger
from sentinelmesh.scheduler import ContractError

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = os.path.realpath(sys.executable)
NL = chr(10)
CAPS = {"dispatches": 9, "tokens": 500, "cost": 200}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
IDENTITY = {"pid": 4242, "boot_id": "12345678-1234-1234-1234-1234567890AB", "executable_sha256": "a" * 64,
            "worktree_id": "wt-1"}
H1, H2 = "1" * 64, "2" * 64


def rows_of(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT seq, command, sha256, previous FROM commands ORDER BY seq").fetchall()
    finally:
        db.close()


def checkpoint_of(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT seq, last_sha FROM checkpoint").fetchall()
    finally:
        db.close()


def execute(path, *statements):
    db = sqlite3.connect(path, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        for statement, parameters in statements:
            db.execute(statement, parameters)
        db.execute("COMMIT")
    finally:
        db.close()


def append_raw(path, command_text):
    """Append ``command_text`` as the next command with a correct chain (a forged but well-linked tail)."""
    seq, previous = rows_of(path)[-1][0], rows_of(path)[-1][2]
    execute(path, ("INSERT INTO commands(seq, command, sha256, previous) VALUES (?, ?, ?, ?)",
                   (seq + 1, command_text, entry_sha256(previous, command_text), previous)))


def views(ledger):
    """Everything an instance can say about request ``req-1``, read from memory."""
    ops = ledger.operation_ids()
    return {"seq": ledger.seq, "generation": ledger.generation, "events": list(ledger.events),
            "operation_ids": list(ops), "operations": [ledger.operation(op) for op in ops],
            "requests": list(ledger.request_ids()), "tasks": list(ledger.task_ids("req-1")),
            "task_views": [ledger.task("req-1", task) for task in ledger.task_ids("req-1")],
            "snapshot": ledger.snapshot("req-1"), "stop_reasons": list(ledger.stop_reasons("req-1"))}


class CatchUpCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="ledger-catch-up-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.path = str(self.work / "ledger.db")

    def code_of(self, error_type, func, *args, **kwargs):
        with self.assertRaises(error_type) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def opened(self):
        ledger = open_ledger(self.path)
        self.addCleanup(ledger.close)
        return ledger

    def fresh_views(self):
        with open_ledger(self.path) as ledger:
            return views(ledger)

    def unavailable(self, ledger):
        self.assertEqual(self.code_of(LedgerError, lambda: ledger.seq), "E_LEDGER_UNAVAILABLE")

    def seeded(self):
        """A created instance with one request and one task: two commands."""
        first = create_ledger(self.path)
        self.addCleanup(first.close)
        first.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        first.seed_task("req-1", "task-1", CAPS, max_attempts=3)
        return first


class CatchUpOracle(CatchUpCase):
    # -- writes catch up ------------------------------------------------------ #

    def test_two_instances_take_turns_and_each_sees_what_the_other_wrote(self):
        first = self.seeded()
        second = self.opened()
        second.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        first.seed_task("req-1", "task-3", CAPS, max_attempts=3)  # catches up task-2, then appends
        self.assertEqual(first.seq, 4)
        claim = second.claim("req-1", "task-1", 1, "idem-a", "owner-1", 1, 1, dict(RESERVATION), ["src/a.py"])
        op = claim["operation_id"]
        # The created instance replays the other one's claim with its recorded id, and goes on from it.
        first.handoff(op, "owner-1", 1, 1)
        first.mark_running(op, "owner-1", 1, process_identity=dict(IDENTITY))
        second.settle(op, {"tokens": 12, "cost": 3}, evidence_hash=H1)
        first.finish(op, "owner-1", 1, H2, True)
        self.assertEqual((first.seq, first.operation_ids()), (9, (op,)))
        second.refresh()
        fresh = self.fresh_views()
        self.assertEqual(views(first), fresh)
        self.assertEqual(views(second), fresh)
        self.assertEqual(fresh["operations"][0]["state"], "REVIEW_PENDING")
        self.assertEqual([row[0] for row in rows_of(self.path)], list(range(1, 10)))

    def test_writes_from_two_processes_are_serialized_and_none_is_lost(self):
        first = self.seeded()
        first.close()
        # Both writers open the ledger, wait at a starting line, then write as fast as they can, so
        # their transactions overlap: only a write lock taken before the catch-up keeps both going.
        go = self.work / "go"
        body = ("import os, sys, time" + NL
                + "from sentinelmesh.durable_ledger import open_ledger" + NL
                + "ledger = open_ledger(sys.argv[1])" + NL
                + "open(sys.argv[3] + '.' + sys.argv[2], 'w').close()" + NL
                + "while not os.path.exists(sys.argv[3]): time.sleep(0.001)" + NL
                + "for n in range(120):" + NL
                + "    ledger.seed_task('req-1', '%s-%d' % (sys.argv[2], n), {'dispatches': 9, 'tokens': 500, 'cost': 200})" + NL
                + "ledger.close()" + NL)
        writers = [subprocess.Popen([PY, "-c", body, self.path, name, str(go)], cwd=str(REPO_ROOT),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                   for name in ("left", "right")]
        deadline = time.monotonic() + 60
        while not all((self.work / ("go." + name)).exists() for name in ("left", "right")) and time.monotonic() < deadline:
            time.sleep(0.01)
        go.touch()
        results = [writer.communicate(timeout=300) for writer in writers]
        self.assertEqual([writer.returncode for writer in writers], [0, 0], results)
        with open_ledger(self.path) as ledger:
            tasks = ledger.task_ids("req-1")
            self.assertEqual(ledger.seq, 242)
        self.assertEqual(sorted(tasks), sorted(["task-1"] + ["%s-%d" % (side, n) for side in ("left", "right")
                                                             for n in range(120)]))

    def test_a_refusal_after_catching_up_keeps_what_was_caught_up_and_appends_nothing(self):
        first = self.seeded()
        second = self.opened()
        second.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        rows = rows_of(self.path)
        self.assertEqual(self.code_of(ContractError, first.claim, "req-1", "task-9", 1, "idem-x", "owner-1", 1, 1,
                                      dict(RESERVATION), ["src/x.py"]), "E_SCOPE_UNKNOWN")
        self.assertEqual(rows_of(self.path), rows)
        self.assertEqual((first.seq, first.task_ids("req-1")), (3, ("task-1", "task-2")))
        first.stop("req-1", "OPERATOR_STOP")  # still usable
        self.assertEqual(first.seq, 4)

    def test_an_error_inside_the_in_memory_call_still_invalidates_and_appends_nothing(self):
        first = self.seeded()
        second = self.opened()
        second.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        rows = rows_of(self.path)
        for error in (TypeError("boom"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                ledger = open_ledger(self.path)
                self.addCleanup(ledger.close)
                second.stop("req-1", "hold-%s" % type(error).__name__)  # something to catch up
                rows = rows_of(self.path)
                with mock.patch.object(type(ledger._scheduler), "resume", side_effect=error):
                    with self.assertRaises(type(error)):
                        ledger.resume("req-1", ["OPERATOR_STOP"], "ops-1", "note")
                self.unavailable(ledger)
                self.assertEqual(rows_of(self.path), rows)
        first.close()

    # -- refresh ------------------------------------------------------------- #

    def test_refresh_reads_what_others_wrote_and_writes_nothing(self):
        first = self.seeded()
        second = self.opened()
        second.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        second.stop("req-1", "SECURITY_HOLD")
        rows = rows_of(self.path)
        self.assertEqual(first.task_ids("req-1"), ("task-1",))  # a read alone does not catch up
        self.assertEqual(first.refresh(), {"seq": 4, "applied": 2})
        self.assertEqual(views(first), self.fresh_views())
        self.assertEqual(first.refresh(), {"seq": 4, "applied": 0})
        self.assertEqual(rows_of(self.path), rows)
        first.close()
        self.assertEqual(self.code_of(LedgerError, first.refresh), "E_LEDGER_UNAVAILABLE")

    # -- what still makes an instance stale ------------------------------------ #

    def test_a_rollover_by_another_controller_makes_this_instance_stale(self):
        first = self.seeded()
        watcher = self.opened()
        done = subprocess.run([PY, "-c", "import sys" + NL + "from sentinelmesh.durable_ledger import open_ledger" + NL
                               + "ledger = open_ledger(sys.argv[1])" + NL
                               + "ledger.seed_task('req-1', 'task-2', {'dispatches': 9, 'tokens': 500, 'cost': 200})" + NL
                               + "print(ledger.rollover())" + NL + "ledger.close()" + NL, self.path],
                              cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "2"), done.stderr)
        rows = rows_of(self.path)
        self.assertEqual(self.code_of(LedgerError, first.stop, "req-1", "OPERATOR_STOP"), "E_LEDGER_STALE")
        self.unavailable(first)
        self.assertEqual(self.code_of(LedgerError, watcher.refresh), "E_LEDGER_STALE")
        self.unavailable(watcher)
        self.assertEqual(rows_of(self.path), rows)
        with open_ledger(self.path) as reopened:
            self.assertEqual((reopened.generation, reopened.seq, reopened.task_ids("req-1")), (2, 4, ("task-1", "task-2")))

    def test_a_checkpoint_past_this_instance_makes_it_stale_and_one_at_its_head_does_not(self):
        first = self.seeded()
        behind = self.opened()
        other = open_ledger(self.path)
        other.compact()  # at sequence 2: the head of both instances
        other.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        first.seed_task("req-1", "task-3", CAPS, max_attempts=3)  # a checkpoint at its head: it catches up
        self.assertEqual((first.seq, first.checkpoint_seq), (4, 2))
        self.assertEqual(views(first), self.fresh_views())
        other.close()
        compactor = self.opened()
        compactor.compact()  # at sequence 4: past ``behind`` (2)
        self.assertEqual(checkpoint_of(self.path)[0][0], 4)
        self.assertEqual(self.code_of(LedgerError, behind.refresh), "E_LEDGER_STALE")
        self.unavailable(behind)
        # compact itself is unchanged: an instance the log moved past refuses to compact.
        compactor.stop("req-1", "OPERATOR_STOP")
        self.assertEqual(self.code_of(LedgerError, first.compact), "E_LEDGER_STALE")

    def test_a_log_that_does_not_link_to_this_instance_is_stale(self):
        first = self.seeded()
        rows = rows_of(self.path)
        # Another history: the second command replaced by a different, well-chained one, and a third after it.
        other = json.dumps({"method": "seed_task", "arguments": {"request": "req-1", "task": "task-other",
                                                                 "caps": CAPS, "max_attempts": 3}})
        execute(self.path, ("DELETE FROM commands WHERE seq = 2", ()),
                ("INSERT INTO commands(seq, command, sha256, previous) VALUES (?, ?, ?, ?)",
                 (2, other, entry_sha256(rows[0][2], other), rows[0][2])))
        append_raw(self.path, json.dumps({"method": "stop", "arguments": {"request": "req-1", "reason": "x"}}))
        self.assertEqual(self.code_of(LedgerError, first.stop, "req-1", "OPERATOR_STOP"), "E_LEDGER_STALE")
        self.unavailable(first)

    def test_a_damaged_tail_is_a_chain_error_and_a_refused_one_a_replay_error(self):
        first = self.seeded()
        second = self.opened()
        second.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        second.seed_task("req-1", "task-3", CAPS, max_attempts=3)
        second.close()
        execute(self.path, ("UPDATE commands SET command = replace(command, 'task-3', 'task-4') WHERE seq = 4", ()))
        self.assertEqual(self.code_of(LedgerError, first.stop, "req-1", "OPERATOR_STOP"), "E_LEDGER_CHAIN")
        self.unavailable(first)
        # A well-linked command the Scheduler refuses on replay: a stop of a request nobody seeded.
        execute(self.path, ("DELETE FROM commands WHERE seq >= 3", ()))
        again = self.opened()
        append_raw(self.path, json.dumps({"method": "stop", "arguments": {"request": "req-nobody", "reason": "x"}}))
        self.assertEqual(self.code_of(LedgerError, again.stop, "req-1", "OPERATOR_STOP"), "E_LEDGER_REPLAY")
        self.unavailable(again)

    def test_a_caught_up_claim_may_not_reuse_an_operation_id_with_another_key(self):
        first = self.seeded()
        first.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        first.claim("req-1", "task-1", 1, "idem-a", "owner-1", 1, 1, dict(RESERVATION), ["src/a.py"])
        behind = self.opened()
        claim = json.loads(rows_of(self.path)[-1][1])
        self.assertEqual(claim["method"], "claim")
        claim["arguments"].update(task="task-2", key="idem-forged", resource_keys=["src/b.py"])
        append_raw(self.path, json.dumps(claim))
        self.assertEqual(self.code_of(LedgerError, behind.refresh), "E_LEDGER_REPLAY")
        self.unavailable(behind)

    def test_a_failure_while_catching_up_invalidates_and_writes_nothing(self):
        cases = [(KeyboardInterrupt(), KeyboardInterrupt, "write"), (KeyboardInterrupt(), KeyboardInterrupt, "refresh"),
                 (RuntimeError("boom"), LedgerError, "write"), (RuntimeError("boom"), LedgerError, "refresh")]
        for n, (error, expected, call) in enumerate(cases):
            with self.subTest(error=type(error).__name__, call=call):
                self.path = str(self.work / ("ledger-%d.db" % n))
                first = self.seeded()
                with open_ledger(self.path) as second:
                    second.seed_task("req-1", "task-2", CAPS, max_attempts=3)
                rows = rows_of(self.path)
                run = (lambda: first.stop("req-1", "OPERATOR_STOP")) if call == "write" else first.refresh
                with mock.patch.object(durable_ledger, "_replay", side_effect=error):
                    with self.assertRaises(expected) as ctx:
                        run()
                if expected is LedgerError:
                    self.assertEqual(ctx.exception.code, "E_LEDGER_REPLAY")
                self.unavailable(first)
                self.assertEqual(rows_of(self.path), rows)

    # -- the surface ---------------------------------------------------------- #

    def test_refresh_is_public_and_the_mutating_surface_is_unchanged(self):
        self.assertTrue(callable(getattr(DurableScheduler, "refresh", None)))
        self.assertNotIn("refresh", durable_ledger.MUTATING_METHODS)
        ledger = self.seeded()
        self.assertEqual(set(ledger.refresh()), {"seq", "applied"})


class OperatorWhileServing(CatchUpCase):
    """The failure measured on 2026-10-16, through real ``smctl`` processes."""

    def write_json(self, name, value):
        path = self.work / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def smctl(self, *argv):
        return subprocess.run([PY, "-m", "sentinelmesh.cli", *map(str, argv)], cwd=str(REPO_ROOT), capture_output=True,
                              text=True, timeout=120)

    def setUp(self):
        super().setUp()
        caps = {"dispatches": 20, "tokens": 1000, "cost": 500}
        spec = {"request_id": "req-1", "repo_id": "repo-1", "caps": caps, "concurrency": 2,
                "tasks": [{"task_id": "task-%d" % n, "caps": caps, "max_attempts": 2} for n in (1, 2)]}
        done = self.smctl("run", "init", self.path, "--spec", self.write_json("spec.json", spec))
        self.assertEqual(done.returncode, 0, done.stderr)
        events = [{"seq": i, "event_id": "e%d" % i, "type": t, "source": "offline", "payload": p} for i, (t, p) in
                  enumerate([("run_started", {}), ("assistant", {}), ("usage", {"tokens": 12, "cost": 3}),
                             ("run_finished", {})])]
        emit = ("import json, sys" + NL + "for e in json.loads(%r):" % json.dumps(events) + NL
                + "    sys.stdout.buffer.write((json.dumps(e) + chr(10)).encode())" + NL)
        self.hold = ("import os, sys, time" + NL + "open(sys.argv[1], 'w').write('1')" + NL
                     + "deadline = time.monotonic() + 30" + NL
                     + "while not os.path.exists(sys.argv[2]) and time.monotonic() < deadline: time.sleep(0.01)" + NL) + emit
        self.emit = emit
        self.jobs = self.work / "jobs"
        self.jobs.mkdir()
        self.started, self.release = self.work / "started", self.work / "release"
        self.addCleanup(lambda: self.release.write_text("1") if self.work.exists() else None)

    def job(self, task, code):
        tree = self.work / ("tree-" + task)
        tree.mkdir(exist_ok=True)
        return {"request_id": "req-1", "task_id": task, "plan": 1, "idempotency_key": "k-" + task, "guard_version": 1,
                "reservation": dict(RESERVATION), "resources": ["r-" + task], "adapter_id": "offline",
                "role": "executor", "profile_id": "p", "command": [PY, "-c", code, str(self.started), str(self.release)],
                "capabilities": [], "cwd": str(tree), "environment": {"LANG": "C", "PATH": os.path.dirname(PY)},
                "allowed_secret_names": [], "timeout_seconds": 60, "usage_dimensions": ["tokens", "cost"]}

    def serve(self):
        return subprocess.Popen([PY, "-m", "sentinelmesh.cli", "run", "serve", self.path, "--request", "req-1",
                                 "--jobs", str(self.jobs), "--evidence", str(self.work / "evidence"), "--worktree-id", "w",
                                 "--owner", "o", "--candidate", "c" * 64, "--parallel", "1"],
                                cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def wait_started(self, controller):
        deadline = time.monotonic() + 60
        while not self.started.exists() and time.monotonic() < deadline and controller.poll() is None:
            time.sleep(0.01)
        self.assertTrue(self.started.exists(), "the held child never started")

    def test_a_stop_from_another_process_lets_the_run_in_flight_finish(self):
        for task, code in (("task-1", self.hold), ("task-2", self.emit)):
            self.write_json("jobs/%s.json" % task, self.job(task, code))
        controller = self.serve()
        self.addCleanup(lambda: controller.poll() is None and controller.kill())
        self.wait_started(controller)
        stop = self.smctl("ledger", "stop", self.path, "--request", "req-1", "--reason", "OPERATOR_STOP")
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.release.write_text("1")
        out, err = controller.communicate(timeout=120)
        self.assertEqual(controller.returncode, 3, err)
        report, refusal = json.loads(out), json.loads(err)
        self.assertEqual([(r["task_id"], r["state"]) for r in report["runs"]], [("task-1", "REVIEW_PENDING")])
        self.assertEqual((refusal["error"], refusal["operation_id"]), ("E_STOPPED", None))
        with open_ledger(self.path) as ledger:
            self.assertEqual([ledger.operation(op)["state"] for op in ledger.operation_ids()], ["REVIEW_PENDING"])
            self.assertEqual(ledger.snapshot("req-1")["tokens"]["held"], 0)

    def test_a_verdict_from_another_process_while_a_run_is_in_flight(self):
        for task, code in (("task-1", self.emit), ("task-2", self.hold)):
            self.write_json("jobs/%s.json" % task, self.job(task, code))
        controller = self.serve()
        self.addCleanup(lambda: controller.poll() is None and controller.kill())
        self.wait_started(controller)
        with open_ledger(self.path) as ledger:
            first = ledger.operation_ids()[0]
        verdict = self.smctl("ledger", "verdict", self.path, "--operation", first, "--candidate", "c" * 64, "--accept",
                             "--evidence", "e" * 64, "--operator", "ops-1", "--note", "read it")
        self.assertEqual(verdict.returncode, 0, verdict.stderr)
        self.release.write_text("1")
        out, err = controller.communicate(timeout=120)
        self.assertEqual((controller.returncode, err), (0, ""), err)
        report = json.loads(out)
        self.assertEqual(([r["task_id"] for r in report["runs"]], report["stopped"]), (["task-1", "task-2"], "drained"))
        with open_ledger(self.path) as ledger:
            self.assertEqual([ledger.operation(op)["state"] for op in ledger.operation_ids()],
                             ["ACCEPTED", "REVIEW_PENDING"])


if __name__ == "__main__":
    unittest.main()
