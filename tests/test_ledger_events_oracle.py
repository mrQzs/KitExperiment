"""Planner oracle for ledger-events (`.agent/design/ledger-events.md`).

Planner-provided; executors must not modify this file.

A compaction removes the commands it covers, so the events are the only
history the ledger keeps -- and an operator could not read them without writing
Python.  The user decided (2026-10-09): ``smctl ledger events`` reads them back
exactly as recorded, filtered by request (an operation's request, or the
payload's for a request-level event; ``controller.fenced`` belongs to none),
operation and type, paged with ``--after`` and ``--limit`` (default 100, at most
10000) and a ``next`` sequence number; the output's header reports the
checkpoint the ledger holds, and ``status`` stays as it was.

The cases run every mutating method at least once (the ledger-compaction
scenario: a rollover, an operator's resolutions, a resume), so every kind of
event the Scheduler emits in such a run is read back and filtered.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import cli, ledger_cli, scheduler as scheduler_module
from sentinelmesh.durable_ledger import LedgerError, create_ledger, open_ledger
from sentinelmesh.scheduler import ContractError

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = os.path.realpath(sys.executable)
CAPS = {"dispatches": 9, "tokens": 500, "cost": 200}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
IDENTITY = {"pid": 4242, "boot_id": "12345678-1234-1234-1234-1234567890AB", "executable_sha256": "a" * 64,
            "worktree_id": "wt-1"}
H1, H2, H3 = "1" * 64, "2" * 64, "3" * 64
TASKS = ("task-1", "task-2", "task-3", "task-4", "task-5")
CALLS = 33
KEYS = {"command", "ledger", "generation", "seq", "checkpoint", "total", "matched", "events", "next"}
EVENT_KEYS = {"seq", "operation_id", "type", "generation", "payload"}
#: The sixteen event types the Scheduler emits, written out here: the command's
#: ``--type`` must offer exactly these.
EVENT_TYPES = {"controller.fenced", "request.created", "request.resumed", "request.stop_observed",
               "request.stopped", "run.finished_for_review", "run.started", "run.verdict",
               "scheduler.cancelled_before_handoff", "scheduler.claimed", "scheduler.handoff_accepted",
               "scheduler.not_started", "scheduler.recovered", "scheduler.start_unknown",
               "scheduler.unknown_resolved", "usage.observed"}


def descriptor(op, evidence_dir):
    return {"spec": {"request_id": "req-1", "run_id": op, "command": ["pi", "--mode", "json"], "cwd": "/w",
                     "environment_sha256": "a" * 64, "timeout_seconds": 1800},
            "evidence_dir": evidence_dir, "worktree_id": "wt-1"}


def scenario(ledger):
    """Every mutating method at least once (CALLS calls); the operation ids in claim order."""
    ledger.seed_request("req-1", "repo-1", CAPS, concurrency=5)
    for task in TASKS:
        ledger.seed_task("req-1", task, CAPS, max_attempts=3)
    a = ledger.claim("req-1", "task-1", 1, "idem-a", "owner-1", 1, 1, RESERVATION, ["src/a.py"])["operation_id"]
    ledger.handoff(a, "owner-1", 1, 1, run=descriptor(a, "/evidence/a"))
    ledger.mark_running(a, "owner-1", 1, process_identity=dict(IDENTITY))
    ledger.settle(a, {"tokens": 12, "cost": 3}, evidence_hash=H1)
    ledger.finish(a, "owner-1", 1, H2, True)
    ledger.fixture_verdict(a, H2, True, H1)
    b = ledger.claim("req-1", "task-2", 1, "idem-b", "owner-1", 1, 1, RESERVATION, ["src/b.py"], now=5,
                     lease=60)["operation_id"]
    ledger.renew(b, "owner-1", 1, 10, 100)
    ledger.handoff(b, "owner-1", 1, 20)
    ledger.release_proven_unstarted(b, "owner-1", 1, evidence_hash=H3)
    c = ledger.claim("req-1", "task-3", 1, "idem-c", "owner-1", 1, 1, RESERVATION, ["src/c.py"])["operation_id"]
    ledger.cancel_unstarted(c, 1)
    d = ledger.claim("req-1", "task-4", 1, "idem-d", "owner-1", 1, 1, RESERVATION, ["src/d.py"])["operation_id"]
    ledger.handoff(d, "owner-1", 1, 2)
    ledger.observe(d, "tokens", "call-d", 1, 7, False, H1)
    e = ledger.claim("req-1", "task-5", 1, "idem-e", "owner-1", 1, 1, RESERVATION, ["src/e.py"])["operation_id"]
    ledger.handoff(e, "owner-1", 1, 3)
    ledger.mark_running(e, "owner-1", 1)
    ledger.unknown(e, "owner-1", 1, reason="CLEANUP_UNCONFIRMED")
    ledger.rollover()
    ledger.recover(d, "owner-2", 2, outcome="START_UNKNOWN", evidence_hash=H2)
    ledger.stop("req-1", "OPERATOR_STOP")
    ledger.resolve_unknown(e, 2, resolution="ABANDONED", operator="op-1", evidence_hash=H2, note="process gone")
    ledger.resolve_unknown(d, 2, resolution="FINISHED", operator="op-1", evidence_hash=H2, note="finished",
                           candidate_hash=H3)
    ledger.seed_request("req-2", "repo-2", CAPS)
    ledger.stop("req-2", "OPERATOR_STOP")
    ledger.resume("req-2", 2, expected_reasons=["OPERATOR_STOP"], operator="op-1", note="resumed")
    return [a, b, c, d, e]


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


def rows_of(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT seq, sha256 FROM commands ORDER BY seq").fetchall()
    finally:
        db.close()


class EventsCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="ledger-events-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.path = str(self.work / "ledger.db")

    def ledger_with_scenario(self):
        ledger = create_ledger(self.path)
        ops = scenario(ledger)
        expected = list(ledger.events)
        requests = {op: ledger.operation(op)["request_id"] for op in ops}
        ledger.close()
        return ops, expected, requests

    def events(self, *extra):
        """``smctl ledger events`` on the ledger: the parsed output; the call must succeed."""
        code, out, err = smctl("ledger", "events", self.path, *extra)
        self.assertEqual((code, err), (0, ""), out)
        result = json.loads(out)
        self.assertEqual(set(result), KEYS)
        return result

    def refused(self, code_expected, *argv):
        before = digest_of(self.path) if os.path.exists(self.path) else None
        code, out, err = smctl("ledger", "events", *argv)
        self.assertEqual((code, out), (3, ""), err)
        self.assertEqual(json.loads(err)["error"], code_expected)
        self.assertEqual(set(json.loads(err)), {"error", "detail"})
        if before is not None:
            self.assertEqual(digest_of(self.path), before, "a refused command changed the ledger")

    def argument_error(self, *argv):
        """An argparse refusal: exit 2, nothing on stdout, the usage line on stderr.

        The usage line is what tells an argparse refusal from ``cli.main``'s
        ``error:`` exit after an exception -- a crash on a value the parser let
        through also exits 2."""
        code, out, err = smctl("ledger", "events", *argv)
        self.assertEqual((code, out), (2, ""), err)
        self.assertIn("usage:", err)
        self.assertNotIn("Traceback", err)


class EventsOracle(EventsCase):
    def test_the_whole_history_reads_back_verbatim(self):
        ops, expected, _requests = self.ledger_with_scenario()
        result = self.events("--limit", 10000)
        self.assertEqual(result["events"], expected, "the events are not what the ledger recorded")
        self.assertEqual([event["seq"] for event in expected], list(range(1, len(expected) + 1)))
        self.assertTrue(all(set(event) == EVENT_KEYS for event in result["events"]))
        self.assertEqual((result["command"], result["ledger"], result["generation"], result["seq"]),
                         ("events", self.path, 2, CALLS))
        self.assertEqual((result["checkpoint"], result["total"], result["matched"], result["next"]),
                         (None, len(expected), len(expected), None))
        # Every kind of event this run can produce is in the history, with its ids.
        self.assertEqual({event["type"] for event in expected} - EVENT_TYPES, set())
        self.assertEqual({event["operation_id"] for event in expected} - {None, *ops}, set())
        self.assertIn("controller.fenced", {event["type"] for event in expected})

    def test_filters_select_by_request_operation_and_type(self):
        ops, expected, requests = self.ledger_with_scenario()
        a, b, c, d, e = ops

        def belongs(event, request):
            if event["operation_id"] is not None:
                return requests[event["operation_id"]] == request
            return event["payload"].get("request_id") == request

        for request in ("req-1", "req-2"):
            with self.subTest(request=request):
                result = self.events("--request", request, "--limit", 10000)
                wanted = [event for event in expected if belongs(event, request)]
                self.assertEqual(result["events"], wanted)
                self.assertEqual((result["total"], result["matched"]), (len(expected), len(wanted)))
        fenced = [event for event in expected if event["type"] == "controller.fenced"]
        self.assertEqual(len(fenced), 1)
        self.assertNotIn(fenced[0], self.events("--request", "req-1", "--limit", 10000)["events"])
        self.assertNotIn(fenced[0], self.events("--request", "req-2", "--limit", 10000)["events"])
        # The two requests and the fenced event partition the history.
        both = (self.events("--request", "req-1", "--limit", 10000)["matched"]
                + self.events("--request", "req-2", "--limit", 10000)["matched"])
        self.assertEqual(both + 1, len(expected))
        with self.subTest(case="request-level events belong by their payload"):
            req2 = self.events("--request", "req-2", "--limit", 10000)["events"]
            self.assertEqual([event["type"] for event in req2],
                             ["request.created", "request.stopped", "request.resumed"])
            self.assertTrue(all(event["operation_id"] is None for event in req2))
        for op in ops:
            with self.subTest(operation=op):
                result = self.events("--operation", op, "--limit", 10000)
                wanted = [event for event in expected if event["operation_id"] == op]
                self.assertEqual(result["events"], wanted)
                self.assertEqual(result["matched"], len(wanted))
                self.assertGreater(len(wanted), 0)
        with self.subTest(case="one type"):
            result = self.events("--type", "run.verdict", "--limit", 10000)
            self.assertEqual([(event["type"], event["operation_id"]) for event in result["events"]],
                             [("run.verdict", a)])
        with self.subTest(case="several types are a union"):
            result = self.events("--type", "request.stopped", "--type", "request.resumed", "--limit", 10000)
            self.assertEqual([event["type"] for event in result["events"]],
                             [event["type"] for event in expected
                              if event["type"] in {"request.stopped", "request.resumed"}])
            self.assertEqual(result["matched"], 3)
        with self.subTest(case="the filters intersect"):
            result = self.events("--request", "req-1", "--type", "scheduler.claimed", "--limit", 10000)
            self.assertEqual([event["operation_id"] for event in result["events"]], ops)
            result = self.events("--operation", d, "--type", "scheduler.recovered")
            self.assertEqual([(event["type"], event["operation_id"]) for event in result["events"]],
                             [("scheduler.recovered", d)])
            result = self.events("--request", "req-2", "--operation", a)
            self.assertEqual((result["matched"], result["events"], result["next"]), (0, [], None))
            result = self.events("--operation", e, "--type", "run.verdict")
            self.assertEqual((result["matched"], result["events"]), (0, []))

    def test_pages_reassemble_the_history(self):
        _ops, expected, _requests = self.ledger_with_scenario()
        after, pages, seen = 0, 0, []
        while True:
            result = self.events("--after", after, "--limit", 7)
            pages += 1
            self.assertLessEqual(len(result["events"]), 7)
            self.assertEqual((result["total"], result["matched"]), (len(expected), len(expected)),
                             "paging changed the counts")
            seen.extend(result["events"])
            if result["next"] is None:
                break
            self.assertEqual(result["next"], result["events"][-1]["seq"])
            self.assertEqual(len(result["events"]), 7, "a page before the last one is not full")
            after = result["next"]
        self.assertEqual(seen, expected, "the pages do not reassemble the history")
        self.assertEqual(pages, -(-len(expected) // 7))
        with self.subTest(case="a page that ends exactly at the history has no next"):
            result = self.events("--after", len(expected) - 7, "--limit", 7)
            self.assertEqual(([event["seq"] for event in result["events"]], result["next"]),
                             (list(range(len(expected) - 6, len(expected) + 1)), None))
        with self.subTest(case="after the end"):
            result = self.events("--after", len(expected))
            self.assertEqual((result["events"], result["next"], result["matched"]), ([], None, len(expected)))
            result = self.events("--after", 10 ** 9)
            self.assertEqual((result["events"], result["next"]), ([], None))
        with self.subTest(case="paging a filtered selection"):
            first = self.events("--request", "req-1", "--type", "scheduler.claimed", "--limit", 2)
            second = self.events("--request", "req-1", "--type", "scheduler.claimed", "--limit", 2,
                                 "--after", first["next"])
            third = self.events("--request", "req-1", "--type", "scheduler.claimed", "--limit", 2,
                                "--after", second["next"])
            self.assertEqual((len(first["events"]), len(second["events"]), len(third["events"])), (2, 2, 1))
            self.assertEqual(third["next"], None)
            self.assertEqual([event["type"] for event in first["events"] + second["events"] + third["events"]],
                             ["scheduler.claimed"] * 5)

    def test_defaults_and_argument_bounds(self):
        ledger = create_ledger(self.path)
        for index in range(60):
            ledger.seed_request("req-%02d" % index, "repo", CAPS)
            ledger.stop("req-%02d" % index, "hold")
        expected = list(ledger.events)
        ledger.close()
        self.assertEqual(len(expected), 120)
        with self.subTest(case="the default page is 100"):
            result = self.events()
            self.assertEqual(result["events"], expected[:100])
            self.assertEqual((result["next"], result["matched"], result["total"]), (100, 120, 120))
            result = self.events("--after", 100)
            self.assertEqual((result["events"], result["next"]), (expected[100:], None))
        with self.subTest(case="the largest page"):
            self.assertEqual(self.events("--limit", 10000)["events"], expected)
        for label, argv in (("limit 0", ("--limit", "0")), ("limit above the ceiling", ("--limit", "10001")),
                            ("limit not an integer", ("--limit", "x")), ("limit with a sign", ("--limit", "+5")),
                            ("limit with underscores", ("--limit", "1_0")), ("after negative", ("--after", "-1")),
                            ("after a float", ("--after", "1.5")), ("after with a space", ("--after", " 5")),
                            ("an unknown type", ("--type", "scheduler.claim")), ("an empty type", ("--type", "")),
                            ("an unknown option", ("--since", "1"))):
            with self.subTest(case=label):
                self.argument_error(self.path, *argv)
        with self.subTest(case="no ledger argument"):
            self.argument_error()
        with self.subTest(case="an unknown type is refused before the ledger is opened"):
            self.argument_error(str(self.work / "missing.db"), "--type", "nope")
        self.assertEqual(set(ledger_cli._EVENT_TYPES), EVENT_TYPES)
        self.assertEqual(len(ledger_cli._EVENT_TYPES), 16)

    def test_refusals(self):
        ops, _expected, _requests = self.ledger_with_scenario()
        self.events("--request", "req-1", "--operation", ops[0])  # legal baseline
        with self.subTest(case="an unknown request"):
            self.refused("E_REQUEST_UNKNOWN", self.path, "--request", "req-9")
        with self.subTest(case="an unknown operation"):
            self.refused("E_OPERATION_UNKNOWN", self.path, "--operation", "op-" + "f" * 32)
            self.refused("E_OPERATION_UNKNOWN", self.path, "--operation", "")
        with self.subTest(case="the request is checked before the operation"):
            self.refused("E_REQUEST_UNKNOWN", self.path, "--request", "req-9", "--operation", "op-x")
        with self.subTest(case="a missing ledger"):
            self.refused("E_LEDGER_MISSING", str(self.work / "missing.db"))
        with self.subTest(case="a ledger that is not one"):
            other = self.work / "notes.txt"
            other.write_text("not a ledger")
            self.refused("E_LEDGER_OPEN", str(other))

    def test_compaction_keeps_the_history_and_reports_the_checkpoint(self):
        _ops, expected, _requests = self.ledger_with_scenario()
        before = self.events("--limit", 10000)
        self.assertIsNone(before["checkpoint"])
        code, out, err = smctl("ledger", "compact", self.path)
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(rows_of(self.path), [])
        after = self.events("--limit", 10000)
        self.assertEqual(after["checkpoint"], CALLS)
        self.assertEqual({key: value for key, value in after.items() if key != "checkpoint"},
                         {key: value for key, value in before.items() if key != "checkpoint"},
                         "the compaction changed the history the command reads")
        self.assertEqual(after["events"], expected)
        # A command after the compaction: a new event, the checkpoint stays where it was.
        code, out, err = smctl("ledger", "stop", self.path, "--request", "req-2", "--reason", "hold")
        self.assertEqual((code, err), (0, ""), out)
        later = self.events("--after", len(expected) - 1, "--limit", 10000)
        self.assertEqual((later["checkpoint"], later["seq"], later["total"]), (CALLS, CALLS + 1, len(expected) + 1))
        self.assertEqual([(event["seq"], event["type"]) for event in later["events"]],
                         [(len(expected), expected[-1]["type"]), (len(expected) + 1, "request.stopped")])
        self.assertEqual(later["events"][-1]["payload"], {"request_id": "req-2", "reason": "hold"})
        # ``status`` did not change shape: the checkpoint is reported here only.
        code, out, err = smctl("ledger", "status", self.path)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(set(json.loads(out)), {"ledger", "generation", "seq", "requests"})

    def test_the_checkpoint_accessor(self):
        ledger = create_ledger(self.path)
        self.assertIsNone(ledger.checkpoint_seq)
        ops = scenario(ledger)
        self.assertIsNone(ledger.checkpoint_seq)
        self.assertEqual(ledger.compact()["seq"], CALLS)
        self.assertEqual(ledger.checkpoint_seq, CALLS)
        ledger.stop("req-2", "hold")
        self.assertEqual((ledger.checkpoint_seq, ledger.seq), (CALLS, CALLS + 1))
        ledger.close()
        self.assertEqual(self.code_of(ledger), "E_LEDGER_UNAVAILABLE")
        reopened = open_ledger(self.path)
        self.assertEqual((reopened.checkpoint_seq, reopened.seq), (CALLS, CALLS + 1))
        self.assertEqual(reopened.compact()["removed"], 1)
        self.assertEqual(reopened.checkpoint_seq, CALLS + 1)
        reopened.close()
        with open_ledger(self.path) as again:
            self.assertEqual(again.checkpoint_seq, CALLS + 1)
        fresh = str(self.work / "fresh.db")
        with create_ledger(fresh) as new:
            self.assertIsNone(new.checkpoint_seq)
        with open_ledger(fresh) as new:
            self.assertIsNone(new.checkpoint_seq)
        self.assertEqual(len(ops), 5)

    def code_of(self, ledger):
        with self.assertRaises(LedgerError) as ctx:
            ledger.checkpoint_seq
        return ctx.exception.code

    def test_the_command_never_writes(self):
        ops, _expected, _requests = self.ledger_with_scenario()
        before = (digest_of(self.path), rows_of(self.path), sorted(os.listdir(self.work)))
        self.events("--limit", 10000)
        self.events("--request", "req-1", "--operation", ops[0], "--type", "scheduler.claimed", "--after", 1,
                    "--limit", 1)
        self.assertEqual((digest_of(self.path), rows_of(self.path), sorted(os.listdir(self.work))), before,
                         "reading the events wrote something")
        with open_ledger(self.path) as ledger:
            self.assertEqual(ledger.seq, CALLS)

    def test_the_event_types_are_the_schedulers(self):
        source = Path(scheduler_module.__file__).read_text(encoding="utf-8")
        emitted = set(re.findall('_emit[(][^,]+, "([a-z_.]+)"', source))
        self.assertEqual(emitted, EVENT_TYPES, "the Scheduler emits a type the command does not offer, or vice versa")
        self.assertEqual(set(ledger_cli._EVENT_TYPES), emitted)

    def test_the_entry_point(self):
        ops, expected, _requests = self.ledger_with_scenario()
        done = subprocess.run([PY, "-m", "sentinelmesh.cli", "ledger", "events", self.path, "--operation", ops[0],
                               "--limit", "2"], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stdout)
        result = json.loads(done.stdout)
        self.assertEqual(set(result), KEYS)
        self.assertEqual(result["events"], [event for event in expected if event["operation_id"] == ops[0]][:2])
        self.assertEqual(result["next"], result["events"][-1]["seq"])
        done = subprocess.run([PY, "-m", "sentinelmesh.cli", "ledger", "events", "--help"], cwd=str(REPO_ROOT),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        for option in ("--request", "--operation", "--type", "--after", "--limit", "controller.fenced"):
            self.assertIn(option, done.stdout)

    def test_importing_has_no_side_effect(self):
        done = subprocess.run([PY, "-c", "import sys, sentinelmesh.ledger_cli, sentinelmesh.durable_ledger; "
                                         "assert 'ctypes' not in sys.modules; print('IMPORT_SAFE_OK')"],
                              cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "IMPORT_SAFE_OK"), done.stderr)
        self.assertTrue(callable(ContractError))


if __name__ == "__main__":
    unittest.main()
