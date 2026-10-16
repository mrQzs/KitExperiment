"""Planner oracle for serve-watch (`.agent/design/serve-watch.md`, sections 3.2, 5 and 6).

``smctl run serve --watch`` keeps walking one request, pass after pass: a pass is
the walk of ``serve --parallel N`` (width 1 without ``--parallel``) followed by
the drain of its runs; between two passes it pauses ``--poll-seconds`` (default
30, through the module-level ``_sleep``) and catches up with the ledger
(``refresh()``), so verdicts an operator gives from another process are seen.
A task whose last run was rejected is claimed again on a later pass; a task
whose last run ended without a review (``NOT_STARTED``, ``START_UNKNOWN``,
``CANCELLED_NOT_STARTED``, ``ABANDONED``) is skipped as ``needs_operator``.
The watch ends ``finished`` when no task with a job can move on without the
operator -- nothing in flight, nothing waiting for a verdict, nothing rejected
with attempts left, nothing never tried -- or ``max_seconds``, ``max_runs``,
``interrupted`` (Ctrl-C, also while it pauses) or ``refused`` (a refusal, a
rollover by another controller included).  The report is ``serve``'s with
``"parallel"`` (the width), ``"passes"`` and ``"watch": {"poll_seconds"}``;
``runs`` hold every pass in claim order, ``skipped`` the last pass only.

The operator is played by another ledger instance in this process (from a
replaced ``_sleep``, so the number of passes is exact) and, once, by real
``smctl`` processes.  No provider CLI, no credentials, no network.
"""
from __future__ import annotations

import _thread
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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
REPORT_KEYS = {"command", "request", "runs", "skipped", "stopped", "refusal", "parallel", "passes", "watch"}
RUN_KEYS = {"task_id", "attempt", "operation_id", "state", "evidence_dir"}


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


class WatchCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="run-cli-serve-watch-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.ledger = str(self.work / "ledger.db")
        self.jobs = self.work / "jobs"
        self.jobs.mkdir()
        self.evidence = self.work / "evidence"
        self.init()

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

    def init(self, tasks=2, **overrides):
        spec = {"request_id": "req-1", "repo_id": "repo-1", "caps": dict(CAPS), "concurrency": 4,
                "tasks": [{"task_id": "task-%d" % n, "caps": dict(CAPS), "max_attempts": 2}
                          for n in range(1, tasks + 1)]}
        spec.update(overrides)
        (self.work / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        return self.ok("run", "init", self.ledger, "--spec", str(self.work / "spec.json"))

    def job(self, task_id, code=None, **overrides):
        tree = self.work / ("tree-" + task_id)
        tree.mkdir(exist_ok=True)
        job = {"request_id": "req-1", "task_id": task_id, "plan": 1, "idempotency_key": "idem-" + task_id,
               "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["src/%s.py" % task_id],
               "adapter_id": "offline", "role": "executor", "profile_id": "stub-profile",
               "command": [PY, "-c", emit(COMPLETE) if code is None else code], "capabilities": [],
               "cwd": str(tree), "environment": dict(BASE_ENV), "allowed_secret_names": [],
               "timeout_seconds": 30, "usage_dimensions": ["tokens", "cost"]}
        job.update(overrides)
        return job

    def write_jobs(self, *jobs):
        for job in jobs:
            (self.jobs / (job["task_id"] + ".json")).write_text(json.dumps(job), encoding="utf-8")

    def argv(self, *extra, watch=True):
        return ("run", "serve", self.ledger, "--request", "req-1", "--jobs", str(self.jobs), "--evidence",
                str(self.evidence), "--worktree-id", "wt-1", "--owner", "owner-1", "--candidate", CANDIDATE,
                *(("--watch",) if watch else ()), *extra)

    def operator(self, decide):
        """Another ledger instance: ``decide(ledger, ops)`` with the request's operations in claim order."""
        with open_ledger(self.ledger) as ledger:
            ops = [ledger.operation(op) for op in ledger.operation_ids()]
            decide(ledger, ops)

    @staticmethod
    def verdict(ledger, op, accepted):
        ledger.verdict(op["operation_id"], ledger.generation, candidate_hash=CANDIDATE, accepted=accepted,
                       evidence_hash=EVIDENCE, operator="ops-1", note="read it")

    def watch(self, *extra, script=(), clock=None):
        """``serve --watch`` in process; the k-th pause runs ``script[k]`` (the operator), if any.

        Returns (exit code, report or None, stderr, number of pauses)."""
        pauses = []

        def sleep(seconds):
            pauses.append(seconds)
            if len(pauses) > 20:
                raise RuntimeError("the watch did not end")
            if len(pauses) <= len(script) and script[len(pauses) - 1] is not None:
                script[len(pauses) - 1]()
            if clock is not None:
                clock[0] += seconds
        patches = [mock.patch.object(run_cli, "_sleep", sleep)]
        if clock is not None:
            patches.append(mock.patch.object(run_cli, "_clock", lambda: clock[0]))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            code, out, err = self.smctl(*self.argv(*extra))
        report = json.loads(out) if out else None
        if report is not None:
            self.assertEqual(set(report), REPORT_KEYS, report)
            for run in report["runs"]:
                self.assertEqual(set(run), RUN_KEYS)
        return code, report, err, pauses

    def states(self):
        with open_ledger(self.ledger) as ledger:
            return [(ledger.operation(op)["task_id"], ledger.operation(op)["state"]) for op in ledger.operation_ids()]


class WatchOracle(WatchCase):
    def test_a_rejected_task_is_claimed_again_and_the_watch_finishes_when_nothing_can_move(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"))

        def first_verdicts():
            self.operator(lambda ledger, ops: (self.verdict(ledger, ops[0], True), self.verdict(ledger, ops[1], False)))

        def second_verdict():
            self.operator(lambda ledger, ops: self.verdict(ledger, ops[2], True))
        code, report, err, pauses = self.watch("--poll-seconds", "7", script=[first_verdicts, second_verdict])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([(r["task_id"], r["attempt"], r["state"]) for r in report["runs"]],
                         [("task-1", 1, "REVIEW_PENDING"), ("task-2", 1, "REVIEW_PENDING"), ("task-2", 2, "REVIEW_PENDING")])
        self.assertEqual((report["stopped"], report["passes"], report["watch"], report["parallel"], report["refusal"]),
                         ("finished", 3, {"poll_seconds": 7}, 1, None))
        self.assertEqual(report["skipped"], [{"task_id": "task-1", "reason": "done"}, {"task_id": "task-2", "reason": "done"}])
        self.assertEqual(pauses, [7, 7])
        self.assertEqual(self.states(), [("task-1", "ACCEPTED"), ("task-2", "REJECTED"), ("task-2", "ACCEPTED")])

    def test_waiting_for_a_verdict_keeps_the_watch_going_until_max_seconds(self):
        self.write_jobs(self.job("task-1"))
        clock = [0.0]
        code, report, err, pauses = self.watch("--poll-seconds", "5", "--max-seconds", "12", clock=clock)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(([r["task_id"] for r in report["runs"]], report["stopped"]), (["task-1"], "max_seconds"))
        # Pauses at 0, 5 and 10 seconds; the fourth pass finds the budget spent before its first claim.
        self.assertEqual((pauses, report["passes"], report["skipped"]), ([5, 5, 5], 4, []))
        self.assertEqual(self.states(), [("task-1", "REVIEW_PENDING")])

    def test_a_run_that_ended_without_a_review_is_left_to_the_operator(self):
        # task-2's relative working directory is refused by the runtime: NOT_STARTED, no review.
        self.write_jobs(self.job("task-1"), self.job("task-2", cwd="tree"))
        accept_first = lambda: self.operator(lambda ledger, ops: self.verdict(ledger, ops[0], True))  # noqa: E731
        code, report, err, pauses = self.watch("--poll-seconds", "1", script=[accept_first])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([(r["task_id"], r["state"]) for r in report["runs"]],
                         [("task-1", "REVIEW_PENDING"), ("task-2", "NOT_STARTED")])
        self.assertEqual((report["stopped"], report["passes"]), ("finished", 2))
        self.assertEqual(report["skipped"], [{"task_id": "task-1", "reason": "done"},
                                             {"task_id": "task-2", "reason": "needs_operator"}])
        self.assertEqual(len(self.states()), 2)  # task-2 was not tried again, although it has an attempt left

    def test_a_rejection_that_arrives_while_another_task_runs_is_claimed_on_the_next_pass(self):
        # task-1 waits for its verdict from an earlier serve; the operator rejects it while task-2's
        # run is in flight (task-2 ends NOT_STARTED: its working directory is relative).  The pass
        # has already gone past task-1, so only the end-of-pass check can see it may move on.
        self.write_jobs(self.job("task-1"))
        self.ok(*self.argv(watch=False))
        self.write_jobs(self.job("task-2", cwd="tree"))
        real_run = run_cli.run_adapter_operation

        def run(*args, **kwargs):
            if os.path.basename(os.path.dirname(kwargs["evidence_dir"])) == "task-2":
                self.operator(lambda ledger, ops: self.verdict(ledger, ops[0], False))
            return real_run(*args, **kwargs)
        accept_second = lambda: self.operator(lambda ledger, ops: self.verdict(ledger, ops[-1], True))  # noqa: E731
        with mock.patch.object(run_cli, "run_adapter_operation", run):
            code, report, err, pauses = self.watch("--poll-seconds", "1", script=[None, accept_second])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([(r["task_id"], r["attempt"], r["state"]) for r in report["runs"]],
                         [("task-2", 1, "NOT_STARTED"), ("task-1", 2, "REVIEW_PENDING")])
        self.assertEqual((report["stopped"], report["passes"], len(pauses)), ("finished", 3, 2))
        self.assertEqual(report["skipped"], [{"task_id": "task-1", "reason": "done"},
                                             {"task_id": "task-2", "reason": "needs_operator"}])

    def test_a_request_with_nothing_to_do_finishes_after_one_pass_without_pausing(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"))
        first = self.ok(*self.argv(watch=False))
        self.operator(lambda ledger, ops: (self.verdict(ledger, ops[0], True), self.verdict(ledger, ops[1], True)))
        code, report, err, pauses = self.watch()
        self.assertEqual((code, err, pauses), (0, "", []))
        self.assertEqual((report["runs"], report["stopped"], report["passes"], report["watch"]),
                         ([], "finished", 1, {"poll_seconds": 30}))
        self.assertEqual(len(first["runs"]), 2)

    def test_a_task_out_of_attempts_and_a_task_without_a_job_do_not_keep_the_watch_going(self):
        self.write_jobs(self.job("task-1"))
        reject = lambda: self.operator(lambda ledger, ops: self.verdict(ledger, ops[-1], False))  # noqa: E731
        code, report, err, pauses = self.watch("--poll-seconds", "1", script=[reject, reject])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([(r["task_id"], r["attempt"]) for r in report["runs"]], [("task-1", 1), ("task-1", 2)])
        self.assertEqual((report["stopped"], report["passes"]), ("finished", 3))
        self.assertEqual(report["skipped"], [{"task_id": "task-1", "reason": "attempts"},
                                             {"task_id": "task-2", "reason": "no_job"}])

    def test_max_runs_counts_every_pass(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"))
        reject_second = lambda: self.operator(lambda ledger, ops: self.verdict(ledger, ops[1], False))  # noqa: E731
        code, report, err, pauses = self.watch("--poll-seconds", "1", "--max-runs", "2", script=[reject_second])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(([r["task_id"] for r in report["runs"]], report["stopped"], report["passes"]),
                         (["task-1", "task-2"], "max_runs", 2))

    def test_an_interrupt_while_pausing_ends_the_watch_with_its_report(self):
        self.write_jobs(self.job("task-1"))

        def interrupt():
            raise KeyboardInterrupt
        with mock.patch.object(run_cli, "_sleep", lambda seconds: interrupt()):
            code, out, err = self.smctl(*self.argv())
        report = json.loads(out)
        self.assertEqual((code, err, report["stopped"], report["passes"]), (0, "", "interrupted", 1))
        self.assertEqual([r["task_id"] for r in report["runs"]], ["task-1"])

    def test_a_real_interrupt_while_pausing_ends_the_watch(self):
        self.write_jobs(self.job("task-1"))

        def later():
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                with open_ledger(self.ledger) as ledger:
                    if any(ledger.operation(op)["state"] == "REVIEW_PENDING" for op in ledger.operation_ids()):
                        break
                time.sleep(0.05)
            time.sleep(0.3)
            _thread.interrupt_main()
        threading.Thread(target=later, daemon=True).start()
        started = time.monotonic()
        try:
            code, out, err = self.smctl(*self.argv("--poll-seconds", "60"))
        except KeyboardInterrupt:
            self.fail("an interrupt while the watch pauses escaped it")
        # The pause is made of short waits, so the interrupt lands long before the minute is over.
        self.assertLess(time.monotonic() - started, 30)
        report = json.loads(out)
        self.assertEqual((code, report["stopped"], [r["task_id"] for r in report["runs"]]), (0, "interrupted", ["task-1"]))

    def test_a_stop_between_passes_refuses_the_next_claim(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"))

        def reject_and_stop():
            def decide(ledger, ops):
                self.verdict(ledger, ops[1], False)
                ledger.stop("req-1", "OPERATOR_STOP")
            self.operator(decide)
        code, report, err, pauses = self.watch("--poll-seconds", "1", script=[reject_and_stop])
        self.assertEqual(code, 3, err)
        self.assertEqual((report["stopped"], report["refusal"]["error"], report["refusal"]["operation_id"]),
                         ("refused", "E_STOPPED", None))
        self.assertEqual(json.loads(err), report["refusal"])
        self.assertEqual(report["passes"], 2)

    def test_a_rollover_by_another_controller_ends_the_watch_as_stale(self):
        self.write_jobs(self.job("task-1"))
        takeover = lambda: self.operator(lambda ledger, ops: ledger.rollover())  # noqa: E731
        code, report, err, pauses = self.watch("--poll-seconds", "1", script=[takeover])
        self.assertEqual(code, 3, err)
        self.assertEqual((report["stopped"], report["refusal"]["error"], report["passes"]), ("refused", "E_LEDGER_STALE", 2))

    def test_the_watch_runs_in_parallel_when_asked(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"))
        accept_all = lambda: self.operator(lambda ledger, ops: [self.verdict(ledger, op, True) for op in ops])  # noqa: E731
        code, report, err, pauses = self.watch("--parallel", "2", "--poll-seconds", "1", script=[accept_all])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual((report["parallel"], report["stopped"], report["passes"], [r["task_id"] for r in report["runs"]]),
                         (2, "finished", 2, ["task-1", "task-2"]))

    def test_poll_seconds_needs_watch_and_a_positive_count(self):
        self.write_jobs(self.job("task-1"))
        code, out, err = self.smctl(*self.argv("--poll-seconds", "5", watch=False))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--poll-seconds", err)
        self.assertEqual(self.states(), [])
        for value in ("0", "-1", "x"):
            with self.subTest(value=value):
                code, out, err = self.smctl(*self.argv("--poll-seconds", value))
                self.assertEqual((code, out), (2, ""))
        self.assertEqual(self.states(), [])

    def test_verdicts_from_real_processes_reach_a_real_watch(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"))
        controller = subprocess.Popen([PY, "-m", "sentinelmesh.cli", *self.argv("--poll-seconds", "1")],
                                      cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: controller.poll() is None and controller.kill())

        def wait_states(expected):
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and controller.poll() is None:
                if [state for _, state in self.states()] == expected:
                    return
                time.sleep(0.1)
            self.fail("the ledger never reached %s: %s" % (expected, self.states()))

        def verdict(index, decision):
            with open_ledger(self.ledger) as ledger:
                op = ledger.operation_ids()[index]
            done = subprocess.run([PY, "-m", "sentinelmesh.cli", "ledger", "verdict", self.ledger, "--operation", op,
                                   "--candidate", CANDIDATE, decision, "--evidence", EVIDENCE, "--operator", "ops-1",
                                   "--note", "read it"], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
        wait_states(["REVIEW_PENDING", "REVIEW_PENDING"])
        verdict(0, "--accept")
        verdict(1, "--reject")
        wait_states(["ACCEPTED", "REJECTED", "REVIEW_PENDING"])
        verdict(2, "--accept")
        out, err = controller.communicate(timeout=120)
        self.assertEqual((controller.returncode, err), (0, ""), err)
        report = json.loads(out)
        self.assertEqual(([(r["task_id"], r["attempt"]) for r in report["runs"]], report["stopped"]),
                         ([("task-1", 1), ("task-2", 1), ("task-2", 2)], "finished"))
        self.assertGreaterEqual(report["passes"], 3)


class WatchCompletion(WatchCase):
    """The planner's completion after attempt-001 (`.agent/reviews/serve-watch/attempt-001.md`, F1)."""

    def test_an_interrupt_at_the_first_clock_read_is_absorbed_with_and_without_watch(self):
        self.write_jobs(self.job("task-1"))
        for extra in (("--parallel", "1"), ("--watch",)):
            with self.subTest(mode=extra[0]):
                def clock():
                    raise KeyboardInterrupt
                with mock.patch.object(run_cli, "_clock", clock):
                    try:
                        code, out, err = self.smctl(*self.argv(*extra, watch=False))
                    except KeyboardInterrupt:
                        self.fail("an interrupt at the first clock read escaped serve %s" % extra[0])
                report = json.loads(out)
                self.assertEqual((code, err, report["stopped"], report["runs"]), (0, "", "interrupted", []))
                if extra[0] == "--watch":
                    self.assertEqual(report["passes"], 1)
        self.assertEqual(self.states(), [])


if __name__ == "__main__":
    unittest.main()
