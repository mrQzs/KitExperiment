"""Executor tests for the two stream ceilings (`.agent/design/stream-ceilings.md`).

The planner oracle pins the constants, the signatures, the stream ceiling, the
protocol's own per-line limit and the order of the checks.  What is left here is
what the change is actually for, measured on synthetic streams in a temporary
directory: the line ceiling is a parameter with a real boundary of its own, the
two ceilings do not bound each other, a bad line ceiling is refused before the
file is looked for, a supervised run is refused before the handoff, and
``smctl ledger reference`` reads the same file with the same ceilings the
controller path uses.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import cli
from sentinelmesh.adapter_protocol import AdapterError, Invocation, OfflineAdapter
from sentinelmesh.adapter_run import (DEFAULT_MAX_LINE_BYTES, MAX_LINE_BYTES, MAX_STREAM_BYTES, STREAM_NAME,
                                      read_stream, run_adapter_operation)
from sentinelmesh.durable_ledger import create_ledger
from sentinelmesh.pi_events import DIALECT
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.scheduler import Scheduler

LF = bytes([10])
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
# One assistant message, as the dialect counts it.
ONE_MESSAGE_USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
                     "cache_write_tokens": 0, "total_tokens": 15}
# (request, task) caps and one run's reservation, as a crashed ledger holds them.
CAPS = {"dispatches": 9, "total_tokens": 10 ** 9, "output_tokens": 10 ** 9}
RESERVATION = {"dispatches": 1, "total_tokens": 10 ** 8, "output_tokens": 10 ** 7}


def smctl(*argv):
    """Run ``smctl`` in this process: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main([str(a) for a in argv])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


# --- synthetic Pi-shaped frames --------------------------------------------

def session():
    return {"type": "session", "version": 3, "id": "01a1-session", "timestamp": "2026-10-10T00:00:00.000Z",
            "cwd": "/w"}


def usage():
    return {"input": 10, "output": 5, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 15}


def assistant():
    return {"type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
                        "usage": usage(), "stopReason": "stop", "provider": "step",
                        "model": "step-5-preview", "responseId": "r-1", "thinkingLevel": "high"}}


def agent_end(line_bytes=None):
    """An ``agent_end`` frame whose serialized line is exactly ``line_bytes`` long."""
    frame = {"type": "agent_end", "willRetry": False, "mirror": ""}
    if line_bytes is None:
        return frame
    base = len(json.dumps(frame).encode("utf-8"))
    assert line_bytes >= base, line_bytes
    frame["mirror"] = "x" * (line_bytes - base)
    assert len(json.dumps(frame).encode("utf-8")) == line_bytes
    return frame


def line_of(frame):
    return json.dumps(frame).encode("utf-8")


def stream_bytes(frames):
    return b"".join(line_of(frame) + LF for frame in frames)


