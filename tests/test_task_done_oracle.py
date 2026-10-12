"""Planner oracle for task-done (`.agent/design/task-done.md`).

An accepted run closes its task: ``claim`` refuses a task one of whose
operations is ``ACCEPTED`` with ``E_TASK_DONE``, ahead of the active-task and
attempts rules, whichever verdict wrote the state.  A rejected run leaves the
task open, and the attempts keep counting as before.  The ledger gives the
tasks of a request as a read -- ``task_ids`` in seeding order and ``task`` with
``task_id``, ``max_attempts``, ``attempts`` and ``done`` -- and ``smctl ledger
status`` lists them per request under ``tasks``.  Nothing is recorded: the rule
and the view are derived from the operations' states, so a rollover, a
compaction and a reopen keep them.  The children are local ``python -c``
programs and ``python -m sentinelmesh.cli`` processes; no provider CLI, no
credentials, no network.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import cli
from sentinelmesh.durable_ledger import open_ledger
from sentinelmesh.scheduler import ContractError, Scheduler

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
CAPS = {"dispatches": 9, "tokens": 500, "cost": 200}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
CANDIDATE, EVIDENCE = "c" * 64, "e" * 64
OPERATOR, NOTE = "ops-1", "diffed the candidate against the task and ran its tests"
TASK_KEYS = {"task_id", "max_attempts", "attempts", "done"}
STATUS_KEYS = {"request_id", "stopped", "stop_reasons", "snapshot", "attention", "reviews", "tasks"}


def ev(seq, typ, payload=None):
    return {"seq": seq, "event_id": "e%d" % seq, "type": typ, "source": "offline",
            "payload": {} if payload is None else payload}


COMPLETE = [ev(0, "run_started"), ev(1, "assistant"), ev(2, "usage", {"tokens": 12, "cost": 3}),
            ev(3, "run_finished")]


def emit(events):
    return ("import json, sys" + NL
            + "for event in json.loads(%r):" % json.dumps(events) + NL
            + "    sys.stdout.buffer.write((json.dumps(event) + chr(10)).encode())" + NL
            + "sys.stdout.buffer.flush()" + NL)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def task_view(task_id, max_attempts, attempts, done):
    return {"task_id": task_id, "max_attempts": max_attempts, "attempts": attempts, "done": done}


class Key(str):
    """A ``str`` subclass: equal to the text it spells, but not exact text."""


class TaskDoneCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="task-done-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.ledger = str(self.work / "ledger.db")
        self.count = 0

    # -- driving the CLI --------------------------------------------------- #

    def smctl(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main([str(a) for a in argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, err), (0, ""), "smctl %s failed: %s" % (argv, err))
        self.assertTrue(out.endswith(NL) and out.count(NL) == 1, "one JSON line expected")
        return json.loads(out)

    def refused(self, expected, *argv):
        before = self.digest()
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, out), (3, ""), "smctl %s should be refused: %s" % (argv, err))
        self.assertTrue(err.endswith(NL) and err.count(NL) == 1)
        error = json.loads(err)
        self.assertEqual(error["error"], expected, error)
        self.assertEqual(self.digest(), before, "a refused command changed the ledger")
        return error

    def verdict(self, op, decision="--accept"):
        return self.ok("ledger", "verdict", self.ledger, "--operation", op, "--candidate", CANDIDATE, decision,
                       "--evidence", EVIDENCE, "--operator", OPERATOR, "--note", NOTE)

    # -- ledgers and runs ---------------------------------------------------- #

    def write_json(self, name, value) -> str:
        path = self.work / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def init(self, max_attempts=3, tasks=6):
        names = ["task-%d" % n for n in range(1, tasks + 1)] if isinstance(tasks, int) else list(tasks)
        spec = {"request_id": "req-1", "repo_id": "repo-1", "caps": dict(CAPS), "concurrency": 4,
                "tasks": [{"task_id": name, "caps": dict(CAPS), "max_attempts": max_attempts} for name in names]}
        return self.ok("run", "init", self.ledger, "--spec", self.write_json("spec.json", spec))

    def job_for(self, task):
        self.count += 1
        job = {"request_id": "req-1", "task_id": task, "plan": 1, "idempotency_key": "idem-%d" % self.count,
               "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["src/%s.py" % task],
               "adapter_id": "offline", "role": "executor", "profile_id": "stub-profile",
               "command": [PY, "-c", emit(COMPLETE)], "capabilities": [], "cwd": str(self.cwd),
               "environment": dict(BASE_ENV), "allowed_secret_names": [], "timeout_seconds": 20,
               "usage_dimensions": ["tokens", "cost"]}
        evidence = self.work / ("evidence-%d" % self.count)
        evidence.mkdir()
        return ["run", "once", self.ledger, "--job", self.write_json("job-%d.json" % self.count, job),
                "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1",
                "--candidate", CANDIDATE]

    def run_once(self, task):
        """One offline run through the product command, to REVIEW_PENDING."""
        result = self.ok(*self.job_for(task))
        self.assertEqual(result["outcome"]["state"], "REVIEW_PENDING")
        return result["operation_id"]

    def run_refused(self, expected, task):
        """The product command refused before any claim: three keys, no operation."""
        error = self.refused(expected, *self.job_for(task))
        self.assertEqual(set(error), {"error", "detail", "operation_id"})
        self.assertIsNone(error["operation_id"])
        return error

    def accepted(self, task, max_attempts=3):
        self.init(max_attempts=max_attempts)
        op = self.run_once(task)
        self.verdict(op, "--accept")
        return op

    def digest(self) -> str:
        return sha256_of(Path(self.ledger).read_bytes())

    def opened(self):
        ledger = open_ledger(self.ledger)
        self.addCleanup(ledger.close)
        return ledger

    def claim(self, ledger, task, key, generation=1, guard=1):
        return ledger.claim("req-1", task, 1, key, "owner-1", generation, guard, RESERVATION, ["src/%s.py" % task])

    def claim_code(self, ledger, task, key, **kwargs):
        before = (getattr(ledger, "seq", None), list(ledger.events), ledger.snapshot("req-1"))
        with self.assertRaises(ContractError) as ctx:
            self.claim(ledger, task, key, **kwargs)
        self.assertEqual(before, (getattr(ledger, "seq", None), list(ledger.events), ledger.snapshot("req-1")),
                         "a refused claim changed the ledger")
        return ctx.exception.code

    def request(self):
        status = self.ok("ledger", "status", self.ledger)
        return status["requests"][0]


class TaskDoneOracle(TaskDoneCase):
    def test_an_accepted_task_is_closed_and_listed_as_done(self):
        op = self.accepted("task-1")
        before = self.digest()
        # The product command: refused before any claim, with the task's code.
        error = self.run_refused("E_TASK_DONE", "task-1")
        self.assertIsNone(error["detail"])
        self.assertEqual(self.digest(), before)
        request = self.request()
        self.assertEqual(set(request), STATUS_KEYS)
        self.assertEqual(request["tasks"], [task_view("task-1", 3, 1, True)]
                         + [task_view("task-%d" % n, 3, 0, False) for n in range(2, 7)])
        self.assertEqual((request["stopped"], request["attention"], request["reviews"]), (False, [], []))
        # The other tasks are untouched by it.
        other = self.run_once("task-2")
        self.assertNotEqual(other, op)
        self.assertEqual(self.request()["tasks"][1], task_view("task-2", 3, 1, False))
        self.assertEqual(self.request()["reviews"][0]["operation_id"], other)
        # The library call says the same, and changes nothing either.
        ledger = self.opened()
        self.assertEqual(self.claim_code(ledger, "task-1", "idem-library"), "E_TASK_DONE")
        self.assertEqual(ledger.task("req-1", "task-1"), task_view("task-1", 3, 1, True))
        self.assertEqual(ledger.task_ids("req-1"), tuple("task-%d" % n for n in range(1, 7)))
        # A task of the same name under another request is another task.
        ledger.seed_request("req-2", "repo-2", CAPS, 4)
        ledger.seed_task("req-2", "task-1", CAPS, 3)
        self.assertEqual(ledger.task("req-2", "task-1"), task_view("task-1", 3, 0, False))
        claimed = ledger.claim("req-2", "task-1", 1, "idem-req-2", "owner-1", 1, 1, RESERVATION, ["src/other.py"])
        self.assertEqual(claimed["state"], "RESERVED")
        self.assertEqual(ledger.task("req-2", "task-1"), task_view("task-1", 3, 1, False))
        self.assertEqual(ledger.task("req-1", "task-1"), task_view("task-1", 3, 1, True))

    def test_a_rejected_task_stays_open_and_every_attempt_counts(self):
        self.init(max_attempts=3)
        first = self.run_once("task-1")
        self.verdict(first, "--reject")
        self.assertEqual(self.request()["tasks"][0], task_view("task-1", 3, 1, False))
        # A claim that never started is an attempt too, as it is for the rule.
        ledger = self.opened()
        cancelled = self.claim(ledger, "task-1", "idem-cancelled")["operation_id"]
        ledger.cancel_unstarted(cancelled, 1)
        self.assertEqual(ledger.task("req-1", "task-1"), task_view("task-1", 3, 2, False))
        ledger.close()
        second = self.run_once("task-1")
        self.assertNotEqual(second, first)
        self.assertEqual(self.request()["tasks"][0], task_view("task-1", 3, 3, False))
        self.verdict(second, "--reject")
        # Three attempts taken, none accepted: the attempts rule, not the task's.
        self.run_refused("E_ATTEMPTS", "task-1")
        self.assertEqual(self.request()["tasks"][0], task_view("task-1", 3, 3, False))

    def test_done_is_decided_before_the_attempts_rule_and_after_the_request_rules(self):
        self.accepted("task-1", max_attempts=1)
        # Attempts exhausted and accepted at once: the task's own fact wins.
        self.run_refused("E_TASK_DONE", "task-1")
        ledger = self.opened()
        self.assertEqual(self.claim_code(ledger, "task-1", "idem-a"), "E_TASK_DONE")
        # What comes before it is unchanged: the generation, the scope, the stop
        # and the guard are all checked first.
        self.assertEqual(self.claim_code(ledger, "task-1", "idem-b", generation=2), "E_FENCED")
        self.assertEqual(self.claim_code(ledger, "task-9", "idem-c"), "E_SCOPE_UNKNOWN")
        self.assertEqual(self.claim_code(ledger, "task-1", "idem-d", guard=2), "E_GUARD_STALE")
        ledger.stop("req-1", "SECURITY_HOLD")
        self.assertEqual(self.claim_code(ledger, "task-1", "idem-e"), "E_STOPPED")
        self.assertEqual(ledger.task("req-1", "task-1"), task_view("task-1", 1, 1, True))

    def test_the_fixture_hook_closes_the_task_too(self):
        self.init()
        op = self.run_once("task-1")
        ledger = self.opened()
        self.assertEqual(ledger.task("req-1", "task-1"), task_view("task-1", 3, 1, False))
        ledger.fixture_verdict(op, CANDIDATE, True, EVIDENCE)
        self.assertEqual(self.claim_code(ledger, "task-1", "idem-after-fixture"), "E_TASK_DONE")
        self.assertEqual(ledger.task("req-1", "task-1"), task_view("task-1", 3, 1, True))
        # The in-memory scheduler is the same rule.
        memory = Scheduler()
        memory.seed_request("req-1", "repo-1", CAPS, 4)
        memory.seed_task("req-1", "task-2", CAPS, 3)
        memory.seed_task("req-1", "task-1", CAPS, 3)
        first = self.claim(memory, "task-1", "idem-m1")["operation_id"]
        memory.handoff(first, "owner-1", 1)
        memory.mark_running(first, "owner-1", 1)
        memory.settle(first, {"tokens": 12, "cost": 3})
        memory.finish(first, "owner-1", 1, CANDIDATE, True)
        memory.fixture_verdict(first, CANDIDATE, True, EVIDENCE)
        self.assertEqual(self.claim_code(memory, "task-1", "idem-m2"), "E_TASK_DONE")
        self.assertEqual(memory.task_ids("req-1"), ("task-2", "task-1"), "seeding order, not sorted")
        self.assertEqual(memory.task("req-1", "task-1"), task_view("task-1", 3, 1, True))
        self.assertEqual(memory.task("req-1", "task-2"), task_view("task-2", 3, 0, False))

    def test_an_idempotent_replay_is_served_before_the_rule(self):
        op = self.accepted("task-1")
        ledger = self.opened()
        operations, events = ledger.operation_ids(), list(ledger.events)
        # The same request and key with the same intent as the run's claim: a
        # replay, not a refusal, and the operation is the accepted one.  (The
        # durable log records the replayed call as it records every call.)
        replay = ledger.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, RESERVATION, ["src/task-1.py"])
        self.assertEqual(replay, {"operation_id": op, "state": "ACCEPTED", "replayed": True})
        self.assertEqual((ledger.operation_ids(), list(ledger.events)), (operations, events))
        # Another key is a new claim, and the rule refuses it.
        self.assertEqual(self.claim_code(ledger, "task-1", "idem-2"), "E_TASK_DONE")

    def test_the_rule_and_the_view_survive_recovery_compaction_and_reopen(self):
        self.accepted("task-1")
        pending = self.run_once("task-2")
        # A recovery rolls the ledger over; the accepted run is not its business.
        recovery = self.ok("run", "recover", self.ledger, "--owner", "owner-2")
        self.assertEqual(recovery["generation"], 2)
        self.assertEqual([o["operation_id"] for o in recovery["operations"]], [])
        self.assertEqual(self.request()["tasks"][:2], [task_view("task-1", 3, 1, True), task_view("task-2", 3, 1, False)])
        self.assertEqual(self.request()["stop_reasons"], ["RECOVERY_REQUIRED"])
        self.ok("ledger", "resume", self.ledger, "--request", "req-1", "--reason", "RECOVERY_REQUIRED",
                "--operator", OPERATOR, "--note", NOTE)
        ledger = self.opened()
        self.assertEqual(self.claim_code(ledger, "task-1", "idem-gen2", generation=2), "E_TASK_DONE")
        self.assertEqual(self.claim_code(ledger, "task-2", "idem-gen2b", generation=2), "E_ACTIVE_TASK")
        ledger.close()
        self.ok("ledger", "compact", self.ledger)
        self.assertEqual(self.request()["tasks"][:2], [task_view("task-1", 3, 1, True), task_view("task-2", 3, 1, False)])
        self.assertEqual(self.request()["reviews"][0]["operation_id"], pending)
        reopened = self.opened()
        self.assertEqual(reopened.task_ids("req-1"), tuple("task-%d" % n for n in range(1, 7)))
        self.assertEqual(self.claim_code(reopened, "task-1", "idem-after-compact", generation=2), "E_TASK_DONE")

    def test_the_task_views_refuse_unknown_scopes_and_touch_nothing(self):
        self.init(tasks=("task-2", "task-3", "task-1"))
        ledger = self.opened()
        before = (ledger.seq, list(ledger.events), self.digest())
        for request, code in (("req-9", "E_REQUEST_UNKNOWN"), (Key("req-1"), "E_REQUEST_UNKNOWN"),
                              (None, "E_REQUEST_UNKNOWN")):
            with self.subTest(request=request):
                with self.assertRaises(ContractError) as ctx:
                    ledger.task_ids(request)
                self.assertEqual(ctx.exception.code, code)
        for request, task in (("req-9", "task-1"), ("req-1", "task-9"), (Key("req-1"), "task-1"),
                              ("req-1", Key("task-1")), ("req-1", None), (1, "task-1")):
            with self.subTest(request=request, task=task):
                with self.assertRaises(ContractError) as ctx:
                    ledger.task(request, task)
                self.assertEqual(ctx.exception.code, "E_SCOPE_UNKNOWN")
        views = [ledger.task("req-1", task_id) for task_id in ledger.task_ids("req-1")]
        self.assertEqual(views, [task_view("task-%d" % n, 3, 0, False) for n in (2, 3, 1)], "seeding order, not sorted")
        self.assertTrue(all(set(view) == TASK_KEYS for view in views))
        self.assertEqual(before, (ledger.seq, list(ledger.events), self.digest()), "a read changed the ledger")
        ledger.close()
        self.assertEqual(self.request()["tasks"], views)
        # A closed instance refuses the reads like every other.
        for call in (lambda: ledger.task_ids("req-1"), lambda: ledger.task("req-1", "task-1")):
            with self.assertRaises(Exception) as ctx:
                call()
            self.assertEqual(getattr(ctx.exception, "code", None), "E_LEDGER_UNAVAILABLE")

    def test_a_real_process_reports_the_closed_task(self):
        self.accepted("task-1")
        argv = [PY, "-m", "sentinelmesh.cli"]
        env = dict(os.environ, PYTHONUTF8="1", PYTHONPATH=str(REPO_ROOT))
        once = subprocess.run(argv + self.job_for("task-1")[:], cwd=str(REPO_ROOT), env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual((once.returncode, once.stdout), (3, ""), once.stderr)
        error = json.loads(once.stderr)
        self.assertEqual((error["error"], error["operation_id"]), ("E_TASK_DONE", None))
        status = subprocess.run(argv + ["ledger", "status", self.ledger], cwd=str(REPO_ROOT), env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual((status.returncode, status.stderr), (0, ""), status.stderr)
        request = json.loads(status.stdout)["requests"][0]
        self.assertEqual(set(request), STATUS_KEYS)
        self.assertEqual(request["tasks"][0], task_view("task-1", 3, 1, True))
        self.assertEqual(len(request["tasks"]), 6)


if __name__ == "__main__":
    unittest.main()
