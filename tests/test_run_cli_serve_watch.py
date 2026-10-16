"""Executor tests for ``smctl run serve --watch`` (`.agent/design/serve-watch.md`).

These cover the pieces the planner oracle leaves to the executor, calling them
directly: ``_last_operation`` on its own (no operation, the last of a task in
claim order, and another request's operation not counting), ``_watch_finished``
on its own (the five ways :func:`_last_operation`/``task`` decide a task with a
job, and a task with no job not being looked at), ``_sleep`` cutting one pause
into poll-sized pieces, and a real ``python -m sentinelmesh.cli run serve …
--poll-seconds 5`` subprocess without ``--watch`` that is a usage error.  The
passes, the verdicts, ``needs_operator``, ``finished``, the ceilings, the
interrupts, the stop/rollover, the concurrency and the real-process watch are the
oracle's to cover.  Every child is a local ``python -c`` program writing the
offline adapter's protocol; no provider CLI, no credentials, no network.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh import cli, run_cli
from sentinelmesh.durable_ledger import open_ledger

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
CAPS = {"dispatches": 20, "tokens": 1000, "cost": 500}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
CANDIDATE, EVIDENCE = "c" * 64, "e" * 64


def _event(seq, typ, payload=None):
    return {"seq": seq, "event_id": "e%d" % seq, "type": typ, "source": "offline",
            "payload": {} if payload is None else payload}


COMPLETE = [_event(0, "run_started"), _event(1, "assistant"),
            _event(2, "usage", {"tokens": 12, "cost": 3}), _event(3, "run_finished")]


def _emit(events):
    """Python code that writes each offline event as one LF-terminated JSON line."""
    return ("import json, sys" + NL
            + "for event in json.loads(%r):" % json.dumps(events) + NL
            + "    sys.stdout.buffer.write((json.dumps(event) + chr(10)).encode())" + NL
            + "sys.stdout.buffer.flush()" + NL)


class _WatchHarness(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="run-cli-serve-watch-x-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.jobs = self.work / "jobs"
        self.jobs.mkdir()
        self.evidence = self.work / "evidence"
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
        return json.loads(out)

    def real(self, *argv, timeout=120):
        return subprocess.run([PY, "-m", "sentinelmesh.cli", *[str(a) for a in argv]], cwd=str(REPO_ROOT),
                              capture_output=True, text=True, timeout=timeout)

    def write_json(self, name, value) -> str:
        self.count += 1
        path = self.work / ("%s-%d.json" % (name, self.count))
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    # -- files --------------------------------------------------------------- #

    def init(self, name, tasks):
        """A fresh ledger ``<name>.db`` seeded with ``tasks`` = [(task_id, max_attempts)]."""
        spec = {"request_id": "req-1", "repo_id": "repo-1", "caps": dict(CAPS), "concurrency": 4,
                "tasks": [{"task_id": task_id, "caps": dict(CAPS), "max_attempts": max_attempts}
                          for task_id, max_attempts in tasks]}
        ledger = str(self.work / (name + ".db"))
        self.ok("run", "init", ledger, "--spec", self.write_json("spec", spec))
        return ledger

    def tree(self, name) -> str:
        path = self.work / ("tree-" + name)
        path.mkdir(exist_ok=True)
        return str(path)

    def job(self, task_id, key, cwd=None):
        # ``cwd=None`` is a real working directory (the run reaches REVIEW_PENDING);
        # a relative ``cwd`` is refused by the runtime, so the run ends NOT_STARTED.
        return {"request_id": "req-1", "task_id": task_id, "plan": 1, "idempotency_key": key,
                "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["src/%s.py" % task_id],
                "adapter_id": "offline", "role": "executor", "profile_id": "stub-profile",
                "command": [PY, "-c", _emit(COMPLETE)], "capabilities": [],
                "cwd": self.tree(task_id) if cwd is None else cwd,
                "environment": dict(BASE_ENV), "allowed_secret_names": [],
                "timeout_seconds": 30, "usage_dimensions": ["tokens", "cost"]}

    def once(self, job):
        """``run once`` one job in a fresh evidence directory; return the operation id."""
        evidence = self.work / ("once-evidence-%d" % self.count)
        evidence.mkdir()
        report = self.ok("run", "once", self.ledger, "--job", self.write_json("job", job),
                         "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1",
                         "--candidate", CANDIDATE)
        return report["operation_id"]

    def decide(self, operation_id, accepted):
        with open_ledger(self.ledger) as ledger:
            ledger.verdict(operation_id, ledger.generation, candidate_hash=CANDIDATE, accepted=accepted,
                           evidence_hash=EVIDENCE, operator="ops-1", note="read it")

    def operations(self):
        with open_ledger(self.ledger) as ledger:
            return [ledger.operation(op) for op in ledger.operation_ids()]


class LastOperationTests(_WatchHarness):
    """``_last_operation`` called directly on one ledger instance."""

    def test_no_operation_of_the_task_returns_none(self):
        self.ledger = self.init("none", [("task-1", 2)])
        with open_ledger(self.ledger) as ledger:
            self.assertIsNone(run_cli._last_operation(ledger, "req-1", "task-1"))

    def test_the_last_of_the_task_in_claim_order_is_the_one_returned(self):
        self.ledger = self.init("order", [("task-1", 3)])
        first = self.once(self.job("task-1", "k1"))       # attempt 1 -> REVIEW_PENDING
        self.decide(first, False)                          # rejected, so the task is claimable again
        second = self.once(self.job("task-1", "k2"))       # attempt 2 -> REVIEW_PENDING
        with open_ledger(self.ledger) as ledger:
            last = run_cli._last_operation(ledger, "req-1", "task-1")
        # The later operation, not the rejected first one: its state tells them apart.
        self.assertEqual(last["operation_id"], second)
        self.assertNotEqual(last["operation_id"], first)
        self.assertEqual(last["state"], "REVIEW_PENDING")
        self.assertEqual([op["operation_id"] for op in self.operations()], [first, second])

    def test_another_requests_operation_does_not_count(self):
        self.ledger = self.init("other", [("task-1", 2)])
        with open_ledger(self.ledger) as ledger:
            # A second request with the very same task id, seeded straight onto the ledger.
            ledger.seed_request("req-2", "repo-1", dict(CAPS), 4)
            ledger.seed_task("req-2", "task-1", dict(CAPS), 2)
            mine = ledger.claim("req-1", "task-1", 1, "k1", "owner-1", ledger.generation, 1,
                                dict(RESERVATION), ["src/req-1.py"], 0)["operation_id"]
            theirs = ledger.claim("req-2", "task-1", 1, "k2", "owner-1", ledger.generation, 1,
                                  dict(RESERVATION), ["src/req-2.py"], 0)["operation_id"]
            last = run_cli._last_operation(ledger, "req-1", "task-1")
        self.assertEqual(last["operation_id"], mine)
        self.assertEqual(last["request_id"], "req-1")
        self.assertNotEqual(last["operation_id"], theirs)


class WatchFinishedTests(_WatchHarness):
    """``_watch_finished`` called directly, one task with a job per branch."""

    def setUp(self):
        super().setUp()
        # One request, seven tasks; the jobs of task-4 and task-7 are written last or never,
        # so after one plain ``serve`` and three verdicts every branch of the predicate has
        # a task in it: task-1 accepted, task-2 waiting, task-3 out of attempts, task-4 never
        # tried (a job but no operation), task-5 rejected with an attempt left, task-6 ended
        # without a review, task-7 with no job at all.
        self.ledger = self.init("states", [("task-%d" % n, 1 if n == 3 else 2) for n in range(1, 8)])
        for task_id in ("task-1", "task-2", "task-3", "task-5"):
            (self.jobs / (task_id + ".json")).write_text(json.dumps(self.job(task_id, "k-" + task_id)),
                                                         encoding="utf-8")
        (self.jobs / "task-6.json").write_text(json.dumps(self.job("task-6", "k-task-6", cwd="tree")),
                                               encoding="utf-8")
        self.ok("run", "serve", self.ledger, "--request", "req-1", "--jobs", str(self.jobs),
                "--evidence", str(self.evidence / "serve"), "--worktree-id", "wt-1", "--owner", "owner-1",
                "--candidate", CANDIDATE)
        ops = self.operations()
        self.assertEqual([op["task_id"] for op in ops], ["task-1", "task-2", "task-3", "task-5", "task-6"])
        self.decide(ops[0]["operation_id"], True)   # task-1 accepted
        self.decide(ops[2]["operation_id"], False)  # task-3 rejected (out of attempts, max 1)
        self.decide(ops[3]["operation_id"], False)  # task-5 rejected (an attempt left, max 2)
        # task-4 now has a job but was never claimed.
        (self.jobs / "task-4.json").write_text(json.dumps(self.job("task-4", "k-task-4")), encoding="utf-8")
        self.with_jobs = {"task-%d" % n: {} for n in range(1, 7)}  # every task but task-7 has a job

    def finished(self, task_ids, jobs):
        with open_ledger(self.ledger) as ledger:
            return run_cli._watch_finished(ledger, "req-1", task_ids, jobs)

    def test_an_accepted_task_passes(self):
        self.assertTrue(self.finished(["task-1"], self.with_jobs))

    def test_a_task_waiting_for_a_verdict_keeps_the_watch_going(self):
        self.assertFalse(self.finished(["task-2"], self.with_jobs))

    def test_a_task_out_of_attempts_passes(self):
        self.assertTrue(self.finished(["task-3"], self.with_jobs))

    def test_a_task_never_tried_keeps_the_watch_going(self):
        self.assertFalse(self.finished(["task-4"], self.with_jobs))

    def test_a_rejected_task_with_an_attempt_left_keeps_the_watch_going(self):
        self.assertFalse(self.finished(["task-5"], self.with_jobs))

    def test_a_task_that_ended_without_a_review_passes(self):
        # task-6 ended NOT_STARTED: not done, not waiting, not out of attempts, not rejected.
        self.assertTrue(self.finished(["task-6"], self.with_jobs))

    def test_a_task_with_no_job_is_not_looked_at(self):
        # task-2 would keep the watch going, but with no job it is not examined at all.
        self.assertTrue(self.finished(["task-2"], {}))
        self.assertTrue(self.finished(["task-1", "task-7"], {"task-1": {}}))

    def test_the_whole_request_is_finished_only_when_every_job_task_passes(self):
        passing = ["task-1", "task-3", "task-6"]       # accepted, out of attempts, no review
        self.assertTrue(self.finished(passing, self.with_jobs))
        self.assertFalse(self.finished(passing + ["task-2"], self.with_jobs))   # one waiting task
        self.assertFalse(self.finished(passing + ["task-4"], self.with_jobs))   # one never tried
        self.assertFalse(self.finished(passing + ["task-5"], self.with_jobs))   # one rejected, attempt left


class SleepTests(_WatchHarness):
    """``_sleep`` cutting one pause into pieces of at most ``_POLL_SECONDS``."""

    def test_the_pause_is_broken_into_poll_sized_pieces(self):
        waits = []
        clock = [1000.0]

        def fake_sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds   # the clock only moves by what is actually slept
        with mock.patch("time.sleep", fake_sleep), mock.patch("time.monotonic", lambda: clock[0]):
            run_cli._sleep(1.0)
        self.assertTrue(waits, "the pause slept at least once")
        self.assertTrue(all(wait <= run_cli._POLL_SECONDS for wait in waits),
                        "every piece is at most _POLL_SECONDS: %r" % (waits,))
        self.assertGreaterEqual(len(waits), 10, "a full second is more than one 0.1s piece")
        self.assertGreaterEqual(sum(waits), 1.0 - 1e-9, "the whole second is waited")


class PollSecondsUsageTests(_WatchHarness):
    """``--poll-seconds`` without ``--watch`` is a usage error, in a real subprocess."""

    def test_a_real_subprocess_refuses_poll_seconds_without_watch(self):
        self.ledger = self.init("usage", [("task-1", 2)])
        (self.jobs / "task-1.json").write_text(json.dumps(self.job("task-1", "k1")), encoding="utf-8")
        completed = self.real("run", "serve", self.ledger, "--request", "req-1", "--jobs", str(self.jobs),
                              "--evidence", str(self.evidence / "serve"), "--worktree-id", "wt-1",
                              "--owner", "owner-1", "--poll-seconds", "5")
        self.assertEqual(completed.returncode, 2, (completed.stdout, completed.stderr))
        self.assertEqual(completed.stdout, "")
        self.assertTrue(completed.stderr.endswith(NL) and completed.stderr.count(NL) == 1,
                        (completed.stdout, completed.stderr))
        refusal = json.loads(completed.stderr)
        self.assertEqual((refusal["error"], refusal["detail"], refusal["operation_id"]),
                         ("E_RUN_USAGE", "--poll-seconds needs --watch", None))
        # The usage error reads nothing: no operation was ever claimed.
        self.assertEqual(self.operations(), [])


if __name__ == "__main__":
    unittest.main()
