"""The one live pi run under the supervisor, replayed (`.agent/design/live-pi-run.md`).

On 2026-10-11 the planner ran the real pi 1.1.0 once on Windows through
``smctl run init`` and ``smctl run once``: the job named ``node.exe`` and pi's
``cli.js`` (``--provider step --model step-5-preview --thinking off``, no
tools, no session, one fixed prompt), the Job Object supervisor recorded the
run, the ``pi`` adapter translated the session v3 stream and the ledger settled
the five token dimensions.  The stream was reduced into
``tests/fixtures/pi_native/windows-pi110-live-smctl-a001.jsonl`` and the
numbers the command settled were recorded next to it (``live_run`` in the
``.meta.json``).  This test replays that stream through the same command with a
local ``python -c`` child and pins the same outcome, so the live evidence stays
checked without a provider, credentials or the network.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import cli
from sentinelmesh.durable_ledger import open_ledger
from sentinelmesh.pi_events import USAGE_KEYS, translate

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pi_native"
FIXTURE = "windows-pi110-live-smctl-a001"
DIMENSIONS = [dimension for _vendor, dimension in USAGE_KEYS]
CAPS = {"dispatches": 9, **{dimension: 10 ** 6 for dimension in DIMENSIONS}}
RESERVATION = {"dispatches": 1, **{dimension: 10 ** 5 for dimension in DIMENSIONS}}
CAT = "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read()); sys.stdout.buffer.flush()"


def frames_of(raw: bytes):
    return [json.loads(line) for line in raw.split(b"\n") if line]


class LivePiFixture(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="live-pi-fixture-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.meta = json.loads((FIXTURES / (FIXTURE + ".meta.json")).read_text(encoding="utf-8"))
        self.raw = (FIXTURES / (FIXTURE + ".jsonl")).read_bytes()

    def smctl(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main([str(a) for a in argv])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual((code, err.getvalue()), (0, ""), "smctl %s failed: %s" % (argv, err.getvalue()))
        return json.loads(out.getvalue())

    def test_the_live_run_is_what_the_fixture_records(self):
        live = self.meta["live_run"]
        source = self.meta["source"]
        # Provenance: pi 1.1.0 on Windows, through node.exe, one assistant
        # message answering the fixed prompt, every frame known to the translator.
        self.assertEqual((source["pi_version"], live["pi_version"], live["node_version"]), ("1.1.0", "1.1.0", "v24.19.0"))
        self.assertTrue(source["platform"].startswith("Windows"))
        self.assertEqual(len(live["node_exe_sha256"]), 64)
        translation = translate(frames_of(self.raw))
        self.assertEqual((translation.unknown, translation.terminal, translation.last_stop_reason,
                          translation.assistant_messages, translation.tool_calls, translation.models),
                         (0, "agent_settled", "stop", 1, 0, (("step", "step-5-preview"),)))
        self.assertEqual(translation.usage, live["ledger_usage"])
        self.assertEqual(self.meta["dispatcher_summary"]["usage"],
                         {vendor: live["ledger_usage"][dimension] for vendor, dimension in USAGE_KEYS})
        self.assertEqual(live["outcome"]["state"], "REVIEW_PENDING")
        self.assertEqual(live["outcome"]["usage_recorded"], {d: "APPLIED" for d in DIMENSIONS})
        self.assertGreater(live["ledger_usage"]["input_tokens"], 0)
        self.assertGreater(live["ledger_usage"]["output_tokens"], 0)

    def test_the_stream_replays_through_the_command_to_the_same_settlement(self):
        ledger = str(self.work / "ledger.db")
        cwd = self.work / "tree"
        cwd.mkdir()
        evidence = self.work / "evidence"
        evidence.mkdir()
        spec = {"request_id": "req-live", "repo_id": "KitExperiment", "caps": dict(CAPS), "concurrency": 1,
                "tasks": [{"task_id": "task-live", "caps": dict(CAPS), "max_attempts": 1}]}
        (self.work / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        self.smctl("run", "init", ledger, "--spec", str(self.work / "spec.json"))
        job = {"request_id": "req-live", "task_id": "task-live", "plan": 1, "idempotency_key": "idem-live",
               "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["live"], "adapter_id": "pi",
               "role": "executor", "profile_id": "pi-live",
               "command": [PY, "-c", CAT, str(FIXTURES / (FIXTURE + ".jsonl"))], "capabilities": [],
               "cwd": str(cwd), "environment": {"LANG": "C", "PATH": os.path.dirname(PY)},
               "allowed_secret_names": [], "timeout_seconds": 60, "usage_dimensions": list(DIMENSIONS)}
        (self.work / "job.json").write_text(json.dumps(job), encoding="utf-8")
        result = self.smctl("run", "once", ledger, "--job", str(self.work / "job.json"), "--evidence", str(evidence),
                            "--worktree-id", "wt-1", "--owner", "owner-1")
        outcome = result["outcome"]
        live = self.meta["live_run"]
        self.assertEqual((outcome["state"], outcome["stop_reason"], outcome["exit_code"], outcome["result_error"]),
                         ("REVIEW_PENDING", "completed", 0, None))
        self.assertEqual(outcome["result"]["status"], live["outcome"]["result_status"])
        self.assertEqual({k: outcome["result"]["usage"][k] for k in DIMENSIONS}, live["ledger_usage"])
        self.assertEqual(outcome["usage_recorded"], live["outcome"]["usage_recorded"])
        self.assertEqual(outcome["stream"]["sha256"], hashlib.sha256(self.raw).hexdigest())
        handle = open_ledger(ledger)
        try:
            usage = handle.operation_usage(result["operation_id"])
        finally:
            handle.close()
        self.assertEqual({d: usage[d]["amount"] for d in DIMENSIONS}, live["ledger_usage"])
        reference = self.smctl("ledger", "reference", ledger, "--operation", result["operation_id"], "--dialect",
                               "pi-session-v3")["reference"]
        self.assertEqual((reference["usage"], reference["unknown"], reference["terminal"], reference["frames"]),
                         (live["ledger_usage"], 0, "agent_settled", self.raw.count(NL.encode())))


if __name__ == "__main__":
    unittest.main()
