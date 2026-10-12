"""Executor tests for the second adapter of ``smctl run once`` (run-cli-pi).

``.agent/design/controller-cli.md`` (sections 3, 5, 6 and 7) lets a job name
``pi`` beside ``offline``.  These tests pin the three places the change lives
that the planner oracle does not check directly -- the registry itself, the
adapter ``cmd_once`` hands to the bridge, and the job file's own key -- plus one
run of the real command over a captured macOS pi stream, read back through
``smctl ledger status``, ``smctl ledger events`` and the ledger's own
``operation_usage``.  The children are local ``python -c`` programs; no provider
CLI, no credentials, no network.
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
from unittest import mock

from sentinelmesh import cli
from sentinelmesh.adapter_protocol import OfflineAdapter
from sentinelmesh.adapter_run import DEFAULT_MAX_LINE_BYTES, DEFAULT_MAX_STREAM_BYTES
from sentinelmesh.durable_ledger import open_ledger
from sentinelmesh.pi_events import USAGE_KEYS, PiNativeAdapter
from sentinelmesh.run_cli import RunSpecError, _ADAPTERS, load_job

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pi_native"
FIXTURE = "macos-pi102-adapter-protocol-port-a001"
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
#: The five dimensions pi's translator names, in the translator's own order.
DIMENSIONS = [dimension for _vendor, dimension in USAGE_KEYS]
CAPS = {"dispatches": 9, **{dimension: 10 ** 8 for dimension in DIMENSIONS}}
RESERVATION = {"dispatches": 1, **{dimension: 10 ** 7 for dimension in DIMENSIONS}}
#: A child that writes the file named by its argument to stdout, byte for byte.
CAT = "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read()); sys.stdout.buffer.flush()"
FAKE_OUTCOME = {"state": "REVIEW_PENDING", "stop_reason": "completed", "exit_code": 0, "error": None,
                "result_error": None, "result": {"status": "SUCCEEDED", "settled": True, "raw_complete": True,
                                                 "usage": {dimension: 1 for dimension in DIMENSIONS}},
                "usage_recorded": {}, "usage_skipped": [], "stream": {"status": "ok"}}


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dispatcher_usage() -> dict[str, int]:
    """The dispatcher's own usage numbers for the fixture, by normalized dimension."""
    meta = json.loads((FIXTURES / (FIXTURE + ".meta.json")).read_text(encoding="utf-8"))
    vendor = meta["dispatcher_summary"]["usage"]
    return {dimension: vendor[key] for key, dimension in USAGE_KEYS}


class RunCliPiCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="run-cli-pi-exec-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.count = 0
        self.ledgers = 0
        self.fresh()

    # -- driving the CLI --------------------------------------------------- #

    def smctl(self, *argv):
        """The command in this process, the way ``smctl`` runs it."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main([str(a) for a in argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def spawn(self, *argv):
        completed = subprocess.run([PY, "-m", "sentinelmesh.cli", *[str(a) for a in argv]],
                                   cwd=str(REPO_ROOT), capture_output=True, text=True)
        return completed.stdout, completed.stderr, completed.returncode

    def ok(self, *argv):
        out, err, code = self.spawn(*argv)
        self.assertEqual((code, err), (0, ""), "smctl %s failed: %s" % (argv, err))
        self.assertTrue(out.endswith(NL) and out.count(NL) == 1, "one JSON line expected")
        return json.loads(out)

    # -- files --------------------------------------------------------------- #

    def write_json(self, name, value) -> str:
        path = self.work / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def fresh(self):
        """A new ledger, seeded with enough tasks for every run below."""
        self.ledgers += 1
        self.ledger = str(self.work / ("ledger-%d.db" % self.ledgers))
        spec = {"request_id": "req-1", "repo_id": "repo-1", "caps": dict(CAPS), "concurrency": 4,
                "tasks": [{"task_id": "task-%d" % n, "caps": dict(CAPS), "max_attempts": 3} for n in range(1, 4)]}
        self.ok("run", "init", self.ledger, "--spec", self.write_json("spec-%d.json" % self.ledgers, spec))

    def job(self, command, **overrides):
        self.count += 1
        task = overrides.pop("task_id", "task-%d" % ((self.count - 1) % 3 + 1))
        job = {"request_id": "req-1", "task_id": task, "plan": 1, "idempotency_key": "idem-%d" % self.count,
               "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["src/%s.py" % task],
               "adapter_id": "pi", "role": "executor", "profile_id": "pi-profile", "command": list(command),
               "capabilities": [], "cwd": str(self.cwd), "environment": dict(BASE_ENV), "allowed_secret_names": [],
               "timeout_seconds": 60, "usage_dimensions": list(DIMENSIONS)}
        job.update(overrides)
        return job

    def stream_job(self, **overrides):
        command = overrides.pop("command", [PY, "-c", CAT, str(FIXTURES / (FIXTURE + ".jsonl"))])
        return self.job(command, **overrides)

    def once(self, job):
        self.count += 1
        evidence = self.work / ("evidence-%d" % self.count)
        evidence.mkdir()
        return self.ok("run", "once", self.ledger, "--job", self.write_json("job-%d.json" % self.count, job),
                       "--evidence", str(evidence), "--worktree-id", "wt-1",
                       "--owner", "owner-1")

    # -- the tests ----------------------------------------------------------- #

    def test_the_registry_is_keyed_by_each_adapters_own_adapter_id(self):
        self.assertEqual(set(_ADAPTERS), {"offline", "pi"})
        self.assertIs(_ADAPTERS["offline"], OfflineAdapter)
        self.assertIs(_ADAPTERS["pi"], PiNativeAdapter)
        # Every key is its class's own ``adapter_id``, and the two classes are
        # distinct: the pi translator is not the offline stub under another name.
        for adapter_id, adapter in _ADAPTERS.items():
            self.assertEqual(adapter_id, adapter.adapter_id)
        self.assertEqual(_ADAPTERS["offline"].protocol_version, "offline-adapter-v1")
        self.assertEqual(_ADAPTERS["pi"].protocol_version, "pi-session-v3")
        self.assertIsNot(_ADAPTERS["pi"], OfflineAdapter)
        # The registry is keyed by adapter id, not by dialect: the dialect is the
        # protocol version, and it names no adapter of its own.
        self.assertNotIn("pi-session-v3", _ADAPTERS)

    def test_once_hands_the_bridge_the_adapter_the_job_names(self):
        jobs = [self.stream_job(), self.stream_job(adapter_id="offline")]
        seen = []
        with mock.patch("sentinelmesh.run_cli.run_adapter_operation") as bridge:
            bridge.side_effect = lambda *args, **kwargs: (seen.append(args), FAKE_OUTCOME)[1]
            for job in jobs:
                self.count += 1
                code, out, err = self.smctl("run", "once", self.ledger, "--job",
                                            self.write_json("job-%d.json" % self.count, job),
                                            "--evidence", str(self.work / "evidence"), "--worktree-id", "wt-1",
                                            "--owner", "owner-1")
                self.assertEqual((code, err), (0, ""), err)
                # The outcome the bridge returned is reported verbatim.
                self.assertEqual(json.loads(out)["outcome"], FAKE_OUTCOME)
        self.assertEqual(len(seen), 2)
        for job, args in zip(jobs, seen):
            adapter, invocation = args[1], args[2]
            expected = PiNativeAdapter if job["adapter_id"] == "pi" else OfflineAdapter
            self.assertIs(type(adapter), expected)
            self.assertEqual(invocation.adapter_id, job["adapter_id"])
        # A fresh instance per call: no adapter object is shared between runs.
        self.assertIsNot(seen[0][1], seen[1][1])

    def test_load_job_accepts_pi_and_refuses_the_dialect(self):
        good = self.stream_job()
        self.assertEqual(load_job(self.write_json("job.json", good)),
                         dict(good, max_stream_bytes=DEFAULT_MAX_STREAM_BYTES, max_line_bytes=DEFAULT_MAX_LINE_BYTES))
        # A legal baseline first: ``offline`` still round-trips.
        offline = self.stream_job(adapter_id="offline", usage_dimensions=["input_tokens"])
        self.assertEqual(load_job(self.write_json("offline.json", offline)),
                         dict(offline, max_stream_bytes=DEFAULT_MAX_STREAM_BYTES,
                              max_line_bytes=DEFAULT_MAX_LINE_BYTES))
        # The dialect is a protocol version, not an adapter id: only the adapter
        # id changes, and the refusal names the ids the contract accepts.
        with self.assertRaises(RunSpecError) as caught:
            load_job(self.write_json("dialect.json", self.stream_job(adapter_id="pi-session-v3")))
        exc = caught.exception
        self.assertEqual((exc.code, exc.detail), ("E_RUN_JOB", "adapter_id must be one of offline, pi"))
        # Case and whitespace are folded nowhere: the job names the id exactly.
        for adapter_id in ("PI", "Pi", " pi", "pi "):
            with self.subTest(adapter_id=adapter_id):
                with self.assertRaises(RunSpecError) as caught:
                    load_job(self.write_json("case.json", self.stream_job(adapter_id=adapter_id)))
                self.assertEqual(caught.exception.detail, "adapter_id must be one of offline, pi")

    def test_a_captured_macos_stream_settles_the_dispatchers_usage(self):
        result = self.once(self.stream_job())
        outcome = result["outcome"]
        self.assertEqual(outcome["state"], "REVIEW_PENDING")
        self.assertEqual(outcome["usage_recorded"], {d: "APPLIED" for d in DIMENSIONS})
        self.assertEqual(outcome["usage_skipped"], [])
        expected = dispatcher_usage()
        self.assertEqual({k: outcome["result"]["usage"][k] for k in DIMENSIONS}, expected)
        # The request's own snapshot counts the same numbers, dimension by dimension.
        snapshot = self.ok("ledger", "status", self.ledger)["requests"][0]["snapshot"]
        for dimension in DIMENSIONS:
            self.assertEqual(snapshot[dimension]["known"], expected[dimension])
        self.assertEqual(snapshot["total_tokens"]["known"], expected["total_tokens"])

    def test_the_run_is_recorded_as_one_review_and_five_usage_observations(self):
        result = self.once(self.stream_job())
        operation = result["operation_id"]
        reviews = self.ok("ledger", "events", self.ledger, "--operation", operation,
                          "--type", "run.finished_for_review")
        self.assertEqual(reviews["matched"], 1)
        self.assertEqual([e["type"] for e in reviews["events"]], ["run.finished_for_review"])
        observations = self.ok("ledger", "events", self.ledger, "--operation", operation, "--type", "usage.observed")
        self.assertEqual(observations["matched"], 5)
        self.assertEqual(sorted(e["payload"]["dimension"] for e in observations["events"]), sorted(DIMENSIONS))
        # Every dimension's usage is settled against the one stream the supervisor
        # read, whose digest is the fixture file's own.
        raw = (FIXTURES / (FIXTURE + ".jsonl")).read_bytes()
        ledger = open_ledger(self.ledger)
        try:
            usage = ledger.operation_usage(operation)
        finally:
            ledger.close()
        self.assertEqual(set(usage), set(DIMENSIONS) | {"dispatches"})
        for dimension in DIMENSIONS:
            self.assertEqual((usage[dimension]["amount"], usage[dimension]["evidence_hash"]),
                             (dispatcher_usage()[dimension], sha256_of(raw)))
        self.assertEqual(usage["dispatches"]["amount"], 1)


if __name__ == "__main__":
    unittest.main()
