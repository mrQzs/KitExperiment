"""Executor tests for ``smctl run serve`` (`.agent/design/serve-loop.md`).

These cover the pieces the planner oracle leaves to the executor: ``load_jobs``
called directly (its round-trip and each refusal), the one-job/one-skip
in-process report, ``--max-runs`` leaving the right number of operations in the
ledger, the exact report shape, a refusal whose stdout report and stderr line are
the same object, and a real ``python -m sentinelmesh.cli run serve`` subprocess
that refuses a missing jobs directory.  Every child is a local ``python -c``
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
from sentinelmesh.adapter_run import DEFAULT_MAX_LINE_BYTES, DEFAULT_MAX_STREAM_BYTES
from sentinelmesh.durable_ledger import open_ledger
from sentinelmesh.run_cli import RunSpecError, load_jobs

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
CAPS = {"dispatches": 20, "tokens": 1000, "cost": 500}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
CANDIDATE = "c" * 64
REPORT_KEYS = {"command", "request", "runs", "skipped", "stopped", "refusal"}
RUN_KEYS = {"task_id", "attempt", "operation_id", "state", "evidence_dir"}
SKIP_KEYS = {"task_id", "reason"}
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


class _ServeHarness(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="run-cli-serve-x-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
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

    def job(self, task_id, **overrides):
        job = {"request_id": "req-1", "task_id": task_id, "plan": 1, "idempotency_key": "idem-" + task_id,
               "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["src/%s.py" % task_id],
               "adapter_id": "offline", "role": "executor", "profile_id": "stub-profile",
               "command": [PY, "-c", _emit(COMPLETE)], "capabilities": [],
               "cwd": str(self.cwd), "environment": dict(BASE_ENV), "allowed_secret_names": [],
               "timeout_seconds": 10, "usage_dimensions": ["tokens", "cost"]}
        job.update(overrides)
        return job

    def write_jobs(self, *jobs, names=None):
        for index, job in enumerate(jobs):
            name = (names or [None] * len(jobs))[index] or (job["task_id"] + ".json")
            self.write_json(self.jobs / name, job)

    def root(self) -> str:
        self.roots += 1
        return str(self.evidence / ("root-%d" % self.roots))

    # -- serve --------------------------------------------------------------- #

    def serve_argv(self, *extra, root=None, jobs=None, owner="owner-1", request="req-1", candidate=CANDIDATE):
        return ("run", "serve", self.ledger, "--request", request, "--jobs", str(jobs or self.jobs),
                "--evidence", root or self.root(), "--worktree-id", "wt-1", "--owner", owner,
                *(("--candidate", candidate) if candidate else ()), *extra)

    def operations(self):
        ledger = open_ledger(self.ledger)
        try:
            return [ledger.operation(op) for op in ledger.operation_ids()]
        finally:
            ledger.close()


class LoadJobsTests(_ServeHarness):
    def test_reads_every_task_file_and_indexes_by_task_id(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"), self.job("task-3"))
        # A non-JSON file is ignored even though reading it as a job would fail.
        (self.jobs / "notes.txt").write_text("this is not json", encoding="utf-8")
        jobs = load_jobs(str(self.jobs), "req-1")
        self.assertEqual(set(jobs), {"task-1", "task-2", "task-3"})
        for task_id, job in jobs.items():
            self.assertEqual(job["task_id"], task_id)
            # The two ceilings load_job fills in are present in the round-trip.
            self.assertEqual(job["max_stream_bytes"], DEFAULT_MAX_STREAM_BYTES)
            self.assertEqual(job["max_line_bytes"], DEFAULT_MAX_LINE_BYTES)

    def test_refuses_a_directory_that_is_not_there(self):
        with self.assertRaises(RunSpecError) as caught:
            load_jobs(str(self.work / "missing"), "req-1")
        self.assertEqual(caught.exception.code, "E_RUN_JOBS")

    def test_refuses_an_empty_directory(self):
        empty = self.work / "empty"
        empty.mkdir()
        with self.assertRaises(RunSpecError) as caught:
            load_jobs(str(empty), "req-1")
        self.assertEqual(caught.exception.code, "E_RUN_JOBS")

    def test_refuses_a_job_that_load_job_refuses(self):
        # The valid baseline first, then exactly one broken file.
        self.write_jobs(self.job("task-1"), self.job("task-2"))
        self.assertEqual(set(load_jobs(str(self.jobs), "req-1")), {"task-1", "task-2"})
        broken = self.job("task-2")
        del broken["cwd"]
        self.write_json(self.jobs / "task-2.json", broken)
        with self.assertRaises(RunSpecError) as caught:
            load_jobs(str(self.jobs), "req-1")
        self.assertEqual(caught.exception.code, "E_RUN_JOBS")
        self.assertTrue(caught.exception.detail.startswith("task-2.json:"), caught.exception.detail)

    def test_refuses_a_wrong_request_id(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"))
        self.assertEqual(set(load_jobs(str(self.jobs), "req-1")), {"task-1", "task-2"})
        self.write_json(self.jobs / "task-2.json", self.job("task-2", request_id="req-9"))
        with self.assertRaises(RunSpecError) as caught:
            load_jobs(str(self.jobs), "req-1")
        self.assertEqual(caught.exception.code, "E_RUN_JOBS")
        self.assertIn("task-2.json", caught.exception.detail)

    def test_refuses_a_task_id_that_is_not_the_file_name(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"))
        self.assertEqual(set(load_jobs(str(self.jobs), "req-1")), {"task-1", "task-2"})
        (self.jobs / "task-2.json").unlink()
        self.write_json(self.jobs / "task-9.json", self.job("task-2"))
        with self.assertRaises(RunSpecError) as caught:
            load_jobs(str(self.jobs), "req-1")
        self.assertEqual(caught.exception.code, "E_RUN_JOBS")
        self.assertIn("task-9.json", caught.exception.detail)


class ServeReportTests(_ServeHarness):
    def test_runs_the_one_job_and_skips_the_task_without_one(self):
        self.write_jobs(self.job("task-1"))
        report = self.ok(*self.serve_argv())
        self.assertEqual([r["task_id"] for r in report["runs"]], ["task-1"])
        self.assertEqual(report["skipped"], [{"task_id": "task-2", "reason": "no_job"}])
        self.assertEqual(report["stopped"], "drained")

    def test_max_runs_one_stops_after_the_first_run(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"))
        report = self.ok(*self.serve_argv("--max-runs", "1"))
        self.assertEqual([r["task_id"] for r in report["runs"]], ["task-1"])
        self.assertEqual(report["stopped"], "max_runs")
        # Exactly one run means exactly one operation in the ledger.
        self.assertEqual([o["task_id"] for o in self.operations()], ["task-1"])

    def test_the_report_shape_is_exactly_the_contract_keys(self):
        self.write_jobs(self.job("task-1"))
        report = self.ok(*self.serve_argv())
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(set(report["runs"][0]), RUN_KEYS)
        self.assertEqual(set(report["skipped"][0]), SKIP_KEYS)
        self.assertIsNone(report["refusal"])

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
        self.assertEqual((error["error"], error["operation_id"]), ("E_RUN_JOBS", None))

    def test_a_real_subprocess_refuses_a_missing_jobs_directory(self):
        self.write_jobs(self.job("task-1"))
        # The valid baseline: the real command, with the jobs directory present, exits 0.
        baseline = self.real(*self.serve_argv())
        self.assertEqual((baseline.returncode, baseline.stderr), (0, ""), baseline.stderr)
        # The one change: the jobs directory is gone.
        completed = self.real(*self.serve_argv(jobs=str(self.work / "no-jobs-here")))
        self.assertEqual(completed.returncode, 3, (completed.stdout, completed.stderr))
        self.assertEqual(completed.stdout.count(NL), 1)
        self.assertEqual(completed.stderr.count(NL), 1)
        report = json.loads(completed.stdout)
        self.assertEqual((report["command"], report["stopped"]), ("serve", "refused"))
        self.assertEqual(report["refusal"]["error"], "E_RUN_JOBS")


if __name__ == "__main__":
    unittest.main()
