"""Executor tests for ``smctl run serve --parallel N`` (`.agent/design/serve-parallel.md`).

These cover the pieces the planner oracle leaves to the executor:
``_shared_directories`` called directly (the three ways it finds nothing to refuse
and the pair it reports when two jobs share a working directory), the in-process
report of a two-task request drained two at a time, ``--max-runs`` leaving exactly
one operation behind, the exact report shape with ``parallel`` in it, a refusal
whose stdout report and stderr line are the same object, and real
``python -m sentinelmesh.cli run serve`` subprocesses that refuse a missing jobs
directory and a ``--parallel`` of zero.  Every child is a local ``python -c``
program writing the offline adapter's protocol; no provider CLI, no credentials,
no network.
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

from sentinelmesh import cli
from sentinelmesh.durable_ledger import open_ledger
from sentinelmesh.run_cli import RunSpecError, _shared_directories

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
CAPS = {"dispatches": 20, "tokens": 1000, "cost": 500}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
CANDIDATE = "c" * 64
REPORT_KEYS = {"command", "request", "runs", "skipped", "stopped", "refusal", "parallel"}
RUN_KEYS = {"task_id", "attempt", "operation_id", "state", "evidence_dir"}
REFUSAL_KEYS = {"error", "detail", "operation_id"}


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


class _ParallelHarness(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="run-cli-serve-parallel-x-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.ledger = str(self.work / "ledger.db")
        self.jobs = self.work / "jobs"
        self.jobs.mkdir()
        self.evidence = self.work / "evidence"
        self.roots = 0
        self.init()

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

    def real(self, *argv, timeout=120):
        return subprocess.run([PY, "-m", "sentinelmesh.cli", *[str(a) for a in argv]], cwd=str(REPO_ROOT),
                              capture_output=True, text=True, timeout=timeout)

    # -- files --------------------------------------------------------------- #

    def write_json(self, path, value) -> str:
        Path(path).write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def spec(self, tasks=2, **overrides):
        spec = {"request_id": "req-1", "repo_id": "repo-1", "caps": dict(CAPS), "concurrency": 4,
                "tasks": [{"task_id": "task-%d" % n, "caps": dict(CAPS), "max_attempts": 2}
                          for n in range(1, tasks + 1)]}
        spec.update(overrides)
        return spec

    def init(self, **overrides):
        return self.ok("run", "init", self.ledger, "--spec",
                       self.write_json(self.work / "spec.json", self.spec(**overrides)))

    def reseed(self, tasks):
        """A fresh ledger, on its own path, seeded with ``tasks`` tasks."""
        self.ledger = str(self.work / ("ledger-%d.db" % tasks))
        return self.init(tasks=tasks)

    def tree(self, name) -> str:
        """A working directory of its own for ``name``."""
        path = self.work / ("tree-" + name)
        path.mkdir(exist_ok=True)
        return str(path)

    def job(self, task_id, **overrides):
        job = {"request_id": "req-1", "task_id": task_id, "plan": 1, "idempotency_key": "idem-" + task_id,
               "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["src/%s.py" % task_id],
               "adapter_id": "offline", "role": "executor", "profile_id": "stub-profile",
               "command": [PY, "-c", _emit(COMPLETE)], "capabilities": [],
               "cwd": self.tree(task_id), "environment": dict(BASE_ENV), "allowed_secret_names": [],
               "timeout_seconds": 30, "usage_dimensions": ["tokens", "cost"]}
        job.update(overrides)
        return job

    def write_jobs(self, *jobs):
        for job in jobs:
            self.write_json(self.jobs / (job["task_id"] + ".json"), job)

    def root(self) -> str:
        self.roots += 1
        return str(self.evidence / ("root-%d" % self.roots))

    def operations(self):
        ledger = open_ledger(self.ledger)
        try:
            return [ledger.operation(op) for op in ledger.operation_ids()]
        finally:
            ledger.close()

    # -- serve --------------------------------------------------------------- #

    def serve_argv(self, *extra, root=None, jobs=None, owner="owner-1", request="req-1", candidate=CANDIDATE,
                   parallel=2):
        return ("run", "serve", self.ledger, "--request", request, "--jobs", str(jobs or self.jobs),
                "--evidence", root or self.root(), "--worktree-id", "wt-1", "--owner", owner,
                *(("--candidate", candidate) if candidate else ()),
                *(("--parallel", str(parallel)) if parallel is not None else ()), *extra)


class SharedDirectoryTests(_ParallelHarness):
    """``_shared_directories`` on its own: the rule, and the pair it names."""

    def test_jobs_in_different_directories_share_nothing(self):
        self.reseed(tasks=3)
        jobs = {task_id: self.job(task_id) for task_id in ("task-1", "task-2", "task-3")}
        self.assertIsNone(_shared_directories(jobs))

    def test_one_directory_with_a_common_resource_key_shares_nothing(self):
        self.reseed(tasks=2)
        tree = self.tree("shared")
        # The valid baseline: the same directory, paid for with a common resource key.
        jobs = {"task-1": self.job("task-1", cwd=tree, resources=["wt", "src/task-1.py"]),
                "task-2": self.job("task-2", cwd=tree, resources=["src/task-2.py", "wt"])}
        self.assertIsNone(_shared_directories(jobs))
        # The one change: the key that paid for the directory is gone from task-2.
        jobs["task-2"]["resources"] = ["src/task-2.py"]
        with self.assertRaises(RunSpecError) as caught:
            _shared_directories(jobs)
        self.assertEqual(caught.exception.code, "E_RUN_JOBS")
        self.assertIn("task-1", caught.exception.detail)
        self.assertIn("task-2", caught.exception.detail)
        self.assertIn(tree, caught.exception.detail)

    def test_another_spelling_of_the_same_directory_is_the_same_directory(self):
        self.reseed(tasks=2)
        tree = self.tree("one")
        jobs = {"task-1": self.job("task-1", cwd=tree),
                "task-2": self.job("task-2", cwd=os.path.join(tree, "sub", ".."))}
        with self.assertRaises(RunSpecError) as caught:
            _shared_directories(jobs)
        self.assertEqual(caught.exception.code, "E_RUN_JOBS")

    def test_the_first_pair_in_task_order_is_the_one_reported(self):
        self.reseed(tasks=4)
        alpha, beta = self.tree("alpha"), self.tree("beta")
        jobs = {"task-1": self.job("task-1", cwd=alpha), "task-3": self.job("task-3", cwd=alpha),
                "task-2": self.job("task-2", cwd=beta), "task-4": self.job("task-4", cwd=beta)}
        with self.assertRaises(RunSpecError) as caught:
            _shared_directories(jobs)
        self.assertEqual(caught.exception.code, "E_RUN_JOBS")
        self.assertIn("task-1", caught.exception.detail)
        self.assertIn("task-3", caught.exception.detail)
        self.assertNotIn("task-2", caught.exception.detail)
        self.assertNotIn("task-4", caught.exception.detail)


class ServeParallelReportTests(_ParallelHarness):
    """The concurrent walk's report, in process and as a real subprocess."""

    def test_two_tasks_are_drained_two_at_a_time_in_task_order(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"))
        root = self.root()
        report = self.ok(*self.serve_argv(root=root))
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual((report["command"], report["request"], report["parallel"]), ("serve", "req-1", 2))
        self.assertEqual([(r["task_id"], r["attempt"], r["state"]) for r in report["runs"]],
                         [("task-1", 1, "REVIEW_PENDING"), ("task-2", 1, "REVIEW_PENDING")])
        self.assertEqual([r["evidence_dir"] for r in report["runs"]],
                         [os.path.join(root, "task-1", "1"), os.path.join(root, "task-2", "1")])
        for run in report["runs"]:
            self.assertEqual(set(run), RUN_KEYS)
        self.assertEqual((report["skipped"], report["stopped"], report["refusal"]), ([], "drained", None))
        self.assertEqual([o["task_id"] for o in self.operations()], ["task-1", "task-2"])

    def test_max_runs_one_stops_after_the_first_run(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"))
        report = self.ok(*self.serve_argv("--max-runs", "1"))
        self.assertEqual(([r["task_id"] for r in report["runs"]], report["stopped"]), (["task-1"], "max_runs"))
        self.assertEqual((report["skipped"], report["refusal"], report["parallel"]), ([], None, 2))
        # Exactly one run means exactly one operation in the ledger.
        self.assertEqual([o["task_id"] for o in self.operations()], ["task-1"])

    def test_a_refusal_writes_the_same_object_on_stdout_and_stderr(self):
        self.write_jobs(self.job("task-1"))
        # The valid baseline: the same command, with the jobs directory present, runs.
        self.assertEqual(self.ok(*self.serve_argv())["stopped"], "drained")
        # The one change: the jobs directory is gone, refused before the ledger opens.
        code, out, err = self.smctl(*self.serve_argv(jobs=str(self.work / "no-jobs-here")))
        self.assertEqual(code, 3, (out, err))
        self.assertTrue(out.endswith(NL) and out.count(NL) == 1, (out, err))
        self.assertTrue(err.endswith(NL) and err.count(NL) == 1, (out, err))
        report, error = json.loads(out), json.loads(err)
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(set(error), REFUSAL_KEYS)
        self.assertEqual((report["stopped"], report["refusal"]), ("refused", error))
        self.assertEqual((report["parallel"], error["error"], error["operation_id"]), (2, "E_RUN_JOBS", None))

    def test_a_real_subprocess_refuses_a_missing_jobs_directory(self):
        self.write_jobs(self.job("task-1"))
        # The valid baseline: the real command, with the jobs directory present, exits 0.
        baseline = self.real(*self.serve_argv())
        self.assertEqual((baseline.returncode, baseline.stderr), (0, ""), baseline.stderr)
        self.assertEqual(json.loads(baseline.stdout)["parallel"], 2)
        # The one change: the jobs directory is gone.
        completed = self.real(*self.serve_argv(jobs=str(self.work / "no-jobs-here")))
        self.assertEqual(completed.returncode, 3, (completed.stdout, completed.stderr))
        self.assertEqual(completed.stdout.count(NL), 1)
        self.assertEqual(completed.stderr.count(NL), 1)
        report = json.loads(completed.stdout)
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual((report["command"], report["stopped"], report["parallel"]), ("serve", "refused", 2))
        self.assertEqual((report["runs"], report["refusal"]["error"]), ([], "E_RUN_JOBS"))

    def test_a_real_subprocess_refuses_a_parallel_of_zero(self):
        self.write_jobs(self.job("task-1"))
        completed = self.real(*self.serve_argv("--parallel", "0", parallel=None))
        self.assertEqual(completed.returncode, 2, (completed.stdout, completed.stderr))
        self.assertEqual((completed.stdout, self.operations()), ("", []))


if __name__ == "__main__":
    unittest.main()
