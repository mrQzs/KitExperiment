"""Planner oracle for run-cli-pi (`.agent/design/controller-cli.md`, sections 3, 5, 6 and 7).

``smctl run once`` learns the second adapter a job may name: ``pi``, the
translator of pi's ``--mode json`` session v3 frames (dialect
``pi-session-v3``), reduced and settled exactly as the offline stub's events
are.  The command starts nothing but the job's own ``command``, so a real pi is
a job whose argv names the real program; here the children are local
``python -c`` programs that write a captured, reduced pi stream from
``tests/fixtures/pi_native/`` byte for byte, and the usage the ledger settles is
checked against the dispatcher's own numbers recorded next to each fixture.  No
provider CLI, no credentials, no network.
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
from sentinelmesh.pi_events import USAGE_KEYS
from sentinelmesh.supervised_record import HANDOFF_NAME, OBSERVATION_NAME

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pi_native"
COMPLETE_FIXTURE = "windows-pi101-kit-compat-a001"
SECOND_FIXTURE = "macos-pi102-adapter-protocol-port-a001"
TRUNCATED_FIXTURE = "windows-pi101-runtime-a001-truncated"
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
#: The five dimensions pi's translator names, in the translator's own order.
DIMENSIONS = [dimension for _vendor, dimension in USAGE_KEYS]
CAPS = {"dispatches": 9, **{dimension: 10 ** 8 for dimension in DIMENSIONS}}
RESERVATION = {"dispatches": 1, **{dimension: 10 ** 7 for dimension in DIMENSIONS}}
REFUSAL_KEYS = {"error", "detail", "operation_id"}
#: A child that writes the file named by its argument to stdout, byte for byte.
CAT = "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read()); sys.stdout.buffer.flush()"
OFFLINE_EVENTS = [{"seq": 0, "event_id": "e0", "type": "run_started", "source": "offline", "payload": {}},
                  {"seq": 1, "event_id": "e1", "type": "usage", "source": "offline",
                   "payload": {"input_tokens": 7}},
                  {"seq": 2, "event_id": "e2", "type": "run_finished", "source": "offline", "payload": {}}]


def emit_offline():
    return ("import json, sys" + NL
            + "for event in json.loads(%r):" % json.dumps(OFFLINE_EVENTS) + NL
            + "    sys.stdout.buffer.write((json.dumps(event) + chr(10)).encode())" + NL
            + "sys.stdout.buffer.flush()" + NL)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dispatcher_usage(name: str) -> dict[str, int]:
    """The dispatcher's independent usage numbers for a fixture, by normalized dimension."""
    meta = json.loads((FIXTURES / (name + ".meta.json")).read_text(encoding="utf-8"))
    vendor = meta["dispatcher_summary"]["usage"]
    return {dimension: vendor[key] for key, dimension in USAGE_KEYS}


class PiCliCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="run-cli-pi-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.count = 0
        self.dirs = 0
        self.ledgers = 0
        self.fresh()

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

    def refused(self, expected, *argv, operation_id=None):
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, out), (3, ""), "smctl %s should be refused: %s" % (argv, err))
        self.assertTrue(err.endswith(NL) and err.count(NL) == 1, "one JSON line expected on stderr")
        error = json.loads(err)
        self.assertEqual(set(error), REFUSAL_KEYS)
        self.assertEqual((error["error"], error["operation_id"]), (expected, operation_id), error)
        return error

    # -- files and jobs ------------------------------------------------------ #

    def write_json(self, name, value) -> str:
        path = self.work / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def fresh(self):
        """A new ledger: a run whose usage stays unknown stops its request."""
        self.ledgers += 1
        self.ledger = str(self.work / ("ledger-%d.db" % self.ledgers))
        spec = {"request_id": "req-1", "repo_id": "repo-1", "caps": dict(CAPS), "concurrency": 4,
                "tasks": [{"task_id": "task-%d" % n, "caps": dict(CAPS), "max_attempts": 3} for n in range(1, 13)]}
        self.ok("run", "init", self.ledger, "--spec", self.write_json("spec-%d.json" % self.ledgers, spec))

    def job(self, command, **overrides):
        self.count += 1
        task = overrides.pop("task_id", "task-%d" % self.count)
        job = {"request_id": "req-1", "task_id": task, "plan": 1, "idempotency_key": "idem-%d" % self.count,
               "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["src/%s.py" % task],
               "adapter_id": "pi", "role": "executor", "profile_id": "pi-profile", "command": list(command),
               "capabilities": [], "cwd": str(self.cwd), "environment": dict(BASE_ENV), "allowed_secret_names": [],
               "timeout_seconds": 60, "usage_dimensions": list(DIMENSIONS)}
        job.update(overrides)
        return job

    def stream_job(self, fixture, **overrides):
        command = overrides.pop("command", [PY, "-c", CAT, str(FIXTURES / (fixture + ".jsonl"))])
        return self.job(command, **overrides)

    def evidence_dir(self) -> Path:
        self.dirs += 1
        path = self.work / ("evidence-%d" % self.dirs)
        path.mkdir()
        return path

    def once(self, job, evidence):
        return self.ok("run", "once", self.ledger, "--job", self.write_json("job-%d.json" % self.count, job),
                       "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1")

    def once_refused(self, expected, job, evidence):
        return self.refused(expected, "run", "once", self.ledger, "--job",
                            self.write_json("job-%d.json" % self.count, job), "--evidence", str(evidence),
                            "--worktree-id", "wt-1", "--owner", "owner-1")

    # -- reading the ledger -------------------------------------------------- #

    def digest(self) -> str:
        return sha256_of(Path(self.ledger).read_bytes())

    def usage(self, operation_id):
        ledger = open_ledger(self.ledger)
        try:
            return ledger.operation(operation_id)["state"], ledger.operation_usage(operation_id), ledger.operation_ids()
        finally:
            ledger.close()

    def request(self):
        status = self.ok("ledger", "status", self.ledger)
        return status["requests"][0]


