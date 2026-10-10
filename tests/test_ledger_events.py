"""Executor tests for ledger-events: ``smctl ledger events`` and ``checkpoint_seq``.

The planner oracle (``tests/test_ledger_events_oracle.py``) reads the whole
history of a 33-command scenario back verbatim and pages it; these cases cover
what a small scenario states more directly, following the same rule throughout:
every rejection first proves a legal baseline -- the very same call with nothing
but the value under test changed -- so a different rule refusing the command
first cannot hide a gap.

The behaviour under test comes from `.agent/design/ledger-events.md` sections 3,
5 and 6: the events are printed exactly as recorded, ``--request`` attributes an
operation's event by the operation's request and a request-level event by its
payload (the fence belongs to none), ``--after``/``--limit`` page the selection,
``status`` is untouched and the checkpoint is reported in this command's header
only.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import cli
from sentinelmesh.durable_ledger import LedgerError, create_ledger, open_ledger

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = os.path.realpath(sys.executable)
CAPS = {"dispatches": 9, "tokens": 500, "cost": 200}
#: A request whose caps the observation below overruns: the usage guard fires on it
#: and records the stop it observed.  The claim still fits (40 of 45 held), and the
#: observed amount is what pushes the exposure over the limit.
TIGHT_CAPS = {"dispatches": 9, "tokens": 45, "cost": 200}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
IDENTITY = {"pid": 4242, "boot_id": "12345678-1234-1234-1234-1234567890AB", "executable_sha256": "a" * 64,
            "worktree_id": "wt-1"}
H1, H2 = "1" * 64, "2" * 64
KEYS = {"command", "ledger", "generation", "seq", "checkpoint", "total", "matched", "events", "next"}
EVENT_KEYS = {"seq", "operation_id", "type", "generation", "payload"}
#: The request-level types, and the fence that belongs to no request.
REQUEST_LEVEL = ("request.created", "request.stopped", "request.stop_observed", "request.resumed")


def descriptor(op):
    return {"spec": {"request_id": "req-1", "run_id": op, "command": ["pi", "--mode", "json"], "cwd": "/w",
                     "environment_sha256": "a" * 64, "timeout_seconds": 1800},
            "evidence_dir": "/evidence/" + op, "worktree_id": "wt-1"}


class EventsCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="ledger-events-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.path = str(self.work / "ledger.db")

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

    def events(self, *argv):
        """``smctl ledger events`` on the ledger: the parsed output; the call must succeed."""
        code, out, err = self.smctl("ledger", "events", self.path, *argv)
        self.assertEqual((code, err), (0, ""), out)
        result = json.loads(out)
        self.assertEqual(set(result), KEYS)
        for event in result["events"]:
            self.assertEqual(set(event), EVENT_KEYS)
        return result

    def argument_error(self, *argv):
        """An argparse refusal: exit 2, nothing on stdout, the usage line on stderr."""
        code, out, err = self.smctl("ledger", "events", self.path, *argv)
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("usage:", err)
        self.assertNotIn("Traceback", err)

    # -- the scenario ------------------------------------------------------ #
    def build(self):
        """A small ledger: two requests, a budget-guarded stop, a fence, one operation.

        Returns ``(operations, requests)`` -- the operation ids in claim order and
        the request each of them belongs to.
        """
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=5)
        for task in ("task-1", "task-2"):
            ledger.seed_task("req-1", task, CAPS, max_attempts=3)
        a = ledger.claim("req-1", "task-1", 1, "idem-a", "owner-1", 1, 1, RESERVATION, ["src/a.py"])["operation_id"]
        ledger.handoff(a, "owner-1", 1, 1, run=descriptor(a))
        ledger.mark_running(a, "owner-1", 1, process_identity=dict(IDENTITY))
        ledger.settle(a, {"tokens": 12, "cost": 3}, evidence_hash=H1)
        ledger.finish(a, "owner-1", 1, H2, True)
        ledger.fixture_verdict(a, H2, True, H1)
        b = ledger.claim("req-1", "task-2", 1, "idem-b", "owner-1", 1, 1, RESERVATION, ["src/b.py"])["operation_id"]
        ledger.handoff(b, "owner-1", 1, 20)
        ledger.release_proven_unstarted(b, "owner-1", 1, evidence_hash=H2)
        # A request the usage guard stops on its own: the only way a
        # ``request.stop_observed`` is recorded.
        ledger.seed_request("req-3", "repo-3", TIGHT_CAPS)
        ledger.seed_task("req-3", "task-3", TIGHT_CAPS, max_attempts=3)
        c = ledger.claim("req-3", "task-3", 1, "idem-c", "owner-1", 1, 1, RESERVATION, ["src/c.py"])["operation_id"]
        ledger.observe(c, "tokens", "call-c", 1, 100, True, H1)
        ledger.rollover()
        ledger.seed_request("req-2", "repo-2", CAPS)
        ledger.stop("req-2", "OPERATOR_STOP")
        ledger.resume("req-2", 2, expected_reasons=["OPERATOR_STOP"], operator="op-1", note="resumed")
        expected = list(ledger.events)
        requests = {op: ledger.operation(op)["request_id"] for op in (a, b, c)}
        ledger.close()
        return (a, b, c), expected, requests

    def belongs(self, event, request, requests):
        """The attribution rule of ``--request``, stated for the test's own oracle."""
        if event["operation_id"] is not None:
            return requests[event["operation_id"]] == request
        payload = event["payload"]
        return isinstance(payload, dict) and payload.get("request_id") == request

    def digest_of(self, path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def rows_of(self, path):
        db = sqlite3.connect(path)
        try:
            return db.execute("SELECT seq, sha256 FROM commands ORDER BY seq").fetchall()
        finally:
            db.close()


class CheckpointSeqCase(EventsCase):
    def test_the_accessor(self):
        with create_ledger(self.path) as ledger:
            self.assertIsNone(ledger.checkpoint_seq)
            ledger.seed_request("req-1", "repo-1", CAPS)
            self.assertIsNone(ledger.checkpoint_seq)
            self.assertEqual(ledger.compact()["seq"], ledger.seq)
            self.assertEqual(ledger.checkpoint_seq, ledger.seq)
            # A command after the checkpoint: the checkpoint stays where it was.
            ledger.stop("req-1", "hold")
            self.assertEqual((ledger.checkpoint_seq, ledger.seq), (1, 2))
        # Reopened without a checkpoint: none, exactly as a fresh ledger.
        with create_ledger(str(self.work / "fresh.db")) as fresh:
            self.assertIsNone(fresh.checkpoint_seq)
        with open_ledger(str(self.work / "fresh.db")) as fresh:
            self.assertIsNone(fresh.checkpoint_seq)
        with open_ledger(self.path) as reopened:
            self.assertEqual((reopened.checkpoint_seq, reopened.seq), (1, 2))

    def test_a_closed_instance_refuses_the_read(self):
        with create_ledger(self.path) as ledger:
            ledger.seed_request("req-1", "repo-1", CAPS)
            ledger.compact()
        with self.assertRaises(LedgerError) as ctx:
            ledger.checkpoint_seq
        self.assertEqual(ctx.exception.code, "E_LEDGER_UNAVAILABLE")


class RequestAttributionCase(EventsCase):
    def test_request_level_events_belong_by_their_payload(self):
        _ops, expected, requests = self.build()
        for request in ("req-1", "req-2", "req-3"):
            with self.subTest(request=request):
                result = self.events("--request", request, "--limit", 10000)
                self.assertEqual(result["events"],
                                 [event for event in expected if self.belongs(event, request, requests)])
                self.assertEqual(result["matched"], len(result["events"]))
                self.assertNotIn("controller.fenced", {event["type"] for event in result["events"]})
        # Every request-level type this scenario records is attributed by its
        # payload, and each of them carries its request id there.
        for request in ("req-1", "req-2", "req-3"):
            with self.subTest(types_of=request):
                types = [event["type"] for event in expected
                         if event["operation_id"] is None and event["type"] in REQUEST_LEVEL
                         and event["payload"].get("request_id") == request]
                self.assertEqual([event["type"] for event in self.events("--request", request,
                                                                         "--limit", 10000)["events"]
                                  if event["operation_id"] is None], types)
                self.assertTrue(types, "the scenario records no request-level event for %s" % request)
        # The fence belongs to no request at all.
        fenced = [event for event in expected if event["type"] == "controller.fenced"]
        self.assertEqual(len(fenced), 1)
        for request in ("req-1", "req-2", "req-3"):
            self.assertNotIn(fenced[0], self.events("--request", request, "--limit", 10000)["events"])
        self.assertIn(fenced[0], self.events("--limit", 10000)["events"])

    def test_an_operations_events_belong_to_its_request(self):
        ops, _expected, requests = self.build()
        for op in ops:
            with self.subTest(operation=op):
                result = self.events("--request", requests[op], "--operation", op, "--limit", 10000)
                self.assertEqual(result["events"],
                                 [event for event in self.events("--operation", op, "--limit", 10000)["events"]])
                self.assertTrue(result["events"])
        # An operation of another request is not selected by the request filter.
        other = self.events("--request", "req-2", "--operation", ops[0], "--limit", 10000)
        self.assertEqual((other["matched"], other["events"], other["next"]), (0, [], None))

    def test_an_empty_selection(self):
        _ops, expected, _requests = self.build()
        result = self.events("--request", "req-3", "--type", "run.verdict", "--limit", 10000)
        self.assertEqual((result["matched"], result["events"], result["next"]), (0, [], None))
        self.assertEqual(result["total"], len(expected))


class PagingCase(EventsCase):
    def test_one_event_per_page_reassembles_the_history(self):
        _ops, expected, _requests = self.build()
        after, seen = 0, []
        while True:
            result = self.events("--after", after, "--limit", 1)
            self.assertEqual(result["total"], len(expected))
            seen.extend(result["events"])
            if result["next"] is None:
                break
            self.assertEqual(result["next"], result["events"][-1]["seq"])
            after = result["next"]
        self.assertEqual(seen, expected)

    def test_after_the_last_event(self):
        _ops, expected, _requests = self.build()
        result = self.events("--after", len(expected), "--limit", 10000)
        self.assertEqual((result["events"], result["next"], result["matched"], result["total"]),
                         ([], None, len(expected), len(expected)))

    def test_the_default_page_is_one_hundred(self):
        ledger = create_ledger(self.path)
        for index in range(51):
            ledger.seed_request("req-%02d" % index, "repo", CAPS)
            if index < 50:
                ledger.stop("req-%02d" % index, "hold")
        expected = list(ledger.events)
        ledger.close()
        self.assertEqual(len(expected), 101)
        result = self.events()
        self.assertEqual(result["events"], expected[:100])
        self.assertEqual((result["next"], result["matched"], result["total"]), (100, 101, 101))
        result = self.events("--after", 100)
        self.assertEqual((result["events"], result["next"]), (expected[100:], None))


class ArgumentCase(EventsCase):
    def setUp(self):
        super().setUp()
        self.build()

    def test_the_page_size_must_be_a_positive_decimal_integer(self):
        self.assertEqual(len(self.events("--limit", 5)["events"]), 5)
        for value in ("0", "10001", "+5", "1_0", "x", " 5", "5 "):
            with self.subTest(limit=value):
                self.argument_error("--limit", value)
        self.assertEqual(len(self.events("--limit", 1)["events"]), 1)

    def test_after_must_be_a_non_negative_decimal_integer(self):
        self.events("--after", 0)
        for value in ("-1", "1.5", "+1", "1_0", "x", " 1"):
            with self.subTest(after=value):
                self.argument_error("--after", value)
        self.events("--after", 3)

    def test_an_unknown_or_empty_type_is_refused(self):
        self.events("--type", "run.verdict")
        for value in ("scheduler.claim", "", "RUN.VERDICT", "run.verdict "):
            with self.subTest(type=value):
                self.argument_error("--type", value)

    def test_an_argument_error_opens_no_ledger(self):
        code, out, err = self.smctl("ledger", "events", str(self.work / "missing.db"), "--limit", "0")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("usage:", err)


class NeverWritesCase(EventsCase):
    def test_reading_and_being_refused_write_nothing(self):
        ops, _expected, _requests = self.build()
        before = (self.digest_of(self.path), self.rows_of(self.path), sorted(os.listdir(self.work)))
        self.events("--limit", 10000)
        self.events("--request", "req-1", "--operation", ops[0], "--type", "scheduler.claimed",
                    "--after", 1, "--limit", 1)
        # The refusal is a refusal: the ledger it found is the ledger it leaves.
        code, out, err = self.smctl("ledger", "events", self.path, "--request", "req-9")
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(json.loads(err)["error"], "E_REQUEST_UNKNOWN")
        self.assertEqual((self.digest_of(self.path), self.rows_of(self.path), sorted(os.listdir(self.work))),
                         before, "reading the events wrote something")
        with open_ledger(self.path) as ledger:
            self.assertEqual(ledger.checkpoint_seq, None)


class EntryPointCase(EventsCase):
    def test_the_command_line_entry_point(self):
        ops, expected, _requests = self.build()
        done = subprocess.run([PY, "-m", "sentinelmesh.cli", "ledger", "events", self.path, "--operation", ops[0],
                               "--limit", "2"], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stdout)
        result = json.loads(done.stdout)
        self.assertEqual(set(result), KEYS)
        self.assertEqual(result["events"],
                         [event for event in expected if event["operation_id"] == ops[0]][:2])
        self.assertEqual(result["next"], result["events"][-1]["seq"])
        done = subprocess.run([PY, "-m", "sentinelmesh.cli", "ledger", "events", "--help"],
                              cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        for option in ("--request", "--operation", "--type", "--after", "--limit"):
            self.assertIn(option, done.stdout)


if __name__ == "__main__":
    unittest.main()
