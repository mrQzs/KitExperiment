"""Executor tests for ``smctl run`` (run_cli.py), complementing the planner oracle.

The oracle drives the two files' item-by-item validation, the command ordering,
replay, the post-handoff refusals and the ceilings/timeouts.  These tests cover
what the oracle leaves to the executor: the ``load_spec``/``load_job`` round trip
and their refusal shape directly, two independent ledgers from two spec files,
the ``once`` outcome read back through ``smctl ledger status``/``events``, the
owner and process identity the run records, a child that exits non-zero with a
complete stream, and a real ``smctl`` process refusing a bad spec.

The child processes are local ``python -c`` programs writing the offline
adapter's protocol; the only ``smctl`` process is a ``python -m
sentinelmesh.cli`` child.  No provider CLI, no credentials, no network.
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
from sentinelmesh.run_cli import RunSpecError, load_job, load_spec
from sentinelmesh.supervised_record import OBSERVATION_NAME

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
CAPS = {"dispatches": 5, "tokens": 100, "cost": 50}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}


def ev(seq, typ, payload=None):
    return {"seq": seq, "event_id": "e%d" % seq, "type": typ, "source": "offline",
            "payload": {} if payload is None else payload}


COMPLETE = [ev(0, "run_started"), ev(1, "assistant"), ev(2, "usage", {"tokens": 12, "cost": 3}),
            ev(3, "run_finished")]


def emit(events, after=""):
    """Python code that writes each event as one LF-terminated JSON line, then ``after``."""
    return ("import json, sys" + NL
            + "for event in json.loads(%r):" % json.dumps(events) + NL
            + "    sys.stdout.buffer.write((json.dumps(event) + chr(10)).encode())" + NL
            + "sys.stdout.buffer.flush()" + NL) + after


class RunCliCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="run-cli-exec-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.ledger = str(self.work / "ledger.db")
        self.count = 0
        self.dirs = 0

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

    def write_json(self, name, value) -> str:
        path = self.work / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    # -- files --------------------------------------------------------------- #

    def spec(self, **overrides):
        spec = {"request_id": "req-1", "repo_id": "repo-1", "caps": dict(CAPS), "concurrency": 4,
                "tasks": [{"task_id": "task-%d" % n, "caps": dict(CAPS), "max_attempts": 3} for n in range(1, 4)]}
        spec.update(overrides)
        return spec

    def job(self, code=None, *args, **overrides):
        self.count += 1
        task = overrides.pop("task_id", "task-%d" % ((self.count - 1) % 3 + 1))
        job = {"request_id": "req-1", "task_id": task, "plan": 1, "idempotency_key": "idem-%d" % self.count,
               "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["src/%s.py" % task],
               "adapter_id": "offline", "role": "executor", "profile_id": "stub-profile",
               "command": [PY, "-c", emit(COMPLETE) if code is None else code, *args], "capabilities": [],
               "cwd": str(self.cwd), "environment": dict(BASE_ENV), "allowed_secret_names": [],
               "timeout_seconds": 10, "usage_dimensions": ["tokens", "cost"]}
        job.update(overrides)
        return job

    def evidence_dir(self) -> Path:
        self.dirs += 1
        path = self.work / ("evidence-%d" % self.dirs)
        path.mkdir()
        return path

    def init(self, **overrides):
        return self.ok("run", "init", self.ledger, "--spec", self.write_json("spec.json", self.spec(**overrides)))

    def once(self, job, evidence, *extra):
        return self.ok("run", "once", self.ledger, "--job", self.write_json("job-%d.json" % self.count, job),
                       "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1", *extra)

    def events_of(self, operation_id, event_type):
        result = self.ok("ledger", "events", self.ledger, "--operation", operation_id)
        return [e for e in result["events"] if e["type"] == event_type]

    # -- the tests ----------------------------------------------------------- #

    def test_load_spec_and_load_job_round_trip_and_refuse_by_key(self):
        # A valid spec round-trips to exactly the file's content.
        spec = self.spec()
        self.assertEqual(load_spec(self.write_json("spec.json", spec)), spec)
        # A valid job round-trips, with the two ceilings defaulted when absent.
        good = self.job()
        expected = dict(good, max_stream_bytes=DEFAULT_MAX_STREAM_BYTES, max_line_bytes=DEFAULT_MAX_LINE_BYTES)
        self.assertEqual(load_job(self.write_json("job.json", good)), expected)
        # ... and kept as written when present.
        with_caps = self.job(max_stream_bytes=4096, max_line_bytes=2048)
        self.assertEqual(load_job(self.write_json("job2.json", with_caps)), with_caps)
        # A bad spec names the offending key in its detail, and carries the shape.
        cases = [("request_id", self.spec(request_id="")),
                 ("caps", self.spec(caps=dict(CAPS, tokens=-1))),
                 ("concurrency", self.spec(concurrency=0))]
        for key, bad in cases:
            with self.subTest(spec_key=key):
                with self.assertRaises(RunSpecError) as caught:
                    load_spec(self.write_json("bad.json", bad))
                exc = caught.exception
                self.assertEqual(exc.code, "E_RUN_SPEC")
                self.assertIsInstance(exc.detail, str)
                self.assertIn(key, exc.detail)
        # A bad job does the same with its own code.
        for key, bad in [("timeout_seconds", self.job(timeout_seconds=0)),
                         ("max_stream_bytes", self.job(max_stream_bytes=0)),
                         ("cwd", self.job(cwd=""))]:
            with self.subTest(job_key=key):
                with self.assertRaises(RunSpecError) as caught:
                    load_job(self.write_json("bad.json", bad))
                exc = caught.exception
                self.assertEqual(exc.code, "E_RUN_JOB")
                self.assertIsInstance(exc.detail, str)
                self.assertIn(key, exc.detail)

    def test_init_two_spec_files_make_two_independent_ledgers(self):
        first = self.ok("run", "init", self.ledger, "--spec", self.write_json("a.json", self.spec(request_id="req-a")))
        self.assertEqual(first["request_id"], "req-a")
        second_path = str(self.work / "ledger-2.db")
        second = self.ok("run", "init", second_path, "--spec", self.write_json("b.json", self.spec(request_id="req-b")))
        self.assertEqual(second["request_id"], "req-b")
        # Each ledger's status shows only its own request.
        status_a = self.ok("ledger", "status", self.ledger)
        status_b = self.ok("ledger", "status", second_path)
        self.assertEqual([r["request_id"] for r in status_a["requests"]], ["req-a"])
        self.assertEqual([r["request_id"] for r in status_b["requests"]], ["req-b"])
        self.assertEqual(status_a["seq"], 4)
        self.assertEqual(status_b["seq"], 4)

    def test_once_review_and_events_read_back_from_the_ledger(self):
        self.init()
        evidence = self.evidence_dir()
        result = self.once(self.job(), evidence)
        op = result["operation_id"]
        self.assertEqual(result["outcome"]["state"], "REVIEW_PENDING")
        # Nothing needs the operator, and the known usage equals the stream's.
        status = self.ok("ledger", "status", self.ledger)
        request = status["requests"][0]
        self.assertEqual(request["attention"], [])
        self.assertEqual((request["snapshot"]["tokens"]["known"], request["snapshot"]["cost"]["known"]), (12, 3))
        # The events command lists the run by operation id.
        self.assertEqual(len(self.events_of(op, "run.started")), 1)
        finished = self.events_of(op, "run.finished_for_review")
        self.assertEqual(len(finished), 1)
        self.assertIn("candidate", finished[0]["payload"])

    def test_once_records_the_owner_identity_and_worktree(self):
        self.init()
        evidence = self.evidence_dir()
        result = self.once(self.job(), evidence, "--owner", "someone-else", "--worktree-id", "wt-9")
        op = result["operation_id"]
        self.assertEqual(result["operation"]["owner"], "someone-else")
        # The run.started event's identity is exactly the observation record's.
        started = self.events_of(op, "run.started")[0]["payload"]["process_identity"]
        observation = json.loads((evidence / OBSERVATION_NAME).read_bytes().decode())
        self.assertEqual(started, observation["observation"]["process_identity"])
        self.assertEqual(started["worktree_id"], "wt-9")
        ledger = open_ledger(self.ledger)
        try:
            self.assertEqual(ledger.run_descriptor(op)["worktree_id"], "wt-9")
        finally:
            ledger.close()

    def test_once_a_child_that_exits_two_with_a_complete_stream_reviews(self):
        self.init()
        evidence = self.evidence_dir()
        # The child writes the complete stream and then exits non-zero.
        result = self.once(self.job(emit(COMPLETE, "import sys" + NL + "sys.exit(2)" + NL)), evidence)
        outcome = result["outcome"]
        self.assertEqual((outcome["state"], outcome["exit_code"]), ("REVIEW_PENDING", 2))
        self.assertEqual(outcome["usage_recorded"], {"tokens": "APPLIED", "cost": "APPLIED"})
        # The operation in the ledger agrees with the outcome.
        ledger = open_ledger(self.ledger)
        try:
            self.assertEqual(ledger.operation(result["operation_id"])["state"], "REVIEW_PENDING")
        finally:
            ledger.close()

    def test_real_process_refuses_a_bad_spec_without_creating_a_file(self):
        # A valid baseline through the real process: the ledger is created.
        good_path = str(self.work / "good.db")
        baseline = subprocess.run([PY, "-m", "sentinelmesh.cli", "run", "init", good_path, "--spec",
                                   self.write_json("good.json", self.spec())],
                                  cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual((baseline.returncode, baseline.stderr), (0, ""))
        self.assertTrue(os.path.exists(good_path))
        # The same call with one required key removed: exit 3, one JSON line, no file.
        bad = self.spec()
        del bad["concurrency"]  # a required key, missing
        spec_path = self.write_json("bad.json", bad)
        completed = subprocess.run([PY, "-m", "sentinelmesh.cli", "run", "init", self.ledger, "--spec", spec_path],
                                   cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual((completed.returncode, completed.stdout), (3, ""))
        self.assertTrue(completed.stderr.endswith(NL) and completed.stderr.count(NL) == 1, "one JSON line on stderr")
        error = json.loads(completed.stderr)
        self.assertEqual(set(error), {"error", "detail", "operation_id"})
        self.assertEqual(error["error"], "E_RUN_SPEC")
        self.assertIsNone(error["operation_id"])
        self.assertFalse(os.path.exists(self.ledger), "a refused init created a ledger")


if __name__ == "__main__":
    unittest.main()
