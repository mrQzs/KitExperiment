"""Planner oracle for ledger-reference (`.agent/design/ledger-reference.md`).

Planner-provided; executors must not modify this file.

After a crash the usage of the run nobody could classify had to be summed by
hand from the vendor's own stream.  The user decided (2026-10-09) that
``smctl ledger reference`` reads that stream for one operation and prints a
*reference* value only: a separate read-only command (never part of ``status``),
the dialect named by hand (``--dialect pi-session-v3`` is the only one), the
sums reported even when the stream is incomplete (marked, with the reasons), and
on a platform without ``O_NOFOLLOW`` the stream read with an equivalent check
(``lstat`` first, binary open, identity compared after the open).

Two kinds of evidence, kept apart:

* **captured** -- the five reduced real streams under ``tests/fixtures/pi_native``
  are copied in as a run's ``stdout.log``; the reference the command prints has
  to agree with the dispatcher's independent summary in each ``.meta.json``;
* **synthetic** -- hand-built frames for the incomplete streams, the stream
  states and the refusals.  They prove the rules, not Pi's behaviour.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh import adapter_run, cli, ledger_cli
from sentinelmesh.adapter_run import DEFAULT_MAX_STREAM_BYTES, MAX_STREAM_BYTES, read_stream
from sentinelmesh.durable_ledger import LedgerError, create_ledger, open_ledger
from sentinelmesh.pi_events import DIALECT, USAGE_KEYS

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pi_native"
CAPS = {"dispatches": 9, "total_tokens": 10 ** 9, "output_tokens": 10 ** 9}
#: Two token dimensions on purpose, in an order that is not the sorted one.
RESERVATION = {"dispatches": 1, "total_tokens": 10 ** 8, "output_tokens": 10 ** 7}
EVIDENCE = "e" * 64
LF, CR, SUB = bytes([10]), bytes([13]), bytes([26])
KEYS = {"command", "operation_id", "request_id", "task_id", "state", "generation", "evidence_dir", "dialect",
        "reservation", "recorded", "missing", "stream", "reference"}
REFERENCE_KEYS = {"complete", "reasons", "terminal", "last_stop_reason", "usage", "assistant_messages",
                  "tool_calls", "tool_errors", "agent_cycles", "frames", "unknown", "models"}
TOKEN_KEYS = tuple(dimension for _vendor, dimension in USAGE_KEYS)


def smctl(*argv):
    """Run ``smctl`` in this process: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main([str(a) for a in argv])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def digest_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fixtures():
    found = []
    for meta_path in sorted(FIXTURES.glob("*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        found.append((meta, FIXTURES / (meta["fixture"] + ".jsonl")))
    return found


# --- synthetic frames -------------------------------------------------------

def session():
    return {"type": "session", "version": 3, "id": "01a1-session", "timestamp": "2026-10-09T00:00:00.000Z",
            "cwd": "/w"}


def usage(input_tokens=10, output_tokens=5, cache_read=0, cache_write=0):
    return {"input": input_tokens, "output": output_tokens, "cacheRead": cache_read, "cacheWrite": cache_write,
            "totalTokens": input_tokens + output_tokens + cache_read + cache_write}


def assistant(stop="stop", usage_obj=None, provider="step", model="step-5-preview", tool_calls=()):
    content = [{"type": "text", "text": "ok"}]
    content += [{"type": "toolCall", "id": call_id, "name": "bash", "arguments": {}} for call_id in tool_calls]
    return {"type": "message_end",
            "message": {"role": "assistant", "content": content, "usage": usage() if usage_obj is None else usage_obj,
                        "stopReason": stop, "provider": provider, "model": model, "responseId": "r-1",
                        "thinkingLevel": "high"}}


def tool_start(call_id="call-1", name="bash"):
    return {"type": "tool_execution_start", "toolCallId": call_id, "toolName": name}


def tool_end(call_id="call-1", name="bash", is_error=False):
    return {"type": "tool_execution_end", "toolCallId": call_id, "toolName": name, "isError": is_error}


def settled():
    return {"type": "agent_settled"}


def agent_end():
    return {"type": "agent_end", "willRetry": False}


def stream_bytes(frames, newline=LF):
    return b"".join(json.dumps(frame).encode("utf-8") + newline for frame in frames)


def sums(*usages):
    total = {dimension: 0 for dimension in TOKEN_KEYS}
    for item in usages:
        for vendor, dimension in USAGE_KEYS:
            total[dimension] += item[vendor]
    return total


class ReferenceCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="ledger-reference-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.path = str(self.work / "ledger.db")
        self.evidence = self.work / "evidence"
        self.evidence.mkdir()

    def descriptor(self, op, evidence_dir):
        return {"spec": {"request_id": "req-1", "run_id": op, "command": ["pi", "--mode", "json"],
                         "cwd": str(self.work), "environment_sha256": "a" * 64, "timeout_seconds": 1800},
                "evidence_dir": evidence_dir, "worktree_id": "wt-1"}

    def crashed(self, evidence_dir=None, reservation=RESERVATION):
        """A ledger as a crash and the restart leave it: a supervised run the
        controller could not classify (START_UNKNOWN, usage unknown) and a
        handoff without a run descriptor."""
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        for task in ("task-1", "task-2"):
            ledger.seed_task("req-1", task, CAPS)
        run = ledger.claim("req-1", "task-1", 1, "idem-task-1", "owner-1", 1, 1, reservation,
                           ["src/task-1.py"])["operation_id"]
        plain = ledger.claim("req-1", "task-2", 1, "idem-task-2", "owner-1", 1, 1, reservation,
                             ["src/task-2.py"])["operation_id"]
        ledger.handoff(run, "owner-1", 1, run=self.descriptor(run, str(self.evidence) if evidence_dir is None
                                                               else evidence_dir))
        ledger.handoff(plain, "owner-1", 1)
        ledger.unknown(run, "owner-1", 1)
        ledger.rollover()
        ledger.close()
        return {"run": run, "plain": plain}

    def write_stream(self, raw, evidence=None):
        # The supervisor's stdout file by its literal name: the command has to
        # read that file, not whatever the module under test calls it.
        target = (self.evidence if evidence is None else evidence) / "stdout.log"
        target.write_bytes(raw)
        return target

    def ok(self, *argv):
        code, out, err = smctl(*argv)
        self.assertEqual((code, err), (0, ""), "smctl %s failed" % (argv,))
        self.assertTrue(out.endswith(LF.decode()) and out.count(LF.decode()) == 1, "one JSON line expected")
        return json.loads(out)

    def refused(self, code_expected, *argv):
        before = digest_of(self.path) if os.path.exists(self.path) else None
        code, out, err = smctl(*argv)
        self.assertEqual((code, out), (3, ""), "smctl %s should be refused" % (argv,))
        error = json.loads(err)
        self.assertEqual(set(error), {"error", "detail"})
        self.assertEqual(error["error"], code_expected)
        if before is not None:
            self.assertEqual(digest_of(self.path), before, "a refused command changed the ledger")
        return error

    def reference(self, op):
        return self.ok("ledger", "reference", self.path, "--operation", op, "--dialect", DIALECT)


# --- captured fixtures ------------------------------------------------------

class FixtureOracle(ReferenceCase):
    def test_each_fixture_gives_the_dispatcher_numbers(self):
        found = fixtures()
        # Five dispatcher captures and the one live run of 2026-10-11 (live-pi-run).
        self.assertEqual(len(found), 6)
        for meta, stream in found:
            with self.subTest(fixture=meta["fixture"]):
                self.setUp()
                raw = stream.read_bytes()
                self.write_stream(raw)
                ops = self.crashed()
                out = self.reference(ops["run"])
                self.assertEqual(set(out), KEYS)
                self.assertEqual((out["command"], out["operation_id"], out["request_id"], out["task_id"],
                                  out["state"], out["generation"], out["evidence_dir"], out["dialect"]),
                                 ("reference", ops["run"], "req-1", "task-1", "START_UNKNOWN", 1,
                                  str(self.evidence), DIALECT))
                self.assertEqual(out["reservation"], RESERVATION)
                # The handoff recorded its own dispatch; nothing else is recorded
                # yet, so both token dimensions are missing, in reservation order.
                self.assertEqual(set(out["recorded"]), {"dispatches"})
                self.assertEqual((out["recorded"]["dispatches"]["amount"], out["recorded"]["dispatches"]["final"]),
                                 (1, True))
                self.assertEqual(out["missing"], ["total_tokens", "output_tokens"])
                self.assertEqual(out["stream"], {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(),
                                                 "bytes": len(raw)})
                summary = meta["dispatcher_summary"]
                reference = out["reference"]
                self.assertEqual(set(reference), REFERENCE_KEYS)
                for vendor, dimension in USAGE_KEYS:
                    self.assertEqual(reference["usage"][dimension], summary["usage"][vendor], dimension)
                self.assertEqual(set(reference["usage"]), set(TOKEN_KEYS))
                self.assertEqual(reference["complete"], summary["complete"])
                self.assertEqual(reference["reasons"], [] if summary["complete"] else ["no_terminal"])
                self.assertEqual(reference["terminal"], summary["terminal_event"])
                self.assertEqual(reference["last_stop_reason"], summary["final_stop_reason"])
                self.assertEqual(reference["assistant_messages"], summary["assistant_messages"])
                self.assertEqual(reference["tool_calls"], sum(summary["tool_calls"].values()))
                self.assertEqual(reference["tool_errors"], summary["tool_errors"])
                self.assertEqual(reference["agent_cycles"], summary["event_counts"]["agent_start"])
                self.assertEqual(reference["frames"], meta["reduction"]["kept_lines"])
                self.assertEqual(reference["unknown"], 0)
                self.assertEqual(reference["models"],
                                 sorted([m["provider"], m["model"]] for m in summary["actual_models"]))


class OperatorWorkflowOracle(ReferenceCase):
    def test_the_operator_settles_what_the_reference_shows(self):
        meta, stream = [item for item in fixtures() if item[0]["fixture"] == "macos-pi104-explicit-holds-a001"][0]
        self.write_stream(stream.read_bytes())
        ops = self.crashed()
        with open_ledger(self.path) as ledger:
            seq_before = ledger.seq
        first = self.reference(ops["run"])
        total = first["reference"]["usage"]["total_tokens"]
        self.assertEqual(total, meta["dispatcher_summary"]["usage"]["totalTokens"])
        # The operator enters the amount by hand, with the stream's digest as
        # the evidence; the reference changes nothing by itself.
        with open_ledger(self.path) as ledger:
            self.assertEqual(ledger.seq, seq_before, "a reference wrote to the ledger")
        settled = self.ok("ledger", "settle", self.path, "--operation", ops["run"], "--dimension", "total_tokens",
                          "--amount", total, "--evidence", first["stream"]["sha256"])
        self.assertEqual(settled["result"], "APPLIED")
        second = self.reference(ops["run"])
        self.assertEqual(second["recorded"]["total_tokens"],
                         {"call_key": "local:%s:total_tokens" % ops["run"], "revision": 1, "amount": total,
                          "final": True, "evidence_hash": first["stream"]["sha256"]})
        self.assertEqual(second["missing"], ["output_tokens"])
        self.assertEqual(second["reference"], first["reference"])
        self.ok("ledger", "settle", self.path, "--operation", ops["run"], "--dimension", "output_tokens",
                "--amount", first["reference"]["usage"]["output_tokens"], "--evidence", first["stream"]["sha256"])
        third = self.reference(ops["run"])
        self.assertEqual(third["missing"], [])
        status = self.ok("ledger", "status", self.path)
        attention = {entry["operation_id"]: entry["reasons"] for entry in status["requests"][0]["attention"]}
        self.assertEqual(attention[ops["run"]], ["start_unknown"])
        with open_ledger(self.path) as ledger:
            self.assertEqual(ledger.seq, seq_before + 2, "only the two settles were recorded")

    def test_the_reference_never_writes(self):
        raw = stream_bytes([session(), assistant(), settled()])
        target = self.write_stream(raw)
        ops = self.crashed()
        before = digest_of(self.path)
        with open_ledger(self.path) as ledger:
            events = ledger.events
        self.assertEqual(self.reference(ops["run"])["reference"]["complete"], True)
        self.assertEqual(digest_of(self.path), before, "a reference wrote to the ledger")
        with open_ledger(self.path) as ledger:
            self.assertEqual(ledger.events, events)
        self.assertEqual((target.read_bytes(), target.stat().st_size), (raw, len(raw)), "the stream was touched")
        target.unlink()
        self.assertEqual(self.reference(ops["run"])["stream"]["status"], "missing")
        self.assertEqual(digest_of(self.path), before, "a reference on a missing stream wrote to the ledger")
        self.assertEqual(sorted(p.name for p in self.evidence.iterdir()), [], "something was written next to the stream")

    def test_a_missing_dimension_is_one_without_a_known_final_amount(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        ops = self.crashed()
        with open_ledger(self.path) as ledger:
            # A non-final amount, then an unknown one: neither is known usage.
            ledger.observe(ops["run"], "total_tokens", "call-1", 1, 5, False, EVIDENCE)
            ledger.observe(ops["run"], "output_tokens", "call-2", 1, None, False, EVIDENCE)
        out = self.reference(ops["run"])
        self.assertEqual(out["missing"], ["total_tokens", "output_tokens"])
        self.assertEqual((out["recorded"]["total_tokens"]["final"], out["recorded"]["output_tokens"]["amount"]),
                         (False, None))
        with open_ledger(self.path) as ledger:
            ledger.observe(ops["run"], "output_tokens", "call-2", 2, 7, True, EVIDENCE)
        self.assertEqual(self.reference(ops["run"])["missing"], ["total_tokens"])


# --- incomplete streams -----------------------------------------------------

class IncompleteOracle(ReferenceCase):
    def run_reference(self, frames):
        self.setUp()  # a fresh ledger and evidence directory per stream
        self.write_stream(stream_bytes(frames))
        ops = self.crashed()
        out = self.reference(ops["run"])
        self.assertEqual(out["stream"]["status"], "ok")
        return out["reference"]

    def test_a_cut_stream_reports_its_sums_as_incomplete(self):
        first, second = usage(10, 5), usage(20, 7, 100)
        reference = self.run_reference([session(), assistant("toolUse", first, tool_calls=("call-1",)),
                                        tool_start(), tool_end(), assistant("stop", second)])
        self.assertEqual((reference["complete"], reference["reasons"], reference["terminal"]),
                         (False, ["no_terminal"], None))
        self.assertEqual(reference["usage"], sums(first, second))
        self.assertEqual((reference["assistant_messages"], reference["tool_calls"], reference["tool_errors"],
                          reference["last_stop_reason"], reference["frames"], reference["unknown"]),
                         (2, 1, 0, "stop", 5, 0))

    def test_an_agent_end_is_not_a_terminal(self):
        reference = self.run_reference([session(), assistant(), agent_end()])
        self.assertEqual((reference["reasons"], reference["terminal"], reference["complete"]),
                         (["no_terminal"], None, False))

    def test_an_illegal_usage_leaves_that_message_out(self):
        legal = usage(10, 5)
        illegal = dict(usage(20, 7))
        illegal["input"] = -1
        reference = self.run_reference([session(), assistant("toolUse", legal), assistant("stop", illegal), settled()])
        self.assertEqual((reference["complete"], reference["reasons"], reference["terminal"]),
                         (False, ["illegal_usage"], "agent_settled"))
        self.assertEqual(reference["usage"], sums(legal))
        self.assertEqual((reference["assistant_messages"], reference["last_stop_reason"]), (2, "stop"))

    def test_an_unknown_frame_is_a_reason_of_its_own(self):
        reference = self.run_reference([session(), assistant(), {"type": "compaction", "summary": "x"}, settled()])
        self.assertEqual((reference["complete"], reference["reasons"], reference["unknown"], reference["terminal"]),
                         (False, ["unknown_frames"], 1, "agent_settled"))
        self.assertEqual(reference["usage"], sums(usage()))

    def test_the_reasons_keep_their_order(self):
        illegal = dict(usage())
        illegal["output"] = "7"
        reference = self.run_reference([session(), assistant("toolUse", illegal), {"type": "queue"}])
        self.assertEqual(reference["reasons"], ["no_terminal", "illegal_usage", "unknown_frames"])
        self.assertEqual(reference["usage"], sums())

    def test_tool_calls_count_starts_and_errors_count_ends(self):
        reference = self.run_reference([session(), assistant("toolUse", tool_calls=("call-1",)), tool_start()])
        self.assertEqual((reference["tool_calls"], reference["tool_errors"], reference["reasons"]),
                         (1, 0, ["no_terminal"]))
        reference = self.run_reference([session(), assistant("toolUse", tool_calls=("call-1",)), tool_start(),
                                        tool_end(is_error=True), assistant(), settled()])
        self.assertEqual((reference["tool_calls"], reference["tool_errors"], reference["complete"]), (1, 1, True))

    def test_a_non_stop_terminal_is_complete_for_the_usage(self):
        reference = self.run_reference([session(), assistant("length"), settled()])
        self.assertEqual((reference["complete"], reference["terminal"], reference["last_stop_reason"]),
                         (True, "agent_settled", "length"))

    def test_models_are_sorted_lists(self):
        reference = self.run_reference([session(), assistant(provider="step"), assistant(provider="bb"),
                                        assistant(provider="step"), settled()])
        self.assertEqual(reference["models"], [["bb", "step-5-preview"], ["step", "step-5-preview"]])
        self.assertEqual(reference["agent_cycles"], 0)


# --- the stream on disk -----------------------------------------------------

class StreamStateOracle(ReferenceCase):
    def test_a_missing_stream_is_an_answer(self):
        ops = self.crashed()
        out = self.reference(ops["run"])
        self.assertEqual((out["stream"], out["reference"]), ({"status": "missing", "sha256": None, "bytes": None}, None))
        shutil.rmtree(self.evidence)
        out = self.reference(ops["run"])
        self.assertEqual((out["stream"]["status"], out["reference"]), ("missing", None))

    def test_a_stream_that_is_not_a_regular_file_is_unreadable(self):
        ops = self.crashed()
        (self.evidence / "stdout.log").mkdir()
        out = self.reference(ops["run"])
        self.assertEqual((out["stream"], out["reference"]),
                         ({"status": "unreadable", "sha256": None, "bytes": None}, None))

    def test_a_symlinked_stream_is_unreadable(self):
        raw = stream_bytes([session(), assistant(), settled()])
        target = self.work / "elsewhere.jsonl"
        target.write_bytes(raw)
        self.write_stream(raw)
        ops = self.crashed()
        self.assertEqual(self.reference(ops["run"])["stream"]["status"], "ok")  # legal baseline
        (self.evidence / "stdout.log").unlink()
        try:
            os.symlink(target, self.evidence / "stdout.log")
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symbolic links cannot be created here: %s" % exc)
        out = self.reference(ops["run"])
        self.assertEqual((out["stream"], out["reference"]),
                         ({"status": "unreadable", "sha256": None, "bytes": None}, None))

    def test_a_hard_linked_stream_is_read(self):
        raw = stream_bytes([session(), assistant(), settled()])
        target = self.work / "elsewhere.jsonl"
        target.write_bytes(raw)
        try:
            os.link(target, self.evidence / "stdout.log")
        except (OSError, NotImplementedError) as exc:
            self.skipTest("hard links cannot be created here: %s" % exc)
        ops = self.crashed()
        out = self.reference(ops["run"])
        self.assertEqual(out["stream"], {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        self.assertEqual(out["reference"]["complete"], True)

    def test_the_ceiling_is_the_largest_one(self):
        # Over the default ceiling, under the largest: still read.
        padding = [{"type": "message_update", "pad": "x" * 1000}] * 1200
        raw = stream_bytes([session(), assistant()] + padding + [settled()])
        self.assertGreater(len(raw), DEFAULT_MAX_STREAM_BYTES)
        self.write_stream(raw)
        ops = self.crashed()
        out = self.reference(ops["run"])
        self.assertEqual(out["stream"], {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        self.assertEqual((out["reference"]["complete"], out["reference"]["frames"]), (True, 1203))
        # One byte over the largest ceiling: too large, and not read.
        with open(self.evidence / "stdout.log", "wb") as handle:
            handle.truncate(MAX_STREAM_BYTES + 1)
        out = self.reference(ops["run"])
        self.assertEqual((out["stream"], out["reference"]),
                         ({"status": "too_large", "sha256": None, "bytes": None}, None))

    def test_an_invalid_line_names_itself(self):
        raw = stream_bytes([session(), assistant()]) + b"not json" + LF + stream_bytes([settled()])
        self.write_stream(raw)
        ops = self.crashed()
        out = self.reference(ops["run"])
        self.assertEqual(out["stream"], {"status": "invalid", "sha256": hashlib.sha256(raw).hexdigest(),
                                         "bytes": len(raw), "line": 3, "code": "E_JSON_SYNTAX"})
        self.assertIsNone(out["reference"])

    def test_a_non_object_line_is_invalid_before_any_translation(self):
        raw = stream_bytes([session()]) + b"[1]" + LF + stream_bytes([assistant(), settled()])
        self.write_stream(raw)
        ops = self.crashed()
        out = self.reference(ops["run"])
        self.assertEqual((out["stream"]["status"], out["stream"]["line"], out["stream"]["code"], out["reference"]),
                         ("invalid", 2, "E_ADAPTER_STREAM", None))

    def test_the_bytes_are_read_verbatim(self):
        # CR LF endings: the digest is of the bytes on disk, CR included.
        raw = stream_bytes([session(), assistant(), settled()], newline=CR + LF)
        self.write_stream(raw)
        ops = self.crashed()
        out = self.reference(ops["run"])
        self.assertEqual(out["stream"], {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        self.assertEqual(out["reference"]["complete"], True)
        # A 0x1A byte is where a text-mode read would stop; the stream is read
        # to its end, so that byte is the invalid line it is.
        raw = stream_bytes([session(), assistant(), settled()], newline=CR + LF) + SUB + LF
        self.write_stream(raw)
        out = self.reference(ops["run"])
        self.assertEqual(out["stream"], {"status": "invalid", "sha256": hashlib.sha256(raw).hexdigest(),
                                         "bytes": len(raw), "line": 4, "code": "E_JSON_SYNTAX"})


@unittest.skipIf(hasattr(os, "O_NOFOLLOW"), "the lstat/fstat identity check replaces O_NOFOLLOW only where it is missing")
class WithoutNofollowOracle(ReferenceCase):
    """The platform branch of ``read_stream`` without ``O_NOFOLLOW`` (Windows)."""

    def test_a_link_planted_after_the_lstat_is_refused(self):
        raw = stream_bytes([session(), assistant(), settled()])
        target = self.work / "elsewhere.jsonl"
        target.write_bytes(raw)
        path = self.write_stream(raw)
        before = os.lstat(path)
        self.assertEqual(read_stream(str(self.evidence))["status"], "ok")  # legal baseline
        path.unlink()
        try:
            os.symlink(target, path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symbolic links cannot be created here: %s" % exc)
        # ``lstat`` saw a regular file; by the time of the open a link is at
        # the path and the open follows it: what was opened is another file.
        with mock.patch.object(adapter_run.os, "lstat", return_value=before):
            outcome = read_stream(str(self.evidence))
        self.assertEqual(outcome, {"status": "unreadable", "sha256": None, "bytes": None, "events": None})

    def test_a_reparse_point_is_refused(self):
        raw = stream_bytes([session(), assistant(), settled()])
        path = self.write_stream(raw)
        real = os.lstat(path)
        self.assertEqual(read_stream(str(self.evidence))["status"], "ok")  # legal baseline
        fake = types.SimpleNamespace(st_mode=real.st_mode, st_dev=real.st_dev, st_ino=real.st_ino,
                                     st_file_attributes=real.st_file_attributes | stat.FILE_ATTRIBUTE_REPARSE_POINT)
        with mock.patch.object(adapter_run.os, "lstat", return_value=fake):
            outcome = read_stream(str(self.evidence))
        self.assertEqual(outcome, {"status": "unreadable", "sha256": None, "bytes": None, "events": None})

    def test_an_open_that_fails_after_the_lstat_maps_like_the_posix_branch(self):
        """Planner decision R10 (review of attempt-001, F1): the open stage reports a
        file that vanished after the ``lstat`` as ``missing`` and any other failure
        to open as ``unreadable``, exactly as the POSIX branch reports ``os.open``."""
        raw = stream_bytes([session(), assistant(), settled()])
        self.write_stream(raw)
        self.assertEqual(read_stream(str(self.evidence))["status"], "ok")  # legal baseline
        with mock.patch.object(adapter_run.os, "open", side_effect=FileNotFoundError(2, "gone between the calls")):
            self.assertEqual(read_stream(str(self.evidence)),
                             {"status": "missing", "sha256": None, "bytes": None, "events": None})
        with mock.patch.object(adapter_run.os, "open", side_effect=PermissionError(13, "denied")):
            self.assertEqual(read_stream(str(self.evidence)),
                             {"status": "unreadable", "sha256": None, "bytes": None, "events": None})
        self.assertEqual(read_stream(str(self.evidence))["status"], "ok")

    def test_an_identity_that_is_not_the_lstat_one_is_refused(self):
        raw = stream_bytes([session(), assistant(), settled()])
        path = self.write_stream(raw)
        real = os.lstat(path)
        fake = types.SimpleNamespace(st_mode=real.st_mode, st_dev=real.st_dev, st_ino=real.st_ino + 1,
                                     st_file_attributes=real.st_file_attributes)
        with mock.patch.object(adapter_run.os, "lstat", return_value=fake):
            outcome = read_stream(str(self.evidence))
        self.assertEqual(outcome["status"], "unreadable")
        self.assertEqual(read_stream(str(self.evidence))["status"], "ok")


# --- refusals and the command's own rules -----------------------------------

class RefusalOracle(ReferenceCase):
    def test_refusals_are_exit_3_with_the_ledger_error(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        ops = self.crashed()
        self.assertEqual(self.reference(ops["run"])["reference"]["complete"], True)  # legal baseline
        self.refused("E_OPERATION_UNKNOWN", "ledger", "reference", self.path, "--operation", "op-nobody",
                     "--dialect", DIALECT)
        error = self.refused("E_NOT_SUPERVISED", "ledger", "reference", self.path, "--operation", ops["plain"],
                             "--dialect", DIALECT)
        self.assertIsInstance(error["detail"], str)
        missing = str(self.work / "missing.db")
        self.refused("E_LEDGER_MISSING", "ledger", "reference", missing, "--operation", ops["run"],
                     "--dialect", DIALECT)
        self.assertFalse(os.path.exists(missing), "a refused command created a ledger")

    def test_the_dialect_and_the_operation_are_named_by_hand(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        ops = self.crashed()
        for argv in (("ledger", "reference", self.path, "--operation", ops["run"]),
                     ("ledger", "reference", self.path, "--operation", ops["run"], "--dialect", "adapter-protocol"),
                     ("ledger", "reference", self.path, "--operation", ops["run"], "--dialect", ""),
                     ("ledger", "reference", self.path, "--dialect", DIALECT),
                     ("ledger", "reference", "--operation", ops["run"], "--dialect", DIALECT)):
            with self.subTest(argv=argv[2:]):
                code, out, err = smctl(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("usage", err)

    def test_the_evidence_dir_is_used_as_recorded(self):
        relative = self.work / "evidence-rel"
        relative.mkdir()
        raw = stream_bytes([session(), assistant(), settled()])
        self.write_stream(raw, relative)
        ops = self.crashed(evidence_dir="evidence-rel")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.work)
        out = self.reference(ops["run"])
        self.assertEqual(out["evidence_dir"], "evidence-rel")
        self.assertEqual(out["stream"], {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
        os.chdir(self.evidence)
        self.assertEqual(self.reference(ops["run"])["stream"]["status"], "missing")

    def test_every_instance_a_command_opens_is_closed(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        ops = self.crashed()
        opened = []
        real = ledger_cli.open_ledger

        def tracking(path):
            ledger = real(path)
            opened.append(ledger)
            return ledger

        with mock.patch.object(ledger_cli, "open_ledger", tracking):
            self.reference(ops["run"])
            self.refused("E_NOT_SUPERVISED", "ledger", "reference", self.path, "--operation", ops["plain"],
                         "--dialect", DIALECT)
            self.refused("E_OPERATION_UNKNOWN", "ledger", "reference", self.path, "--operation", "op-nobody",
                         "--dialect", DIALECT)
            (self.evidence / "stdout.log").unlink()
            self.assertEqual(self.reference(ops["run"])["stream"]["status"], "missing")
        self.assertEqual(len(opened), 4)
        for ledger in opened:
            with self.assertRaises(LedgerError) as ctx:
                ledger.request_ids()
            self.assertEqual(ctx.exception.code, "E_LEDGER_UNAVAILABLE", "a command left its ledger open")


class EntryPointOracle(ReferenceCase):
    def test_the_installed_entry_point_runs_the_reference(self):
        meta, stream = fixtures()[0]
        raw = stream.read_bytes()
        self.write_stream(raw)
        ops = self.crashed()
        done = subprocess.run([sys.executable, "-m", "sentinelmesh.cli", "ledger", "reference", self.path,
                               "--operation", ops["run"], "--dialect", DIALECT],
                              cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        out = json.loads(done.stdout)
        self.assertEqual((out["stream"]["sha256"], out["reference"]["usage"]["total_tokens"]),
                         (hashlib.sha256(raw).hexdigest(), meta["dispatcher_summary"]["usage"]["totalTokens"]))
        refused = subprocess.run([sys.executable, "-m", "sentinelmesh.cli", "ledger", "reference", self.path,
                                  "--operation", ops["plain"], "--dialect", DIALECT],
                                 cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual((refused.returncode, refused.stdout), (3, ""))
        self.assertEqual(json.loads(refused.stderr)["error"], "E_NOT_SUPERVISED")


if __name__ == "__main__":
    unittest.main()
