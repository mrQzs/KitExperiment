"""Planner oracle for request-resume (`.agent/design/request-resume.md`).

Planner-provided; executors must not modify this file.

After a crash and the restart recovery every request stays stopped, and a
START_UNKNOWN operation had no way out.  The user decided (2026-10-08):

* an operator resolves a START_UNKNOWN operation as ``NOT_STARTED``,
  ``ABANDONED`` (the process is gone, the result is void, the task may be
  claimed again as a new attempt) or ``FINISHED`` (into review with a
  candidate) -- ``Scheduler.resolve_unknown``;
* a request resumes only once everything in it is decided, its usage is known
  and it is within budget, comparing the generation and the stop reasons --
  ``Scheduler.resume`` (the follow-up decisions of section 9 made the
  comparison name every reason since the last resume);
* no cap raise.

Planner decisions P1-P7 fix the names, the ABANDONED accounting (no longer
live, still unresolved for usage), the check order and the durable ledger's
method list.  The last case runs the whole path with real processes: a
controller killed mid-run, the restart recovery, the operator's usage and
resolution, the review, the resume, a reopen, and the abandoned task claimed
again and run to review.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from sentinelmesh.durable_ledger import MUTATING_METHODS, LedgerError, create_ledger, open_ledger
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.restart_recovery import recover_after_restart
from sentinelmesh.runtime import InvocationSpec
from sentinelmesh.scheduler import ContractError, Scheduler
from sentinelmesh.supervised_operation import run_supervised_operation

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
CAPS = {"dispatches": 9, "tokens": 500}
RESERVATION = {"dispatches": 1, "tokens": 40}
EVIDENCE, CANDIDATE = "e" * 64, "d" * 64
OPERATOR, NOTE = "ops-1", "checked the process table and the worktree"
WAIT_FOR_RELEASE = ("import os, sys, time\n"
                    "started, release = sys.argv[1:3]\n"
                    "open(started + '.tmp', 'w').write(str(os.getpid()))\n"
                    "os.rename(started + '.tmp', started)\n"
                    "deadline = time.monotonic() + 30\n"
                    "while not os.path.exists(release) and time.monotonic() < deadline:\n"
                    "    time.sleep(0.01)\n")
CONTROLLER = r'''
import json, sys
from sentinelmesh.durable_ledger import create_ledger
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec
from sentinelmesh.supervised_operation import run_supervised_operation

config = json.loads(sys.argv[1])
env = {"LANG": "C", "PATH": "/usr/bin:/bin"}
ledger = create_ledger(config["ledger"])
ledger.seed_request("req-1", "repo-1", {"dispatches": 9, "tokens": 500}, concurrency=4)
for task in ("task-1", "task-2"):
    ledger.seed_task("req-1", task, {"dispatches": 9, "tokens": 500})


def claim(task):
    return ledger.claim("req-1", task, 1, "idem-" + task, "owner-1", 1, 1, {"dispatches": 1, "tokens": 40},
                        ["src/%s.py" % task])["operation_id"]


def run(op, evidence, code, *args, before_finish=None):
    spec = InvocationSpec("req-1", op, (config["python"], "-c", code) + tuple(args), config["cwd"],
                          environment_digest(env), 30)
    return run_supervised_operation(ledger, operation_id=op, owner="owner-1", generation=1, spec=spec,
                                    environment=env, evidence_dir=evidence, worktree_id="wt-1",
                                    candidate_hash=lambda directory: "c" * 64, before_finish=before_finish)


ops = {"reviewed": claim("task-1")}
run(ops["reviewed"], config["evidence"]["reviewed"], "pass",
    before_finish=lambda run: ledger.settle(ops["reviewed"], {"tokens": 5}))
ops["crashed"] = claim("task-2")
print(json.dumps(ops), flush=True)
run(ops["crashed"], config["evidence"]["crashed"], config["wait"], config["started"], config["release"])
'''


class Key(str):
    """A ``str`` subclass: equal to the text it spells, but not exact text."""


def pid_in(path: Path):
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text else None


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class ResumeCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="request-resume-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)

    def scheduler(self, ledger=None, *, task_caps=None):
        scheduler = ledger or Scheduler()
        scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        for task in ("task-1", "task-2", "task-3"):
            scheduler.seed_task("req-1", task, task_caps or CAPS)
        return scheduler

    def claim(self, scheduler, task, *, key=None, owner="owner-1", generation=1, resources=None):
        return scheduler.claim("req-1", task, 1, key or "idem-" + task, owner, generation, 1, RESERVATION,
                               resources or ["src/%s.py" % task])["operation_id"]

    def start_unknown(self, scheduler, task="task-1"):
        """A handed-off operation the controller could not decide (request stopped START_UNKNOWN)."""
        op = self.claim(scheduler, task)
        scheduler.handoff(op, "owner-1", 1)
        scheduler.unknown(op, "owner-1", 1, reason="START_UNKNOWN")
        return op

    def reviewed(self, scheduler, task="task-2", *, settled=True):
        """A finished run in review; its tokens settled unless asked otherwise."""
        op = self.claim(scheduler, task)
        scheduler.handoff(op, "owner-1", 1)
        scheduler.mark_running(op, "owner-1", 1)
        if settled:
            scheduler.settle(op, {"tokens": 5})
        scheduler.finish(op, "owner-1", 1, CANDIDATE, True)
        return op

    def resolve(self, scheduler, op, resolution="ABANDONED", *, generation=1, **changes):
        arguments = dict(resolution=resolution, operator=OPERATOR, evidence_hash=EVIDENCE, note=NOTE,
                         candidate_hash=CANDIDATE if resolution == "FINISHED" else None)
        arguments.update(changes)
        return scheduler.resolve_unknown(op, generation, **arguments)

    def resume(self, scheduler, *, generation=1, **changes):
        arguments = dict(expected_reasons=["START_UNKNOWN"], operator=OPERATOR, note=NOTE)
        arguments.update(changes)
        return scheduler.resume("req-1", generation, **arguments)

    def refused(self, scheduler, code, call, *, op=None):
        before = (scheduler.events, None if op is None else scheduler.operation(op),
                  scheduler.snapshot("req-1"))
        with self.assertRaises(ContractError) as ctx:
            call()
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual((scheduler.events, None if op is None else scheduler.operation(op),
                          scheduler.snapshot("req-1")), before, "a refused call changed the ledger")


class ResolveUnknownOracle(ResumeCase):
    def test_not_started_releases_the_operation(self):
        scheduler = self.scheduler()
        op = self.start_unknown(scheduler)
        self.resolve(scheduler, op, "NOT_STARTED")
        self.assertEqual(scheduler.operation(op)["state"], "NOT_STARTED")
        tokens = scheduler.snapshot("req-1")["tokens"]
        self.assertEqual((tokens["held"], tokens["unknown_operations"]), (0, 0))
        event = scheduler.events[-1]
        self.assertEqual((event["type"], event["operation_id"], event["payload"]),
                         ("scheduler.unknown_resolved", op,
                          {"resolution": "NOT_STARTED", "state": "NOT_STARTED", "operator": OPERATOR,
                           "evidence": EVIDENCE, "note": NOTE}))
        self.assertEqual(scheduler._requests["req-1"].stopped, "START_UNKNOWN", "a resolution resumed the request")

    def test_abandoned_keeps_the_reservation_until_the_usage_is_known(self):
        scheduler = self.scheduler()
        op = self.start_unknown(scheduler)
        self.resolve(scheduler, op, "ABANDONED")
        self.assertEqual(scheduler.operation(op)["state"], "ABANDONED")
        tokens = scheduler.snapshot("req-1")["tokens"]
        self.assertEqual((tokens["known"], tokens["held"], tokens["unknown_operations"]), (0, 40, 1),
                         "an abandoned run's usage was taken as zero")
        scheduler.settle(op, {"tokens": 7})
        tokens = scheduler.snapshot("req-1")["tokens"]
        self.assertEqual((tokens["known"], tokens["held"], tokens["unknown_operations"]), (7, 0, 0))
        self.assertEqual(scheduler.snapshot("req-1")["dispatches"]["known"], 1, "the dispatch stopped counting")
        # No longer live: the task may be claimed again once the request resumes,
        # as a new attempt, on the very resources the abandoned run held.
        self.resume(scheduler)
        again = self.claim(scheduler, "task-1", key="idem-task-1-again")
        self.assertNotEqual(again, op)
        self.assertEqual(scheduler.operation(again)["state"], "RESERVED")

    def test_finished_goes_to_review_with_its_candidate(self):
        scheduler = self.scheduler()
        op = self.start_unknown(scheduler)
        self.resolve(scheduler, op, "FINISHED")
        self.assertEqual(scheduler.operation(op)["state"], "REVIEW_PENDING")
        self.assertEqual(scheduler.events[-1]["payload"],
                         {"resolution": "FINISHED", "state": "REVIEW_PENDING", "operator": OPERATOR,
                          "evidence": EVIDENCE, "note": NOTE, "candidate": CANDIDATE})
        with self.assertRaises(ContractError) as ctx:
            scheduler.fixture_verdict(op, "c" * 64, True, EVIDENCE)
        self.assertEqual(ctx.exception.code, "E_VERDICT_CANDIDATE")
        scheduler.fixture_verdict(op, CANDIDATE, True, EVIDENCE)
        self.assertEqual(scheduler.operation(op)["state"], "ACCEPTED")

    def test_a_refused_resolution_changes_nothing(self):
        cases = [
            ("a resolution that is not one of the three", "E_RESOLUTION", dict(resolution="RECOVERED")),
            ("a resolution that is not exact text", "E_RESOLUTION", dict(resolution=Key("ABANDONED"))),
            ("an evidence hash in upper case", "E_EVIDENCE_HASH", dict(evidence_hash="E" * 64)),
            ("an evidence hash that is not text", "E_EVIDENCE_HASH", dict(evidence_hash=None)),
            ("a finished run without a candidate", "E_CLEANUP_OR_CANDIDATE",
             dict(resolution="FINISHED", candidate_hash=None)),
            ("an abandoned run with a candidate", "E_CLEANUP_OR_CANDIDATE", dict(candidate_hash=CANDIDATE)),
            ("an empty operator", "E_OPERATOR", dict(operator="")),
            ("an operator too long", "E_OPERATOR", dict(operator="o" * 201)),
            ("an operator with a line break", "E_OPERATOR", dict(operator="ops\n1")),
            ("an operator with a lone surrogate", "E_OPERATOR", dict(operator="ops-\udcff")),
            ("an operator that is not exact text", "E_OPERATOR", dict(operator=Key("ops-1"))),
            ("an empty note", "E_NOTE", dict(note="")),
            ("a note too long", "E_NOTE", dict(note="n" * 501)),
            ("a note with a NUL", "E_NOTE", dict(note="checked\x00")),
            ("a note that is not text", "E_NOTE", dict(note=None)),
            ("an older generation", "E_FENCED", dict(generation=0)),
            ("a generation that is a bool", "E_FENCED", dict(generation=True)),
        ]
        for label, code, changes in cases:
            with self.subTest(case=label):
                scheduler = self.scheduler()
                op = self.start_unknown(scheduler)
                self.refused(scheduler, code, lambda: self.resolve(scheduler, op, **changes), op=op)
                # Legal baseline: the same operation, nothing changed but the one value.
                self.resolve(scheduler, op)
                self.assertEqual(scheduler.operation(op)["state"], "ABANDONED")
        with self.subTest(case="an unknown operation"):
            scheduler = self.scheduler()
            self.start_unknown(scheduler)
            for value in ("op-" + "0" * 32, None, ["op"]):
                self.refused(scheduler, "E_OPERATION_UNKNOWN", lambda: self.resolve(scheduler, value))
        with self.subTest(case="an operation that is not START_UNKNOWN"):
            scheduler = self.scheduler()
            reserved = self.claim(scheduler, "task-3")
            reviewed = self.reviewed(scheduler)
            self.start_unknown(scheduler)
            for op in (reserved, reviewed):
                self.refused(scheduler, "E_RESOLVE_STATE", lambda: self.resolve(scheduler, op), op=op)

    def test_the_resolution_checks_come_in_their_order(self):
        scheduler = self.scheduler()
        reserved = self.claim(scheduler, "task-3")
        op = self.start_unknown(scheduler)
        self.refused(scheduler, "E_RESOLUTION",
                     lambda: self.resolve(scheduler, op, resolution="X", operator="", generation=0), op=op)
        self.refused(scheduler, "E_OPERATOR", lambda: self.resolve(scheduler, op, operator="", generation=0), op=op)
        # Each adjacent pair at once (review of attempt-001, L2).
        self.refused(scheduler, "E_OPERATOR", lambda: self.resolve(scheduler, op, operator="", note=""), op=op)
        self.refused(scheduler, "E_EVIDENCE_HASH",
                     lambda: self.resolve(scheduler, op, evidence_hash="E" * 64, candidate_hash=CANDIDATE), op=op)
        self.refused(scheduler, "E_FENCED", lambda: self.resolve(scheduler, "op-" + "0" * 32, generation=0))
        self.refused(scheduler, "E_FENCED", lambda: self.resolve(scheduler, reserved, generation=0), op=reserved)
        self.refused(scheduler, "E_RESOLVE_STATE", lambda: self.resolve(scheduler, reserved), op=reserved)


class ResumeOracle(ResumeCase):
    def test_a_decided_request_resumes_and_keeps_its_history(self):
        scheduler = self.scheduler()
        reviewed = self.reviewed(scheduler, "task-1")
        cancelled = self.claim(scheduler, "task-2")
        scheduler.cancel_unstarted(cancelled, 1)
        scheduler.stop("req-1")
        before = scheduler.snapshot("req-1")
        events = scheduler.events
        self.resume(scheduler, expected_reasons=["OPERATOR_STOP"])
        self.assertIsNone(scheduler._requests["req-1"].stopped)
        self.assertEqual(scheduler.snapshot("req-1"), before, "the resume changed the usage or the reservations")
        self.assertEqual(scheduler.events[:-1], events, "the resume rewrote the history")
        event = scheduler.events[-1]
        self.assertEqual((event["type"], event["operation_id"], event["payload"]),
                         ("request.resumed", None, {"request_id": "req-1", "previous_reasons": ["OPERATOR_STOP"],
                                                    "operator": OPERATOR, "note": NOTE}))
        self.assertEqual(scheduler.operation(reviewed)["state"], "REVIEW_PENDING")
        # Attempts are not reset: task-2 already used one of its three.
        again = self.claim(scheduler, "task-2", key="idem-task-2-again")
        self.assertEqual(scheduler.operation(again)["state"], "RESERVED")
        scheduler.cancel_unstarted(again, 1)
        self.claim(scheduler, "task-2", key="idem-task-2-third")
        with self.assertRaises(ContractError) as ctx:
            scheduler.claim("req-1", "task-2", 1, "idem-task-2-fourth", "owner-1", 1, 1, RESERVATION,
                            ["src/task-2-b.py"])
        self.assertIn(ctx.exception.code, ("E_ACTIVE_TASK", "E_ATTEMPTS"))

    def test_each_precondition_refuses_the_resume(self):
        def stopped_unknown():
            scheduler = self.scheduler()
            self.start_unknown(scheduler)
            return scheduler, ["START_UNKNOWN"]

        def not_stopped():
            return self.scheduler(), ["OPERATOR_STOP"]

        def undecided(state):
            def build():
                scheduler = self.scheduler()
                op = self.claim(scheduler, "task-1")
                if state != "RESERVED":
                    scheduler.handoff(op, "owner-1", 1)
                if state == "RUNNING":
                    scheduler.mark_running(op, "owner-1", 1)
                scheduler.rollover()
                return scheduler, ["RECOVERY_REQUIRED"]
            return build

        def usage_unknown():
            scheduler = self.scheduler()
            self.reviewed(scheduler, settled=False)
            return scheduler, ["USAGE_UNKNOWN"]

        def over_budget():
            scheduler = self.scheduler()
            op = self.reviewed(scheduler, settled=False)
            scheduler.settle(op, {"tokens": 600})
            return scheduler, ["USAGE_UNKNOWN", "BUDGET_EXCEEDED"]

        def over_a_task_budget():
            scheduler = self.scheduler(task_caps={"dispatches": 9, "tokens": 50})
            op = self.reviewed(scheduler, settled=False)
            scheduler.settle(op, {"tokens": 60})
            return scheduler, ["USAGE_UNKNOWN", "BUDGET_EXCEEDED"]

        cases = [
            ("a request that is not stopped", not_stopped, "E_NOT_STOPPED", {}),
            ("another stop reason", stopped_unknown, "E_STOP_REASON_CHANGED", dict(expected_reasons=["OPERATOR_STOP"])),
            ("a START_UNKNOWN operation", stopped_unknown, "E_RESUME_UNKNOWN", {}),
            ("an older generation's claim", undecided("RESERVED"), "E_RESUME_UNDECIDED", dict(generation=2)),
            ("an older generation's handoff", undecided("HANDOFF_ACCEPTED"), "E_RESUME_UNDECIDED", dict(generation=2)),
            ("an older generation's run", undecided("RUNNING"), "E_RESUME_UNDECIDED", dict(generation=2)),
            ("usage still unknown", usage_unknown, "E_RESUME_USAGE_UNKNOWN", {}),
            ("the request over its budget", over_budget, "E_RESUME_BUDGET", {}),
            ("a task over its budget", over_a_task_budget, "E_RESUME_BUDGET", {}),
            ("an older generation", stopped_unknown, "E_FENCED", dict(generation=0)),
            ("an empty expected reason", stopped_unknown, "E_STOP_REASON", dict(expected_reasons=[""])),
            ("expected reasons that are not a list", stopped_unknown, "E_STOP_REASON", dict(expected_reasons=None)),
            ("an operator with a line break", stopped_unknown, "E_OPERATOR", dict(operator="ops\n1")),
            ("a note too long", stopped_unknown, "E_NOTE", dict(note="n" * 501)),
        ]
        for label, build, code, changes in cases:
            with self.subTest(case=label):
                scheduler, reasons = build()
                if build is not not_stopped:
                    self.assertEqual(scheduler.stop_reasons("req-1"), tuple(reasons))
                arguments = dict(expected_reasons=reasons)
                arguments.update(changes)
                stopped = scheduler._requests["req-1"].stopped
                self.refused(scheduler, code, lambda: self.resume(scheduler, **arguments))
                self.assertEqual(scheduler._requests["req-1"].stopped, stopped)
        with self.subTest(case="an unknown request"):
            scheduler, reasons = stopped_unknown()
            for value in ("req-9", None, ["req-1"]):
                self.refused(scheduler, "E_REQUEST_UNKNOWN",
                             lambda: scheduler.resume(value, 1, expected_reasons=reasons, operator=OPERATOR, note=NOTE))

    def test_the_resume_checks_come_in_their_order(self):
        scheduler = self.scheduler()
        # Every fault at once: an undecided older handoff, a run in review whose
        # tokens are unknown, and a START_UNKNOWN operation (the first stop reason).
        old = self.claim(scheduler, "task-2")
        scheduler.handoff(old, "owner-1", 1)
        review = self.claim(scheduler, "task-3")
        scheduler.handoff(review, "owner-1", 1)
        scheduler.mark_running(review, "owner-1", 1)
        unknown = self.start_unknown(scheduler, "task-1")
        scheduler.finish(review, "owner-1", 1, CANDIDATE, True)
        scheduler.rollover()
        reasons = ["START_UNKNOWN", "USAGE_UNKNOWN", "RECOVERY_REQUIRED"]
        self.assertEqual(scheduler.stop_reasons("req-1"), tuple(reasons))
        self.refused(scheduler, "E_OPERATOR",
                     lambda: self.resume(scheduler, operator="", expected_reasons=[""], generation=1))
        self.refused(scheduler, "E_STOP_REASON", lambda: self.resume(scheduler, expected_reasons=[""], generation=1))
        # Each adjacent pair at once (review of attempt-001, L2).
        self.refused(scheduler, "E_OPERATOR", lambda: self.resume(scheduler, operator="", note="", generation=2))
        self.refused(scheduler, "E_FENCED",
                     lambda: scheduler.resume("req-9", 1, expected_reasons=["X"], operator=OPERATOR, note=NOTE))
        self.refused(scheduler, "E_FENCED", lambda: self.resume(scheduler, expected_reasons=["X"], generation=1))
        self.refused(scheduler, "E_STOP_REASON_CHANGED",
                     lambda: self.resume(scheduler, expected_reasons=["RECOVERY_REQUIRED"], generation=2))
        self.refused(scheduler, "E_RESUME_UNKNOWN", lambda: self.resume(scheduler, expected_reasons=reasons, generation=2))
        self.resolve(scheduler, unknown, generation=2)
        self.refused(scheduler, "E_RESUME_UNDECIDED",
                     lambda: self.resume(scheduler, expected_reasons=reasons, generation=2))
        scheduler.recover(old, "owner-2", 2, outcome="NOT_STARTED", evidence_hash=EVIDENCE)
        self.refused(scheduler, "E_RESUME_USAGE_UNKNOWN",
                     lambda: self.resume(scheduler, expected_reasons=reasons, generation=2))
        # Over budget while the review's tokens are still unknown: the unknown
        # usage is reported first.  Going over budget adds its own reason.
        scheduler.settle(unknown, {"tokens": 600})
        reasons.append("BUDGET_EXCEEDED")
        self.assertEqual(scheduler.stop_reasons("req-1"), tuple(reasons))
        self.refused(scheduler, "E_RESUME_USAGE_UNKNOWN",
                     lambda: self.resume(scheduler, expected_reasons=reasons, generation=2))
        scheduler.settle(review, {"tokens": 5})
        self.refused(scheduler, "E_RESUME_BUDGET", lambda: self.resume(scheduler, expected_reasons=reasons, generation=2))
        # No cap raise: a correction of the recorded usage is what brings it back.
        scheduler.settle(unknown, {"tokens": 7}, revision=2, correction=True)
        self.resume(scheduler, expected_reasons=reasons, generation=2)
        self.assertIsNone(scheduler._requests["req-1"].stopped)
        self.assertEqual(scheduler.stop_reasons("req-1"), ())

    def test_a_run_of_the_current_generation_does_not_block_the_resume(self):
        scheduler = self.scheduler()
        op = self.claim(scheduler, "task-1")
        scheduler.handoff(op, "owner-1", 1)
        scheduler.mark_running(op, "owner-1", 1)
        scheduler.stop("req-1")
        self.resume(scheduler, expected_reasons=["OPERATOR_STOP"])
        scheduler.settle(op, {"tokens": 5})
        scheduler.finish(op, "owner-1", 1, CANDIDATE, True)
        self.assertEqual(scheduler.operation(op)["state"], "REVIEW_PENDING")
        self.assertIsNone(scheduler._requests["req-1"].stopped)


class DurableResumeOracle(ResumeCase):
    def test_the_ledger_records_resolutions_and_resumes(self):
        self.assertEqual(MUTATING_METHODS[-2:], ("resolve_unknown", "resume"))
        path = str(self.work / "ledger.db")
        ledger = self.scheduler(create_ledger(path))
        op = self.start_unknown(ledger)
        # A note the log could not encode is refused before the in-memory call
        # changes anything, so the ledger stays usable.
        with self.assertRaises(ContractError) as ctx:
            self.resolve(ledger, op, note="checked \udcff")
        self.assertEqual(ctx.exception.code, "E_NOTE")
        self.resolve(ledger, op, "ABANDONED")
        ledger.settle(op, {"tokens": 7})
        self.resume(ledger)
        again = self.claim(ledger, "task-1", key="idem-task-1-again")
        expected = (ledger.events, ledger.snapshot("req-1"), ledger.operation(op), ledger.operation(again))
        ledger.close()
        with open_ledger(path) as reopened:
            self.assertEqual((reopened.events, reopened.snapshot("req-1"), reopened.operation(op),
                              reopened.operation(again)), expected)
            # Still resumed after the replay: another task can be claimed.
            self.claim(reopened, "task-2")
        with open_ledger(path) as reopened:
            with self.assertRaises(ContractError) as ctx:
                self.resume(reopened)
            self.assertEqual(ctx.exception.code, "E_NOT_STOPPED")
            reopened.seed_task("req-1", "task-9", CAPS)
        with self.assertRaises(LedgerError):
            ledger.operation(op)


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class CrashToResumeOracle(ResumeCase):
    def test_a_crashed_controller_is_recovered_resolved_resumed_and_runs_again(self):
        ledger_path = str(self.work / "ledger.db")
        cwd = self.work / "tree"
        cwd.mkdir()
        evidence = {name: self.work / ("evidence-" + name) for name in ("reviewed", "crashed", "again")}
        for path in evidence.values():
            path.mkdir()
        started, release = self.work / "started", self.work / "release"
        self.addCleanup(lambda: release.touch())
        config = {"ledger": ledger_path, "python": PY, "cwd": str(cwd),
                  "evidence": {k: str(v) for k, v in evidence.items()},
                  "wait": WAIT_FOR_RELEASE, "started": str(started), "release": str(release)}
        controller = subprocess.Popen([PY, "-c", CONTROLLER, json.dumps(config)], cwd=str(REPO_ROOT),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: controller.poll() is None and controller.kill())
        deadline = time.monotonic() + 30
        while pid_in(started) is None and time.monotonic() < deadline and controller.poll() is None:
            time.sleep(0.01)
        child = pid_in(started)
        self.assertIsNotNone(child, "the supervised child never started")
        controller.kill()
        out, err = controller.communicate(timeout=60)
        self.assertEqual(controller.returncode, -signal.SIGKILL, err)
        ops = json.loads(out.strip().splitlines()[-1])
        release.touch()
        deadline = time.monotonic() + 30
        while not gone(child) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(gone(child), "the supervised child never exited")

        ledger = open_ledger(ledger_path)
        report = recover_after_restart(ledger, owner="owner-2", candidate_hash=lambda directory: CANDIDATE)
        self.assertEqual(report["operations"], [{"operation_id": ops["crashed"], "action": "recovered",
                                                 "state": "START_UNKNOWN"}])
        with self.assertRaises(ContractError) as ctx:
            self.resume(ledger, generation=2, expected_reasons=["RECOVERY_REQUIRED"])
        self.assertEqual(ctx.exception.code, "E_RESUME_UNKNOWN")
        # The operator looks at the run, enters what it cost and abandons it,
        # and gives the earlier run its verdict.
        ledger.settle(ops["crashed"], {"tokens": 9})
        # The bridge recorded a run descriptor: an operator may not call the run
        # unstarted, whatever the records say (section 10, decision 1).
        with self.assertRaises(ContractError) as ctx:
            self.resolve(ledger, ops["crashed"], "NOT_STARTED", generation=2)
        self.assertEqual(ctx.exception.code, "E_RESOLVE_CONFLICT")
        self.resolve(ledger, ops["crashed"], "ABANDONED", generation=2)
        ledger.fixture_verdict(ops["reviewed"], "c" * 64, True, EVIDENCE)
        self.resume(ledger, generation=2, expected_reasons=["RECOVERY_REQUIRED"])
        ledger.close()

        ledger = open_ledger(ledger_path)
        self.addCleanup(ledger.close)
        again = ledger.claim("req-1", "task-2", 1, "idem-task-2-again", "owner-2", 2, 1, RESERVATION,
                             ["src/task-2.py"])["operation_id"]
        spec = InvocationSpec("req-1", again, (PY, "-c", "pass"), str(cwd), environment_digest(BASE_ENV), 30)
        outcome = run_supervised_operation(ledger, operation_id=again, owner="owner-2", generation=2, spec=spec,
                                           environment=BASE_ENV, evidence_dir=str(evidence["again"]),
                                           worktree_id="wt-1", candidate_hash=lambda directory: CANDIDATE,
                                           before_finish=lambda run: ledger.settle(again, {"tokens": 3}))
        self.assertEqual(outcome["state"], "REVIEW_PENDING")
        self.assertIsNone(ledger._scheduler._requests["req-1"].stopped)
        tokens = ledger.snapshot("req-1")["tokens"]
        self.assertEqual((tokens["known"], tokens["held"], tokens["unknown_operations"]), (5 + 9 + 3, 0, 0))
        types = [e["type"] for e in ledger.events]
        self.assertEqual([t for t in types if t in ("scheduler.unknown_resolved", "request.resumed")],
                         ["scheduler.unknown_resolved", "request.resumed"])


if __name__ == "__main__":
    unittest.main()
