"""Executor tests for ledger-reference: ``smctl ledger reference``.

These cover what the planner oracle (``tests/test_ledger_reference_oracle.py``)
leaves out, following one rule throughout: every case that refuses something
first proves a legal baseline -- the very same call with nothing but the value
under test changed -- and then asserts the specific answer, so a different rule
refusing the call first cannot hide a gap.  Every ledger is built with
``create_ledger`` in a temporary directory, every stream is written as bytes into
a temporary evidence directory, and nothing is read from ``.agent/runs``.

The behaviour under test comes from `.agent/design/ledger-reference.md` sections
3, 5 and 6: a separate read-only command that reads the stream of one supervised
run, names the dialect by hand, reports the sums of an incomplete stream with the
reasons it is incomplete, never writes the ledger, and reads the stream through an
equivalent check on a platform without ``O_NOFOLLOW``.
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
from sentinelmesh.pi_events import DIALECT, USAGE_KEYS, translate

REPO_ROOT = Path(__file__).resolve().parents[1]
LF, CR, SUB = bytes([10]), bytes([13]), bytes([26])
CAPS = {"dispatches": 9, "total_tokens": 10 ** 9, "cache_read_tokens": 10 ** 9, "output_tokens": 10 ** 9}
#: Three token dimensions on purpose, in an order that is not the sorted one.
RESERVATION = {"dispatches": 1, "total_tokens": 10 ** 8, "cache_read_tokens": 10 ** 7, "output_tokens": 10 ** 6}
EVIDENCE, CANDIDATE = "e" * 64, "d" * 64
OPERATOR, NOTE = "ops-1", "checked the process table and the worktree"
KEYS = {"command", "operation_id", "request_id", "task_id", "state", "generation", "evidence_dir", "dialect",
        "reservation", "recorded", "missing", "stream", "reference"}
REFERENCE_KEYS = {"complete", "reasons", "terminal", "last_stop_reason", "usage", "assistant_messages",
                  "tool_calls", "tool_errors", "agent_cycles", "frames", "unknown", "models"}
UNREADABLE = {"status": "unreadable", "sha256": None, "bytes": None, "events": None}


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


def agent_start():
    return {"type": "agent_start"}


def tool_start(call_id="call-1", name="bash"):
    return {"type": "tool_execution_start", "toolCallId": call_id, "toolName": name}


def tool_end(call_id="call-1", name="bash", is_error=False):
    return {"type": "tool_execution_end", "toolCallId": call_id, "toolName": name, "isError": is_error}


def settled():
    return {"type": "agent_settled"}


def stream_bytes(frames, newline=LF):
    return b"".join(json.dumps(frame).encode("utf-8") + newline for frame in frames)


def sums(*usages):
    """The five dimensions summed over the legal assistant messages given."""
    total = {dimension: 0 for _vendor, dimension in USAGE_KEYS}
    for item in usages:
        for vendor, dimension in USAGE_KEYS:
            total[dimension] += item[vendor]
    return total


class ReferenceCase(unittest.TestCase):
    """A ledger in a temporary directory, an evidence directory, and the CLI."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="ledger-reference-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.path = str(self.work / "ledger.db")
        self.evidence = self.work / "evidence"
        self.evidence.mkdir()

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
        """A command that answers: one JSON line on stdout, nothing else."""
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, err), (0, ""), "smctl %s failed" % (argv,))
        self.assertTrue(out.endswith("\n") and out.count("\n") == 1, "one JSON line expected")
        return json.loads(out)

    def refused(self, expected, *argv):
        """A command the ledger refuses: exit 3, one JSON error on stderr."""
        before = self.digest()
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, out), (3, ""), "smctl %s should be refused" % (argv,))
        error = json.loads(err)
        self.assertEqual(set(error), {"error", "detail"})
        self.assertEqual(error["error"], expected)
        self.assertEqual(self.digest(), before, "a refused command changed the ledger")
        return error

    def digest(self):
        return hashlib.sha256(Path(self.path).read_bytes()).hexdigest() if os.path.exists(self.path) else None

    def reference(self, operation, dialect=DIALECT):
        return self.ok("ledger", "reference", self.path, "--operation", operation, "--dialect", dialect)

    # -- reading the ledger back ------------------------------------------- #
    def seq(self):
        with open_ledger(self.path) as ledger:
            return ledger.seq

    def events(self):
        with open_ledger(self.path) as ledger:
            return ledger.events

    def view(self, operation):
        with open_ledger(self.path) as ledger:
            return ledger.operation(operation)

    # -- building ledgers -------------------------------------------------- #
    def descriptor(self, operation, evidence_dir):
        return {"spec": {"request_id": "req-1", "run_id": operation, "command": ["pi", "--mode", "json"],
                         "cwd": str(self.work), "environment_sha256": "a" * 64, "timeout_seconds": 1800},
                "evidence_dir": evidence_dir, "worktree_id": "wt-1"}

    def build(self, *, evidence_dir=None, reservation=RESERVATION, handoffs=True):
        """One request, two tasks, two claimed operations, the ledger left open.

        The first operation is handed off with a run descriptor pointing at the
        evidence directory; the second is handed off without one, or left
        ``RESERVED`` when ``handoffs`` is false.
        """
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        for task in ("task-1", "task-2"):
            ledger.seed_task("req-1", task, CAPS)
        operations = {}
        for name, task in (("run", "task-1"), ("plain", "task-2")):
            operations[name] = ledger.claim("req-1", task, 1, "idem-" + task, "owner-1", 1, 1, reservation,
                                            ["src/%s.py" % task])["operation_id"]
        ledger.handoff(operations["run"], "owner-1", 1,
                       run=self.descriptor(operations["run"],
                                           str(self.evidence) if evidence_dir is None else evidence_dir))
        if handoffs:
            ledger.handoff(operations["plain"], "owner-1", 1)
        return ledger, operations

    def crashed(self, evidence_dir=None, reservation=RESERVATION):
        """A crash and the restart as they leave the ledger: a supervised run
        nobody could classify, of an older generation."""
        ledger, operations = self.build(evidence_dir=evidence_dir, reservation=reservation)
        ledger.unknown(operations["run"], "owner-1", 1)
        ledger.rollover()
        ledger.close()
        return operations

    def abandoned(self, evidence_dir=None):
        """A supervised run an operator has already decided is void."""
        ledger, operations = self.build(evidence_dir=evidence_dir)
        ledger.unknown(operations["run"], "owner-1", 1)
        ledger.resolve_unknown(operations["run"], ledger.generation, resolution="ABANDONED",
                               operator=OPERATOR, evidence_hash=EVIDENCE, note=NOTE)
        ledger.close()
        return operations

    def in_review(self, evidence_dir=None):
        """A supervised run that came back and waits for its verdict."""
        ledger, operations = self.build(evidence_dir=evidence_dir)
        ledger.mark_running(operations["run"], "owner-1", 1)
        ledger.finish(operations["run"], "owner-1", 1, CANDIDATE, True)
        ledger.close()
        return operations

    def reserved(self, evidence_dir=None):
        """A supervised handoff beside an operation that was never handed off."""
        ledger, operations = self.build(evidence_dir=evidence_dir, handoffs=False)
        ledger.close()
        return operations

    # -- streams ----------------------------------------------------------- #
    def write_stream(self, raw, evidence=None):
        # The supervisor's stdout file by its literal name: the command has to
        # read that file, not whatever the module under test calls it.
        target = (self.evidence if evidence is None else evidence) / "stdout.log"
        target.write_bytes(raw)
        return target

    def reference_for(self, frames, newline=LF):
        """A fresh ledger and evidence directory per stream, and its reference."""
        self.setUp()
        self.write_stream(stream_bytes(frames, newline))
        out = self.reference(self.crashed()["run"])
        self.assertEqual(out["stream"]["status"], "ok")
        return out["reference"]