class PiOracle(PiCliCase):
    def test_a_captured_pi_stream_runs_to_review_with_the_dispatchers_usage(self):
        for fixture in (COMPLETE_FIXTURE, SECOND_FIXTURE):
            with self.subTest(fixture=fixture):
                evidence = self.evidence_dir()
                result = self.once(self.stream_job(fixture), evidence)
                outcome = result["outcome"]
                op = result["operation_id"]
                self.assertEqual((outcome["state"], outcome["stop_reason"], outcome["exit_code"], outcome["error"],
                                  outcome["result_error"]), ("REVIEW_PENDING", "completed", 0, None, None))
                raw = (FIXTURES / (fixture + ".jsonl")).read_bytes()
                self.assertEqual((evidence / "stdout.log").read_bytes(), raw)
                self.assertEqual(outcome["stream"], {"status": "ok", "sha256": sha256_of(raw), "bytes": len(raw)})
                self.assertEqual(sorted(os.listdir(evidence)),
                                 [HANDOFF_NAME, OBSERVATION_NAME, "stderr.log", "stdout.log"])
                # The adapter's result is the translation settled: the five
                # dimensions are the dispatcher's own numbers for this stream.
                expected = dispatcher_usage(fixture)
                adapter = outcome["result"]
                self.assertEqual((adapter["status"], adapter["settled"], adapter["raw_complete"]),
                                 ("SUCCEEDED", True, True))
                self.assertEqual({k: adapter["usage"][k] for k in DIMENSIONS}, expected)
                self.assertEqual((adapter["usage"]["complete"], adapter["usage"]["assistant_messages"] > 0), (True, True))
                self.assertEqual(outcome["usage_recorded"], {d: "APPLIED" for d in DIMENSIONS})
                self.assertEqual(outcome["usage_skipped"], [])
                state, usage, _ids = self.usage(op)
                self.assertEqual(state, "REVIEW_PENDING")
                self.assertEqual({d: usage[d]["amount"] for d in DIMENSIONS}, expected)
                self.assertEqual({usage[d]["evidence_hash"] for d in DIMENSIONS}, {sha256_of(raw)})
                # The ledger's own reference of the same stream agrees, frame for frame.
                reference = self.ok("ledger", "reference", self.ledger, "--operation", op, "--dialect",
                                    "pi-session-v3")["reference"]
                self.assertEqual(reference["usage"], expected)
                self.assertEqual((reference["frames"], reference["complete"], reference["terminal"]),
                                 (raw.count(NL.encode()), True, "agent_settled"))
        request = self.request()
        self.assertEqual((request["stopped"], request["attention"]), (False, []))
        self.assertEqual(request["snapshot"]["input_tokens"]["known"],
                         dispatcher_usage(COMPLETE_FIXTURE)["input_tokens"]
                         + dispatcher_usage(SECOND_FIXTURE)["input_tokens"])

    def test_a_truncated_stream_reviews_with_its_usage_unknown(self):
        evidence = self.evidence_dir()
        result = self.once(self.stream_job(TRUNCATED_FIXTURE), evidence)
        outcome = result["outcome"]
        self.assertEqual((outcome["state"], outcome["stop_reason"], outcome["result_error"]),
                         ("REVIEW_PENDING", "completed", None))
        adapter = outcome["result"]
        self.assertEqual((adapter["status"], adapter["settled"], adapter["raw_complete"], adapter["usage"]),
                         ("UNKNOWN", False, False, {"status": "unknown"}))
        # Nothing is settled from a stream that never reached its terminal
        # frame: every dimension the job named is skipped, in the job's order,
        # and the request is held until an operator enters the usage.
        self.assertEqual((outcome["usage_recorded"], outcome["usage_skipped"]), ({}, DIMENSIONS))
        state, usage, _ids = self.usage(result["operation_id"])
        self.assertEqual(state, "REVIEW_PENDING")
        self.assertEqual(set(usage), {"dispatches"})
        request = self.request()
        self.assertEqual((request["stopped"], request["stop_reasons"]), (True, ["USAGE_UNKNOWN"]))
        self.assertEqual([(a["operation_id"], a["reasons"]) for a in request["attention"]],
                         [(result["operation_id"], ["usage_unknown"])])
        self.once_refused("E_STOPPED", self.stream_job(COMPLETE_FIXTURE), self.evidence_dir())

    def test_the_adapter_is_the_one_the_job_names(self):
        # Offline events handed to the pi translator are not a pi session: the
        # adapter's refusal is the outcome's result_error, nothing is settled.
        result = self.once(self.job([PY, "-c", emit_offline()]), self.evidence_dir())
        outcome = result["outcome"]
        self.assertEqual((outcome["state"], outcome["result_error"], outcome["result"], outcome["usage_recorded"],
                          outcome["usage_skipped"]), ("REVIEW_PENDING", "E_ADAPTER_SETTLEMENT", None, {}, DIMENSIONS))
        # Pi frames handed to the offline stub are not normalized events.
        self.fresh()
        result = self.once(self.stream_job(COMPLETE_FIXTURE, adapter_id="offline", usage_dimensions=["input_tokens"]),
                           self.evidence_dir())
        outcome = result["outcome"]
        self.assertEqual((outcome["state"], outcome["result_error"], outcome["usage_recorded"],
                          outcome["usage_skipped"]), ("REVIEW_PENDING", "E_ADAPTER_EVENT", {}, ["input_tokens"]))
        # The same offline events through the offline stub settle as before.
        self.fresh()
        result = self.once(self.job([PY, "-c", emit_offline()], adapter_id="offline",
                                    usage_dimensions=["input_tokens"]), self.evidence_dir())
        self.assertEqual((result["outcome"]["state"], result["outcome"]["usage_recorded"]),
                         ("REVIEW_PENDING", {"input_tokens": "APPLIED"}))
        # A job that names fewer dimensions than the translator reports settles
        # only those, and skips nothing.
        self.fresh()
        result = self.once(self.stream_job(COMPLETE_FIXTURE, usage_dimensions=["output_tokens", "cache_read_tokens"]),
                           self.evidence_dir())
        self.assertEqual((result["outcome"]["usage_recorded"], result["outcome"]["usage_skipped"]),
                         ({"output_tokens": "APPLIED", "cache_read_tokens": "APPLIED"}, []))
        expected = dispatcher_usage(COMPLETE_FIXTURE)
        _state, usage, _ids = self.usage(result["operation_id"])
        self.assertEqual({k: usage[k]["amount"] for k in ("output_tokens", "cache_read_tokens")},
                         {k: expected[k] for k in ("output_tokens", "cache_read_tokens")})
        # Neither the total nor any other dimension the translator reports is
        # added behind the job's back (mutation M15 of attempt-001).
        self.assertEqual(set(usage), {"dispatches", "output_tokens", "cache_read_tokens"})

    def test_the_job_names_the_adapter_exactly(self):
        before = self.digest()
        evidence = self.evidence_dir()
        for adapter_id in ("pi-session-v3", "PI", "Pi", " pi", "pi ", "pi" + chr(0), "", None, ["pi"]):
            with self.subTest(adapter_id=adapter_id):
                error = self.once_refused("E_RUN_JOB", self.stream_job(COMPLETE_FIXTURE, adapter_id=adapter_id),
                                          evidence)
                if isinstance(adapter_id, str) and adapter_id:
                    self.assertIn("offline", error["detail"])
                    self.assertIn("pi", error["detail"])
                self.assertEqual(self.digest(), before, "a refused job changed the ledger")
                self.assertEqual(os.listdir(evidence), [])
        ledger = open_ledger(self.ledger)
        try:
            self.assertEqual(ledger.operation_ids(), ())
        finally:
            ledger.close()

    def test_the_program_is_the_jobs_argv_resolved_by_the_supervisor(self):
        # A bare name is looked up on the job's PATH and resolved to the real
        # program by the supervisor: here the interpreter itself.
        evidence = self.evidence_dir()
        result = self.once(self.stream_job(COMPLETE_FIXTURE, command=[os.path.basename(PY).rsplit(".", 1)[0], "-c",
                                                                      CAT, str(FIXTURES / (COMPLETE_FIXTURE + ".jsonl"))]),
                           evidence)
        self.assertEqual((result["outcome"]["state"], result["outcome"]["usage_recorded"]),
                         ("REVIEW_PENDING", {d: "APPLIED" for d in DIMENSIONS}))
        if os.name != "nt":
            return
        # On Windows pi is installed as a ``pi.cmd`` shim; the supervisor
        # refuses a shim (it is not a program), so a job must name the real
        # program -- ``node.exe`` and pi's ``cli.js`` -- and a job naming the
        # shim, by path or by bare name on a PATH that holds it, is NOT_STARTED
        # with the identity code, and no stream is written.
        shim = self.cwd / "pi.cmd"
        shim.write_text("@echo off" + NL + "echo {}" + NL, encoding="utf-8")
        for name, job in (("by path", self.stream_job(COMPLETE_FIXTURE, command=[str(shim), "--mode", "json"])),
                          ("by bare name", self.stream_job(COMPLETE_FIXTURE, command=["pi", "--mode", "json"],
                                                           environment={"LANG": "C", "PATH": str(self.cwd)}))):
            with self.subTest(case=name):
                evidence = self.evidence_dir()
                result = self.once(job, evidence)
                outcome = result["outcome"]
                self.assertEqual((outcome["state"], outcome["error"], outcome["stream"]["status"],
                                  outcome["usage_recorded"]), ("NOT_STARTED", "E_RUNTIME_IDENTITY", "missing", {}))
                self.assertNotIn("stdout.log", os.listdir(evidence))
                self.assertEqual(self.usage(result["operation_id"])[0], "NOT_STARTED")


if __name__ == "__main__":
    unittest.main()
