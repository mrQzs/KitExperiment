"""Executor cases for restart-recovery (`.agent/design/durable-ledger.md`, section 5 item 3).

These cover what the planner oracle does not: the recovery driver called twice,
a recovery that failed and is retried, the two evidence outcomes the records
alone decide (``START_UNKNOWN`` for evidence that is gone, ``NOT_STARTED`` for a
handoff nothing was ever recorded for), a recovery on the durable ledger that
survives a reopen, the claim order of ``reserve``, and every rejection of a run
descriptor -- each one after a legal baseline, with exactly one thing changed.

Every rejection names its contract code and leaves the ledger exactly as it was:
no event, no state change, no stored descriptor.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh.durable_ledger import LedgerError, create_ledger, open_ledger
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.restart_recovery import recover_after_restart
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.scheduler import ContractError, Scheduler
from sentinelmesh.supervised_operation import run_supervised_operation
from sentinelmesh.supervised_record import record_supervised_run

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
CAPS = {"dispatches": 9, "tokens": 500}
RESERVATION = {"dispatches": 1, "tokens": 40}
USAGE = {"tokens": 5}
NEW = "d" * 64
DONE = "import os; os.write(1, b'done\\n')"
# An executable that cannot be resolved: the recorder refuses before the start,
# so the handoff is on disk and the evidence directory holds nothing.
MISSING = "definitely-not-an-executable-sentinelmesh"
TASKS = ("task-1", "task-2", "task-3", "task-4", "task-5", "task-6")
# Operation ids that sort in the opposite order to the one they are claimed in.
LATE_FIRST = ("op-" + "f" * 32, "op-" + "e" * 32)


class Key(str):
    """A ``str`` subclass: equal to the text it spells, but not exact text."""


class Descriptor(dict):
    """A ``dict`` subclass: equal to the descriptor it holds, but not an exact dict."""


class Coded(Exception):
    """An exception carrying a stable code, as the runtime's errors do."""

    code = "E_SOME_CODE"


class OddCode(Exception):
    """An exception whose ``code`` is not exact text."""

    code = 7


class RecoveryCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="restart-recovery-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.candidates = []

    def candidate_hash(self, evidence_dir):
        self.candidates.append(evidence_dir)
        return NEW

    def evidence(self, name):
        path = self.work / ("evidence-" + name)
        path.mkdir()
        return path

    def scheduler(self, ledger=None):
        scheduler = ledger or Scheduler()
        scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=8)
        for task in TASKS:
            scheduler.seed_task("req-1", task, CAPS, max_attempts=20)
        return scheduler

    def claim(self, scheduler, task):
        return scheduler.claim("req-1", task, 1, "idem-" + task, "owner-1", 1, 1, RESERVATION,
                               ["src/%s.py" % task])["operation_id"]

    def spec(self, op, code=DONE, command=None):
        return InvocationSpec("req-1", op, command or (PY, "-c", code), str(self.cwd),
                              environment_digest(BASE_ENV), 30)

    def descriptor(self, op, evidence, *, spec=None):
        spec = spec or self.spec(op)
        return {"spec": {"request_id": spec.request_id, "run_id": spec.run_id, "command": list(spec.command),
                         "cwd": spec.cwd, "environment_sha256": spec.environment_sha256,
                         "timeout_seconds": spec.timeout_seconds},
                "evidence_dir": str(evidence), "worktree_id": "wt-1"}

    def finished_run(self, scheduler, task, *, descriptor=True, running=False):
        """A handoff whose run ended and was recorded, but was never settled."""
        op = self.claim(scheduler, task)
        evidence = self.evidence(task)
        spec = self.spec(op)
        if descriptor:
            scheduler.handoff(op, "owner-1", 1, run=self.descriptor(op, evidence, spec=spec))
        else:
            scheduler.handoff(op, "owner-1", 1)
        record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
        if running:
            scheduler.mark_running(op, "owner-1", 1, process_identity={"pid": 1, "boot_id": "boot-1",
                                                                       "executable_sha256": "0" * 64,
                                                                       "worktree_id": "wt-1"})
        return op, str(evidence)

    def reviewed_run(self, scheduler, task):
        """A handoff the old controller settled itself, usage included."""
        op = self.claim(scheduler, task)
        run_supervised_operation(scheduler, operation_id=op, owner="owner-1", generation=1, spec=self.spec(op),
                                 environment=BASE_ENV, evidence_dir=str(self.evidence(task)), worktree_id="wt-1",
                                 candidate_hash=lambda directory: "c" * 64,
                                 before_finish=lambda run: scheduler.settle(op, USAGE))
        return op


