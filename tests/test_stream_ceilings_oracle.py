"""Planner oracle for stream-ceilings (`.agent/design/stream-ceilings.md`).

Planner-provided; executors must not modify this file.

Two ceilings guard the read of a supervised run's stdout stream: the largest
whole stream a caller may ask ``read_stream`` to pull into memory, and the
longest line the protocol's parser accepts.  Measured on the 34 real Step
streams the dispatcher captured, a run at the kit's own 1800 s limit sits about
1.5 times under both (11.3 MB of 16 MiB; a 719 KB ``agent_end`` line of 1 MiB),
and a stream over either leaves the usage unknown with no tool-supported way
out.  The user decided (2026-10-10): the largest stream ceiling is 64 MiB; the
line ceiling becomes a parameter, ``max_line_bytes``, 1 MiB by default (the
protocol's own document limit) and 8 MiB at most; the controller path and
``smctl ledger reference`` use the same constants; the default stream ceiling
stays 1 MiB.

Every stream here is synthetic: Pi-shaped frames with a padding field sized to
the byte, or small frames piled up past the old ceiling.  They prove the rules,
not Pi's behaviour.
"""
from __future__ import annotations

import contextlib
import hashlib
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import adapter_run, cli, core
from sentinelmesh.adapter_protocol import AdapterError, Invocation, OfflineAdapter
from sentinelmesh.adapter_run import (DEFAULT_MAX_LINE_BYTES, DEFAULT_MAX_STREAM_BYTES, MAX_LINE_BYTES,
                                      MAX_STREAM_BYTES, STREAM_NAME, read_stream, run_adapter_operation)
from sentinelmesh.durable_ledger import create_ledger
from sentinelmesh.pi_events import DIALECT, USAGE_KEYS, translate
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.scheduler import Scheduler

REPO_ROOT = Path(__file__).resolve().parents[1]
MIB = 1_048_576
OLD_MAX_STREAM_BYTES = 16 * MIB
LF = bytes([10])
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
CAPS = {"dispatches": 9, "total_tokens": 10 ** 9, "output_tokens": 10 ** 9}
RESERVATION = {"dispatches": 1, "total_tokens": 10 ** 8, "output_tokens": 10 ** 7}
TOKEN_KEYS = tuple(dimension for _vendor, dimension in USAGE_KEYS)
BAD_CEILINGS = (0, -1, True, 1.5, "64", None, 2 ** 40)


def smctl(*argv):
    """Run ``smctl`` in this process: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main([str(a) for a in argv])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


# --- synthetic frames -------------------------------------------------------

def session():
    return {"type": "session", "version": 3, "id": "01a1-session", "timestamp": "2026-10-10T00:00:00.000Z",
            "cwd": "/w"}


def usage(input_tokens=10, output_tokens=5, cache_read=0, cache_write=0):
    return {"input": input_tokens, "output": output_tokens, "cacheRead": cache_read, "cacheWrite": cache_write,
            "totalTokens": input_tokens + output_tokens + cache_read + cache_write}


def assistant(stop="stop", usage_obj=None):
    return {"type": "message_end",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}],
                        "usage": usage() if usage_obj is None else usage_obj, "stopReason": stop,
                        "provider": "step", "model": "step-5-preview", "responseId": "r-1",
                        "thinkingLevel": "high"}}


def settled():
    return {"type": "agent_settled"}


def line_of(frame):
    return json.dumps(frame).encode("utf-8")


def agent_end(line_bytes=None):
    """An ``agent_end`` frame whose serialized line is exactly ``line_bytes`` long.

    Pi's own frame mirrors the whole conversation; here the mirror is one
    padding field, so the line length is chosen to the byte.
    """
    frame = {"type": "agent_end", "willRetry": False, "mirror": ""}
    if line_bytes is None:
        return frame
    base = len(line_of(frame))
    assert line_bytes >= base, line_bytes
    frame["mirror"] = "x" * (line_bytes - base)
    assert len(line_of(frame)) == line_bytes
    return frame


def padding(count, size=1000):
    return [{"type": "message_update", "pad": "x" * size}] * count


def stream_bytes(frames):
    return b"".join(line_of(frame) + LF for frame in frames)


def sums(*usages):
    total = {dimension: 0 for dimension in TOKEN_KEYS}
    for item in usages:
        for vendor, dimension in USAGE_KEYS:
            total[dimension] += item[vendor]
    return total


def long_run(line_bytes):
    """A run whose ``agent_end`` line is ``line_bytes`` long; the usage is the one assistant message's."""
    return [session(), assistant(), agent_end(line_bytes), settled()]


class CeilingCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="stream-ceilings-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.evidence = self.work / "evidence"
        self.evidence.mkdir()

    def write_stream(self, raw, evidence=None):
        target = (self.evidence if evidence is None else evidence) / STREAM_NAME
        target.write_bytes(raw)
        return target

    def grow(self, size):
        """Extend the stream file to exactly ``size`` bytes (zeros after what was written)."""
        with open(self.evidence / STREAM_NAME, "r+b") as handle:
            handle.truncate(size)

    def assert_ok(self, out, raw, count):
        self.assertEqual((out["status"], out["bytes"], out["sha256"], len(out["events"])),
                         ("ok", len(raw), hashlib.sha256(raw).hexdigest(), count))

    def assert_invalid(self, out, raw, line, code="E_JSON_LIMIT"):
        self.assertEqual(out, {"status": "invalid", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                               "events": None, "line": line, "code": code})


# --- the constants and the signatures --------------------------------------

class Constants(CeilingCase):
    def test_the_four_ceilings(self):
        self.assertEqual((DEFAULT_MAX_STREAM_BYTES, MAX_STREAM_BYTES, DEFAULT_MAX_LINE_BYTES, MAX_LINE_BYTES),
                         (MIB, 64 * MIB, MIB, 8 * MIB))
        for value in (DEFAULT_MAX_STREAM_BYTES, MAX_STREAM_BYTES, DEFAULT_MAX_LINE_BYTES, MAX_LINE_BYTES):
            self.assertIs(type(value), int)
        # The default line ceiling is the protocol's own document limit.
        self.assertEqual(DEFAULT_MAX_LINE_BYTES, core.DEFAULT_MAX_JSON_BYTES)
        for name in ("DEFAULT_MAX_LINE_BYTES", "DEFAULT_MAX_STREAM_BYTES", "MAX_LINE_BYTES", "MAX_STREAM_BYTES"):
            self.assertIn(name, adapter_run.__all__)

    def test_the_signatures_carry_the_line_ceiling(self):
        parameters = inspect.signature(read_stream).parameters
        self.assertEqual(list(parameters), ["evidence_dir", "max_bytes", "expected", "max_line_bytes"])
        for name in ("max_bytes", "expected", "max_line_bytes"):
            self.assertIs(parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual((parameters["max_bytes"].default, parameters["expected"].default,
                          parameters["max_line_bytes"].default),
                         (DEFAULT_MAX_STREAM_BYTES, None, DEFAULT_MAX_LINE_BYTES))
        parameters = inspect.signature(run_adapter_operation).parameters
        for name in ("max_stream_bytes", "max_line_bytes"):
            self.assertIs(parameters[name].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual((parameters["max_stream_bytes"].default, parameters["max_line_bytes"].default),
                         (DEFAULT_MAX_STREAM_BYTES, DEFAULT_MAX_LINE_BYTES))

    def test_the_old_ceiling_is_not_written_into_the_sources_any_more(self):
        for relative in ("sentinelmesh/adapter_run.py", "sentinelmesh/ledger_cli.py", "tests/test_adapter_run.py",
                         "tests/test_adapter_run_oracle.py"):
            text = (REPO_ROOT / relative).read_text(encoding="utf-8")
            for literal in ("16 * 1_048_576", "16777216"):
                self.assertFalse(literal in text, "%s still names the old ceiling as %s" % (relative, literal))


# --- the line ceiling -------------------------------------------------------

class LineCeiling(CeilingCase):
    def test_a_line_over_the_protocol_limit_is_invalid_by_default_and_read_with_the_line_ceiling(self):
        raw = stream_bytes(long_run(DEFAULT_MAX_LINE_BYTES + 1))
        self.write_stream(raw)
        # By default the line is one byte over the protocol's limit: the whole
        # stream is invalid at that line, whatever the stream ceiling.
        self.assert_invalid(read_stream(str(self.evidence), max_bytes=MAX_STREAM_BYTES), raw, 3)
        self.assert_invalid(read_stream(str(self.evidence), max_bytes=MAX_STREAM_BYTES,
                                        max_line_bytes=DEFAULT_MAX_LINE_BYTES), raw, 3)
        # With a line ceiling that covers it the same bytes are read, and the
        # frames come back as written.
        out = read_stream(str(self.evidence), max_bytes=MAX_STREAM_BYTES, max_line_bytes=DEFAULT_MAX_LINE_BYTES + 1)
        self.assert_ok(out, raw, 4)
        self.assertEqual(out["events"], long_run(DEFAULT_MAX_LINE_BYTES + 1))
        larger = read_stream(str(self.evidence), max_bytes=MAX_STREAM_BYTES, max_line_bytes=MAX_LINE_BYTES)
        self.assertEqual(larger, out)
        # The translation of the long run is the translation of a short one:
        # the mirror is never copied, the usage is the assistant message's.
        long_translation = translate(out["events"])
        short_translation = translate(long_run(None))
        self.assertEqual((long_translation.usage, long_translation.usage_complete, long_translation.frames),
                         (sums(usage()), True, 4))
        self.assertEqual((long_translation.usage, long_translation.events), (short_translation.usage,
                                                                             short_translation.events))
        for event in long_translation.events:
            self.assertLess(len(json.dumps(event["payload"]).encode("utf-8")), 4096, event["type"])

    def test_the_largest_line_ceiling_is_8_mib_inclusive(self):
        raw = stream_bytes(long_run(MAX_LINE_BYTES))
        self.write_stream(raw)
        out = read_stream(str(self.evidence), max_bytes=MAX_STREAM_BYTES, max_line_bytes=MAX_LINE_BYTES)
        self.assert_ok(out, raw, 4)
        self.assertEqual(translate(out["events"]).usage, sums(usage()))
        raw = stream_bytes(long_run(MAX_LINE_BYTES + 1))
        self.write_stream(raw)
        self.assert_invalid(read_stream(str(self.evidence), max_bytes=MAX_STREAM_BYTES,
                                        max_line_bytes=MAX_LINE_BYTES), raw, 3)

    def test_the_line_ceiling_counts_the_line_without_its_newline(self):
        # A small ceiling is honoured too: the parameter is the rule, not a
        # switch between the two constants.
        size = 2000
        raw = stream_bytes(long_run(size))
        self.write_stream(raw)
        self.assert_ok(read_stream(str(self.evidence), max_line_bytes=size), raw, 4)
        self.assert_invalid(read_stream(str(self.evidence), max_line_bytes=size - 1), raw, 3)
        # The first line over the ceiling names itself, in file order.
        raw = stream_bytes([session(), agent_end(size), assistant(), agent_end(size + 7), settled()])
        self.write_stream(raw)
        self.assert_invalid(read_stream(str(self.evidence), max_line_bytes=size), raw, 4)
        self.assert_invalid(read_stream(str(self.evidence), max_line_bytes=size - 1), raw, 2)

    def test_the_two_ceilings_are_independent(self):
        # A line ceiling above the stream ceiling is legal: the stream ceiling
        # still decides how much is read, the line ceiling how long a line may be.
        raw = stream_bytes(long_run(3000))
        self.write_stream(raw)
        self.assert_ok(read_stream(str(self.evidence), max_bytes=len(raw), max_line_bytes=MAX_LINE_BYTES), raw, 4)
        self.assertEqual(read_stream(str(self.evidence), max_bytes=len(raw) - 1, max_line_bytes=MAX_LINE_BYTES),
                         {"status": "too_large", "sha256": None, "bytes": None, "events": None})
        self.assert_invalid(read_stream(str(self.evidence), max_bytes=len(raw), max_line_bytes=2999), raw, 3)

    def test_the_depth_limit_is_still_the_parsers(self):
        nested = json.loads("[" * 65 + "]" * 65)
        raw = stream_bytes([session(), {"type": "agent_end", "willRetry": False, "deep": nested}, settled()])
        self.write_stream(raw)
        self.assert_invalid(read_stream(str(self.evidence), max_line_bytes=MAX_LINE_BYTES), raw, 2)


# --- the stream ceiling -----------------------------------------------------

class StreamCeiling(CeilingCase):
    def test_a_stream_between_the_old_and_the_new_ceiling_is_read(self):
        frames = [session(), assistant()] + padding(17_000) + [agent_end(None), settled()]
        raw = stream_bytes(frames)
        self.assertGreater(len(raw), OLD_MAX_STREAM_BYTES)
        self.assertLess(len(raw), MAX_STREAM_BYTES)
        self.write_stream(raw)
        out = read_stream(str(self.evidence), max_bytes=MAX_STREAM_BYTES)
        self.assert_ok(out, raw, len(frames))
        self.assertEqual(translate(out["events"]).usage, sums(usage()))
        # The ceiling is still per call: the old value, asked for, refuses the stream.
        self.assertEqual(read_stream(str(self.evidence), max_bytes=OLD_MAX_STREAM_BYTES)["status"], "too_large")
        self.assertEqual(read_stream(str(self.evidence))["status"], "too_large")
        # Exactly the largest ceiling is read in full (the zeros after the frames
        # are one line the parser refuses, at the first line after them)...
        self.grow(MAX_STREAM_BYTES)
        out = read_stream(str(self.evidence), max_bytes=MAX_STREAM_BYTES)
        self.assertEqual((out["status"], out["bytes"], out["line"], out["code"]),
                         ("invalid", MAX_STREAM_BYTES, len(frames) + 1, "E_JSON_LIMIT"))
        # ... and one byte more is not read at all.
        self.grow(MAX_STREAM_BYTES + 1)
        self.assertEqual(read_stream(str(self.evidence), max_bytes=MAX_STREAM_BYTES),
                         {"status": "too_large", "sha256": None, "bytes": None, "events": None})

    def test_changed_still_comes_before_too_large(self):
        raw = stream_bytes(long_run(None))
        target = self.write_stream(raw)
        info = os.stat(target)
        fact = {"device": info.st_dev, "inode": info.st_ino, "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest()}
        self.assert_ok(read_stream(str(self.evidence), max_bytes=MAX_STREAM_BYTES, expected=fact), raw, 4)
        self.grow(MAX_STREAM_BYTES + 1)
        self.assertEqual(read_stream(str(self.evidence), max_bytes=MAX_STREAM_BYTES, expected=fact)["status"],
                         "changed")
        self.assertEqual(read_stream(str(self.evidence), max_bytes=MAX_STREAM_BYTES)["status"], "too_large")

    def test_the_defaults_are_unchanged(self):
        raw = b"x" * (DEFAULT_MAX_STREAM_BYTES + 1)
        self.write_stream(raw)
        self.assertEqual(read_stream(str(self.evidence))["status"], "too_large")
        # Over the default line ceiling, under the stream ceiling asked for: the
        # parser's own code, at line 1.
        self.assert_invalid(read_stream(str(self.evidence), max_bytes=MAX_STREAM_BYTES), raw, 1)
        raw = stream_bytes(long_run(None))
        self.write_stream(raw)
        self.assert_ok(read_stream(str(self.evidence)), raw, 4)


# --- the checks before the file ---------------------------------------------

class Checks(CeilingCase):
    def test_a_bad_line_ceiling_is_refused_before_the_file_is_looked_for(self):
        missing = str(self.work / "no-such-evidence")
        self.assertEqual(read_stream(missing, max_bytes=MAX_STREAM_BYTES, max_line_bytes=MAX_LINE_BYTES)["status"],
                         "missing")  # legal baseline: the file is what is missing
        for value in BAD_CEILINGS + (MAX_LINE_BYTES + 1,):
            with self.subTest(max_line_bytes=repr(value)):
                with self.assertRaises(AdapterError) as raised:
                    read_stream(missing, max_bytes=MAX_STREAM_BYTES, max_line_bytes=value)
                self.assertEqual(raised.exception.code, "E_ADAPTER_SCHEMA")
                self.assertIn("max_line_bytes", str(raised.exception))
        for value in (1, 2, DEFAULT_MAX_LINE_BYTES, MAX_LINE_BYTES):
            with self.subTest(legal=value):
                self.assertEqual(read_stream(missing, max_line_bytes=value)["status"], "missing")

    def test_the_stream_ceiling_is_checked_first_and_expected_last(self):
        missing = str(self.work / "no-such-evidence")
        with self.assertRaises(AdapterError) as raised:
            read_stream(missing, max_bytes=0, max_line_bytes=0, expected="not a fact")
        self.assertIn("max_stream_bytes", str(raised.exception))
        with self.assertRaises(AdapterError) as raised:
            read_stream(missing, max_bytes=MAX_STREAM_BYTES + 1, max_line_bytes=MAX_LINE_BYTES + 1)
        self.assertIn("max_stream_bytes", str(raised.exception))
        with self.assertRaises(AdapterError) as raised:
            read_stream(missing, max_line_bytes=0, expected="not a fact")
        self.assertIn("max_line_bytes", str(raised.exception))
        with self.assertRaises(AdapterError) as raised:
            read_stream(missing, max_line_bytes=MAX_LINE_BYTES, expected="not a fact")
        self.assertIn("expected", str(raised.exception))

    def test_the_run_refuses_a_bad_line_ceiling_before_the_handoff(self):
        scheduler = Scheduler()
        scheduler.seed_request("req-1", "repo-1", {"dispatches": 5, "tokens": 100}, concurrency=4)
        scheduler.seed_task("req-1", "task-1", {"dispatches": 5, "tokens": 100}, max_attempts=3)
        op = scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 40},
                             ["src/a.py"])["operation_id"]
        invocation = Invocation("req-1", op, "executor", "stub", ("python3", "-c", "pass"), (),
                                environment_digest(BASE_ENV))
        before = scheduler.events
        arguments = dict(cwd=str(self.work), timeout_seconds=10, environment=BASE_ENV, owner="owner-1",
                         generation=1, evidence_dir=str(self.evidence), worktree_id="wt-1",
                         candidate_hash=lambda directory: "c" * 64, usage_dimensions=("tokens",),
                         max_stream_bytes=MAX_STREAM_BYTES)
        cases = [({"max_line_bytes": value}, "max_line_bytes") for value in BAD_CEILINGS + (MAX_LINE_BYTES + 1,)]
        # The stream ceiling is checked before the line ceiling; the line ceiling
        # before the runtime specification and before the reservation.
        cases += [({"max_stream_bytes": 0, "max_line_bytes": 0}, "max_stream_bytes"),
                  ({"max_line_bytes": 0, "timeout_seconds": 1.5}, "max_line_bytes"),
                  ({"max_line_bytes": 0, "usage_dimensions": ("unreserved",)}, "max_line_bytes")]
        for extra, named in cases:
            with self.subTest(**{key: repr(value) for key, value in extra.items()}):
                with self.assertRaises(AdapterError) as raised:
                    run_adapter_operation(scheduler, OfflineAdapter(), invocation, **dict(arguments, **extra))
                self.assertEqual(raised.exception.code, "E_ADAPTER_SCHEMA")
                self.assertIn(named, str(raised.exception))
                self.assertEqual(scheduler.events, before, "a refused call changed the Scheduler")
                self.assertEqual(scheduler.operation(op)["state"], "RESERVED")
                self.assertEqual(os.listdir(self.evidence), [], "a refused call wrote evidence")


# --- the operator command ---------------------------------------------------

class ReferenceCommand(CeilingCase):
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

    def reference(self, op):
        code, out, err = smctl("ledger", "reference", self.path, "--operation", op, "--dialect", DIALECT)
        self.assertEqual((code, err), (0, ""), "smctl reference failed")
        return json.loads(out)

    def test_the_command_reads_a_line_over_the_protocol_limit(self):
        raw = stream_bytes(long_run(DEFAULT_MAX_LINE_BYTES + 1))
        self.write_stream(raw)
        op = self.crashed()
        out = self.reference(op)
        self.assertEqual(out["stream"], {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        self.assertEqual((out["reference"]["complete"], out["reference"]["usage"], out["reference"]["frames"]),
                         (True, sums(usage()), 4))
        # The command's line ceiling is the largest one, inclusive, and no more.
        raw = stream_bytes(long_run(MAX_LINE_BYTES))
        self.write_stream(raw)
        self.assertEqual(self.reference(op)["stream"]["status"], "ok")
        raw = stream_bytes(long_run(MAX_LINE_BYTES + 1))
        self.write_stream(raw)
        out = self.reference(op)
        self.assertEqual(out["stream"], {"status": "invalid", "sha256": hashlib.sha256(raw).hexdigest(),
                                         "bytes": len(raw), "line": 3, "code": "E_JSON_LIMIT"})
        self.assertIsNone(out["reference"])

    def test_the_command_reads_a_stream_over_the_old_ceiling(self):
        frames = [session(), assistant()] + padding(17_000) + [agent_end(None), settled()]
        raw = stream_bytes(frames)
        self.assertGreater(len(raw), OLD_MAX_STREAM_BYTES)
        self.write_stream(raw)
        op = self.crashed()
        out = self.reference(op)
        self.assertEqual(out["stream"], {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        self.assertEqual((out["reference"]["complete"], out["reference"]["usage"], out["reference"]["frames"]),
                         (True, sums(usage()), len(frames)))
        self.grow(MAX_STREAM_BYTES + 1)
        out = self.reference(op)
        self.assertEqual((out["stream"], out["reference"]),
                         ({"status": "too_large", "sha256": None, "bytes": None}, None))

    def test_the_entry_point_reads_the_long_line_too(self):
        raw = stream_bytes(long_run(DEFAULT_MAX_LINE_BYTES + 1))
        self.write_stream(raw)
        op = self.crashed()
        done = subprocess.run([sys.executable, "-m", "sentinelmesh.cli", "ledger", "reference", self.path,
                               "--operation", op, "--dialect", DIALECT],
                              cwd=str(REPO_ROOT), capture_output=True, timeout=120)
        self.assertEqual((done.returncode, done.stderr), (0, b""))
        out = json.loads(done.stdout.decode("utf-8"))
        self.assertEqual((out["stream"]["status"], out["stream"]["bytes"], out["reference"]["complete"]),
                         ("ok", len(raw), True))


if __name__ == "__main__":
    unittest.main()
