"""Executor tests for ledger-cli: ``smctl ledger`` and the two read methods.

These cover what the planner oracle (``tests/test_ledger_cli_oracle.py``) leaves
out, following one rule throughout: every rejection case first proves a legal
baseline -- the very same command with nothing but the value under test changed
-- and then asserts the specific error, so a different rule refusing the command
first cannot hide a gap.  Every refusal is also checked against the ledger file
it found, so a command that reports a refusal while writing something is caught.

The behaviour under test comes from `.agent/design/ledger-cli.md` sections 5 and
6: the operator entry point has five commands (status, stop, settle, resolve,
resume), a resume names every stop reason by hand with no shortcut, a CLI write
while a controller holds the ledger keeps the fail-closed behaviour, and the
scheduler and the durable ledger each gain ``request_ids`` and ``operation_usage``
as read-only methods.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import cli
from sentinelmesh.durable_ledger import LedgerError, create_ledger, open_ledger
from sentinelmesh.scheduler import ContractError, Scheduler

CAPS = {"dispatches": 9, "tokens": 500}
RESERVATION = {"dispatches": 1, "tokens": 40}
EVIDENCE, CANDIDATE = "e" * 64, "d" * 64
OPERATOR, NOTE = "ops-1", "checked the process table and the worktree"
#: The stop reason an explicit hold carries in these tests.
HOLD = "SECURITY_HOLD"


class LedgerCliCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="ledger-cli-")).resolve()
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

    def ok(self, *argv):
        """A command the ledger accepts: one JSON line on stdout, nothing else."""
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
        return hashlib.sha256(Path(self.path).read_bytes()).hexdigest()

    # -- building ledgers -------------------------------------------------- #
    def build(self, *, requests=("req-1",), tasks=("task-1", "task-2", "task-3")):
        """A ledger with the requests and tasks named, open for more commands."""
        ledger = create_ledger(self.path)
        for request in requests:
            ledger.seed_request(request, "repo-1", CAPS, concurrency=4)
            for task in tasks:
                ledger.seed_task(request, task, CAPS)
        return ledger

    def claim(self, ledger, request, task, key):
        return ledger.claim(request, task, 1, key, "owner-1", 1, 1, RESERVATION,
                            ["src/%s.py" % task])["operation_id"]

    def states(self):
        """One request, three runs: unknown, in review with incomplete usage, running.

        The three are claimed and handed off before the first ``unknown``: an
        unknown start stops the request, and a stopped request refuses every
        further claim and handoff.
        """
        ledger = self.build()
        ops = {}
        for name, task in (("unknown", "task-1"), ("review", "task-2"), ("running", "task-3")):
            ops[name] = self.claim(ledger, "req-1", task, "idem-" + task)
        for name in ("unknown", "review", "running"):
            ledger.handoff(ops[name], "owner-1", 1)
        ledger.unknown(ops["unknown"], "owner-1", 1)
        ledger.mark_running(ops["review"], "owner-1", 1)
        ledger.finish(ops["review"], "owner-1", 1, CANDIDATE, True)
        ledger.mark_running(ops["running"], "owner-1", 1)
        ledger.close()
        return ops

    def two_unknown(self):
        """Two runs nobody could classify, so one can be the baseline of the other."""
        ledger = self.build(tasks=("task-1", "task-2"))
        ops = {}
        for name, task in (("first", "task-1"), ("second", "task-2")):
            ops[name] = self.claim(ledger, "req-1", task, "idem-" + task)
        for name in ("first", "second"):
            ledger.handoff(ops[name], "owner-1", 1)
        for name in ("first", "second"):
            ledger.unknown(ops[name], "owner-1", 1)
        ledger.close()
        return ops

    def attention(self, status, request="req-1"):
        for entry in status["requests"]:
            if entry["request_id"] == request:
                return {item["operation_id"]: item for item in entry["attention"]}
        raise AssertionError("no request %r in the status" % request)


class ReadMethodsCase(LedgerCliCase):
    def test_the_usage_of_an_operation_survives_a_correction(self):
        ops = self.states()
        self.ok("ledger", "settle", self.path, "--operation", ops["review"], "--dimension", "tokens",
                "--amount", 5, "--evidence", EVIDENCE)
        corrected = self.ok("ledger", "settle", self.path, "--operation", ops["review"], "--dimension",
                            "tokens", "--amount", 3, "--revision", 2, "--evidence", EVIDENCE, "--correction")
        with open_ledger(self.path) as ledger:
            usage = ledger.operation_usage(ops["review"])
            # The dispatch the handoff recorded is part of the usage, and the
            # corrected dimension carries the revision and the amount of the
            # correction, not of the observation it replaced.
            self.assertEqual(set(usage), {"dispatches", "tokens"})
            self.assertEqual((usage["tokens"]["amount"], usage["tokens"]["revision"], usage["tokens"]["final"]),
                             (3, 2, True))
            self.assertEqual(usage["tokens"]["call_key"], "local:%s:tokens" % ops["review"])
            self.assertEqual(usage["dispatches"]["amount"], 1)
            self.assertEqual(usage["dispatches"]["final"], True)
            self.assertEqual(ledger.request_ids(), ("req-1",))
            # An operation whose only observation is the dispatch its handoff
            # recorded reads back that one dimension and nothing else.
            self.assertEqual(set(ledger.operation_usage(ops["running"])), {"dispatches"})

    def test_the_read_usage_is_a_copy_of_what_the_ledger_keeps(self):
        ops = self.states()
        with open_ledger(self.path) as ledger:
            usage = ledger.operation_usage(ops["review"])
            usage["dispatches"]["amount"] = 99
            usage["tokens"] = {"call_key": "forged", "revision": 9, "amount": 9, "final": True,
                               "evidence_hash": EVIDENCE}
            again = ledger.operation_usage(ops["review"])
        self.assertEqual(again["dispatches"]["amount"], 1, "the read shares state with the ledger")
        self.assertNotIn("tokens", again, "the read shares state with the ledger")

    def test_request_ids_keep_the_seeding_order_through_a_rollover(self):
        ledger = self.build(requests=("req-b", "req-a"), tasks=("task-1",))
        ledger.rollover()
        ledger.close()
        with open_ledger(self.path) as reopened:
            self.assertEqual(reopened.request_ids(), ("req-b", "req-a"))

    def test_the_reads_of_a_closed_ledger_are_refused(self):
        ops = self.states()
        ledger = open_ledger(self.path)
        ledger.close()
        for read in (ledger.request_ids, lambda: ledger.operation_usage(ops["review"])):
            with self.assertRaises(LedgerError) as ctx:
                read()
            self.assertEqual(ctx.exception.code, "E_LEDGER_UNAVAILABLE")

    def test_a_read_records_no_event(self):
        ops = self.states()
        with open_ledger(self.path) as ledger:
            before = len(ledger.events)
            ledger.request_ids()
            ledger.operation_usage(ops["review"])
            self.assertEqual(len(ledger.events), before)


class StatusAttentionCase(LedgerCliCase):
    def test_an_abandoned_run_with_incomplete_usage_is_usage_unknown(self):
        ops = self.states()
        # The legal baseline: the run is decided as abandoned, which is what the
        # state under test is.  Only the incomplete usage is left.
        resolved = self.ok("ledger", "resolve", self.path, "--operation", ops["unknown"], "--resolution",
                           "ABANDONED", "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)
        self.assertEqual(resolved["operation"]["state"], "ABANDONED")
        attention = self.attention(self.ok("ledger", "status", self.path))
        self.assertEqual(attention[ops["unknown"]]["reasons"], ["usage_unknown"])
        self.assertEqual(attention[ops["unknown"]]["state"], "ABANDONED")

    def test_a_run_in_review_with_incomplete_usage_is_usage_unknown(self):
        ops = self.states()
        attention = self.attention(self.ok("ledger", "status", self.path))
        self.assertEqual(attention[ops["review"]]["reasons"], ["usage_unknown"])
        self.assertEqual(attention[ops["review"]]["state"], "REVIEW_PENDING")
        # The usage reported is the incomplete one, dimension by dimension.
        self.assertEqual(set(attention[ops["review"]]["usage"]), {"dispatches"})

    def test_a_running_run_of_the_current_generation_is_not_listed(self):
        ops = self.states()
        status = self.ok("ledger", "status", self.path)
        attention = self.attention(status)
        self.assertEqual(list(attention), [ops["unknown"], ops["review"]], "not in claim order, or too many")
        self.assertNotIn(ops["running"], attention)
        with open_ledger(self.path) as ledger:
            view = ledger.operation(ops["running"])
            self.assertEqual((view["state"], view["generation"]), ("RUNNING", ledger.generation))

    def test_a_live_run_of_an_older_generation_is_undecided(self):
        ops = self.states()
        with open_ledger(self.path) as ledger:
            ledger.rollover()
            ledger.close()
        attention = self.attention(self.ok("ledger", "status", self.path))
        self.assertEqual(attention[ops["running"]]["reasons"], ["undecided"])
        self.assertEqual((attention[ops["running"]]["state"], attention[ops["running"]]["generation"]),
                         ("RUNNING", 1))

    def test_several_requests_are_attended_to_independently(self):
        ledger = self.build(requests=("req-1", "req-2"), tasks=("task-1", "task-2"))
        ops = {}
        # A resource key is held per repository, so each request works on its own
        # file: two requests claiming the same key would be a conflict, not a
        # second request to attend to.
        for request, task in (("req-1", "task-1"), ("req-2", "task-2")):
            ops[request] = self.claim(ledger, request, task, "idem-" + request)
        for request in ("req-1", "req-2"):
            ledger.handoff(ops[request], "owner-1", 1)
        ledger.rollover()
        ledger.close()
        status = self.ok("ledger", "status", self.path)
        self.assertEqual([entry["request_id"] for entry in status["requests"]], ["req-1", "req-2"])
        self.assertEqual([entry["stop_reasons"] for entry in status["requests"]],
                         [["RECOVERY_REQUIRED"], ["RECOVERY_REQUIRED"]])
        for request in ("req-1", "req-2"):
            attention = self.attention(status, request)
            self.assertEqual(list(attention), [ops[request]],
                             "%s attends to another request's operation" % request)
            self.assertEqual(attention[ops[request]]["reasons"], ["undecided"])

    def test_a_request_with_nothing_to_attend_has_an_empty_list(self):
        self.build()
        status = self.ok("ledger", "status", self.path)
        self.assertEqual(status["requests"][0]["attention"], [])
        self.assertIs(status["requests"][0]["stopped"], False)
        self.assertEqual(status["requests"][0]["stop_reasons"], [])


class SettleCase(LedgerCliCase):
    def test_a_replay_of_the_same_observation_is_not_applied_twice(self):
        ops = self.states()
        argv = ("ledger", "settle", self.path, "--operation", ops["review"], "--dimension", "tokens",
                "--amount", 5, "--evidence", EVIDENCE)
        self.assertEqual(self.ok(*argv)["result"], "APPLIED")
        again = self.ok(*argv)
        self.assertEqual(again["result"], "REPLAY")
        self.assertEqual(again["usage"]["tokens"]["amount"], 5)

    def test_an_older_revision_is_historical_only(self):
        ops = self.states()
        self.ok("ledger", "settle", self.path, "--operation", ops["review"], "--dimension", "tokens",
                "--amount", 5, "--revision", 2, "--evidence", EVIDENCE)
        older = self.ok("ledger", "settle", self.path, "--operation", ops["review"], "--dimension", "tokens",
                        "--amount", 99, "--revision", 1, "--evidence", EVIDENCE)
        self.assertEqual(older["result"], "HISTORICAL_ONLY")
        self.assertEqual((older["usage"]["tokens"]["amount"], older["usage"]["tokens"]["revision"]), (5, 2))

    def test_a_correction_lowers_a_recorded_amount(self):
        ops = self.states()
        # The legal baseline: record the tokens of the run in review once.
        first = self.ok("ledger", "settle", self.path, "--operation", ops["review"], "--dimension", "tokens",
                        "--amount", 5, "--evidence", EVIDENCE)
        self.assertEqual((first["result"], first["usage"]["tokens"]["amount"]), ("APPLIED", 5))
        # Only the amount changed: a smaller one at a higher revision is refused.
        self.refused("E_USAGE_CORRECTION_REQUIRED", "ledger", "settle", self.path, "--operation",
                     ops["review"], "--dimension", "tokens", "--amount", 3, "--revision", 2,
                     "--evidence", EVIDENCE)
        corrected = self.ok("ledger", "settle", self.path, "--operation", ops["review"], "--dimension",
                            "tokens", "--amount", 3, "--revision", 2, "--evidence", EVIDENCE, "--correction")
        self.assertEqual((corrected["result"], corrected["usage"]["tokens"]["amount"],
                          corrected["usage"]["tokens"]["revision"], corrected["usage"]["tokens"]["final"]),
                         ("APPLIED", 3, 2, True))
        with open_ledger(self.path) as ledger:
            self.assertEqual(ledger.operation_usage(ops["review"])["tokens"]["amount"], 3)
            # The correction moves the held reservation, it does not raise it.
            self.assertEqual(ledger.snapshot("req-1")["tokens"]["known"], 3)

    def test_a_dimension_outside_the_reservation_is_refused(self):
        ops = self.states()
        self.ok("ledger", "settle", self.path, "--operation", ops["review"], "--dimension", "tokens",
                "--amount", 5, "--evidence", EVIDENCE)
        self.refused("E_OBSERVATION_SCOPE", "ledger", "settle", self.path, "--operation", ops["review"],
                     "--dimension", "cpu", "--amount", 5, "--evidence", EVIDENCE)

    def test_the_local_metric_dimension_is_refused(self):
        ops = self.states()
        self.refused("E_LOCAL_METRIC_RESERVED", "ledger", "settle", self.path, "--operation", ops["review"],
                     "--dimension", "dispatches", "--amount", 2, "--evidence", EVIDENCE)

    def test_an_unknown_operation_is_refused(self):
        ops = self.states()
        self.ok("ledger", "settle", self.path, "--operation", ops["review"], "--dimension", "tokens",
                "--amount", 5, "--evidence", EVIDENCE)
        self.refused("E_OPERATION_UNKNOWN", "ledger", "settle", self.path, "--operation", "op-" + "0" * 32,
                     "--dimension", "tokens", "--amount", 5, "--evidence", EVIDENCE)

    def test_an_evidence_that_is_not_a_digest_is_refused(self):
        ops = self.states()
        self.ok("ledger", "settle", self.path, "--operation", ops["review"], "--dimension", "tokens",
                "--amount", 5, "--evidence", EVIDENCE)
        self.refused("E_OBSERVATION_SHAPE", "ledger", "settle", self.path, "--operation", ops["review"],
                     "--dimension", "tokens", "--amount", 6, "--evidence", "not-a-digest")


class ResolveCase(LedgerCliCase):
    def test_a_finished_run_goes_to_review_with_its_candidate(self):
        ops = self.two_unknown()
        # The legal baseline: the very same decision with the candidate it needs.
        resolved = self.ok("ledger", "resolve", self.path, "--operation", ops["first"], "--resolution",
                           "FINISHED", "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE,
                           "--candidate", CANDIDATE)
        self.assertEqual((resolved["command"], resolved["operation"]["state"]), ("resolve", "REVIEW_PENDING"))
        self.assertEqual(resolved["operation"]["operation_id"], ops["first"])
        # Only the candidate is missing now.
        self.refused("E_CLEANUP_OR_CANDIDATE", "ledger", "resolve", self.path, "--operation", ops["second"],
                     "--resolution", "FINISHED", "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)
        # A resolution that takes no candidate refuses one all the same.
        self.refused("E_CLEANUP_OR_CANDIDATE", "ledger", "resolve", self.path, "--operation", ops["second"],
                     "--resolution", "ABANDONED", "--operator", OPERATOR, "--evidence", EVIDENCE,
                     "--note", NOTE, "--candidate", CANDIDATE)

    def test_a_run_that_never_started_keeps_its_reservation(self):
        ops = self.two_unknown()
        resolved = self.ok("ledger", "resolve", self.path, "--operation", ops["first"], "--resolution",
                           "NOT_STARTED", "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)
        self.assertEqual(resolved["operation"]["state"], "NOT_STARTED")
        attention = self.attention(self.ok("ledger", "status", self.path))
        self.assertNotIn(ops["first"], attention, "a proven unstarted run is not something to attend to")
        self.assertIn(ops["second"], attention)

    def test_a_run_the_ledger_saw_start_is_not_unstarted(self):
        ops = self.states()
        with open_ledger(self.path) as ledger:
            ledger.unknown(ops["running"], "owner-1", 1)
        self.refused("E_RESOLVE_CONFLICT", "ledger", "resolve", self.path, "--operation", ops["running"],
                     "--resolution", "NOT_STARTED", "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)
        self.ok("ledger", "resolve", self.path, "--operation", ops["running"], "--resolution", "ABANDONED",
                "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)

    def test_an_evidence_that_is_not_a_digest_is_refused(self):
        ops = self.two_unknown()
        self.ok("ledger", "resolve", self.path, "--operation", ops["first"], "--resolution", "ABANDONED",
                "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)
        self.refused("E_EVIDENCE_HASH", "ledger", "resolve", self.path, "--operation", ops["second"],
                     "--resolution", "ABANDONED", "--operator", OPERATOR, "--evidence", "e" * 63, "--note", NOTE)

    def test_an_operator_or_a_note_the_ledger_refuses_is_reported(self):
        ops = self.two_unknown()
        self.ok("ledger", "resolve", self.path, "--operation", ops["first"], "--resolution", "ABANDONED",
                "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)
        self.refused("E_NOTE", "ledger", "resolve", self.path, "--operation", ops["second"], "--resolution",
                     "ABANDONED", "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", "a\nb")
        self.refused("E_OPERATOR", "ledger", "resolve", self.path, "--operation", ops["second"],
                     "--resolution", "ABANDONED", "--operator", "x" * 201, "--evidence", EVIDENCE, "--note", NOTE)


class StopAndResumeCase(LedgerCliCase):
    def test_a_non_ascii_stop_reason_survives_the_output(self):
        self.build()
        stopped = self.ok("ledger", "stop", self.path, "--request", "req-1", "--reason", "安全暂停")
        self.assertEqual(stopped, {"command": "stop", "request_id": "req-1", "stop_reasons": ["安全暂停"]})
        # The line itself keeps the characters as they were written, not escaped.
        code, out, err = self.smctl("ledger", "status", self.path)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("安全暂停", out)
        with open_ledger(self.path) as ledger:
            self.assertEqual(ledger.stop_reasons("req-1"), ("安全暂停",))
        # And it is named back by hand, in the operator's own words.
        resumed = self.ok("ledger", "resume", self.path, "--request", "req-1", "--reason", "安全暂停",
                          "--operator", OPERATOR, "--note", NOTE)
        self.assertEqual(resumed, {"command": "resume", "request_id": "req-1",
                                   "previous_reasons": ["安全暂停"]})

    def test_an_unknown_request_is_refused_after_a_legal_baseline(self):
        self.build()
        self.assertEqual(self.ok("ledger", "stop", self.path, "--request", "req-1", "--reason", HOLD),
                         {"command": "stop", "request_id": "req-1", "stop_reasons": [HOLD]})
        self.refused("E_REQUEST_UNKNOWN", "ledger", "stop", self.path, "--request", "req-9", "--reason", HOLD)

    def test_an_empty_reason_is_refused(self):
        self.build()
        self.ok("ledger", "stop", self.path, "--request", "req-1", "--reason", HOLD)
        self.refused("E_STOP_REASON", "ledger", "stop", self.path, "--request", "req-1", "--reason", "")

    def test_a_resume_of_a_request_that_is_not_stopped_is_refused(self):
        self.build()
        self.refused("E_NOT_STOPPED", "ledger", "resume", self.path, "--request", "req-1", "--reason", HOLD,
                     "--operator", OPERATOR, "--note", NOTE)
        self.ok("ledger", "stop", self.path, "--request", "req-1", "--reason", HOLD)
        self.ok("ledger", "resume", self.path, "--request", "req-1", "--reason", HOLD, "--operator",
                OPERATOR, "--note", NOTE)

    def test_a_resume_names_every_hold_in_the_order_the_ledger_recorded_them(self):
        self.build()
        for reason in (HOLD, "MAINTENANCE_WINDOW"):
            self.ok("ledger", "stop", self.path, "--request", "req-1", "--reason", reason)
        status = self.ok("ledger", "status", self.path)
        self.assertEqual(status["requests"][0]["stop_reasons"], [HOLD, "MAINTENANCE_WINDOW"])
        # The order is the ledger's, not the one typed: the reversed pair is refused.
        self.refused("E_STOP_REASON_CHANGED", "ledger", "resume", self.path, "--request", "req-1",
                     "--reason", "MAINTENANCE_WINDOW", "--reason", HOLD, "--operator", OPERATOR, "--note", NOTE)
        resumed = self.ok("ledger", "resume", self.path, "--request", "req-1", "--reason", HOLD, "--reason",
                          "MAINTENANCE_WINDOW", "--operator", OPERATOR, "--note", NOTE)
        self.assertEqual(resumed["previous_reasons"], [HOLD, "MAINTENANCE_WINDOW"])
        status = self.ok("ledger", "status", self.path)
        self.assertEqual((status["requests"][0]["stopped"], status["requests"][0]["stop_reasons"]), (False, []))


class CommandGroupCase(LedgerCliCase):
    def test_the_ledger_group_requires_a_subcommand(self):
        self.build()
        for argv in (["ledger"], ["ledger", "frobnicate", self.path]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as ctx:
                        cli.main(argv)
                self.assertEqual(ctx.exception.code, 2)
                self.assertIn("usage:", err.getvalue())

    def test_a_missing_subcommand_argument_is_a_usage_error(self):
        self.build()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["ledger", "settle", self.path, "--operation", "op-1", "--dimension", "tokens",
                          "--evidence", EVIDENCE])
        self.assertEqual(ctx.exception.code, 2)

    def test_a_ledger_that_cannot_be_opened_is_refused_without_being_created(self):
        missing = str(self.work / "missing.db")
        code, out, err = self.smctl("ledger", "status", missing)
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(json.loads(err)["error"], "E_LEDGER_MISSING")
        self.assertFalse(Path(missing).exists(), "the CLI created a ledger")

    def test_a_directory_is_not_a_ledger(self):
        code, out, err = self.smctl("ledger", "status", str(self.work))
        self.assertEqual((code, out), (3, ""))
        self.assertEqual(json.loads(err)["error"], "E_LEDGER_OPEN")

    def test_every_command_closes_the_ledger_it_opened(self):
        ops = self.states()
        for _ in range(2):
            self.ok("ledger", "status", self.path)
            self.ok("ledger", "stop", self.path, "--request", "req-1", "--reason", HOLD)
            self.refused("E_OPERATION_UNKNOWN", "ledger", "settle", self.path, "--operation", "op-" + "0" * 32,
                         "--dimension", "tokens", "--amount", 1, "--evidence", EVIDENCE)
        self.ok("ledger", "resolve", self.path, "--operation", ops["unknown"], "--resolution",
                "ABANDONED", "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)
        # The ledger can still be written afterwards.  (An instance left open
        # would not prevent that in DELETE journal mode; that every command
        # closes its instance is pinned by the oracle, which keeps each one.)
        with open_ledger(self.path) as ledger:
            ledger.stop("req-1", "OPERATOR_STOP")

    def test_the_status_line_is_the_path_that_was_given(self):
        self.build()
        status = self.ok("ledger", "status", self.path)
        self.assertEqual((status["ledger"], status["generation"], status["seq"]), (self.path, 1, 4))
        self.assertEqual(status["requests"][0]["snapshot"]["tokens"],
                         {"limit": 500, "known": 0, "held": 0, "unknown_operations": 0, "exposure": 0})


if __name__ == "__main__":
    unittest.main()
