"""Planner oracle for claude-adapter (`.agent/design/claude-adapter.md`, sections 3.2, 5 and 6).

``sentinelmesh.claude_events`` translates Claude Code's ``--output-format
stream-json`` stream (dialect ``claude-stream-json``) into the normalized adapter
events and hands them to the reference reducer unchanged, exactly as
``pi_events`` does for pi:

* ``system`` with ``subtype: init`` is ``run_started`` and must be line 1;
* ``assistant`` (role assistant, content blocks ``text``/``thinking`` only, text
  never copied) is an ``assistant`` event;
* ``rate_limit_event`` is counted and ignored;
* ``result`` is the only terminal: its usage becomes one ``usage`` event with
  ``total_tokens`` the sum of the four counters, then ``run_finished`` for
  ``subtype: success`` with ``is_error: false`` and at least one assistant
  message, ``error`` otherwise;
* anything else -- another type, another ``system`` subtype, a malformed
  tool-use content block, a frame after ``result``, a frame that is not an
  object -- fails closed: the stream does not settle and no usage is recorded.

Re-pinned by the planner for claude-tools (`.agent/design/claude-tools.md`):
the usage is now the sum of ``result.modelUsage`` (equal to ``result.usage`` in
this one-model recording), so the illegal-usage cases change ``modelUsage``; a
well-formed tool-use block is now a tool call, so the matrix uses one without an
id.  `tests/test_claude_tools_oracle.py` covers the rest.

The registry ``run_cli._ADAPTERS`` gains ``claude``.  The real stream recorded on
2026-10-16 (`tests/fixtures/claude_stream/windows-claude2_1_301-live-a001.*`) is
replayed through a real ``smctl run once`` with a local ``python -c`` child: no
provider, no credentials, no network.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import cli, run_cli
from sentinelmesh.adapter_protocol import AdapterError, Invocation
from sentinelmesh.durable_ledger import open_ledger

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "claude_stream"
FIXTURE = "windows-claude2_1_301-live-a001"
PI_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "pi_native" / "windows-pi110-live-smctl-a001.jsonl"
DIMENSIONS = ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens"]
CAPS = {"dispatches": 9, **{dimension: 10 ** 6 for dimension in DIMENSIONS}}
RESERVATION = {"dispatches": 1, **{dimension: 10 ** 5 for dimension in DIMENSIONS}}
CAT = "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read()); sys.stdout.buffer.flush()"
EXPECTED_USAGE = {"input_tokens": 2, "output_tokens": 4, "cache_read_tokens": 649, "cache_write_tokens": 2330,
                  "total_tokens": 2985}


def frames_of(raw: bytes):
    return [json.loads(line) for line in raw.split(b"\n") if line.strip()]


def fixture_frames():
    return frames_of((FIXTURES / (FIXTURE + ".jsonl")).read_bytes())


class ClaudeCase(unittest.TestCase):
    def setUp(self):
        from sentinelmesh import claude_events  # the module the contract adds
        self.ce = claude_events
        self.work = Path(tempfile.mkdtemp(prefix="claude-adapter-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.meta = json.loads((FIXTURES / (FIXTURE + ".meta.json")).read_text(encoding="utf-8"))

    def translate(self, frames):
        return self.ce.translate(frames)

    def types(self, translation):
        return [event["type"] for event in translation.events]

    def smctl(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main([str(a) for a in argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def once(self, stream_path, adapter_id="claude", dimensions=None):
        """``smctl run once`` with a child that writes ``stream_path`` byte for byte."""
        dimensions = list(DIMENSIONS if dimensions is None else dimensions)
        caps = {"dispatches": 9, **{dimension: 10 ** 6 for dimension in dimensions}}
        reservation = {"dispatches": 1, **{dimension: 10 ** 5 for dimension in dimensions}}
        ledger = str(self.work / ("ledger-%s.db" % len(list(self.work.glob("ledger-*.db")))))
        tree = self.work / "tree"
        tree.mkdir(exist_ok=True)
        evidence = Path(tempfile.mkdtemp(prefix="ev-", dir=str(self.work)))
        spec = {"request_id": "req-c", "repo_id": "KitExperiment", "caps": caps, "concurrency": 1,
                "tasks": [{"task_id": "task-c", "caps": caps, "max_attempts": 1}]}
        (self.work / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        code, out, err = self.smctl("run", "init", ledger, "--spec", str(self.work / "spec.json"))
        self.assertEqual(code, 0, err)
        job = {"request_id": "req-c", "task_id": "task-c", "plan": 1, "idempotency_key": "idem-c", "guard_version": 1,
               "reservation": reservation, "resources": ["c"], "adapter_id": adapter_id, "role": "executor",
               "profile_id": "claude-code", "command": [PY, "-c", CAT, str(stream_path)], "capabilities": [],
               "cwd": str(tree), "environment": {"LANG": "C", "PATH": os.path.dirname(PY)},
               "allowed_secret_names": [], "timeout_seconds": 60, "usage_dimensions": dimensions}
        (self.work / "job.json").write_text(json.dumps(job), encoding="utf-8")
        code, out, err = self.smctl("run", "once", ledger, "--job", str(self.work / "job.json"), "--evidence",
                                    str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1")
        self.assertEqual((code, err), (0, ""), err)
        result = json.loads(out)
        with open_ledger(ledger) as handle:
            usage = handle.operation_usage(result["operation_id"])
        return result["outcome"], usage

    def write_stream(self, frames, name="stream.jsonl"):
        path = self.work / name
        path.write_bytes("".join(json.dumps(frame) + NL for frame in frames).encode("utf-8"))
        return path


class ClaudeOracle(ClaudeCase):
    # -- the recorded stream --------------------------------------------------- #

    def test_the_recorded_stream_translates_to_its_four_events(self):
        translation = self.translate(fixture_frames())
        self.assertEqual(translation.dialect, "claude-stream-json")
        self.assertEqual(self.types(translation), ["run_started", "assistant", "usage", "run_finished"])
        self.assertEqual((translation.frames, translation.ignored, translation.unknown), (4, {"rate_limit_event": 1}, 0))
        self.assertEqual((translation.usage, translation.usage_complete), (EXPECTED_USAGE, True))
        self.assertEqual((translation.terminal, translation.subtype, translation.is_error, translation.num_turns,
                          translation.assistant_messages), ("result", "success", False, 1, 1))
        report = self.meta["cli_report"]["usage"]
        self.assertEqual((report["input_tokens"], report["output_tokens"], report["cache_read_input_tokens"],
                          report["cache_creation_input_tokens"]), (2, 4, 649, 2330))
        # Every event is the adapter's own and carries no text of the conversation.
        for event in translation.events:
            self.assertEqual(event["source"], "claude")
            self.assertNotIn("OK", json.dumps(event["payload"]))

    def test_the_recorded_stream_replays_through_smctl_to_the_cli_reported_usage(self):
        outcome, usage = self.once(FIXTURES / (FIXTURE + ".jsonl"))
        self.assertEqual((outcome["state"], outcome["exit_code"], outcome["result_error"]), ("REVIEW_PENDING", 0, None))
        self.assertEqual(outcome["result"]["status"], "SUCCEEDED")
        self.assertEqual({d: outcome["result"]["usage"][d] for d in DIMENSIONS}, EXPECTED_USAGE)
        self.assertEqual(outcome["usage_recorded"], {d: "APPLIED" for d in DIMENSIONS})
        self.assertEqual({d: usage[d]["amount"] for d in DIMENSIONS}, EXPECTED_USAGE)

    def test_a_stream_cut_before_its_result_reviews_with_its_usage_unknown(self):
        path = self.write_stream(fixture_frames()[:3])
        outcome, usage = self.once(path)
        self.assertEqual((outcome["state"], outcome["result"]["status"], outcome["result"]["raw_complete"]),
                         ("REVIEW_PENDING", "UNKNOWN", False))
        self.assertEqual((outcome["usage_recorded"], sorted(outcome["usage_skipped"])), ({}, sorted(DIMENSIONS)))

    # -- the terminal ------------------------------------------------------------ #

    def test_an_error_result_is_a_complete_error_terminal(self):
        for label, change in (("is_error", {"is_error": True}), ("subtype", {"subtype": "error_max_turns"}),
                              ("both", {"is_error": True, "subtype": "error_during_execution"})):
            with self.subTest(case=label):
                frames = fixture_frames()
                frames[3].update(change)
                translation = self.translate(frames)
                self.assertEqual(self.types(translation), ["run_started", "assistant", "usage", "error"])
                self.assertEqual(translation.usage, EXPECTED_USAGE)
                self.assertIn("reason", translation.events[-1]["payload"])
        frames = fixture_frames()
        del frames[1]  # success without any assistant message
        translation = self.translate(frames)
        self.assertEqual(self.types(translation)[-1], "error")
        self.assertEqual(translation.events[-1]["payload"]["reason"], "no_assistant_message")

    def test_an_error_result_settles_its_usage_as_an_error(self):
        frames = fixture_frames()
        frames[3]["is_error"] = True
        outcome, usage = self.once(self.write_stream(frames))
        self.assertEqual(outcome["result"]["status"], "ERROR")
        self.assertEqual({d: usage[d]["amount"] for d in DIMENSIONS}, EXPECTED_USAGE)

    # -- failing closed ------------------------------------------------------------ #

    def test_anything_outside_the_dialect_fails_closed(self):
        base = fixture_frames()
        cases = {
            "a user frame": lambda f: f.insert(2, {"type": "user", "message": {"role": "user", "content": []}}),
            "a stream event": lambda f: f.insert(2, {"type": "stream_event", "event": {}}),
            "another system subtype": lambda f: f.insert(2, {"type": "system", "subtype": "compact_boundary"}),
            "a tool-use block without an id": lambda f: f[1]["message"]["content"].append({"type": "tool_use", "name": "Bash", "input": {}}),
            "a frame after the result": lambda f: f.append({"type": "rate_limit_event"}),
            "a frame that is not an object": lambda f: f.insert(2, ["not", "an", "object"]),
            "a type that is not text": lambda f: f.insert(2, {"type": 7}),
            "a second init": lambda f: f.insert(2, dict(f[0])),
            "a result whose is_error is text": lambda f: f[3].update(is_error="false"),
            "a result without num_turns": lambda f: f[3].pop("num_turns"),
            "an assistant message without its model": lambda f: f[1]["message"].pop("model"),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                frames = copy.deepcopy(base)
                mutate(frames)
                translation = self.translate(frames)
                self.assertGreater(translation.unknown, 0)
                self.assertFalse(translation.usage_complete)
                self.assertEqual(translation.usage, {})
                # A usage event after an odd frame never claims a complete usage.
                odd_seen = False
                for event in translation.events:
                    if event["type"] in ("claude.unknown_frame", "claude.invalid_frame"):
                        odd_seen = True
                    elif event["type"] == "usage" and odd_seen:
                        self.assertIs(event["payload"]["complete"], False)
                        self.assertFalse(set(DIMENSIONS) & set(event["payload"]))
                outcome, usage = self.once(self.write_stream(frames, "case.jsonl"))
                # Refused by the reducer or the stream parser (no result at all), or settled as UNKNOWN:
                # either way nothing is recorded.
                status = None if outcome["result"] is None else outcome["result"]["status"]
                self.assertIn(status, (None, "UNKNOWN"))
                self.assertEqual(outcome["usage_recorded"], {})

    def test_illegal_usage_settles_nothing(self):
        for label, change in (("negative", -1), ("float", 2.0), ("bool", True), ("text", "2"), ("missing", None),
                              ("too large", 2 ** 53)):
            with self.subTest(case=label):
                frames = fixture_frames()
                if change is None:
                    del frames[3]["modelUsage"]["claude-opus-5-5"]["cacheReadInputTokens"]
                else:
                    frames[3]["modelUsage"]["claude-opus-5-5"]["inputTokens"] = change
                translation = self.translate(frames)
                self.assertFalse(translation.usage_complete)
                self.assertEqual(translation.usage, {})
                outcome, usage = self.once(self.write_stream(frames, "usage.jsonl"))
                self.assertEqual(outcome["usage_recorded"], {})

    def test_the_init_must_come_first(self):
        frames = fixture_frames()
        frames.insert(0, frames.pop(2))  # the rate-limit event first
        translation = self.translate(frames)
        self.assertGreater(translation.unknown, 0)
        outcome, usage = self.once(self.write_stream(frames))
        self.assertIsNotNone(outcome["result_error"])
        self.assertEqual(outcome["usage_recorded"], {})

    # -- the registry and the other adapters ------------------------------------- #

    def test_the_registry_knows_claude_by_its_exact_id(self):
        self.assertEqual(sorted(run_cli._ADAPTERS), ["claude", "offline", "pi"])
        self.assertIs(run_cli._ADAPTERS["claude"], self.ce.ClaudeStreamAdapter)
        adapter = self.ce.ClaudeStreamAdapter()
        self.assertEqual((adapter.adapter_id, adapter.probe()["dialect"]), ("claude", "claude-stream-json"))
        # Matched exactly: no case or whitespace folding, as for the other two.
        for wrong in ("Claude", "claude ", "claude-code"):
            with self.subTest(adapter_id=wrong):
                job = {"request_id": "r", "task_id": "t", "plan": 1, "idempotency_key": "k", "guard_version": 1,
                       "reservation": {"dispatches": 1}, "resources": [], "adapter_id": wrong, "role": "executor",
                       "profile_id": "p", "command": [PY], "capabilities": [], "cwd": str(self.work),
                       "environment": {}, "allowed_secret_names": [], "timeout_seconds": 1, "usage_dimensions": []}
                path = self.work / "job-wrong.json"
                path.write_text(json.dumps(job), encoding="utf-8")
                with self.assertRaises(run_cli.RunSpecError) as ctx:
                    run_cli.load_job(str(path))
                self.assertEqual(ctx.exception.code, "E_RUN_JOB")

    def test_streams_of_the_other_adapters_do_not_settle_through_claude_and_back(self):
        outcome, _ = self.once(PI_FIXTURE, adapter_id="claude")
        self.assertIsNotNone(outcome["result_error"])
        self.assertEqual(outcome["usage_recorded"], {})
        pi_dimensions = ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens"]
        outcome, _ = self.once(FIXTURES / (FIXTURE + ".jsonl"), adapter_id="pi", dimensions=pi_dimensions)
        self.assertIsNotNone(outcome["result_error"])
        self.assertEqual(outcome["usage_recorded"], {})

    def test_the_adapter_refuses_another_adapters_invocation(self):
        adapter = self.ce.ClaudeStreamAdapter()
        invocation = Invocation("req", "op-1", "executor", "p", ("x",), (), "e" * 64, "pi")
        with self.assertRaises(AdapterError) as ctx:
            adapter.run(invocation, fixture_frames())
        self.assertEqual(ctx.exception.code, "E_ADAPTER_IDENTITY")
        with self.assertRaises(AdapterError) as ctx:
            self.ce.translate(b"{}")
        self.assertEqual(ctx.exception.code, "E_ADAPTER_EVENT")

    def test_importing_the_module_loads_nothing_the_pi_translator_does_not(self):
        probe = ("import sys, importlib; importlib.import_module(sys.argv[1]); "
                 "print(sorted(m for m in sys.modules if m.split('.')[0] in "
                 "('ctypes', 'subprocess', 'socket', 'sqlite3', 'urllib', 'http', 'ssl', 'threading', 'multiprocessing')))")
        loaded = {}
        for module in ("sentinelmesh.pi_events", "sentinelmesh.claude_events"):
            done = subprocess.run([PY, "-c", probe, module], cwd=str(REPO_ROOT), capture_output=True, text=True,
                                  timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            loaded[module] = set(json.loads(done.stdout.strip().replace("'", '"')))
        self.assertLessEqual(loaded["sentinelmesh.claude_events"], loaded["sentinelmesh.pi_events"])


if __name__ == "__main__":
    unittest.main()
