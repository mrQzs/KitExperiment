"""Planner oracle for run-cli-once (`.agent/design/controller-cli.md`, sections 3, 5 and 6).

``smctl run init`` creates a ledger and seeds one request from a strictly checked
specification file; ``smctl run once`` claims exactly one operation and runs
exactly one adapter invocation under the platform's supervisor, with everything
the child sees named in the job file.  The conventions are ``smctl ledger``'s:
one JSON line on stdout, a refusal as one JSON line on stderr with exit 3, an
argparse usage error with exit 2.  The child processes are local ``python -c``
programs writing the offline adapter's protocol; no provider CLI, no
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
import time
import unittest
from pathlib import Path

from sentinelmesh import cli
from sentinelmesh.adapter_run import DEFAULT_MAX_LINE_BYTES, DEFAULT_MAX_STREAM_BYTES, MAX_LINE_BYTES, MAX_STREAM_BYTES
from sentinelmesh.durable_ledger import open_ledger
from sentinelmesh.supervised_operation import evidence_digest
from sentinelmesh.supervised_record import HANDOFF_NAME, OBSERVATION_NAME

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
CAPS = {"dispatches": 5, "tokens": 100, "cost": 50}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
CANDIDATE = "c" * 64
OUTCOME_KEYS = {"operation_id", "state", "decision", "error", "stop_reason", "exit_code", "stream", "result",
                "result_error", "usage_recorded", "usage_skipped"}
REFUSAL_KEYS = {"error", "detail", "operation_id"}


def ev(seq, typ, payload=None):
    return {"seq": seq, "event_id": "e%d" % seq, "type": typ, "source": "offline",
            "payload": {} if payload is None else payload}


COMPLETE = [ev(0, "run_started"), ev(1, "assistant"), ev(2, "usage", {"tokens": 12, "cost": 3}),
            ev(3, "run_finished")]


def emit(events, after=""):
    """Python code that writes each event as one LF-terminated JSON line, byte for byte."""
    return ("import json, sys" + NL
            + "for event in json.loads(%r):" % json.dumps(events) + NL
            + "    sys.stdout.buffer.write((json.dumps(event) + chr(10)).encode())" + NL
            + "sys.stdout.buffer.flush()" + NL) + after


# Reports what the child sees - its environment keys and its working directory -
# on stderr, then writes the complete stream on stdout.
REPORT_THEN_EMIT = ("import json, os, sys" + NL
                    + "sys.stderr.write(json.dumps({'keys': sorted(k.upper() for k in os.environ),"
                    + " 'cwd': os.getcwd(), 'argv': sys.argv[1:]}))" + NL
                    + "sys.stderr.flush()" + NL) + emit(COMPLETE)


def stream_bytes(events) -> bytes:
    return b"".join(json.dumps(event).encode() + bytes([10]) for event in events)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RunCliCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="run-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.ledger = str(self.work / "ledger.db")
        self.count = 0
        self.dirs = 0

    # -- driving the CLI --------------------------------------------------- #

    def smctl(self, *argv):
        """Run ``smctl`` in this process: (exit code, stdout, stderr)."""
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

    def refused(self, expected, *argv, operation_id=None):
        """A refusal: exit 3, nothing on stdout, one JSON line on stderr with the three keys."""
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, out), (3, ""), "smctl %s should be refused: %s" % (argv, err))
        self.assertTrue(err.endswith(NL) and err.count(NL) == 1, "one JSON line expected on stderr")
        error = json.loads(err)
        self.assertEqual(set(error), REFUSAL_KEYS)
        self.assertEqual((error["error"], error["operation_id"]), (expected, operation_id), error)
        self.assertIn(type(error["detail"]), (str, type(None)))
        return error

    def usage_error(self, *argv):
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, out), (2, ""), "smctl %s should be a usage error" % (argv,))
        self.assertNotEqual(err, "")

    # -- files --------------------------------------------------------------- #

    def write_json(self, name, value) -> str:
        path = self.work / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def spec(self, **overrides):
        spec = {"request_id": "req-1", "repo_id": "repo-1", "caps": dict(CAPS), "concurrency": 4,
                "tasks": [{"task_id": "task-%d" % n, "caps": dict(CAPS), "max_attempts": 3} for n in range(1, 7)]}
        spec.update(overrides)
        return spec

    def job(self, code=None, *args, **overrides):
        self.count += 1
        task = overrides.pop("task_id", "task-%d" % self.count)
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

    def fresh(self, name):
        """A second ledger, for a step that must not inherit a stopped request."""
        self.ledger = str(self.work / name)
        self.init()

    def init(self, **overrides):
        return self.ok("run", "init", self.ledger, "--spec", self.write_json("spec.json", self.spec(**overrides)))

    def once(self, job, evidence, *extra):
        return self.ok("run", "once", self.ledger, "--job", self.write_json("job-%d.json" % self.count, job),
                       "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1", *extra)

    def digest(self) -> str:
        return sha256_of(Path(self.ledger).read_bytes())

    def events_of(self, operation_id, event_type):
        ledger = open_ledger(self.ledger)
        try:
            return [e for e in ledger.events if e["operation_id"] == operation_id and e["type"] == event_type]
        finally:
            ledger.close()

    def operation(self, operation_id):
        ledger = open_ledger(self.ledger)
        try:
            return ledger.operation(operation_id), ledger.operation_usage(operation_id), ledger.operation_ids()
        finally:
            ledger.close()

    def ids(self):
        ledger = open_ledger(self.ledger)
        try:
            return ledger.operation_ids()
        finally:
            ledger.close()


class InitOracle(RunCliCase):
    def test_init_creates_the_ledger_and_seeds_the_request(self):
        result = self.init()
        self.assertEqual(result, {"command": "init", "ledger": self.ledger, "request_id": "req-1",
                                  "tasks": ["task-%d" % n for n in range(1, 7)], "generation": 1, "seq": 7})
        status = self.ok("ledger", "status", self.ledger)
        self.assertEqual([r["request_id"] for r in status["requests"]], ["req-1"])
        self.assertEqual((status["requests"][0]["stopped"], status["seq"]), (False, 7))
        self.assertEqual(status["requests"][0]["snapshot"]["tokens"]["limit"], 100)
        # Both tasks were seeded with their own caps: each can be claimed.
        ledger = open_ledger(self.ledger)
        try:
            for task in ("task-1", "task-2"):
                claim = ledger.claim("req-1", task, 1, "idem-" + task, "owner-1", 1, 1, RESERVATION,
                                     ["src/%s.py" % task])
                self.assertIs(claim["replayed"], False)
        finally:
            ledger.close()

    def test_init_refuses_a_path_that_already_holds_a_ledger_and_leaves_it(self):
        self.init()
        before = self.digest()
        self.refused("E_LEDGER_EXISTS", "run", "init", self.ledger, "--spec",
                     self.write_json("spec.json", self.spec(request_id="req-2")))
        self.assertEqual(self.digest(), before)
        self.assertEqual([r["request_id"] for r in self.ok("ledger", "status", self.ledger)["requests"]], ["req-1"])

    def test_init_refuses_a_bad_specification_before_anything_exists(self):
        good = self.spec()
        cases = {
            "extra key": dict(good, owner="x"),
            "missing key": {k: v for k, v in good.items() if k != "concurrency"},
            "not an object": [good],
            "empty request_id": dict(good, request_id=""),
            "request_id not text": dict(good, request_id=1),
            "caps empty": dict(good, caps={}),
            "caps negative": dict(good, caps=dict(CAPS, tokens=-1)),
            "caps boolean": dict(good, caps=dict(CAPS, tokens=True)),
            "caps float": dict(good, caps=dict(CAPS, tokens=1.0)),
            "concurrency zero": dict(good, concurrency=0),
            "concurrency boolean": dict(good, concurrency=True),
            "tasks empty": dict(good, tasks=[]),
            "tasks not a list": dict(good, tasks={"task_id": "task-1"}),
            "task extra key": dict(good, tasks=[dict(good["tasks"][0], owner="x")]),
            "task missing key": dict(good, tasks=[{"task_id": "task-1", "caps": CAPS}]),
            "task max_attempts zero": dict(good, tasks=[dict(good["tasks"][0], max_attempts=0)]),
            "duplicate task_id": dict(good, tasks=[good["tasks"][0], dict(good["tasks"][0])]),
        }
        for name, spec in cases.items():
            with self.subTest(case=name):
                self.refused("E_RUN_SPEC", "run", "init", self.ledger, "--spec", self.write_json("bad.json", spec))
                self.assertFalse(os.path.exists(self.ledger), "a refused init created a ledger")
        with self.subTest(case="missing file"):
            self.refused("E_RUN_SPEC", "run", "init", self.ledger, "--spec", str(self.work / "absent.json"))
        with self.subTest(case="not JSON"):
            (self.work / "garbage.json").write_bytes(b"{not json")
            self.refused("E_RUN_SPEC", "run", "init", self.ledger, "--spec", str(self.work / "garbage.json"))
        with self.subTest(case="not UTF-8"):
            (self.work / "latin.json").write_bytes(b'{"request_id": "r\xe9"}')
            self.refused("E_RUN_SPEC", "run", "init", self.ledger, "--spec", str(self.work / "latin.json"))
        with self.subTest(case="over 64 KiB"):
            (self.work / "huge.json").write_bytes(b" " * 65536 + json.dumps(good).encode())
            self.refused("E_RUN_SPEC", "run", "init", self.ledger, "--spec", str(self.work / "huge.json"))
        with self.subTest(case="exactly 64 KiB is read"):
            padding = 65536 - len(json.dumps(good).encode())
            (self.work / "wide.json").write_bytes(b" " * padding + json.dumps(good).encode())
            self.assertEqual(os.path.getsize(self.work / "wide.json"), 65536)
            self.ok("run", "init", self.ledger, "--spec", str(self.work / "wide.json"))
        self.assertFalse(os.path.exists(self.work / "bad.json.partial"))

    def test_init_refuses_what_the_scheduler_refuses_without_creating_a_file(self):
        # A dimension name the scheduler does not accept passes the file's own
        # shape check; the rehearsal on an in-memory scheduler refuses it first.
        self.refused("E_BUDGET_DIMENSION", "run", "init", self.ledger, "--spec",
                     self.write_json("spec.json", self.spec(caps={"Tokens": 1, "dispatches": 1})))
        self.assertFalse(os.path.exists(self.ledger))
        # A request the ledger itself refuses (a path that cannot be created)
        # is the ledger's own code; the specification was fine.
        self.refused("E_LEDGER_EXISTS", "run", "init", str(self.cwd), "--spec",
                     self.write_json("spec.json", self.spec()))

    def test_usage_errors_exit_2_without_touching_anything(self):
        self.usage_error("run")
        self.usage_error("run", "init", self.ledger)
        self.usage_error("run", "init", self.ledger, "--spec", "a.json", "--extra")
        self.usage_error("run", "once", self.ledger, "--job", "a.json")
        self.usage_error("run", "once", self.ledger, "--job", "a.json", "--evidence", "e", "--worktree-id", "w",
                         "--owner", "o", "--candidate", "zz")
        self.usage_error("run", "once", self.ledger, "--job", "a.json", "--evidence", "e", "--worktree-id", "w",
                         "--owner", "o", "--candidate", "C" * 64)
        self.usage_error("run", "once", self.ledger, "--job", "a.json", "--evidence", "e", "--worktree-id", "w",
                         "--owner", "o", "--now", "-1")
        self.usage_error("run", "once", self.ledger, "--job", "a.json", "--evidence", "e", "--worktree-id", "w",
                         "--owner", "o", "--now", "1.5")
        self.assertFalse(os.path.exists(self.ledger))
        code, out, err = self.smctl("run", "--help")
        self.assertEqual(code, 0)
        self.assertIn("init", out)
        self.assertIn("once", out)


class OnceOracle(RunCliCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_once_claims_runs_and_reviews_exactly_one_operation(self):
        evidence = self.evidence_dir()
        result = self.once(self.job(), evidence)
        self.assertEqual(set(result), {"command", "operation_id", "outcome", "operation"})
        self.assertEqual(result["command"], "once")
        outcome = result["outcome"]
        self.assertEqual(set(outcome), OUTCOME_KEYS)
        op = result["operation_id"]
        self.assertEqual((outcome["operation_id"], outcome["state"], outcome["stop_reason"], outcome["exit_code"],
                          outcome["error"]), (op, "REVIEW_PENDING", "completed", 0, None))
        expected = stream_bytes(COMPLETE)
        self.assertEqual((evidence / "stdout.log").read_bytes(), expected)
        self.assertEqual(outcome["stream"], {"status": "ok", "sha256": sha256_of(expected), "bytes": len(expected)})
        self.assertEqual((outcome["usage_recorded"], outcome["usage_skipped"], outcome["result_error"]),
                         ({"tokens": "APPLIED", "cost": "APPLIED"}, [], None))
        self.assertEqual(result["operation"]["state"], "REVIEW_PENDING")
        self.assertEqual(result["operation"]["owner"], "owner-1")
        self.assertEqual(sorted(os.listdir(evidence)), [HANDOFF_NAME, OBSERVATION_NAME, "stderr.log", "stdout.log"])
        operation, usage, ids = self.operation(op)
        self.assertEqual(ids, (op,))
        self.assertEqual(operation["state"], "REVIEW_PENDING")
        self.assertEqual((usage["tokens"]["amount"], usage["cost"]["amount"], usage["tokens"]["evidence_hash"]),
                         (12, 3, sha256_of(expected)))
        status = self.ok("ledger", "status", self.ledger)
        self.assertEqual((status["requests"][0]["snapshot"]["tokens"]["known"], status["requests"][0]["attention"]),
                         (12, []))
        # The run descriptor the recovery path needs was recorded with the handoff.
        ledger = open_ledger(self.ledger)
        try:
            descriptor = ledger.run_descriptor(op)
        finally:
            ledger.close()
        self.assertEqual((descriptor["evidence_dir"], descriptor["worktree_id"], descriptor["spec"]["cwd"],
                          descriptor["spec"]["timeout_seconds"]), (str(evidence), "wt-1", str(self.cwd), 10))

    def test_once_runs_through_a_real_smctl_process(self):
        evidence = self.evidence_dir()
        job = self.write_json("job.json", self.job())
        completed = subprocess.run([PY, "-m", "sentinelmesh.cli", "run", "once", self.ledger, "--job", job,
                                    "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1"],
                                   cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual((completed.returncode, completed.stderr), (0, ""))
        self.assertEqual(completed.stdout.count(NL), 1)
        result = json.loads(completed.stdout)
        self.assertEqual((result["outcome"]["state"], result["outcome"]["usage_recorded"]),
                         ("REVIEW_PENDING", {"tokens": "APPLIED", "cost": "APPLIED"}))
        self.assertEqual(self.operation(result["operation_id"])[0]["state"], "REVIEW_PENDING")
        # A refusal through the real process: exit 3, one JSON line on stderr.
        again = subprocess.run([PY, "-m", "sentinelmesh.cli", "run", "once", self.ledger, "--job", job,
                                "--evidence", str(self.evidence_dir()), "--worktree-id", "wt-1", "--owner", "owner-1"],
                               cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual((again.returncode, again.stdout), (3, ""))
        self.assertEqual(json.loads(again.stderr)["error"], "E_RUN_CLAIM_REPLAYED")

    def test_the_child_sees_exactly_what_the_job_names(self):
        evidence = self.evidence_dir()
        environment = {"LANG": "C", "PATH": os.path.dirname(PY), "SMCTL_MARK": "from the job"}
        result = self.once(self.job(REPORT_THEN_EMIT, "alpha", "beta", environment=environment), evidence)
        self.assertEqual(result["outcome"]["state"], "REVIEW_PENDING")
        seen = json.loads((evidence / "stderr.log").read_bytes().decode())
        self.assertEqual(seen["keys"], sorted(k.upper() for k in environment),
                         "the child's environment is not the job's mapping")
        self.assertEqual(os.path.realpath(seen["cwd"]), os.path.realpath(str(self.cwd)))
        self.assertEqual(seen["argv"], ["alpha", "beta"])
        # A credential-shaped name is refused by the supervisor unless the job
        # allows it: the outcome says so, the command itself succeeded.
        secret = dict(environment, SERVICE_API_TOKEN="not-a-real-value")
        refused = self.once(self.job(environment=secret), self.evidence_dir())
        self.assertEqual((refused["outcome"]["state"], refused["outcome"]["error"]),
                         ("NOT_STARTED", "E_RUNTIME_ENVIRONMENT"))
        self.assertEqual(self.operation(refused["operation_id"])[0]["state"], "NOT_STARTED")
        allowed = self.once(self.job(environment=secret, allowed_secret_names=["SERVICE_API_TOKEN"]),
                            self.evidence_dir())
        self.assertEqual(allowed["outcome"]["state"], "REVIEW_PENDING")

    def test_the_candidate_is_the_operators_or_the_records_digest(self):
        evidence = self.evidence_dir()
        result = self.once(self.job(), evidence)
        finished = self.events_of(result["operation_id"], "run.finished_for_review")
        self.assertEqual(finished[0]["payload"], {"candidate": evidence_digest(str(evidence))})
        evidence = self.evidence_dir()
        result = self.once(self.job(), evidence, "--candidate", CANDIDATE)
        finished = self.events_of(result["operation_id"], "run.finished_for_review")
        self.assertEqual(finished[0]["payload"], {"candidate": CANDIDATE})

    def test_once_refuses_a_bad_job_before_opening_the_ledger(self):
        good = self.job()
        before = self.digest()
        evidence = self.evidence_dir()
        cases = {
            "extra key": dict(good, owner="x"),
            "missing key": {k: v for k, v in good.items() if k != "cwd"},
            "not an object": [good],
            "plan zero": dict(good, plan=0),
            "plan boolean": dict(good, plan=True),
            "guard_version zero": dict(good, guard_version=0),
            "reservation empty": dict(good, reservation={}),
            "reservation float": dict(good, reservation=dict(RESERVATION, tokens=40.0)),
            "resources not a list": dict(good, resources="src/a.py"),
            "resources repeated": dict(good, resources=["src/a.py", "src/a.py"]),
            "resources not text": dict(good, resources=[1]),
            "adapter unknown": dict(good, adapter_id="pi-session-v3"),
            "adapter not text": dict(good, adapter_id=None),
            "command empty": dict(good, command=[]),
            "command not text": dict(good, command=[PY, 1]),
            "capabilities repeated": dict(good, capabilities=["a", "a"]),
            "cwd empty": dict(good, cwd=""),
            "environment not an object": dict(good, environment=[]),
            "environment value not text": dict(good, environment={"LANG": 1}),
            "allowed_secret_names not a list": dict(good, allowed_secret_names="X"),
            "timeout zero": dict(good, timeout_seconds=0),
            "timeout float": dict(good, timeout_seconds=1.5),
            "usage_dimensions repeated": dict(good, usage_dimensions=["tokens", "tokens"]),
            "max_stream_bytes zero": dict(good, max_stream_bytes=0),
            "max_stream_bytes over the ceiling": dict(good, max_stream_bytes=MAX_STREAM_BYTES + 1),
            "max_line_bytes over the ceiling": dict(good, max_line_bytes=MAX_LINE_BYTES + 1),
            "max_line_bytes boolean": dict(good, max_line_bytes=True),
        }
        for name, job in cases.items():
            with self.subTest(case=name):
                self.refused("E_RUN_JOB", "run", "once", self.ledger, "--job", self.write_json("bad.json", job),
                             "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1")
                self.assertEqual(self.digest(), before, "a refused job changed the ledger")
                self.assertEqual(os.listdir(evidence), [])
        with self.subTest(case="missing file"):
            self.refused("E_RUN_JOB", "run", "once", self.ledger, "--job", str(self.work / "absent.json"),
                         "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1")
        with self.subTest(case="a ledger that cannot be opened, with a valid job"):
            self.refused("E_LEDGER_MISSING", "run", "once", str(self.work / "absent.db"), "--job",
                         self.write_json("good.json", good), "--evidence", str(evidence), "--worktree-id", "wt-1",
                         "--owner", "owner-1")
        with self.subTest(case="the ceilings at their limits are accepted"):
            result = self.once(dict(good, max_stream_bytes=MAX_STREAM_BYTES, max_line_bytes=MAX_LINE_BYTES), evidence)
            self.assertEqual(result["outcome"]["state"], "REVIEW_PENDING")

    def test_once_refuses_a_claim_the_ledger_refuses_without_a_run(self):
        before = self.digest()
        for name, job, code in (("unknown task", self.job(task_id="task-9"), "E_SCOPE_UNKNOWN"),
                                ("unknown request", self.job(request_id="req-9"), "E_SCOPE_UNKNOWN"),
                                ("stale guard", self.job(guard_version=2), "E_GUARD_STALE"),
                                ("reservation without dispatches", self.job(reservation={"tokens": 40, "cost": 20}),
                                 "E_CLAIM_SHAPE")):
            with self.subTest(case=name):
                evidence = self.evidence_dir()
                self.refused(code, "run", "once", self.ledger, "--job", self.write_json("bad.json", job),
                             "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1")
                self.assertEqual(os.listdir(evidence), [])
        self.assertEqual(self.digest(), before, "a refused claim changed the ledger")
        self.assertEqual(self.ids(), ())

    def test_once_does_not_run_a_replayed_claim(self):
        evidence = self.evidence_dir()
        result = self.once(self.job(idempotency_key="idem-same"), evidence)
        op = result["operation_id"]
        again = self.evidence_dir()
        error = self.refused("E_RUN_CLAIM_REPLAYED", "run", "once", self.ledger, "--job",
                             self.write_json("same.json", self.job(idempotency_key="idem-same", task_id="task-1")),
                             "--evidence", str(again), "--worktree-id", "wt-1", "--owner", "owner-1",
                             operation_id=op)
        self.assertIn("REVIEW_PENDING", error["detail"])
        self.assertEqual(os.listdir(again), [], "a replayed claim started a run")
        operation, usage, ids = self.operation(op)
        self.assertEqual((ids, operation["state"], usage["tokens"]["amount"]), ((op,), "REVIEW_PENDING", 12))
        self.assertEqual(len(self.events_of(op, "run.started")), 1)
        # The same key with another intent is the ledger's own refusal, before any claim.
        self.refused("E_IDEMPOTENCY_CONFLICT", "run", "once", self.ledger, "--job",
                     self.write_json("other.json", self.job(idempotency_key="idem-same", task_id="task-3")),
                     "--evidence", str(again), "--worktree-id", "wt-1", "--owner", "owner-1")
        self.assertEqual(self.ids(), (op,))

    def test_a_refusal_after_the_claim_names_the_operation(self):
        evidence = self.evidence_dir()
        code, out, err = self.smctl("run", "once", self.ledger, "--job",
                                    self.write_json("bad.json", self.job(usage_dimensions=["tokens", "gpu"])),
                                    "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1")
        self.assertEqual((code, out), (3, ""))
        error = json.loads(err)
        self.assertEqual(set(error), REFUSAL_KEYS)
        self.assertEqual(error["error"], "E_ADAPTER_SCHEMA")
        op = error["operation_id"]
        self.assertIsNotNone(op, "the claim that happened is not named")
        operation, usage, ids = self.operation(op)
        self.assertEqual((ids, operation["state"]), ((op,), "RESERVED"))
        self.assertEqual(os.listdir(evidence), [])
        self.assertEqual(self.events_of(op, "scheduler.handoff_accepted"), [])

    def test_the_ceilings_and_the_timeout_come_from_the_job(self):
        expected = stream_bytes(COMPLETE)
        evidence = self.evidence_dir()
        result = self.once(self.job(max_stream_bytes=len(expected) - 1), evidence)
        self.assertEqual((result["outcome"]["state"], result["outcome"]["stream"]["status"],
                          result["outcome"]["usage_recorded"]), ("REVIEW_PENDING", "too_large", {}))
        self.fresh("ledger-2.db")
        evidence = self.evidence_dir()
        result = self.once(self.job(max_line_bytes=40), evidence)
        self.assertEqual((result["outcome"]["stream"]["status"], result["outcome"]["stream"]["code"]),
                         ("invalid", "E_JSON_LIMIT"))
        self.assertEqual(result["outcome"]["usage_recorded"], {})
        # A fresh ledger again: an unsettled usage stops the request above.
        self.fresh("ledger-3.db")
        evidence = self.evidence_dir()
        started = time.monotonic()
        result = self.once(self.job(emit(COMPLETE, "import time" + NL + "time.sleep(30)" + NL), timeout_seconds=1),
                           evidence)
        self.assertLess(time.monotonic() - started, 8.0)
        self.assertEqual((result["outcome"]["state"], result["outcome"]["stop_reason"]),
                         ("REVIEW_PENDING", "timeout_killed"))
        self.assertEqual(result["outcome"]["usage_recorded"], {"tokens": "APPLIED", "cost": "APPLIED"})

    def test_a_run_the_runtime_refuses_is_an_outcome_not_a_refusal(self):
        evidence = self.evidence_dir()
        result = self.once(self.job(cwd="tree"), evidence)
        self.assertEqual((result["outcome"]["state"], result["outcome"]["error"], result["outcome"]["stop_reason"]),
                         ("NOT_STARTED", "E_RUNTIME_INVOCATION", None))
        self.assertEqual(self.operation(result["operation_id"])[0]["state"], "NOT_STARTED")
        self.assertEqual(os.listdir(evidence), [])
        # A program that cannot start is a run that happened and was recorded.
        garbage = self.cwd / "not-a-program.exe"
        garbage.write_bytes(bytes([0, 1, 2]) + b" not an executable image" + NL.encode())
        result = self.once(self.job(command=[str(garbage)]), self.evidence_dir())
        self.assertEqual((result["outcome"]["state"], result["outcome"]["stop_reason"]),
                         ("NOT_STARTED", "start_failed" if os.name == "nt" else result["outcome"]["stop_reason"]))

    def test_the_default_ceilings_are_the_adapter_runs_own(self):
        evidence = self.evidence_dir()
        # The child builds the long line itself: a job file stays small.
        width = DEFAULT_MAX_LINE_BYTES - 2000
        big = [ev(0, "run_started"), ev(1, "assistant", {"text": "x" * width}),
               ev(2, "usage", {"tokens": 12, "cost": 3}), ev(3, "run_finished")]
        self.assertLess(len(stream_bytes(big)), DEFAULT_MAX_STREAM_BYTES)
        code = ("import json, sys" + NL
                + "events = json.loads(%r)" % json.dumps(COMPLETE) + NL
                + "events[1]['payload'] = {'text': 'x' * %d}" % width + NL
                + "for event in events:" + NL
                + "    sys.stdout.buffer.write((json.dumps(event) + chr(10)).encode())" + NL)
        result = self.once(self.job(code), evidence)
        self.assertEqual((evidence / "stdout.log").read_bytes(), stream_bytes(big))
        self.assertEqual((result["outcome"]["state"], result["outcome"]["stream"]["status"]),
                         ("REVIEW_PENDING", "ok"))
        # One byte over the default stream ceiling, with the line still under
        # its own: the default is DEFAULT_MAX_STREAM_BYTES, not the maximum.
        self.fresh("ledger-4.db")
        evidence = self.evidence_dir()
        width = DEFAULT_MAX_STREAM_BYTES + 1 - (len(stream_bytes(big)) - (DEFAULT_MAX_LINE_BYTES - 2000))
        over = [ev(0, "run_started"), ev(1, "assistant", {"text": "x" * width}),
                ev(2, "usage", {"tokens": 12, "cost": 3}), ev(3, "run_finished")]
        self.assertEqual(len(stream_bytes(over)), DEFAULT_MAX_STREAM_BYTES + 1)
        self.assertLess(len(json.dumps(over[1]).encode()), DEFAULT_MAX_LINE_BYTES)
        code = ("import json, sys" + NL
                + "events = json.loads(%r)" % json.dumps(COMPLETE) + NL
                + "events[1]['payload'] = {'text': 'x' * %d}" % width + NL
                + "for event in events:" + NL
                + "    sys.stdout.buffer.write((json.dumps(event) + chr(10)).encode())" + NL)
        result = self.once(self.job(code), evidence)
        self.assertEqual(len((evidence / "stdout.log").read_bytes()), DEFAULT_MAX_STREAM_BYTES + 1)
        self.assertEqual((result["outcome"]["stream"]["status"], result["outcome"]["usage_recorded"]),
                         ("too_large", {}))

    def test_once_claims_in_the_ledgers_current_generation(self):
        # A rolled-over ledger is in generation 2 and its request is held back
        # for recovery; once the operator resumes it, a claim must be made in
        # the current generation, never in the first.
        ledger = open_ledger(self.ledger)
        try:
            ledger.rollover()
            self.assertEqual(ledger.generation, 2)
        finally:
            ledger.close()
        status = self.ok("ledger", "status", self.ledger)
        self.assertEqual(status["requests"][0]["stop_reasons"], ["RECOVERY_REQUIRED"])
        self.ok("ledger", "resume", self.ledger, "--request", "req-1", "--reason", "RECOVERY_REQUIRED",
                "--operator", "ops-1", "--note", "nothing was in flight")
        evidence = self.evidence_dir()
        result = self.once(self.job(), evidence)
        self.assertEqual((result["outcome"]["state"], result["operation"]["generation"]), ("REVIEW_PENDING", 2))


if __name__ == "__main__":
    unittest.main()
