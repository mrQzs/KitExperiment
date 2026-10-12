"""Planner oracle for review-verdict (`.agent/design/review-verdict.md`).

A run that finished waits in ``REVIEW_PENDING`` with the candidate it put up
for review.  ``Scheduler.verdict`` records an operator's decision on it in the
``resolve_unknown`` mould -- the current generation, the candidate named by
hand, the evidence digest, the operator and the note, every input validated
before anything is mutated -- and ``smctl ledger verdict`` is the operator's
entry to it; ``smctl ledger status`` lists the runs waiting for a verdict per
request under ``reviews``.  ``fixture_verdict`` stays the hook tests use.  The
children are local ``python -c`` programs and ``python -m sentinelmesh.cli``
processes; no provider CLI, no credentials, no network.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import cli
from sentinelmesh.durable_ledger import MUTATING_METHODS, create_ledger, open_ledger
from sentinelmesh.scheduler import ContractError, Scheduler

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
CAPS = {"dispatches": 9, "tokens": 500, "cost": 200}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
CANDIDATE, OTHER, EVIDENCE = "c" * 64, "d" * 64, "e" * 64
OPERATOR, NOTE = "ops-1", "diffed the candidate against the task and ran its tests"
VERDICT_KEYS = {"state", "evidence", "candidate", "operator", "note"}
REVIEW_KEYS = {"operation_id", "task_id", "generation", "candidate"}


def ev(seq, typ, payload=None):
    return {"seq": seq, "event_id": "e%d" % seq, "type": typ, "source": "offline",
            "payload": {} if payload is None else payload}


COMPLETE = [ev(0, "run_started"), ev(1, "assistant"), ev(2, "usage", {"tokens": 12, "cost": 3}),
            ev(3, "run_finished")]


def emit(events):
    return ("import json, sys" + NL
            + "for event in json.loads(%r):" % json.dumps(events) + NL
            + "    sys.stdout.buffer.write((json.dumps(event) + chr(10)).encode())" + NL
            + "sys.stdout.buffer.flush()" + NL)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Key(str):
    """A ``str`` subclass: equal to the text it spells, but not exact text."""


class VerdictCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="review-verdict-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.ledger = str(self.work / "ledger.db")
        self.count = 0

    # -- driving the CLI --------------------------------------------------- #

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

    def refused(self, expected, *argv):
        before = self.digest()
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, out), (3, ""), "smctl %s should be refused: %s" % (argv, err))
        self.assertTrue(err.endswith(NL) and err.count(NL) == 1)
        error = json.loads(err)
        self.assertEqual(set(error), {"error", "detail"})
        self.assertEqual(error["error"], expected, error)
        self.assertEqual(self.digest(), before, "a refused verdict changed the ledger")
        return error

    def usage_error(self, *argv):
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, out), (2, ""), "smctl %s should be a usage error" % (argv,))
        self.assertNotEqual(err, "")

    def verdict(self, op, decision="--accept", *, candidate=CANDIDATE, evidence=EVIDENCE, operator=OPERATOR, note=NOTE):
        return self.ok("ledger", "verdict", self.ledger, "--operation", op, "--candidate", candidate, decision,
                       "--evidence", evidence, "--operator", operator, "--note", note)

    def verdict_refused(self, expected, op, decision="--accept", *, candidate=CANDIDATE, evidence=EVIDENCE,
                        operator=OPERATOR, note=NOTE):
        return self.refused(expected, "ledger", "verdict", self.ledger, "--operation", op, "--candidate", candidate,
                            decision, "--evidence", evidence, "--operator", operator, "--note", note)

    # -- ledgers and runs ---------------------------------------------------- #

    def write_json(self, name, value) -> str:
        path = self.work / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def init(self, max_attempts=3):
        spec = {"request_id": "req-1", "repo_id": "repo-1", "caps": dict(CAPS), "concurrency": 4,
                "tasks": [{"task_id": "task-%d" % n, "caps": dict(CAPS), "max_attempts": max_attempts}
                          for n in range(1, 7)]}
        return self.ok("run", "init", self.ledger, "--spec", self.write_json("spec.json", spec))

    def run_once(self, task=None, *, candidate=CANDIDATE):
        """One offline run through the product command, to REVIEW_PENDING with ``candidate``."""
        self.count += 1
        task = task or "task-%d" % self.count
        job = {"request_id": "req-1", "task_id": task, "plan": 1, "idempotency_key": "idem-%d" % self.count,
               "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["src/%s.py" % task],
               "adapter_id": "offline", "role": "executor", "profile_id": "stub-profile",
               "command": [PY, "-c", emit(COMPLETE)], "capabilities": [], "cwd": str(self.cwd),
               "environment": dict(BASE_ENV), "allowed_secret_names": [], "timeout_seconds": 20,
               "usage_dimensions": ["tokens", "cost"]}
        evidence = self.work / ("evidence-%d" % self.count)
        evidence.mkdir()
        result = self.ok("run", "once", self.ledger, "--job", self.write_json("job-%d.json" % self.count, job),
                         "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1",
                         "--candidate", candidate)
        self.assertEqual(result["outcome"]["state"], "REVIEW_PENDING")
        return result["operation_id"]

    def digest(self) -> str:
        return sha256_of(Path(self.ledger).read_bytes())

    def state(self, op):
        ledger = open_ledger(self.ledger)
        try:
            return ledger.operation(op)["state"]
        finally:
            ledger.close()

    def events_of(self, op, event_type):
        ledger = open_ledger(self.ledger)
        try:
            return [e for e in ledger.events if e["operation_id"] == op and e["type"] == event_type]
        finally:
            ledger.close()

    def request(self):
        status = self.ok("ledger", "status", self.ledger)
        return status["requests"][0]


class VerdictOracle(VerdictCase):
    def test_a_review_is_listed_and_decided_through_the_command(self):
        self.init()
        op = self.run_once()
        request = self.request()
        self.assertEqual(set(request), {"request_id", "stopped", "stop_reasons", "snapshot", "attention", "reviews",
                                        "tasks"})
        self.assertEqual(request["attention"], [])
        self.assertEqual(request["reviews"], [{"operation_id": op, "task_id": "task-1", "generation": 1,
                                               "candidate": CANDIDATE}])
        result = self.verdict(op, "--accept")
        self.assertEqual(set(result), {"command", "operation"})
        self.assertEqual((result["command"], result["operation"]["state"], result["operation"]["operation_id"]),
                         ("verdict", "ACCEPTED", op))
        self.assertEqual(self.state(op), "ACCEPTED")
        events = self.events_of(op, "run.verdict")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"], {"state": "ACCEPTED", "evidence": EVIDENCE, "candidate": CANDIDATE,
                                                "operator": OPERATOR, "note": NOTE})
        request = self.request()
        self.assertEqual((request["reviews"], request["attention"], request["stopped"]), ([], [], False))
        # The usage and the request's budget are what the run left: a verdict
        # settles nothing and holds nothing.
        self.assertEqual((request["snapshot"]["tokens"]["known"], request["snapshot"]["tokens"]["held"]), (12, 0))
        # A rejection is the same decision with the other state.
        other = self.run_once()
        result = self.verdict(other, "--reject", note="the diff touches files outside the task")
        self.assertEqual(result["operation"]["state"], "REJECTED")
        self.assertEqual(self.events_of(other, "run.verdict")[0]["payload"]["state"], "REJECTED")
        self.assertEqual(self.events_of(other, "run.verdict")[0]["payload"]["note"],
                         "the diff touches files outside the task")
        listed = self.ok("ledger", "events", self.ledger, "--type", "run.verdict")
        self.assertEqual([e["operation_id"] for e in listed["events"]], [op, other])

    def test_reviews_list_every_pending_run_in_claim_order_with_its_own_candidate(self):
        self.init()
        first = self.run_once(candidate="1" * 64)
        second = self.run_once(candidate="2" * 64)
        third = self.run_once(candidate="3" * 64)
        self.assertEqual([(r["operation_id"], r["candidate"]) for r in self.request()["reviews"]],
                         [(first, "1" * 64), (second, "2" * 64), (third, "3" * 64)])
        self.verdict(second, "--accept", candidate="2" * 64)
        self.assertEqual([r["operation_id"] for r in self.request()["reviews"]], [first, third])
        for entry in self.request()["reviews"]:
            self.assertEqual(set(entry), REVIEW_KEYS)
        # A review that came in through a resolution carries the operator's candidate.
        ledger = open_ledger(self.ledger)
        try:
            op = ledger.claim("req-1", "task-4", 1, "idem-unknown", "owner-1", 1, 1, RESERVATION, ["src/task-4.py"])["operation_id"]
            ledger.handoff(op, "owner-1", 1)
            ledger.unknown(op, "owner-1", 1)
            ledger.settle(op, {"tokens": 1, "cost": 1})
            ledger.resolve_unknown(op, 1, resolution="FINISHED", operator=OPERATOR, evidence_hash=EVIDENCE,
                                   note=NOTE, candidate_hash=OTHER)
        finally:
            ledger.close()
        self.assertEqual([(r["operation_id"], r["candidate"]) for r in self.request()["reviews"]],
                         [(first, "1" * 64), (third, "3" * 64), (op, OTHER)])
        self.verdict(op, "--reject", candidate=OTHER)
        self.assertEqual(self.state(op), "REJECTED")

    def test_a_verdict_is_refused_with_the_ledger_unchanged(self):
        self.init()
        op = self.run_once()
        with self.subTest(case="another candidate"):
            self.verdict_refused("E_VERDICT_CANDIDATE", op, candidate=OTHER)
        with self.subTest(case="an unknown operation"):
            self.verdict_refused("E_VERDICT_CANDIDATE", "op-" + "0" * 32)
        with self.subTest(case="evidence not a digest"):
            self.verdict_refused("E_VERDICT_SHAPE", op, evidence="E" * 64)
            self.verdict_refused("E_VERDICT_SHAPE", op, evidence="e" * 63)
        with self.subTest(case="operator and note"):
            self.verdict_refused("E_OPERATOR", op, operator="")
            self.verdict_refused("E_OPERATOR", op, operator="x" * 201)
            self.verdict_refused("E_NOTE", op, note="")
            self.verdict_refused("E_NOTE", op, note="n" + chr(0))
        self.assertEqual(self.state(op), "REVIEW_PENDING")
        self.assertEqual(self.events_of(op, "run.verdict"), [])
        self.verdict(op, "--accept")
        with self.subTest(case="decided twice"):
            self.verdict_refused("E_VERDICT_CANDIDATE", op, "--reject")
        with self.subTest(case="a run that is not in review"):
            ledger = open_ledger(self.ledger)
            try:
                reserved = ledger.claim("req-1", "task-5", 1, "idem-reserved", "owner-1", 1, 1, RESERVATION,
                                        ["src/task-5.py"])["operation_id"]
            finally:
                ledger.close()
            self.verdict_refused("E_VERDICT_CANDIDATE", reserved)
        self.assertEqual(self.state(op), "ACCEPTED")
        self.assertEqual(len(self.events_of(op, "run.verdict")), 1)
        with self.subTest(case="usage errors"):
            self.usage_error("ledger", "verdict", self.ledger, "--operation", op, "--candidate", CANDIDATE,
                             "--evidence", EVIDENCE, "--operator", OPERATOR, "--note", NOTE)
            self.usage_error("ledger", "verdict", self.ledger, "--operation", op, "--candidate", CANDIDATE,
                             "--accept", "--reject", "--evidence", EVIDENCE, "--operator", OPERATOR, "--note", NOTE)
            self.usage_error("ledger", "verdict", self.ledger, "--operation", op, "--accept",
                             "--evidence", EVIDENCE, "--operator", OPERATOR, "--note", NOTE)
        with self.subTest(case="a ledger that cannot be opened"):
            self.refused("E_LEDGER_MISSING", "ledger", "verdict", str(self.work / "absent.db"), "--operation", op,
                         "--candidate", CANDIDATE, "--accept", "--evidence", EVIDENCE, "--operator", OPERATOR,
                         "--note", NOTE)

    def test_the_verdict_is_in_the_ledgers_current_generation(self):
        self.init()
        op = self.run_once()
        ledger = open_ledger(self.ledger)
        try:
            ledger.rollover()
        finally:
            ledger.close()
        # The command decides in generation 2 whatever the run's generation was;
        # the scheduler refuses any other generation outright.
        result = self.verdict(op, "--accept")
        self.assertEqual((result["operation"]["state"], result["operation"]["generation"]), ("ACCEPTED", 1))
        ledger = open_ledger(self.ledger)
        try:
            other = self.run_once_in(ledger, "task-2")
            for generation in (1, 3, True, 2.0, Key("2")):
                with self.subTest(generation=generation):
                    with self.assertRaises(ContractError) as ctx:
                        ledger.verdict(other, generation, candidate_hash=CANDIDATE, accepted=True,
                                       evidence_hash=EVIDENCE, operator=OPERATOR, note=NOTE)
                    self.assertEqual(ctx.exception.code, "E_FENCED")
            self.assertEqual(ledger.operation(other)["state"], "REVIEW_PENDING")
        finally:
            ledger.close()

    def run_once_in(self, ledger, task):
        """A review made with the library on an open ledger, in its current generation."""
        generation = ledger.generation
        if ledger.stop_reasons("req-1"):
            ledger.resume("req-1", generation, expected_reasons=list(ledger.stop_reasons("req-1")), operator=OPERATOR,
                          note=NOTE)
        op = ledger.claim("req-1", task, 1, "idem-" + task, "owner-2", generation, 1, RESERVATION,
                          ["src/%s.py" % task])["operation_id"]
        ledger.handoff(op, "owner-2", generation)
        ledger.mark_running(op, "owner-2", generation)
        ledger.settle(op, {"tokens": 5, "cost": 1})
        ledger.finish(op, "owner-2", generation, CANDIDATE, True)
        return op

    def test_the_scheduler_validates_in_order_before_mutating(self):
        scheduler = Scheduler()
        scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        scheduler.seed_task("req-1", "task-1", CAPS)
        op = scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, RESERVATION, ["src/a.py"])["operation_id"]
        scheduler.handoff(op, "owner-1", 1)
        scheduler.mark_running(op, "owner-1", 1)
        scheduler.settle(op, {"tokens": 5, "cost": 1})
        scheduler.finish(op, "owner-1", 1, CANDIDATE, True)
        good = dict(candidate_hash=CANDIDATE, accepted=True, evidence_hash=EVIDENCE, operator=OPERATOR, note=NOTE)

        def refused(code, **changes):
            before = len(scheduler.events)
            with self.assertRaises(ContractError) as ctx:
                scheduler.verdict(changes.pop("operation", op), changes.pop("generation", 1), **dict(good, **changes))
            self.assertEqual(ctx.exception.code, code)
            self.assertEqual((scheduler.operation(op)["state"], len(scheduler.events)), ("REVIEW_PENDING", before))

        # The shape first, then the texts, then the generation, then the operation:
        # each case breaks everything after it as well, so the code names the order.
        refused("E_VERDICT_SHAPE", accepted=1, operator="", generation=9, candidate_hash=OTHER)
        refused("E_VERDICT_SHAPE", evidence_hash=Key(EVIDENCE), operator="", generation=9)
        refused("E_OPERATOR", operator="", note="", generation=9, candidate_hash=OTHER)
        refused("E_OPERATOR", operator=Key(OPERATOR))
        refused("E_NOTE", note="", generation=9, candidate_hash=OTHER)
        refused("E_FENCED", generation=9, candidate_hash=OTHER)
        refused("E_VERDICT_CANDIDATE", candidate_hash=OTHER)
        refused("E_VERDICT_CANDIDATE", candidate_hash=Key(CANDIDATE))
        refused("E_VERDICT_CANDIDATE", operation=Key(op))
        scheduler.verdict(op, 1, **good)
        self.assertEqual(scheduler.operation(op)["state"], "ACCEPTED")
        self.assertEqual(scheduler.events[-1]["type"], "run.verdict")
        self.assertEqual(set(scheduler.events[-1]["payload"]), VERDICT_KEYS)
        # The fixture hook is untouched: still no generation, operator or note.
        self.assertIn("fixture_verdict", MUTATING_METHODS)
        self.assertEqual(MUTATING_METHODS[MUTATING_METHODS.index("fixture_verdict") + 1], "verdict")
        self.assertEqual(MUTATING_METHODS[-2:], ("resolve_unknown", "resume"))

    def test_a_rejected_task_takes_a_new_attempt_and_an_accepted_one_is_closed(self):
        self.init(max_attempts=2)
        first = self.run_once("task-1")
        self.verdict(first, "--reject")
        # A rejection leaves the task open: it takes a new attempt, and the
        # attempts are counted whatever the verdicts were.  An acceptance closes
        # it (task-done), which is what the next claim reports.
        second = self.run_once("task-1")
        self.assertNotEqual(second, first)
        self.verdict(second, "--accept")
        ledger = open_ledger(self.ledger)
        try:
            with self.assertRaises(ContractError) as ctx:
                ledger.claim("req-1", "task-1", 1, "idem-third", "owner-1", 1, 1, RESERVATION, ["src/task-1.py"])
            self.assertEqual(ctx.exception.code, "E_TASK_DONE")
            self.assertEqual([ledger.operation(o)["state"] for o in (first, second)], ["REJECTED", "ACCEPTED"])
        finally:
            ledger.close()
        self.assertEqual(self.request()["reviews"], [])

    def test_the_verdict_survives_a_reopen_and_a_compaction(self):
        self.init()
        op = self.run_once()
        self.verdict(op, "--accept")
        self.ok("ledger", "compact", self.ledger)
        self.assertEqual(self.state(op), "ACCEPTED")
        self.assertEqual(self.events_of(op, "run.verdict")[0]["payload"]["operator"], OPERATOR)
        self.verdict_refused("E_VERDICT_CANDIDATE", op)

    def test_the_command_runs_as_a_real_process(self):
        self.init()
        op = self.run_once()
        completed = subprocess.run([PY, "-m", "sentinelmesh.cli", "ledger", "verdict", self.ledger, "--operation", op,
                                    "--candidate", CANDIDATE, "--accept", "--evidence", EVIDENCE, "--operator",
                                    OPERATOR, "--note", NOTE], cwd=str(REPO_ROOT), capture_output=True, text=True,
                                   timeout=120)
        self.assertEqual((completed.returncode, completed.stderr, completed.stdout.count(NL)), (0, "", 1))
        self.assertEqual(json.loads(completed.stdout)["operation"]["state"], "ACCEPTED")
        again = subprocess.run([PY, "-m", "sentinelmesh.cli", "ledger", "verdict", self.ledger, "--operation", op,
                                "--candidate", CANDIDATE, "--reject", "--evidence", EVIDENCE, "--operator",
                                OPERATOR, "--note", NOTE], cwd=str(REPO_ROOT), capture_output=True, text=True,
                               timeout=120)
        self.assertEqual((again.returncode, again.stdout), (3, ""))
        self.assertEqual(json.loads(again.stderr)["error"], "E_VERDICT_CANDIDATE")


if __name__ == "__main__":
    unittest.main()