class StreamCeilings(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="stream-ceilings-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.evidence = self.work / "evidence"
        self.evidence.mkdir()

    def write_stream(self, raw):
        target = self.evidence / STREAM_NAME
        target.write_bytes(raw)
        return target

    def grow(self, size):
        """Extend the stream file to exactly ``size`` bytes (zeros after what was written)."""
        with open(self.evidence / STREAM_NAME, "r+b") as handle:
            handle.truncate(size)


# --- the line ceiling is a parameter with a boundary of its own -------------

class LineCeiling(StreamCeilings):
    SIZE = 1500

    def long_run(self, line_bytes=None):
        return [session(), assistant(), agent_end(line_bytes), {"type": "agent_settled"}]

    def test_a_line_is_read_up_to_the_ceiling_asked_for(self):
        raw = stream_bytes(self.long_run(self.SIZE))
        self.write_stream(raw)
        # A legal baseline on the same bytes: exactly the line's length is read,
        # and one byte less is the parser's own limit, at that line's number.
        out = read_stream(str(self.evidence), max_line_bytes=self.SIZE)
        self.assertEqual((out["status"], out["bytes"], out["sha256"], len(out["events"])),
                         ("ok", len(raw), hashlib.sha256(raw).hexdigest(), 4))
        out = read_stream(str(self.evidence), max_line_bytes=self.SIZE - 1)
        self.assertEqual(out, {"status": "invalid", "sha256": hashlib.sha256(raw).hexdigest(),
                              "bytes": len(raw), "events": None, "line": 3, "code": "E_JSON_LIMIT"})

    def test_the_line_over_the_ceiling_names_itself_in_file_order(self):
        raw = stream_bytes([session(), agent_end(self.SIZE), assistant(), agent_end(self.SIZE + 7),
                            {"type": "agent_settled"}])
        self.write_stream(raw)
        out = read_stream(str(self.evidence), max_line_bytes=self.SIZE)
        self.assertEqual((out["status"], out["line"], out["code"]), ("invalid", 4, "E_JSON_LIMIT"))
        # The first line over the smaller ceiling is the second one, not the fourth.
        out = read_stream(str(self.evidence), max_line_bytes=self.SIZE - 1)
        self.assertEqual((out["status"], out["line"], out["code"]), ("invalid", 2, "E_JSON_LIMIT"))

    def test_the_two_ceilings_do_not_bound_each_other(self):
        raw = stream_bytes(self.long_run(3000))
        self.write_stream(raw)
        # A line ceiling above the stream ceiling is legal, and the stream
        # ceiling still decides how much of the file is read.
        out = read_stream(str(self.evidence), max_bytes=len(raw), max_line_bytes=MAX_LINE_BYTES)
        self.assertEqual((out["status"], out["bytes"]), ("ok", len(raw)))
        self.assertEqual(read_stream(str(self.evidence), max_bytes=len(raw) - 1,
                                     max_line_bytes=MAX_LINE_BYTES),
                         {"status": "too_large", "sha256": None, "bytes": None, "events": None})


# --- the checks before the file ---------------------------------------------

class BadLineCeiling(StreamCeilings):
    def test_a_bad_line_ceiling_is_refused_before_the_file_is_looked_for(self):
        missing = str(self.work / "no-such-evidence")
        # Legal baselines on the same directory: the file is what is missing.
        for value in (1, DEFAULT_MAX_LINE_BYTES, MAX_LINE_BYTES):
            self.assertEqual(read_stream(missing, max_line_bytes=value)["status"], "missing")
        for value in (0, -1, True, 1.5, "8", MAX_LINE_BYTES + 1):
            with self.subTest(max_line_bytes=repr(value)):
                with self.assertRaises(AdapterError) as raised:
                    read_stream(missing, max_line_bytes=value)
                self.assertEqual(raised.exception.code, "E_ADAPTER_SCHEMA")
                self.assertIn("max_line_bytes", str(raised.exception))

    def test_the_stream_ceiling_is_still_checked_first(self):
        missing = str(self.work / "no-such-evidence")
        with self.assertRaises(AdapterError) as raised:
            read_stream(missing, max_bytes=0, max_line_bytes=0)
        self.assertEqual(raised.exception.code, "E_ADAPTER_SCHEMA")
        self.assertIn("max_stream_bytes", str(raised.exception))


# --- the supervised run -----------------------------------------------------

class RunRefusal(StreamCeilings):
    def arguments(self):
        scheduler = Scheduler()
        scheduler.seed_request("req-1", "repo-1", {"dispatches": 5, "tokens": 100}, concurrency=4)
        scheduler.seed_task("req-1", "task-1", {"dispatches": 5, "tokens": 100}, max_attempts=3)
        operation_id = scheduler.claim(
            "req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 40},
            ["src/a.py"])["operation_id"]
        invocation = Invocation("req-1", operation_id, "executor", "stub", ("python3", "-c", "pass"), (),
                                environment_digest(BASE_ENV))
        arguments = dict(cwd=str(self.work), timeout_seconds=10, environment=BASE_ENV, owner="owner-1",
                         generation=1, evidence_dir=str(self.evidence), worktree_id="wt-1",
                         candidate_hash=lambda directory: "c" * 64, usage_dimensions=("tokens",),
                         max_stream_bytes=MAX_STREAM_BYTES)
        return scheduler, operation_id, invocation, arguments

    def test_a_bad_line_ceiling_is_refused_before_the_handoff(self):
        scheduler, operation_id, invocation, arguments = self.arguments()
        before = scheduler.events
        with self.assertRaises(AdapterError) as raised:
            run_adapter_operation(scheduler, OfflineAdapter(), invocation, **dict(arguments, max_line_bytes=0))
        self.assertEqual(raised.exception.code, "E_ADAPTER_SCHEMA")
        self.assertIn("max_line_bytes", str(raised.exception))
        self.assertEqual(scheduler.events, before, "a refused call changed the Scheduler")
        self.assertEqual(scheduler.operation(operation_id)["state"], "RESERVED")
        self.assertEqual(os.listdir(self.evidence), [], "a refused call wrote evidence")

    def test_the_stream_ceiling_is_still_refused_first(self):
        scheduler, operation_id, invocation, arguments = self.arguments()
        before = scheduler.events
        with self.assertRaises(AdapterError) as raised:
            run_adapter_operation(scheduler, OfflineAdapter(), invocation,
                                  **dict(arguments, max_stream_bytes=0, max_line_bytes=0))
        self.assertEqual(raised.exception.code, "E_ADAPTER_SCHEMA")
        self.assertIn("max_stream_bytes", str(raised.exception))
        self.assertEqual(scheduler.events, before, "a refused call changed the Scheduler")
        self.assertEqual(scheduler.operation(operation_id)["state"], "RESERVED")
        self.assertEqual(os.listdir(self.evidence), [], "a refused call wrote evidence")


# --- the operator command ---------------------------------------------------

class ReferenceCommand(StreamCeilings):
    def setUp(self):
        super().setUp()
        self.path = str(self.work / "ledger.db")

    def crashed(self):
        """A ledger as a crash and the restart leave it: one supervised run the
        controller could not classify, its descriptor naming this evidence directory."""
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        ledger.seed_task("req-1", "task-1", CAPS)
        run = ledger.claim("req-1", "task-1", 1, "idem-task-1", "owner-1", 1, 1, RESERVATION,
                           ["src/task-1.py"])["operation_id"]
        descriptor = {"spec": {"request_id": "req-1", "run_id": run, "command": ["pi", "--mode", "json"],
                               "cwd": str(self.work), "environment_sha256": "a" * 64, "timeout_seconds": 1800},
                      "evidence_dir": str(self.evidence), "worktree_id": "wt-1"}
        ledger.handoff(run, "owner-1", 1, run=descriptor)
        ledger.unknown(run, "owner-1", 1)
        ledger.rollover()
        ledger.close()
        return run

    def reference(self, operation_id):
        code, out, err = smctl("ledger", "reference", self.path, "--operation", operation_id,
                               "--dialect", DIALECT)
        self.assertEqual((code, err), (0, ""), "smctl reference failed")
        return json.loads(out)

    def test_the_command_reads_a_line_over_the_protocol_limit(self):
        raw = stream_bytes([session(), assistant(), agent_end(DEFAULT_MAX_LINE_BYTES + 1),
                            {"type": "agent_settled"}])
        self.write_stream(raw)
        operation_id = self.crashed()
        out = self.reference(operation_id)
        self.assertEqual(out["stream"], {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(),
                                         "bytes": len(raw)})
        self.assertTrue(out["reference"]["complete"])
        self.assertEqual(out["reference"]["usage"], ONE_MESSAGE_USAGE)

    def test_the_command_refuses_a_stream_over_the_largest_ceiling(self):
        raw = stream_bytes([session(), assistant(), agent_end(DEFAULT_MAX_LINE_BYTES + 1),
                            {"type": "agent_settled"}])
        self.write_stream(raw)
        operation_id = self.crashed()
        self.assertEqual(self.reference(operation_id)["stream"]["status"], "ok")
        # Grown past the stream ceiling the command shares with the controller
        # path, the same file is ``too_large`` and there is no reference value.
        self.grow(MAX_STREAM_BYTES + 1)
        out = self.reference(operation_id)
        self.assertEqual(out["stream"], {"status": "too_large", "sha256": None, "bytes": None})
        self.assertIsNone(out["reference"])


if __name__ == "__main__":
    unittest.main()