class RunDescriptorLedger(RecoveryCase):
    """The descriptor the ledger records at handoff, and how it is read back."""

    def test_a_valid_descriptor_is_recorded_and_read_back(self):
        scheduler = self.scheduler()
        with mock.patch.object(Scheduler, "_new_operation_id", side_effect=LATE_FIRST):
            first = self.claim(scheduler, "task-1")
            second = self.claim(scheduler, "task-2")
        # ``reserve`` is the same claim: its operation joins the same order.
        third = scheduler.reserve("req-1", "task-3", 1, "idem-task-3", "owner-1", 1, 1, RESERVATION,
                                  ["src/task-3.py"])["operation_id"]
        self.assertEqual(scheduler.operation_ids(), (first, second, third), "not in claim order")
        evidence = self.evidence("one")
        descriptor = self.descriptor(first, evidence)
        before = scheduler.events
        scheduler.handoff(first, "owner-1", 1, run=descriptor)
        # The caller keeps its objects: mutating them afterwards changes nothing.
        descriptor["spec"]["command"].append("--late")
        descriptor["evidence_dir"] = "/elsewhere"
        expected = self.descriptor(first, evidence)
        self.assertEqual(scheduler.run_descriptor(first), expected, "the ledger kept the caller's objects")
        self.assertEqual(scheduler.run_descriptor(second), None)
        handed_off = [e for e in scheduler.events if e["type"] == "scheduler.handoff_accepted"]
        self.assertEqual([e["payload"] for e in handed_off], [{"generation": 1}], "the handoff event changed")
        # Reading records no event either.
        before = scheduler.events
        self.assertEqual(scheduler.operation_ids(), (first, second, third))
        self.assertEqual(scheduler.run_descriptor(first), expected)
        self.assertEqual(scheduler.events, before, "a read recorded an event")

    def test_the_read_returns_a_copy_the_caller_cannot_write_through(self):
        scheduler = self.scheduler()
        op = self.claim(scheduler, "task-1")
        evidence = self.evidence("copy")
        scheduler.handoff(op, "owner-1", 1, run=self.descriptor(op, evidence))
        first = scheduler.run_descriptor(op)
        first["spec"]["command"].append("--extra")
        first["spec"]["cwd"] = "/elsewhere"
        first["evidence_dir"] = "/elsewhere"
        self.assertEqual(scheduler.run_descriptor(op), self.descriptor(op, evidence),
                         "the read shares state with the ledger")
        # A handoff without a descriptor stores none, and reading one back is
        # not an error.
        other = self.claim(scheduler, "task-2")
        scheduler.handoff(other, "owner-1", 1)
        self.assertIsNone(scheduler.run_descriptor(other))

    def test_the_reads_refuse_a_foreign_operation(self):
        scheduler = self.scheduler()
        op = self.claim(scheduler, "task-1")
        scheduler.handoff(op, "owner-1", 1, run=self.descriptor(op, self.evidence("known")))
        for value in ("op-missing", None, [op], Key(op), 7):
            with self.subTest(operation=repr(value)):
                with self.assertRaises(ContractError) as ctx:
                    scheduler.run_descriptor(value)
                self.assertEqual(ctx.exception.code, "E_OPERATION_UNKNOWN")

    def test_a_malformed_descriptor_is_refused_after_a_legal_baseline(self):
        def without(spec_key=None, run_key=None, **changes):
            def mutate(descriptor):
                spec = dict(descriptor["spec"])
                if spec_key is not None:
                    spec.pop(spec_key)
                spec.update(changes)
                result = dict(descriptor, spec=spec)
                if run_key is not None:
                    result.pop(run_key)
                return result
            return mutate

        cases = {
            "not a dict": lambda d: [d],
            "a dict subclass": lambda d: Descriptor(d),
            "a missing key": without(run_key="worktree_id"),
            "an extra key": lambda d: dict(d, note="x"),
            "a key that is not exact text": lambda d: {Key("spec"): d["spec"], "evidence_dir": d["evidence_dir"],
                                                       "worktree_id": d["worktree_id"]},
            "a spec that is not a dict": lambda d: dict(d, spec=None),
            "a spec that is a list": lambda d: dict(d, spec=[]),
            "a spec that is a dict subclass": lambda d: dict(d, spec=Descriptor(d["spec"])),
            "a spec without cwd": without(spec_key="cwd"),
            "a spec with an extra key": without(shell=False),
            "a spec key that is not exact text": lambda d: dict(d, spec={Key("cwd"): d["spec"]["cwd"],
                                                                        **{k: v for k, v in d["spec"].items()
                                                                           if k != "cwd"}}),
            "a command that is a tuple": without(command=tuple(self.spec("op-" + "0" * 32).command)),
            "a command with a non-text part": without(command=[PY, 7]),
            "an empty command": without(command=[]),
            "a command with a NUL": without(command=[PY, "-c", "pass\x00"]),
            "a command with a lone surrogate": without(command=[PY, "-c", "pass\udcff"]),
            "a request that is not text": without(request_id=None),
            "a run id that is not text": without(run_id=None),
            "a cwd that is not text": without(cwd=None),
            "an environment digest that is not text": without(environment_sha256=None),
            "an environment digest the runtime refuses": without(environment_sha256="not-a-digest"),
            "a timeout that is a float": without(timeout_seconds=1.5),
            "a timeout that is a bool": without(timeout_seconds=True),
            "a timeout the runtime refuses": without(timeout_seconds=0),
            "a negative timeout": without(timeout_seconds=-1),
            "a timeout past the integer envelope": without(timeout_seconds=2 ** 53),
            "another run id": without(run_id="op-" + "0" * 32),
            "another request": without(request_id="req-2"),
            "an empty evidence directory": lambda d: dict(d, evidence_dir=""),
            "an evidence directory that is not text": lambda d: dict(d, evidence_dir=None),
            "an evidence directory with a NUL": lambda d: dict(d, evidence_dir=d["evidence_dir"] + "\x00"),
            "an evidence directory with a newline": lambda d: dict(d, evidence_dir=d["evidence_dir"] + "\n"),
            "an evidence directory with a delete character": lambda d: dict(d, evidence_dir=d["evidence_dir"] + "\x7f"),
            "an evidence directory with a lone surrogate": lambda d: dict(d, evidence_dir=d["evidence_dir"] + "\udcff"),
            "an empty worktree": lambda d: dict(d, worktree_id=""),
            "a worktree that is not text": lambda d: dict(d, worktree_id=None),
            "a worktree with a control character": lambda d: dict(d, worktree_id="wt\n1"),
        }
        for index, (label, mutate) in enumerate(cases.items()):
            with self.subTest(case=label):
                scheduler = self.scheduler()
                evidence = self.work / ("baseline-%d" % index)
                evidence.mkdir()
                baseline = self.claim(scheduler, "task-1")
                good = self.descriptor(baseline, evidence)
                # The legal baseline first: the same shape, with one field set
                # to the value this case changes.
                scheduler.handoff(baseline, "owner-1", 1, run=good)
                self.assertEqual(scheduler.run_descriptor(baseline), self.descriptor(baseline, evidence))
                op = self.claim(scheduler, "task-2")
                before = scheduler.events
                with self.assertRaises(ContractError) as ctx:
                    scheduler.handoff(op, "owner-1", 1, run=mutate(self.descriptor(op, evidence)))
                self.assertEqual(ctx.exception.code, "E_RUN_DESCRIPTOR")
                self.assertEqual(scheduler.events, before, "a refused handoff emitted an event")
                self.assertEqual(scheduler.operation(op)["state"], "RESERVED")
                self.assertIsNone(scheduler.run_descriptor(op), "a refused handoff stored a descriptor")
                self.assertEqual(scheduler.run_descriptor(baseline), self.descriptor(baseline, evidence))

    def test_the_descriptor_is_checked_after_the_checks_the_handoff_already_had(self):
        scheduler = self.scheduler()
        evidence = self.evidence("order")
        bad = dict(self.descriptor(self.claim(scheduler, "task-1"), evidence), note="an extra key")

        def refused(code, call):
            before = scheduler.events
            with self.assertRaises(ContractError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, code)
            self.assertEqual(scheduler.events, before)

        # ``now`` is checked first, before the descriptor.
        first = self.claim(scheduler, "task-1")
        refused("E_INTEGER", lambda: scheduler.handoff(first, "owner-1", 1, "0", run=bad))
        # A foreign owner is fenced before the descriptor is looked at.
        refused("E_FENCED", lambda: scheduler.handoff(first, "owner-2", 1, run=bad))
        refused("E_FENCED", lambda: scheduler.handoff(first, "owner-1", 2, run=bad))
        # The state and the lease come next: an expired lease is a handoff
        # problem, not a descriptor one.
        refused("E_HANDOFF_STATE", lambda: scheduler.handoff(first, "owner-1", 1, 10 ** 6, run=bad))
        # A stopped request comes next.
        scheduler.stop("req-1")
        refused("E_STOPPED", lambda: scheduler.handoff(first, "owner-1", 1, run=bad))
        # ``run`` is keyword-only: a positional descriptor is a wrong call, and
        # it is refused before anything is recorded.
        with self.assertRaises(TypeError):
            scheduler.handoff(first, "owner-1", 1, 0, bad)
        self.assertEqual(scheduler.operation(first)["state"], "RESERVED")
        self.assertIsNone(scheduler.run_descriptor(first))

    def test_the_durable_ledger_records_the_descriptor_with_the_handoff(self):
        path = str(self.work / "ledger.db")
        ledger = self.scheduler(create_ledger(path))
        first = self.claim(ledger, "task-1")
        second = self.claim(ledger, "task-2")
        evidence = self.evidence("durable")
        descriptor = self.descriptor(first, evidence)
        # A descriptor the log could not record is refused before the in-memory
        # handoff, so the ledger stays usable and the log stays replayable.
        with self.assertRaises(ContractError) as ctx:
            ledger.handoff(first, "owner-1", 1, run=dict(descriptor, worktree_id="wt-\udcff"))
        self.assertEqual(ctx.exception.code, "E_RUN_DESCRIPTOR")
        ledger.handoff(first, "owner-1", 1, run=descriptor)
        ledger.handoff(second, "owner-1", 1)
        self.assertEqual((ledger.operation_ids(), ledger.run_descriptor(first), ledger.run_descriptor(second)),
                         ((first, second), self.descriptor(first, evidence), None))
        before = ledger.events
        ledger.close()
        reopened = open_ledger(path)
        self.assertEqual((reopened.operation_ids(), reopened.run_descriptor(first),
                          reopened.run_descriptor(second)),
                         ((first, second), self.descriptor(first, evidence), None))
        self.assertEqual(reopened.events, before, "the handoff events changed on replay")
        reopened.close()


