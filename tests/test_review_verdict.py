"""Executor tests for the operator's review verdict (review-verdict).

The planner oracle drives the product commands end to end; these tests pin the
places the oracle cannot see from the outside: the generated durable method and
the method table it comes from, a verdict that has to survive a close and a
reopen, the candidate an operator is shown per operation, the per-request list of
runs waiting for a verdict, and that ``fixture_verdict`` is still the two-key
hook the fixtures were written against.  No provider CLI, no credentials, no
network: every child is a ``python -c`` or a ``python -m sentinelmesh.cli``.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import cli, ledger_cli
from sentinelmesh.durable_ledger import (
    LEDGER_FORMAT,
    MUTATING_METHODS,
    DurableScheduler,
    create_ledger,
    open_ledger,
)
from sentinelmesh.scheduler import Scheduler

NL = chr(10)
CAPS = {"dispatches": 9, "tokens": 500, "cost": 200}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
CANDIDATE, RECOVERED, EVIDENCE = "c" * 64, "d" * 64, "e" * 64
OPERATOR, NOTE = "ops-1", "diffed the candidate against the task and ran its tests"
VERDICT_KEYS = {"state", "evidence", "candidate", "operator", "note"}
REVIEW_KEYS = {"operation_id", "task_id", "generation", "candidate"}


class VerdictCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="review-verdict-exec-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.path = str(self.work / "ledger.db")

    # -- ledgers and runs ---------------------------------------------------- #

    def seed(self, ledger, request="req-1", *tasks):
        ledger.seed_request(request, "repo-1", CAPS, concurrency=4)
        for task in tasks:
            ledger.seed_task(request, task, CAPS)
        return ledger

    def claim(self, ledger, request, task, key, owner="owner-1", generation=None):
        generation = ledger.generation if generation is None else generation
        return ledger.claim(request, task, 1, key, owner, generation, 1, RESERVATION,
                            ["src/%s.py" % task])["operation_id"]

    def review(self, ledger, request, task, key, owner="owner-1", candidate=CANDIDATE):
        """One operation of ``request`` left in REVIEW_PENDING with ``candidate``."""
        op = self.claim(ledger, request, task, key, owner)
        ledger.handoff(op, owner, ledger.generation)
        ledger.mark_running(op, owner, ledger.generation)
        ledger.settle(op, {"tokens": 5, "cost": 1})
        ledger.finish(op, owner, ledger.generation, candidate, True)
        return op

    def smctl(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main([str(a) for a in argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, err), (0, ""), "smctl %s failed: %s" % (argv, err))
        self.assertTrue(out.endswith(NL) and out.count(NL) == 1, "one JSON line expected")
        return json.loads(out)


class VerdictSurfaceTest(VerdictCase):
    def test_the_generated_method_and_the_table_that_produces_it(self):
        # The signature is the Scheduler's own: the durable wrapper is generated
        # from it, so anything that changed the signature would show up here.
        self.assertEqual(inspect.signature(DurableScheduler.verdict),
                         inspect.signature(Scheduler.verdict))
        self.assertEqual(MUTATING_METHODS[MUTATING_METHODS.index("fixture_verdict") + 1], "verdict")
        self.assertEqual(MUTATING_METHODS[-2:], ("resolve_unknown", "resume"))
        self.assertEqual(LEDGER_FORMAT, "sentinelmesh-ledger-v2")


class VerdictReplayTest(VerdictCase):
    def test_a_verdict_survives_a_close_and_a_reopen(self):
        ledger = create_ledger(self.path)
        try:
            self.seed(ledger, "req-1", "task-1")
            op = self.review(ledger, "req-1", "task-1", "idem-1")
            before = ledger.seq
            ledger.verdict(op, ledger.generation, candidate_hash=CANDIDATE, accepted=True,
                           evidence_hash=EVIDENCE, operator=OPERATOR, note=NOTE)
            self.assertEqual(ledger.seq, before + 1)
            written = [e for e in ledger.events if e["type"] == "run.verdict"]
            self.assertEqual(len(written), 1)
            self.assertEqual(set(written[0]["payload"]), VERDICT_KEYS)
        finally:
            ledger.close()
        reopened = open_ledger(self.path)
        try:
            self.assertEqual(reopened.operation(op)["state"], "ACCEPTED")
            self.assertEqual(reopened.seq, before + 1)
            replayed = [e for e in reopened.events if e["type"] == "run.verdict"]
            self.assertEqual(len(replayed), 1)
            self.assertEqual(replayed[0]["payload"], written[0]["payload"])
        finally:
            reopened.close()


class CandidateOfTest(VerdictCase):
    def test_the_candidate_of_one_operation_is_the_last_one_its_events_record(self):
        ledger = create_ledger(self.path)
        try:
            self.seed(ledger, "req-1", "task-1", "task-2")
            finished = self.review(ledger, "req-1", "task-1", "idem-1")
            self.assertEqual(ledger_cli._candidate_of(ledger, finished), CANDIDATE)

            # A review that came in through a recovery carries the candidate the
            # recovery recorded, and the recovery's own generation.
            recovered = self.claim(ledger, "req-1", "task-2", "idem-2", "owner-2")
            ledger.handoff(recovered, "owner-2", ledger.generation)
            ledger.mark_running(recovered, "owner-2", ledger.generation)
            ledger.stop("req-1", "operator hold")
            ledger.rollover()
            ledger.recover(recovered, "owner-2", ledger.generation, outcome="FINISHED",
                           evidence_hash=EVIDENCE, candidate_hash=RECOVERED)
            self.assertEqual(ledger.operation(recovered)["state"], "REVIEW_PENDING")
            self.assertEqual(ledger_cli._candidate_of(ledger, recovered), RECOVERED)

            # An operation that never recorded a candidate has none to review.
            self.seed(ledger, "req-2", "task-1")
            reserved = self.claim(ledger, "req-2", "task-1", "idem-3", "owner-3")
            self.assertIsNone(ledger_cli._candidate_of(ledger, reserved))
        finally:
            ledger.close()

    def test_the_candidate_of_an_operation_with_no_events_at_all_is_none(self):
        ledger = create_ledger(self.path)
        try:
            self.seed(ledger, "req-1", "task-1")
            self.assertIsNone(ledger_cli._candidate_of(ledger, "op-" + "0" * 32))
        finally:
            ledger.close()


class ReviewsTest(VerdictCase):
    def test_each_request_lists_only_the_runs_it_is_waiting_on(self):
        ledger = create_ledger(self.path)
        try:
            self.seed(ledger, "req-1", "task-1")
            self.seed(ledger, "req-2", "task-1")
            first = self.review(ledger, "req-1", "task-1", "idem-1")
            second = self.review(ledger, "req-2", "task-1", "idem-2", candidate="1" * 64)
            own = ledger_cli._reviews(ledger, "req-1")
            self.assertEqual(own, [{"operation_id": first, "task_id": "task-1", "generation": 1,
                                    "candidate": CANDIDATE}])
            self.assertEqual([entry["operation_id"] for entry in ledger_cli._reviews(ledger, "req-2")], [second])
            self.assertEqual(set(own[0]), REVIEW_KEYS)
        finally:
            ledger.close()

    def test_a_decided_run_leaves_the_list(self):
        ledger = create_ledger(self.path)
        try:
            self.seed(ledger, "req-1", "task-1")
            op = self.review(ledger, "req-1", "task-1", "idem-1")
            self.assertEqual(len(ledger_cli._reviews(ledger, "req-1")), 1)
            ledger.verdict(op, ledger.generation, candidate_hash=CANDIDATE, accepted=False,
                           evidence_hash=EVIDENCE, operator=OPERATOR, note=NOTE)
            self.assertEqual(ledger.operation(op)["state"], "REJECTED")
            self.assertEqual(ledger_cli._reviews(ledger, "req-1"), [])
        finally:
            ledger.close()


class CmdVerdictTest(VerdictCase):
    def test_the_command_decides_and_reports_the_operation_it_decided(self):
        ledger = create_ledger(self.path)
        try:
            self.seed(ledger, "req-1", "task-1")
            op = self.review(ledger, "req-1", "task-1", "idem-1")
        finally:
            ledger.close()
        result = self.ok("ledger", "verdict", self.path, "--operation", op, "--candidate", CANDIDATE,
                         "--reject", "--evidence", EVIDENCE, "--operator", OPERATOR, "--note", NOTE)
        self.assertEqual(set(result), {"command", "operation"})
        self.assertEqual(result["command"], "verdict")
        reopened = open_ledger(self.path)
        try:
            self.assertEqual(reopened.operation(op)["state"], "REJECTED")
            self.assertEqual(result["operation"], reopened.operation(op))
        finally:
            reopened.close()

    def test_the_fixture_hook_still_records_only_the_state_and_the_evidence(self):
        scheduler = Scheduler()
        scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        scheduler.seed_task("req-1", "task-1", CAPS)
        op = scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, RESERVATION,
                             ["src/task-1.py"])["operation_id"]
        scheduler.handoff(op, "owner-1", 1)
        scheduler.mark_running(op, "owner-1", 1)
        scheduler.settle(op, {"tokens": 5, "cost": 1})
        scheduler.finish(op, "owner-1", 1, CANDIDATE, True)
        scheduler.fixture_verdict(op, CANDIDATE, True, EVIDENCE)
        self.assertEqual(scheduler.operation(op)["state"], "ACCEPTED")
        verdict = scheduler.events[-1]
        self.assertEqual(verdict["type"], "run.verdict")
        self.assertEqual(verdict["payload"], {"state": "ACCEPTED", "evidence": EVIDENCE})


if __name__ == "__main__":
    unittest.main()
