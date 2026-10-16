"""Planner oracle for ledger-cli (`.agent/design/ledger-cli.md`).

Planner-provided; executors must not modify this file.

The operator steps after a crash -- look, enter usage, resolve, resume -- had
to be written in Python.  The user decided (2026-10-09) to keep Python and to
add ``smctl ledger`` with five commands (status, stop, settle, resolve,
resume); a resume names every stop reason by hand (no shortcut), and a CLI
write while a controller holds the ledger keeps the fail-closed behaviour.
Planner decisions C1-C5 add ``request_ids`` and ``operation_usage`` read
methods, exit codes (0, 3 for a ledger refusal with a JSON error on stderr,
2 for argparse) and the JSON shapes.
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
from unittest import mock

from sentinelmesh import cli, ledger_cli
from sentinelmesh.durable_ledger import LedgerError, create_ledger, open_ledger
from sentinelmesh.scheduler import ContractError, Scheduler

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPS = {"dispatches": 9, "tokens": 500}
RESERVATION = {"dispatches": 1, "tokens": 40}
EVIDENCE, CANDIDATE = "e" * 64, "d" * 64
OPERATOR, NOTE = "ops-1", "checked the process table and the worktree"


class Key(str):
    """A ``str`` subclass: equal to the text it spells, but not exact text."""


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


class CliCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="ledger-cli-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.path = str(self.work / "ledger.db")

    def descriptor(self, op):
        return {"spec": {"request_id": "req-1", "run_id": op, "command": ["/usr/bin/true"], "cwd": str(self.work),
                         "environment_sha256": "a" * 64, "timeout_seconds": 30},
                "evidence_dir": str(self.work), "worktree_id": "wt-1"}

    def crashed(self):
        """A ledger as a crash and the restart leave it: an undecided older handoff,
        a supervised START_UNKNOWN run with unknown tokens, a run in review."""
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        for task in ("task-1", "task-2", "task-3"):
            ledger.seed_task("req-1", task, CAPS)
        ops = {}
        for name, task in (("review", "task-3"), ("undecided", "task-2"), ("unknown", "task-1")):
            ops[name] = ledger.claim("req-1", task, 1, "idem-" + task, "owner-1", 1, 1, RESERVATION,
                                     ["src/%s.py" % task])["operation_id"]
        ledger.handoff(ops["review"], "owner-1", 1)
        ledger.mark_running(ops["review"], "owner-1", 1)
        ledger.settle(ops["review"], {"tokens": 5})
        ledger.finish(ops["review"], "owner-1", 1, CANDIDATE, True)
        ledger.handoff(ops["undecided"], "owner-1", 1)
        ledger.handoff(ops["unknown"], "owner-1", 1, run=self.descriptor(ops["unknown"]))
        ledger.unknown(ops["unknown"], "owner-1", 1)
        ledger.rollover()
        ledger.close()
        return ops

    def ok(self, *argv):
        code, out, err = smctl(*argv)
        self.assertEqual((code, err), (0, ""), "smctl %s failed" % (argv,))
        self.assertTrue(out.endswith("\n") and out.count("\n") == 1, "one JSON line expected")
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


class StatusOracle(CliCase):
    def test_status_shows_the_holds_and_the_operations_that_need_an_operator(self):
        ops = self.crashed()
        before = digest_of(self.path)
        status = self.ok("ledger", "status", self.path)
        self.assertEqual(digest_of(self.path), before, "status wrote to the ledger")
        with open_ledger(self.path) as ledger:
            self.assertEqual((status["ledger"], status["generation"], status["seq"]), (self.path, 2, ledger.seq))
            self.assertEqual(len(status["requests"]), 1)
            request = status["requests"][0]
            self.assertEqual(request["request_id"], "req-1")
            self.assertIs(request["stopped"], True)
            # The review finished with its usage known before the other run went
            # unknown, so the budget guard never fired: two reasons.
            self.assertEqual(request["stop_reasons"], ["START_UNKNOWN", "RECOVERY_REQUIRED"])
            self.assertEqual(request["snapshot"], ledger.snapshot("req-1"))
            attention = {entry["operation_id"]: entry for entry in request["attention"]}
            self.assertEqual(list(attention), [ops["undecided"], ops["unknown"]], "not in claim order, or too many")
            self.assertEqual(attention[ops["unknown"]], {
                "operation_id": ops["unknown"], "task_id": "task-1", "state": "START_UNKNOWN", "generation": 1,
                "supervised": True, "usage": ledger.operation_usage(ops["unknown"]),
                "reasons": ["start_unknown", "usage_unknown"]})
            self.assertEqual(attention[ops["undecided"]], {
                "operation_id": ops["undecided"], "task_id": "task-2", "state": "HANDOFF_ACCEPTED", "generation": 1,
                "supervised": False, "usage": ledger.operation_usage(ops["undecided"]), "reasons": ["undecided"]})

    def test_a_running_request_has_nothing_to_attend(self):
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        ledger.seed_request("req-2", "repo-2", CAPS, concurrency=4)
        ledger.close()
        status = self.ok("ledger", "status", self.path)
        self.assertEqual([(r["request_id"], r["stopped"], r["stop_reasons"], r["attention"]) for r in status["requests"]],
                         [("req-1", False, [], []), ("req-2", False, [], [])])


class OperatorWorkflowOracle(CliCase):
    def test_the_operator_takes_a_crashed_request_back_with_the_cli_alone(self):
        ops = self.crashed()
        # The undecided handoff is the controller's to recover at restart; here it
        # is taken over as unstarted from its (empty) records, as the driver would.
        with open_ledger(self.path) as ledger:
            ledger.recover(ops["undecided"], "owner-2", 2, outcome="NOT_STARTED", evidence_hash=EVIDENCE)
        self.refused("E_RESOLVE_CONFLICT", "ledger", "resolve", self.path, "--operation", ops["unknown"],
                     "--resolution", "NOT_STARTED", "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)
        settled = self.ok("ledger", "settle", self.path, "--operation", ops["unknown"], "--dimension", "tokens",
                          "--amount", 9, "--evidence", EVIDENCE)
        self.assertEqual((settled["command"], settled["operation_id"], settled["dimension"], settled["result"]),
                         ("settle", ops["unknown"], "tokens", "APPLIED"))
        self.assertEqual((settled["usage"]["tokens"]["amount"], settled["usage"]["tokens"]["final"]), (9, True))
        resolved = self.ok("ledger", "resolve", self.path, "--operation", ops["unknown"], "--resolution", "ABANDONED",
                           "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)
        self.assertEqual((resolved["command"], resolved["operation"]["state"]), ("resolve", "ABANDONED"))
        reasons = ["START_UNKNOWN", "RECOVERY_REQUIRED"]
        argv = ["ledger", "resume", self.path, "--request", "req-1"]
        for reason in reasons:
            argv += ["--reason", reason]
        resumed = self.ok(*argv, "--operator", OPERATOR, "--note", NOTE)
        self.assertEqual(resumed, {"command": "resume", "request_id": "req-1", "previous_reasons": reasons})
        status = self.ok("ledger", "status", self.path)
        self.assertEqual((status["requests"][0]["stopped"], status["requests"][0]["attention"]), (False, []))
        with open_ledger(self.path) as ledger:
            again = ledger.claim("req-1", "task-1", 1, "idem-task-1-again", "owner-2", 2, 1, RESERVATION,
                                 ["src/task-1.py"])["operation_id"]
            self.assertEqual(ledger.operation(again)["state"], "RESERVED")
            self.assertEqual(ledger.events[-2]["type"], "request.resumed")
        # A claim of the current generation is not something to attend to.
        status = self.ok("ledger", "status", self.path)
        self.assertEqual(status["requests"][0]["attention"], [])

    def test_every_stop_reason_is_typed_by_hand(self):
        self.crashed()
        before = digest_of(self.path)
        code, out, err = smctl("ledger", "resume", self.path, "--request", "req-1", "--operator", OPERATOR,
                               "--note", NOTE)
        self.assertEqual((code, out), (2, ""), "a resume without --reason must be a usage error")
        self.assertEqual(digest_of(self.path), before)
        self.refused("E_STOP_REASON_CHANGED", "ledger", "resume", self.path, "--request", "req-1",
                     "--reason", "RECOVERY_REQUIRED", "--operator", OPERATOR, "--note", NOTE)

    def test_stop_puts_an_explicit_hold_each_time(self):
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        ledger.close()
        first = self.ok("ledger", "stop", self.path, "--request", "req-1", "--reason", "SECURITY_HOLD")
        self.assertEqual(first, {"command": "stop", "request_id": "req-1", "stop_reasons": ["SECURITY_HOLD"]})
        second = self.ok("ledger", "stop", self.path, "--request", "req-1", "--reason", "SECURITY_HOLD")
        self.assertEqual(second["stop_reasons"], ["SECURITY_HOLD", "SECURITY_HOLD"])
        self.ok("ledger", "resume", self.path, "--request", "req-1", "--reason", "SECURITY_HOLD",
                "--reason", "SECURITY_HOLD", "--operator", OPERATOR, "--note", NOTE)


class RefusalOracle(CliCase):
    def test_refusals_are_exit_3_with_the_ledger_error(self):
        ops = self.crashed()
        self.refused("E_OPERATION_UNKNOWN", "ledger", "resolve", self.path, "--operation", "op-" + "0" * 32,
                     "--resolution", "ABANDONED", "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)
        self.refused("E_RESOLVE_STATE", "ledger", "resolve", self.path, "--operation", ops["review"],
                     "--resolution", "ABANDONED", "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)
        self.refused("E_NOTE", "ledger", "resolve", self.path, "--operation", ops["unknown"],
                     "--resolution", "ABANDONED", "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", "a\nb")
        self.refused("E_OBSERVATION_SHAPE", "ledger", "settle", self.path, "--operation", ops["unknown"],
                     "--dimension", "tokens", "--amount", -1, "--evidence", EVIDENCE)
        self.refused("E_REQUEST_UNKNOWN", "ledger", "stop", self.path, "--request", "req-9", "--reason", "HOLD")
        code, out, err = smctl("ledger", "resolve", self.path, "--operation", ops["unknown"], "--resolution",
                               "RECOVERED", "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)
        self.assertEqual((code, out), (2, ""), "an unknown resolution must be a usage error")

    def test_a_ledger_that_cannot_be_opened_is_refused(self):
        self.refused("E_LEDGER_MISSING", "ledger", "status", str(self.work / "missing.db"))
        self.assertFalse((self.work / "missing.db").exists(), "the CLI created a ledger")
        self.crashed()
        db = sqlite3.connect(self.path)
        db.execute("UPDATE commands SET sha256=? WHERE seq=1", ("0" * 64,))
        db.commit()
        db.close()
        self.refused("E_LEDGER_CHAIN", "ledger", "status", self.path)

    def test_a_write_while_a_controller_holds_the_ledger_is_caught_up_by_the_controller(self):
        # ledger-catch-up (2026-10-16): the old assertion -- the controller's next write is
        # E_LEDGER_STALE -- is wrong now; the controller replays the operator's stop first.
        self.crashed()
        controller = open_ledger(self.path)
        self.addCleanup(controller.close)
        self.ok("ledger", "stop", self.path, "--request", "req-1", "--reason", "SECURITY_HOLD")
        controller.stop("req-1", "OPERATOR_STOP")
        with open_ledger(self.path) as reopened:
            self.assertEqual(reopened.stop_reasons("req-1")[-2:], ("SECURITY_HOLD", "OPERATOR_STOP"))
            self.assertEqual(reopened.seq, controller.seq)


class ClosingOracle(CliCase):
    def test_every_instance_a_command_opens_is_closed(self):
        # Planner completion after the review of attempt-001 (L4): CPython frees
        # an instance nobody holds, so counting descriptors cannot see a missing
        # close; the opened instances are kept here instead.
        ops = self.crashed()
        opened = []
        real = ledger_cli.open_ledger

        def tracking(path):
            ledger = real(path)
            opened.append(ledger)
            return ledger

        with mock.patch.object(ledger_cli, "open_ledger", tracking):
            self.ok("ledger", "status", self.path)
            self.ok("ledger", "stop", self.path, "--request", "req-1", "--reason", "SECURITY_HOLD")
            self.refused("E_RESOLVE_CONFLICT", "ledger", "resolve", self.path, "--operation", ops["unknown"],
                         "--resolution", "NOT_STARTED", "--operator", OPERATOR, "--evidence", EVIDENCE,
                         "--note", NOTE)
        self.assertEqual(len(opened), 3)
        for ledger in opened:
            with self.assertRaises(LedgerError) as ctx:
                ledger.request_ids()
            self.assertEqual(ctx.exception.code, "E_LEDGER_UNAVAILABLE", "a command left its ledger open")

    @unittest.skipIf(os.name != "posix", "descriptor census through /dev/fd or /proc/self/fd; Windows stays NOT_RUN")
    def test_every_command_closes_the_ledger(self):
        ops = self.crashed()
        fd_dir = "/dev/fd" if os.path.isdir("/dev/fd") else "/proc/self/fd"
        smctl("ledger", "status", self.path)
        before = len(os.listdir(fd_dir))
        for _ in range(3):
            self.ok("ledger", "status", self.path)
            self.ok("ledger", "stop", self.path, "--request", "req-1", "--reason", "SECURITY_HOLD")
            self.refused("E_RESOLVE_CONFLICT", "ledger", "resolve", self.path, "--operation", ops["unknown"],
                         "--resolution", "NOT_STARTED", "--operator", OPERATOR, "--evidence", EVIDENCE,
                         "--note", NOTE)
            self.refused("E_LEDGER_MISSING", "ledger", "status", str(self.work / "missing.db"))
        self.assertEqual(len(os.listdir(fd_dir)), before, "a command left the ledger open")


class CompletionOracle(CliCase):
    """Planner completion after the review of attempt-001 (L1, L2, L3, L4)."""

    def test_a_non_final_amount_is_still_unknown_usage(self):
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        ledger.seed_task("req-1", "task-1", CAPS)
        op = ledger.claim("req-1", "task-1", 1, "idem-task-1", "owner-1", 1, 1, RESERVATION,
                          ["src/task-1.py"])["operation_id"]
        ledger.handoff(op, "owner-1", 1)
        ledger.mark_running(op, "owner-1", 1)
        ledger.observe(op, "tokens", "call-1", 1, 5, False, EVIDENCE)
        ledger.finish(op, "owner-1", 1, CANDIDATE, True)
        ledger.close()
        attention = self.ok("ledger", "status", self.path)["requests"][0]["attention"]
        self.assertEqual([(entry["operation_id"], entry["reasons"]) for entry in attention], [(op, ["usage_unknown"])])
        self.assertIs(attention[0]["usage"]["tokens"]["final"], False)

    def test_the_resume_reports_what_the_ledger_reports_first(self):
        self.crashed()
        self.refused("E_NOTE", "ledger", "resume", self.path, "--request", "req-9", "--reason", "X",
                     "--operator", OPERATOR, "--note", "a\nb")
        self.refused("E_OPERATOR", "ledger", "resume", self.path, "--request", "req-9", "--reason", "X",
                     "--operator", "", "--note", NOTE)

    def test_an_empty_evidence_is_refused_not_derived(self):
        ops = self.crashed()
        self.refused("E_OBSERVATION_SHAPE", "ledger", "settle", self.path, "--operation", ops["unknown"],
                     "--dimension", "tokens", "--amount", 9, "--evidence", "")
        with open_ledger(self.path) as ledger:
            self.assertNotIn("tokens", ledger.operation_usage(ops["unknown"]))

    def test_an_ascii_terminal_does_not_fail_a_committed_command(self):
        ledger = create_ledger(self.path)
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        ledger.close()
        env = dict(os.environ, PYTHONIOENCODING="ascii")
        done = subprocess.run([sys.executable, "-m", "sentinelmesh.cli", "ledger", "stop", self.path,
                               "--request", "req-1", "--reason", "安全暂停"],
                              cwd=str(REPO_ROOT), capture_output=True, env=env, timeout=120)
        self.assertEqual((done.returncode, done.stderr), (0, b""))
        self.assertEqual(json.loads(done.stdout.decode("utf-8"))["stop_reasons"], ["安全暂停"])
        with open_ledger(self.path) as reopened:
            self.assertEqual(reopened.stop_reasons("req-1"), ("安全暂停",))


class ReadMethodsOracle(CliCase):
    def test_request_ids_and_operation_usage(self):
        scheduler = Scheduler()
        scheduler.seed_request("req-b", "repo-1", CAPS)
        scheduler.seed_request("req-a", "repo-1", CAPS)
        self.assertEqual(scheduler.request_ids(), ("req-b", "req-a"), "not in the order the requests were seeded")
        scheduler.seed_task("req-b", "task-1", CAPS)
        op = scheduler.claim("req-b", "task-1", 1, "k", "owner-1", 1, 1, RESERVATION, ["src/a.py"])["operation_id"]
        self.assertEqual(scheduler.operation_usage(op), {})
        scheduler.handoff(op, "owner-1", 1)
        scheduler.settle(op, "tokens", amount=7, evidence_hash=EVIDENCE)
        usage = scheduler.operation_usage(op)
        self.assertEqual(set(usage), {"dispatches", "tokens"})
        self.assertEqual(usage["tokens"], {"call_key": "local:%s:tokens" % op, "revision": 1, "amount": 7,
                                           "final": True, "evidence_hash": EVIDENCE})
        usage["tokens"]["amount"] = 0
        self.assertEqual(scheduler.operation_usage(op)["tokens"]["amount"], 7, "the read shares state with the ledger")
        events = scheduler.events
        for value in ("op-" + "0" * 32, None, [op], Key(op)):
            with self.subTest(operation=repr(value)):
                with self.assertRaises(ContractError) as ctx:
                    scheduler.operation_usage(value)
                self.assertEqual(ctx.exception.code, "E_OPERATION_UNKNOWN")
        self.assertEqual(scheduler.events, events, "a read recorded an event")

    def test_the_durable_ledger_reads_them_back(self):
        ops = self.crashed()
        with open_ledger(self.path) as ledger:
            self.assertEqual(ledger.request_ids(), ("req-1",))
            usage = ledger.operation_usage(ops["review"])
        self.assertEqual(usage["tokens"]["amount"], 5)
        ledger.close()
        for read in (ledger.request_ids, lambda: ledger.operation_usage(ops["review"])):
            with self.assertRaises(LedgerError) as ctx:
                read()
            self.assertEqual(ctx.exception.code, "E_LEDGER_UNAVAILABLE")


class EntryPointOracle(CliCase):
    def test_the_installed_entry_point_runs_the_status(self):
        self.crashed()
        done = subprocess.run([sys.executable, "-m", "sentinelmesh.cli", "ledger", "status", self.path],
                              cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        status = json.loads(done.stdout)
        self.assertEqual((status["generation"], status["requests"][0]["stopped"]), (2, True))
        refused = subprocess.run([sys.executable, "-m", "sentinelmesh.cli", "ledger", "status",
                                  str(self.work / "missing.db")], cwd=str(REPO_ROOT), capture_output=True,
                                 text=True, timeout=120)
        self.assertEqual((refused.returncode, refused.stdout), (3, ""))
        self.assertEqual(json.loads(refused.stderr)["error"], "E_LEDGER_MISSING")


if __name__ == "__main__":
    unittest.main()