class RecoverAfterRestartLedger(RecoveryCase):
    """The recovery driver on an in-memory ledger, without any recorded run."""

    def test_the_inputs_are_checked_before_the_rollover(self):
        scheduler = self.scheduler()
        self.claim(scheduler, "task-1")
        before = scheduler.events
        for label, owner, candidate_hash in (("no owner", None, self.candidate_hash),
                                             ("an empty owner", "", self.candidate_hash),
                                             ("an owner that is not text", 7, self.candidate_hash),
                                             ("an owner that is not exact text", Key("owner-2"), self.candidate_hash),
                                             ("no candidate callback", "owner-2", None),
                                             ("a candidate that cannot be called", "owner-2", NEW)):
            with self.subTest(case=label):
                with mock.patch.object(Scheduler, "rollover") as rollover:
                    with self.assertRaises(ContractError) as ctx:
                        recover_after_restart(scheduler, owner=owner, candidate_hash=candidate_hash)
                    rollover.assert_not_called()
                self.assertEqual(ctx.exception.code, "E_RECOVERY_INPUT")
                self.assertEqual(scheduler.events, before)
                self.assertEqual(scheduler.generation, 1, "a refused recovery rolled the ledger over")
        self.assertIsNone(scheduler._requests["req-1"].stopped)

    def test_a_claim_that_never_reached_a_handoff_is_cancelled(self):
        scheduler = self.scheduler()
        op = self.claim(scheduler, "task-1")
        report = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(report, {"generation": 2, "operations": [
            {"operation_id": op, "action": "cancelled", "state": "CANCELLED_NOT_STARTED"}]})
        self.assertEqual(scheduler.operation(op)["state"], "CANCELLED_NOT_STARTED")
        self.assertEqual(scheduler.generation, 2)
        self.assertEqual(scheduler._requests["req-1"].stopped, "RECOVERY_REQUIRED")
        # The reservation and the hold are given back.
        self.assertEqual(scheduler._resource_holds, {})
        totals = scheduler.snapshot("req-1")
        self.assertEqual((totals["tokens"]["held"], totals["tokens"]["known"]), (0, 0))
        self.assertEqual((totals["dispatches"]["held"], totals["dispatches"]["known"]), (0, 0))
        self.assertEqual(self.candidates, [], "a cancelled claim needed no candidate hash")

    def test_a_handoff_without_a_descriptor_is_skipped(self):
        scheduler = self.scheduler()
        op = self.claim(scheduler, "task-1")
        scheduler.handoff(op, "owner-1", 1)
        report = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(report, {"generation": 2, "operations": [
            {"operation_id": op, "action": "skipped", "state": "HANDOFF_ACCEPTED", "reason": "no run descriptor"}]})
        self.assertEqual((scheduler.operation(op)["state"], scheduler.operation(op)["generation"],
                          scheduler.operation(op)["owner"]), ("HANDOFF_ACCEPTED", 1, "owner-1"))
        self.assertEqual(self.candidates, [])
        self.assertEqual(scheduler._requests["req-1"].stopped, "RECOVERY_REQUIRED")

    def test_the_report_follows_the_claim_order(self):
        scheduler = self.scheduler()
        with mock.patch.object(Scheduler, "_new_operation_id", side_effect=LATE_FIRST):
            late = self.claim(scheduler, "task-1")
            early = self.claim(scheduler, "task-2")
        reserved = self.claim(scheduler, "task-3")
        scheduler.handoff(early, "owner-1", 1)
        report = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual([row["operation_id"] for row in report["operations"]], [late, early, reserved])
        self.assertEqual([row["action"] for row in report["operations"]],
                         ["cancelled", "skipped", "cancelled"])

    def test_a_second_recovery_only_acts_on_what_is_left(self):
        scheduler = self.scheduler()
        # An operation the old controller already decided.
        decided = self.claim(scheduler, "task-1")
        # A handoff proven not to have started, and a cancelled claim.
        unstarted = self.claim(scheduler, "task-2")
        cancelled = self.claim(scheduler, "task-3")
        # A handoff the ledger holds no descriptor for.
        bare = self.claim(scheduler, "task-4")
        scheduler.handoff(decided, "owner-1", 1)
        scheduler.handoff(unstarted, "owner-1", 1)
        scheduler.handoff(bare, "owner-1", 1)
        scheduler.unknown(decided, "owner-1", 1, reason="START_UNKNOWN")
        scheduler.release_proven_unstarted(unstarted, "owner-1", 1, evidence_hash="a" * 64)
        scheduler.cancel_unstarted(cancelled, 1)
        before = {op: scheduler.operation(op) for op in (decided, unstarted, cancelled)}
        bare_before = scheduler.operation(bare)
        first = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(first, {"generation": 2, "operations": [
            {"operation_id": bare, "action": "skipped", "state": "HANDOFF_ACCEPTED", "reason": "no run descriptor"}]})
        second = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(second, {"generation": 3, "operations": [
            {"operation_id": bare, "action": "skipped", "state": "HANDOFF_ACCEPTED", "reason": "no run descriptor"}]})
        # Everything that was already decided or cancelled is unchanged, and
        # none of it is reported again.
        self.assertEqual({op: scheduler.operation(op) for op in (decided, unstarted, cancelled)}, before)
        self.assertNotIn(decided, [row["operation_id"] for row in second["operations"]])
        self.assertNotIn(unstarted, [row["operation_id"] for row in second["operations"]])
        self.assertNotIn(cancelled, [row["operation_id"] for row in second["operations"]])
        self.assertEqual(scheduler.operation(bare), bare_before)

    def test_a_failure_is_reported_per_operation_and_the_recovery_continues(self):
        scheduler = self.scheduler()
        first = self.claim(scheduler, "task-1")
        second = self.claim(scheduler, "task-2")
        third = self.claim(scheduler, "task-3")
        failures = {first: Coded("no descriptor"), second: OddCode("a code that is not text"),
                    third: ValueError("nothing carries a code")}
        expected = {first: "E_SOME_CODE", second: "OddCode", third: "ValueError"}

        def cancel(self, operation, generation):
            if operation in failures:
                raise failures[operation]

        with mock.patch.object(Scheduler, "cancel_unstarted", cancel):
            report = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(report, {"generation": 2, "operations": [
            {"operation_id": first, "action": "failed", "state": "RESERVED", "error": expected[first]},
            {"operation_id": second, "action": "failed", "state": "RESERVED", "error": expected[second]},
            {"operation_id": third, "action": "failed", "state": "RESERVED", "error": expected[third]}]})
        for op in (first, second, third):
            self.assertEqual(scheduler.operation(op)["state"], "RESERVED", "a failed recovery changed its operation")
        # The reason gone, the same operations are cancelled by the next call.
        second_report = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(second_report, {"generation": 3, "operations": [
            {"operation_id": first, "action": "cancelled", "state": "CANCELLED_NOT_STARTED"},
            {"operation_id": second, "action": "cancelled", "state": "CANCELLED_NOT_STARTED"},
            {"operation_id": third, "action": "cancelled", "state": "CANCELLED_NOT_STARTED"}]})

    def test_a_ledger_error_stops_the_recovery_and_is_not_reported(self):
        scheduler = self.scheduler()
        first = self.claim(scheduler, "task-1")
        second = self.claim(scheduler, "task-2")
        real = Scheduler.cancel_unstarted

        def cancel(self, operation, generation):
            if operation == first:
                raise LedgerError("E_LEDGER_WRITE", "the log could not be appended")
            return real(self, operation, generation)

        with mock.patch.object(Scheduler, "cancel_unstarted", cancel):
            with self.assertRaises(LedgerError) as ctx:
                recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(ctx.exception.code, "E_LEDGER_WRITE")
        self.assertEqual(scheduler.generation, 2, "the rollover itself is not undone")
        self.assertEqual(scheduler.operation(second)["state"], "RESERVED",
                         "the recovery continued past a ledger error")

    def test_an_interrupt_is_passed_on_unchanged(self):
        scheduler = self.scheduler()
        first = self.claim(scheduler, "task-1")
        second = self.claim(scheduler, "task-2")

        def cancel(self, operation, generation):
            if operation == first:
                raise KeyboardInterrupt

        with mock.patch.object(Scheduler, "cancel_unstarted", cancel):
            with self.assertRaises(KeyboardInterrupt):
                recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(scheduler.operation(second)["state"], "RESERVED",
                         "the recovery continued past an interrupt")
        self.assertEqual(scheduler.operation(first)["state"], "RESERVED")


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class RecoverAfterRestartRuns(RecoveryCase):
    """The recovery driver deciding from the records a real run left behind."""

    def test_a_finished_run_is_recovered_and_a_second_call_leaves_it_alone(self):
        scheduler = self.scheduler()
        reviewed = self.reviewed_run(scheduler, "task-1")
        claimed = self.claim(scheduler, "task-2")
        finished, evidence = self.finished_run(scheduler, "task-3")
        first = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(first, {"generation": 2, "operations": [
            {"operation_id": claimed, "action": "cancelled", "state": "CANCELLED_NOT_STARTED"},
            {"operation_id": finished, "action": "recovered", "state": "REVIEW_PENDING"}]})
        self.assertEqual(self.candidates, [evidence])
        after = {op: scheduler.operation(op) for op in (reviewed, claimed, finished)}
        second = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(second, {"generation": 3, "operations": []})
        self.assertEqual({op: scheduler.operation(op) for op in (reviewed, claimed, finished)}, after)
        self.assertEqual(self.candidates, [evidence], "a decided run was reviewed again")

    def test_a_running_operation_is_recovered_from_its_records(self):
        scheduler = self.scheduler()
        running, evidence = self.finished_run(scheduler, "task-1", running=True)
        self.assertEqual(scheduler.operation(running)["state"], "RUNNING")
        report = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(report, {"generation": 2, "operations": [
            {"operation_id": running, "action": "recovered", "state": "REVIEW_PENDING"}]})
        self.assertEqual((scheduler.operation(running)["owner"], scheduler.operation(running)["generation"]),
                         ("owner-2", 2))
        self.assertEqual(self.candidates, [evidence])

    def test_a_failed_recovery_is_reported_and_the_next_call_recovers_it(self):
        scheduler = self.scheduler()
        refused, evidence = self.finished_run(scheduler, "task-1")
        fine, _ = self.finished_run(scheduler, "task-2")
        calls = []

        def once_refusing(evidence_dir):
            calls.append(evidence_dir)
            if evidence_dir == evidence and len(calls) == 1:
                raise RuntimeError("no candidate for this run yet")
            return NEW

        first = recover_after_restart(scheduler, owner="owner-2", candidate_hash=once_refusing)
        self.assertEqual(first, {"generation": 2, "operations": [
            {"operation_id": refused, "action": "failed", "state": "HANDOFF_ACCEPTED", "error": "RuntimeError"},
            {"operation_id": fine, "action": "recovered", "state": "REVIEW_PENDING"}]})
        self.assertEqual((scheduler.operation(refused)["state"], scheduler.operation(refused)["generation"]),
                         ("HANDOFF_ACCEPTED", 1), "a failed recovery changed its operation")
        second = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(second, {"generation": 3, "operations": [
            {"operation_id": refused, "action": "recovered", "state": "REVIEW_PENDING"}]})
        self.assertEqual((scheduler.operation(refused)["owner"], scheduler.operation(refused)["generation"]),
                         ("owner-2", 3))

    def test_a_descriptor_whose_evidence_directory_is_gone_is_start_unknown(self):
        scheduler = self.scheduler()
        op, evidence = self.finished_run(scheduler, "task-1")
        shutil.rmtree(evidence)
        report = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(report, {"generation": 2, "operations": [
            {"operation_id": op, "action": "recovered", "state": "START_UNKNOWN"}]})
        self.assertEqual(scheduler.operation(op)["state"], "START_UNKNOWN")
        self.assertEqual(self.candidates, [], "an unknown start is never reviewed")
        self.assertEqual(scheduler._requests["req-1"].stopped, "RECOVERY_REQUIRED")
        self.assertEqual([e["payload"]["outcome"] for e in scheduler.events if e["type"] == "scheduler.recovered"],
                         ["START_UNKNOWN"])

    def test_an_evidence_directory_without_records_is_not_started(self):
        scheduler = self.scheduler()
        op = self.claim(scheduler, "task-1")
        evidence = self.evidence("refused")
        spec = self.spec(op, command=(MISSING, "-c", DONE))
        scheduler.handoff(op, "owner-1", 1, run=self.descriptor(op, evidence, spec=spec))
        # The recorder refuses before the start -- nothing was handed off to a
        # process, so the evidence directory holds no record at all.
        with self.assertRaises(RuntimeErrorCode):
            record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
        self.assertEqual(sorted(p.name for p in evidence.iterdir()), [])
        totals = scheduler.snapshot("req-1")
        self.assertEqual(totals["tokens"]["held"], 40, "the reservation is still held before the recovery")
        report = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(report, {"generation": 2, "operations": [
            {"operation_id": op, "action": "recovered", "state": "NOT_STARTED"}]})
        self.assertEqual(scheduler.operation(op)["state"], "NOT_STARTED")
        self.assertEqual(self.candidates, [])
        # The unused reservation and the hold are given back; the dispatch the
        # handoff recorded stays counted.
        released = scheduler.snapshot("req-1")
        self.assertEqual((released["tokens"]["held"], released["tokens"]["known"]), (0, 0))
        self.assertEqual((released["dispatches"]["known"], released["dispatches"]["held"]), (1, 0))
        self.assertEqual(scheduler._resource_holds, {})
        self.assertEqual([e["payload"]["outcome"] for e in scheduler.events if e["type"] == "scheduler.recovered"],
                         ["NOT_STARTED"])

    def test_the_recovery_on_a_durable_ledger_survives_a_reopen(self):
        path = str(self.work / "ledger.db")
        ledger = self.scheduler(create_ledger(path))
        reviewed = self.reviewed_run(ledger, "task-1")
        claimed = self.claim(ledger, "task-2")
        finished, evidence = self.finished_run(ledger, "task-3")
        bare, _ = self.finished_run(ledger, "task-4", descriptor=False)
        report = recover_after_restart(ledger, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(report, {"generation": 2, "operations": [
            {"operation_id": claimed, "action": "cancelled", "state": "CANCELLED_NOT_STARTED"},
            {"operation_id": finished, "action": "recovered", "state": "REVIEW_PENDING"},
            {"operation_id": bare, "action": "skipped", "state": "HANDOFF_ACCEPTED", "reason": "no run descriptor"}]})
        self.assertEqual(self.candidates, [evidence])
        expected = {op: ledger.operation(op) for op in (reviewed, claimed, finished, bare)}
        events = ledger.events
        descriptors = {op: ledger.run_descriptor(op) for op in (reviewed, claimed, finished, bare)}
        ledger.close()
        reopened = open_ledger(path)
        self.assertEqual({op: reopened.operation(op) for op in expected}, expected,
                         "the recovery did not survive a reopen")
        self.assertEqual({op: reopened.run_descriptor(op) for op in descriptors}, descriptors)
        self.assertEqual(reopened.events, events)
        self.assertEqual(reopened.generation, 2)
        reopened.close()


if __name__ == "__main__":
    unittest.main()