class OutputCase(ReferenceCase):
    """The thirteen keys, and where each of them comes from."""

    def test_the_keys_are_the_ledgers_own_view(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.crashed()
        view = self.view(operations["run"])
        with open_ledger(self.path) as ledger:
            descriptor = ledger.run_descriptor(operations["run"])
            recorded = ledger.operation_usage(operations["run"])
        out = self.reference(operations["run"])
        self.assertEqual(set(out), KEYS)
        self.assertEqual(out["command"], "reference")
        for key in ("operation_id", "request_id", "task_id", "state", "generation", "reservation"):
            self.assertEqual(out[key], view[key], key)
        self.assertEqual(out["evidence_dir"], descriptor["evidence_dir"])
        self.assertEqual(out["dialect"], DIALECT)
        self.assertEqual(out["recorded"], recorded)
        self.assertEqual(set(out["reference"]), REFERENCE_KEYS)

    def test_the_generation_is_the_operations_own(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.crashed()
        with open_ledger(self.path) as ledger:
            self.assertEqual(ledger.generation, 2, "the crash rolled the ledger over")
        out = self.reference(operations["run"])
        self.assertEqual(out["generation"], 1)
        self.assertEqual(self.view(operations["run"])["generation"], 1)

    def test_an_abandoned_operation_is_read_the_same_way(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.abandoned()
        out = self.reference(operations["run"])
        self.assertEqual((out["state"], out["generation"]), ("ABANDONED", 1))
        self.assertEqual(out["reference"]["complete"], True)
        self.assertEqual(out["missing"], ["total_tokens", "cache_read_tokens", "output_tokens"])

    def test_an_operation_in_review_is_read_the_same_way(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.in_review()
        out = self.reference(operations["run"])
        self.assertEqual(out["state"], "REVIEW_PENDING")
        self.assertEqual(out["reference"]["complete"], True)


class MissingCase(ReferenceCase):
    """``missing``: the reserved dimensions with no known final amount."""

    def test_a_dimension_nobody_observed_is_missing(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.crashed()
        out = self.reference(operations["run"])
        self.assertEqual(out["missing"], ["total_tokens", "cache_read_tokens", "output_tokens"])
        self.assertEqual(set(out["recorded"]), {"dispatches"})

    def test_an_amount_of_none_is_missing(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.crashed()
        with open_ledger(self.path) as ledger:
            ledger.observe(operations["run"], "total_tokens", "call-1", 1, None, False, EVIDENCE)
        out = self.reference(operations["run"])
        self.assertEqual(out["missing"], ["total_tokens", "cache_read_tokens", "output_tokens"])
        self.assertEqual((out["recorded"]["total_tokens"]["amount"], out["recorded"]["total_tokens"]["final"]),
                         (None, False))

    def test_an_amount_that_is_not_final_is_missing(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.crashed()
        with open_ledger(self.path) as ledger:
            ledger.observe(operations["run"], "total_tokens", "call-1", 1, 5, False, EVIDENCE)
        out = self.reference(operations["run"])
        self.assertEqual(out["missing"], ["total_tokens", "cache_read_tokens", "output_tokens"])
        self.assertEqual((out["recorded"]["total_tokens"]["amount"], out["recorded"]["total_tokens"]["final"]),
                         (5, False))

    def test_a_known_final_amount_is_not_missing(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.crashed()
        with open_ledger(self.path) as ledger:
            ledger.observe(operations["run"], "total_tokens", "call-1", 1, 5, True, EVIDENCE)
        out = self.reference(operations["run"])
        self.assertNotIn("total_tokens", out["missing"])
        self.assertEqual(out["missing"], ["cache_read_tokens", "output_tokens"])
        self.assertEqual(out["recorded"]["total_tokens"]["amount"], 5)

    def test_the_order_is_the_reservations_own_not_the_alphabetical_one(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        # The reservation is claimed as dispatches, total_tokens, cache_read_tokens,
        # output_tokens; the handoff's own dispatch leaves the other three.
        operations = self.crashed()
        self.assertEqual(self.reference(operations["run"])["missing"],
                         ["total_tokens", "cache_read_tokens", "output_tokens"])
        with open_ledger(self.path) as ledger:
            # One final amount in the middle of the reservation: the order of what
            # is left must not change into a sorted one.
            ledger.observe(operations["run"], "cache_read_tokens", "call-1", 1, 3, True, EVIDENCE)
        self.assertEqual(self.reference(operations["run"])["missing"], ["total_tokens", "output_tokens"])

    def test_the_operators_own_settlement_shows_up_as_recorded(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.crashed()
        first = self.reference(operations["run"])
        entry = self.ok("ledger", "settle", self.path, "--operation", operations["run"],
                        "--dimension", "total_tokens", "--amount", 12, "--evidence", EVIDENCE)
        self.assertEqual(entry["result"], "APPLIED")
        second = self.reference(operations["run"])
        self.assertEqual(second["recorded"]["total_tokens"]["amount"], 12)
        self.assertEqual(second["missing"], ["cache_read_tokens", "output_tokens"])
        self.assertEqual(second["reference"], first["reference"])


class ReasonCase(ReferenceCase):
    """``reasons``: the three ways a stream is not complete, in one fixed order."""

    def test_a_settled_stream_has_no_reason(self):
        reference = self.reference_for([session(), assistant(), settled()])
        self.assertEqual((reference["complete"], reference["reasons"], reference["terminal"]),
                         (True, [], "agent_settled"))

    def test_no_terminal_alone(self):
        reference = self.reference_for([session(), assistant()])
        self.assertEqual((reference["complete"], reference["reasons"], reference["terminal"]),
                         (False, ["no_terminal"], None))
        self.assertEqual(reference["usage"], sums(usage()))

    def test_an_agent_end_is_not_a_terminal(self):
        reference = self.reference_for([session(), assistant(), {"type": "agent_end", "willRetry": False}])
        self.assertEqual((reference["reasons"], reference["terminal"]), (["no_terminal"], None))

    def test_illegal_usage_alone(self):
        legal, illegal = usage(10, 5), dict(usage(20, 7), input=-1)
        reference = self.reference_for([session(), assistant("toolUse", legal), assistant("stop", illegal),
                                        settled()])
        self.assertEqual((reference["complete"], reference["reasons"], reference["terminal"]),
                         (False, ["illegal_usage"], "agent_settled"))
        # The illegal message is left out of the sum, the legal one is in it.
        self.assertEqual(reference["usage"], sums(legal))
        self.assertEqual((reference["assistant_messages"], reference["last_stop_reason"]), (2, "stop"))

    def test_unknown_frames_alone(self):
        reference = self.reference_for([session(), assistant(), {"type": "compaction", "summary": "x"}, settled()])
        self.assertEqual((reference["complete"], reference["reasons"], reference["unknown"], reference["terminal"]),
                         (False, ["unknown_frames"], 1, "agent_settled"))
        self.assertEqual(reference["usage"], sums(usage()))

    def test_no_terminal_and_illegal_usage_together(self):
        reference = self.reference_for([session(), assistant("toolUse", dict(usage(), output="7"))])
        self.assertEqual(reference["reasons"], ["no_terminal", "illegal_usage"])
        self.assertEqual((reference["complete"], reference["usage"]), (False, sums()))

    def test_no_terminal_and_unknown_frames_together(self):
        reference = self.reference_for([session(), assistant(), {"type": "queue"}])
        self.assertEqual(reference["reasons"], ["no_terminal", "unknown_frames"])
        self.assertEqual((reference["complete"], reference["usage"]), (False, sums(usage())))

    def test_illegal_usage_and_unknown_frames_together(self):
        reference = self.reference_for([session(), assistant("toolUse", dict(usage(), output="7")),
                                        {"type": "queue"}, settled()])
        self.assertEqual(reference["reasons"], ["illegal_usage", "unknown_frames"])
        self.assertEqual((reference["complete"], reference["usage"]), (False, sums()))

    def test_all_three_reasons_keep_their_order(self):
        reference = self.reference_for([session(), assistant("toolUse", dict(usage(), output="7")), {"type": "queue"}])
        self.assertEqual(reference["reasons"], ["no_terminal", "illegal_usage", "unknown_frames"])
        self.assertEqual((reference["complete"], reference["usage"], reference["unknown"]), (False, sums(), 1))

    def test_an_incomplete_stream_still_sums_the_legal_messages(self):
        first, second = usage(10, 5), usage(20, 7, 100)
        reference = self.reference_for([session(), assistant("toolUse", first), tool_start(), tool_end(),
                                        assistant("stop", second)])
        self.assertEqual(reference["reasons"], ["no_terminal"])
        self.assertEqual(reference["usage"], sums(first, second))


class CountCase(ReferenceCase):
    """The counts and the models the translation reports, unchanged."""

    def test_models_are_deduplicated_and_sorted_lists(self):
        reference = self.reference_for([session(), assistant(provider="step"), assistant(provider="bb"),
                                        assistant(provider="step"), settled()])
        self.assertEqual(reference["models"], [["bb", "step-5-preview"], ["step", "step-5-preview"]])

    def test_agent_cycles_count_the_agent_starts(self):
        self.assertEqual(self.reference_for([session(), assistant(), settled()])["agent_cycles"], 0)
        self.assertEqual(self.reference_for([session(), agent_start(), assistant(), settled()])["agent_cycles"], 1)
        self.assertEqual(self.reference_for([session(), agent_start(), agent_start(), assistant(),
                                             settled()])["agent_cycles"], 2)

    def test_tool_calls_count_starts_and_errors_count_failed_ends(self):
        reference = self.reference_for([session(), assistant("toolUse", tool_calls=("call-1",)), tool_start()])
        self.assertEqual((reference["tool_calls"], reference["tool_errors"]), (1, 0))
        reference = self.reference_for([session(), assistant("toolUse", tool_calls=("call-1",)), tool_start(),
                                        tool_end(is_error=True), assistant(), settled()])
        self.assertEqual((reference["tool_calls"], reference["tool_errors"], reference["complete"]), (1, 1, True))

    def test_frames_count_frames_and_not_events(self):
        frames = [session(), assistant(), settled()]
        reference = self.reference_for(frames)
        self.assertEqual(reference["frames"], 3)
        # One assistant message is two events (the message and its running usage),
        # so the frame count cannot be the event count of the same stream.
        self.assertEqual(len(translate(frames).events), 4)

    def test_the_usage_is_the_sum_of_the_legal_assistant_messages(self):
        first, second, third = usage(1, 2), usage(3, 4, 5), usage(6, 7, 8, 9)
        reference = self.reference_for([session(), assistant("toolUse", first), assistant("toolUse", second),
                                        assistant("stop", third), settled()])
        self.assertEqual(reference["usage"], sums(first, second, third))
        self.assertEqual(set(reference["usage"]), {dimension for _vendor, dimension in USAGE_KEYS})


class StreamStateCase(ReferenceCase):
    """Every state a stream on disk can be in, and the shape reported for it."""

    def test_a_missing_file_is_an_answer(self):
        operations = self.crashed()
        out = self.reference(operations["run"])
        self.assertEqual(out["stream"], {"status": "missing", "sha256": None, "bytes": None})
        self.assertIsNone(out["reference"])
        self.assertEqual(out["missing"], ["total_tokens", "cache_read_tokens", "output_tokens"])

    def test_a_missing_evidence_directory_is_an_answer(self):
        operations = self.crashed()
        shutil.rmtree(self.evidence)
        out = self.reference(operations["run"])
        self.assertEqual((out["stream"]["status"], out["reference"]), ("missing", None))

    def test_a_directory_where_the_stream_belongs_is_unreadable(self):
        raw = stream_bytes([session(), assistant(), settled()])
        target = self.write_stream(raw)
        operations = self.crashed()
        self.assertEqual(self.reference(operations["run"])["stream"]["status"], "ok")  # legal baseline
        target.unlink()
        target.mkdir()
        out = self.reference(operations["run"])
        self.assertEqual(out["stream"], {"status": "unreadable", "sha256": None, "bytes": None})
        self.assertIsNone(out["reference"])

    def test_a_stream_over_the_largest_ceiling_is_too_large(self):
        raw = stream_bytes([session(), assistant(), settled()])
        target = self.write_stream(raw)
        operations = self.crashed()
        self.assertEqual(self.reference(operations["run"])["stream"]["status"], "ok")  # legal baseline
        with open(target, "wb") as handle:
            handle.truncate(MAX_STREAM_BYTES + 1)
        out = self.reference(operations["run"])
        self.assertEqual(out["stream"], {"status": "too_large", "sha256": None, "bytes": None})
        self.assertIsNone(out["reference"])

    def test_a_stream_over_the_default_ceiling_is_read(self):
        padding = [{"type": "message_update", "pad": "x" * 1000}] * 1100
        raw = stream_bytes([session(), assistant()] + padding + [settled()])
        self.assertGreater(len(raw), DEFAULT_MAX_STREAM_BYTES)
        self.assertLess(len(raw), MAX_STREAM_BYTES)
        self.write_stream(raw)
        out = self.reference(self.crashed()["run"])
        self.assertEqual(out["stream"], {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(),
                                         "bytes": len(raw)})
        self.assertEqual(out["reference"]["complete"], True)

    def test_an_invalid_line_names_itself(self):
        raw = stream_bytes([session(), assistant(), settled()])
        target = self.write_stream(raw)
        operations = self.crashed()
        self.assertEqual(self.reference(operations["run"])["stream"]["status"], "ok")  # legal baseline
        broken = stream_bytes([session(), assistant()]) + b"not json" + LF + stream_bytes([settled()])
        target.write_bytes(broken)
        out = self.reference(operations["run"])
        self.assertEqual(out["stream"], {"status": "invalid", "sha256": hashlib.sha256(broken).hexdigest(),
                                         "bytes": len(broken), "line": 3, "code": "E_JSON_SYNTAX"})
        self.assertIsNone(out["reference"])

    def test_a_line_that_is_not_an_object_is_invalid(self):
        raw = stream_bytes([session(), assistant(), settled()])
        target = self.write_stream(raw)
        operations = self.crashed()
        self.assertEqual(self.reference(operations["run"])["stream"]["status"], "ok")  # legal baseline
        broken = stream_bytes([session()]) + b"[1]" + LF + stream_bytes([assistant(), settled()])
        target.write_bytes(broken)
        out = self.reference(operations["run"])
        self.assertEqual((out["stream"]["status"], out["stream"]["line"], out["stream"]["code"]),
                         ("invalid", 2, "E_ADAPTER_STREAM"))
        self.assertIsNone(out["reference"])

    def test_the_bytes_are_read_verbatim(self):
        # CR LF endings: the digest is of the bytes on disk, CR included.
        raw = stream_bytes([session(), assistant(), settled()], newline=CR + LF)
        self.write_stream(raw)
        out = self.reference(self.crashed()["run"])
        self.assertEqual(out["stream"], {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(),
                                         "bytes": len(raw)})
        self.assertEqual(out["reference"]["complete"], True)


class ArgumentCase(ReferenceCase):
    """What argparse refuses before the ledger is opened at all."""

    def test_the_dialect_must_be_named_and_known(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.crashed()
        for argv in (("ledger", "reference", self.path, "--operation", operations["run"]),
                     ("ledger", "reference", self.path, "--operation", operations["run"],
                      "--dialect", "adapter-protocol"),
                     ("ledger", "reference", self.path, "--operation", operations["run"], "--dialect", "")):
            with self.subTest(argv=argv[3:]):
                before = self.digest()
                code, out, err = self.smctl(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("usage", err)
                self.assertEqual(self.digest(), before, "an argument error changed the ledger")

    def test_the_operation_must_be_named(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        self.crashed()
        before = self.digest()
        code, out, err = self.smctl("ledger", "reference", self.path, "--dialect", DIALECT)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("usage", err)
        self.assertEqual(self.digest(), before, "an argument error changed the ledger")

    def test_the_ledger_must_be_named(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.crashed()
        before = self.digest()
        for argv in (("ledger", "reference"),
                     ("ledger", "reference", "--operation", operations["run"], "--dialect", DIALECT)):
            with self.subTest(argv=argv[2:]):
                code, out, err = self.smctl(*argv)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("usage", err)
        self.assertEqual(self.digest(), before, "an argument error changed the ledger")


class RefusalCase(ReferenceCase):
    """The refusals, each after the same call on a legal baseline."""

    def test_an_unknown_operation_is_refused(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.crashed()
        self.assertEqual(self.reference(operations["run"])["reference"]["complete"], True)  # legal baseline
        self.refused("E_OPERATION_UNKNOWN", "ledger", "reference", self.path, "--operation", "op-nobody",
                     "--dialect", DIALECT)

    def test_a_handoff_without_a_run_descriptor_is_refused(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.crashed()
        self.assertEqual(self.reference(operations["run"])["reference"]["complete"], True)  # legal baseline
        error = self.refused("E_NOT_SUPERVISED", "ledger", "reference", self.path, "--operation",
                             operations["plain"], "--dialect", DIALECT)
        self.assertIsInstance(error["detail"], str)
        self.assertIn(operations["plain"], error["detail"])

    def test_an_operation_that_was_never_handed_off_is_refused(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.reserved()
        self.assertEqual(self.reference(operations["run"])["reference"]["complete"], True)  # legal baseline
        self.assertEqual(self.view(operations["plain"])["state"], "RESERVED")
        self.refused("E_NOT_SUPERVISED", "ledger", "reference", self.path, "--operation", operations["plain"],
                     "--dialect", DIALECT)

    def test_a_ledger_that_is_not_there_is_refused(self):
        missing = str(self.work / "missing.db")
        error = self.refused("E_LEDGER_MISSING", "ledger", "reference", missing, "--operation", "op-1",
                             "--dialect", DIALECT)
        self.assertIsInstance(error["detail"], str)
        self.assertFalse(os.path.exists(missing), "a refused command created a ledger")


class NeverWritesCase(ReferenceCase):
    """The command reads: the ledger file, the sequence, the events, the stream."""

    def assert_unchanged(self, digest, seq, events):
        self.assertEqual(self.digest(), digest, "a reference wrote to the ledger")
        self.assertEqual(self.seq(), seq, "a reference recorded a command")
        self.assertEqual(self.events(), events, "a reference emitted an event")

    def test_a_reference_changes_neither_the_ledger_nor_the_evidence(self):
        raw = stream_bytes([session(), assistant(), settled()])
        target = self.write_stream(raw)
        operations = self.crashed()
        before, seq, events = self.digest(), self.seq(), self.events()
        self.assertEqual(self.reference(operations["run"])["reference"]["complete"], True)
        self.assert_unchanged(before, seq, events)
        self.assertEqual((target.read_bytes(), target.stat().st_size), (raw, len(raw)), "the stream was touched")
        self.assertEqual(sorted(path.name for path in self.evidence.iterdir()), ["stdout.log"],
                         "something was written next to the stream")

    def test_a_reference_on_a_missing_stream_changes_nothing(self):
        operations = self.crashed()
        before, seq, events = self.digest(), self.seq(), self.events()
        self.assertEqual(self.reference(operations["run"])["stream"]["status"], "missing")
        self.assert_unchanged(before, seq, events)
        self.assertEqual(sorted(path.name for path in self.evidence.iterdir()), [],
                         "something was written next to the stream")


class ClosedCase(ReferenceCase):
    def test_every_instance_a_command_opens_is_closed(self):
        self.write_stream(stream_bytes([session(), assistant(), settled()]))
        operations = self.crashed()
        opened = []
        real = ledger_cli.open_ledger

        def tracking(path):
            ledger = real(path)
            opened.append(ledger)
            return ledger

        with mock.patch.object(ledger_cli, "open_ledger", tracking):
            self.reference(operations["run"])
            self.refused("E_NOT_SUPERVISED", "ledger", "reference", self.path, "--operation",
                         operations["plain"], "--dialect", DIALECT)
            self.refused("E_OPERATION_UNKNOWN", "ledger", "reference", self.path, "--operation", "op-nobody",
                         "--dialect", DIALECT)
            (self.evidence / "stdout.log").unlink()
            self.assertEqual(self.reference(operations["run"])["stream"]["status"], "missing")
        self.assertEqual(len(opened), 4)
        for ledger in opened:
            with self.assertRaises(LedgerError) as ctx:
                ledger.request_ids()
            self.assertEqual(ctx.exception.code, "E_LEDGER_UNAVAILABLE", "a command left its ledger open")


class EvidenceDirCase(ReferenceCase):
    def test_a_relative_evidence_dir_is_used_as_recorded(self):
        relative = self.work / "evidence-rel"
        relative.mkdir()
        raw = stream_bytes([session(), assistant(), settled()])
        self.write_stream(raw, relative)
        operations = self.crashed(evidence_dir="evidence-rel")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.work)
        out = self.reference(operations["run"])
        self.assertEqual(out["evidence_dir"], "evidence-rel")
        self.assertEqual(out["stream"], {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(),
                                         "bytes": len(raw)})
        # The same recorded path, read from another working directory: the stream
        # is not there, which is what "used as recorded" means.
        os.chdir(self.evidence)
        self.assertEqual(self.reference(operations["run"])["stream"]["status"], "missing")


@unittest.skipIf(hasattr(os, "O_NOFOLLOW"),
                 "the lstat/fstat identity check is the branch of read_stream that runs only where the "
                 "platform has no O_NOFOLLOW (Windows); the POSIX branch is covered elsewhere")
class WithoutNofollowCase(unittest.TestCase):
    """The platform branch of ``read_stream`` without ``O_NOFOLLOW``."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="read-stream-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.evidence = self.work / "evidence"
        self.evidence.mkdir()

    def write(self, raw):
        target = self.evidence / "stdout.log"
        target.write_bytes(raw)
        return target

    def test_a_regular_file_is_read_byte_for_byte(self):
        frames = [session(), assistant(), settled()]
        raw = stream_bytes(frames, newline=CR + LF)
        self.write(raw)
        self.assertEqual(read_stream(str(self.evidence)),
                         {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                          "events": frames})

    def test_a_substitute_byte_is_read_to_the_end(self):
        # 0x1A is where a text-mode read would stop; the stream is read to its
        # end, so that byte is the invalid line it is.
        raw = stream_bytes([session(), assistant(), settled()], newline=CR + LF) + SUB + LF
        self.write(raw)
        outcome = read_stream(str(self.evidence))
        self.assertEqual((outcome["status"], outcome["bytes"]), ("invalid", len(raw)))
        self.assertEqual((outcome["line"], outcome["code"]), (4, "E_JSON_SYNTAX"))

    def test_a_directory_is_unreadable(self):
        target = self.write(stream_bytes([session(), assistant(), settled()]))
        self.assertEqual(read_stream(str(self.evidence))["status"], "ok")  # legal baseline
        target.unlink()
        target.mkdir()
        self.assertEqual(read_stream(str(self.evidence)), UNREADABLE)

    def test_a_symlinked_stream_is_unreadable(self):
        raw = stream_bytes([session(), assistant(), settled()])
        target = self.write(raw)
        self.assertEqual(read_stream(str(self.evidence))["status"], "ok")  # legal baseline
        elsewhere = self.work / "elsewhere.jsonl"
        elsewhere.write_bytes(raw)
        target.unlink()
        try:
            os.symlink(elsewhere, target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symbolic links cannot be created here: %s" % exc)
        self.assertEqual(read_stream(str(self.evidence)), UNREADABLE)

    def test_a_hard_linked_stream_is_read(self):
        raw = stream_bytes([session(), assistant(), settled()])
        elsewhere = self.work / "elsewhere.jsonl"
        elsewhere.write_bytes(raw)
        try:
            os.link(elsewhere, self.evidence / "stdout.log")
        except (OSError, NotImplementedError) as exc:
            self.skipTest("hard links cannot be created here: %s" % exc)
        outcome = read_stream(str(self.evidence))
        self.assertEqual((outcome["status"], outcome["sha256"], outcome["bytes"]),
                         ("ok", hashlib.sha256(raw).hexdigest(), len(raw)))

    def test_a_reparse_point_is_refused(self):
        path = self.write(stream_bytes([session(), assistant(), settled()]))
        self.assertEqual(read_stream(str(self.evidence))["status"], "ok")  # legal baseline
        real = os.lstat(path)
        fake = types.SimpleNamespace(st_mode=real.st_mode, st_dev=real.st_dev, st_ino=real.st_ino,
                                     st_file_attributes=getattr(real, "st_file_attributes", 0)
                                     | stat.FILE_ATTRIBUTE_REPARSE_POINT)
        with mock.patch.object(adapter_run.os, "lstat", return_value=fake):
            self.assertEqual(read_stream(str(self.evidence)), UNREADABLE)
        self.assertEqual(read_stream(str(self.evidence))["status"], "ok")

    def test_an_identity_that_is_not_the_lstat_one_is_refused(self):
        path = self.write(stream_bytes([session(), assistant(), settled()]))
        real = os.lstat(path)
        fake = types.SimpleNamespace(st_mode=real.st_mode, st_dev=real.st_dev, st_ino=real.st_ino + 1,
                                     st_file_attributes=getattr(real, "st_file_attributes", 0))
        with mock.patch.object(adapter_run.os, "lstat", return_value=fake):
            self.assertEqual(read_stream(str(self.evidence)), UNREADABLE)
        self.assertEqual(read_stream(str(self.evidence))["status"], "ok")

    def test_a_link_planted_after_the_lstat_is_refused(self):
        raw = stream_bytes([session(), assistant(), settled()])
        elsewhere = self.work / "elsewhere.jsonl"
        elsewhere.write_bytes(raw)
        path = self.write(raw)
        before = os.lstat(path)
        path.unlink()
        try:
            os.symlink(elsewhere, path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symbolic links cannot be created here: %s" % exc)
        # ``lstat`` saw the regular file; by the time of the open a link is at the
        # path and the open follows it, so what was opened is another file.
        with mock.patch.object(adapter_run.os, "lstat", return_value=before):
            self.assertEqual(read_stream(str(self.evidence)), UNREADABLE)
        # The link is gone again: the very same path reads as it did before.
        path.unlink()
        self.write(raw)
        self.assertEqual(read_stream(str(self.evidence))["status"], "ok")


class EntryPointCase(ReferenceCase):
    def test_the_module_entry_point_runs_the_reference(self):
        raw = stream_bytes([session(), assistant(), settled()])
        self.write_stream(raw)
        operations = self.crashed()
        done = subprocess.run([sys.executable, "-m", "sentinelmesh.cli", "ledger", "reference", self.path,
                               "--operation", operations["run"], "--dialect", DIALECT],
                              cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        out = json.loads(done.stdout)
        self.assertEqual(set(out), KEYS)
        self.assertEqual(out["stream"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(out["reference"]["usage"]["total_tokens"], 15)
        refused = subprocess.run([sys.executable, "-m", "sentinelmesh.cli", "ledger", "reference", self.path,
                                  "--operation", operations["plain"], "--dialect", DIALECT],
                                 cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual((refused.returncode, refused.stdout), (3, ""))
        self.assertEqual(json.loads(refused.stderr)["error"], "E_NOT_SUPERVISED")


if __name__ == "__main__":
    unittest.main()
