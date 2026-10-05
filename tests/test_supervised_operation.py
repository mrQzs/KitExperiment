"""Cases for the supervised-operation bridge that the planner oracle does not cover.

The oracle (``tests/test_supervised_operation_oracle.py``) drives the happy paths
against real local process trees.  What is left out is exactly the conservative
half of the contract, so it is what this file pins down:

* a candidate hash that cannot be produced (an exception, or a value that is not
  a hash) must leave the operation ``START_UNKNOWN`` with its ``run.started``
  event recorded, and must re-raise the caller's own error;
* an interrupt that reaches ``record_supervised_run`` must settle the operation
  from the records that are already on disk -- nothing written is ``NOT_STARTED``,
  a handoff without its observation is ``START_UNKNOWN`` -- and must still reach
  the caller;
* ``evidence_digest`` has to survive every file state a record can be in
  (missing, directory, symlink, fifo, at the byte ceiling and one byte past it)
  without raising;
* ``recover_supervised_operation`` must refuse a specification for another run,
  leave the ledger untouched when the candidate hash fails, and let the
  scheduler's own fencing errors through.

Every rejection case first proves a legal baseline and then changes exactly one
condition; where the baseline itself stops the request (an unknown or a rollover),
the changed case runs on a freshly seeded scheduler so the two differ in that one
condition only.  Every case that can start a process kills the process group it
started, even when an assertion fails.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh import supervised_operation
from sentinelmesh.core import canonical_json
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.scheduler import ContractError, Scheduler
from sentinelmesh.supervised_operation import (
    evidence_digest,
    recover_supervised_operation,
    run_supervised_operation,
)
from sentinelmesh.supervised_record import (
    HANDOFF_NAME,
    OBSERVATION_NAME,
    record_supervised_run,
    replay_decision,
)

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
CANDIDATE = "c" * 64
CEILING = 65536
PRINT_OK = "print('ok')"
HANDOFF_BYTES = b"handoff bytes\n"
OBSERVATION_BYTES = b"observation bytes\n"


class OtherSpec(InvocationSpec):
    """An invocation spec that is not exactly an ``InvocationSpec``."""


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class SupervisedOperationBridgeTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="supervised-operation-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.scheduler = self.new_scheduler()
        self.count = 0
        self.candidates = []
        self.pids = []
        self.addCleanup(self.kill_started_groups)

    # fixtures --------------------------------------------------------------

    def new_scheduler(self):
        """Return a freshly seeded scheduler; every case starts from this state."""
        scheduler = Scheduler()
        scheduler.seed_request("req-1", "repo-1", {"dispatches": 5, "tokens": 400}, concurrency=4)
        for task in ("task-1", "task-2", "task-3"):
            scheduler.seed_task("req-1", task, {"dispatches": 5, "tokens": 400}, max_attempts=3)
        return scheduler

    def candidate_hash(self, evidence_dir):
        self.candidates.append(evidence_dir)
        return CANDIDATE

    def operation(self, task="task-1", resource="src/a.py"):
        """Claim one operation on ``task``; its process group is killed on cleanup."""
        self.count += 1
        claim = self.scheduler.claim("req-1", task, 1, "idem-%d" % self.count, "owner-1", 1, 1,
                                     {"dispatches": 1, "tokens": 40}, [resource])
        evidence = self.work / ("evidence-%d" % self.count)
        evidence.mkdir()
        return claim["operation_id"], evidence

    def spec(self, op, code=PRINT_OK, *args, timeout=10, command=None, environment=None):
        argv = command if command is not None else (PY, "-c", code, *args)
        return InvocationSpec("req-1", op, argv, str(self.cwd), environment_digest(environment or BASE_ENV), timeout)

    def other_spec(self, op, code=PRINT_OK):
        """Return the same invocation as a spec of another type."""
        return OtherSpec("req-1", op, (PY, "-c", code), str(self.cwd), environment_digest(BASE_ENV), 10)

    def run_op(self, op, spec, evidence, environment=None, candidate_hash=None, **kwargs):
        outcome = run_supervised_operation(self.scheduler, operation_id=op, owner="owner-1", generation=1,
                                           spec=spec, environment=environment or BASE_ENV,
                                           evidence_dir=str(evidence), worktree_id="wt-1",
                                           candidate_hash=candidate_hash or self.candidate_hash, **kwargs)
        self.track(op)
        return outcome

    def handed_off(self, task="task-1"):
        op, evidence = self.operation(task=task)
        self.scheduler.handoff(op, "owner-1", 1)
        return op, evidence

    def record(self, spec, evidence):
        """Write both records with the real recorder (a local ``python`` process)."""
        run = record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
        if run.identity is not None:
            self.pids.append(run.identity.pid)
        return run

    def recover(self, op, spec, evidence, owner="owner-2", generation=2, candidate_hash=None):
        return recover_supervised_operation(self.scheduler, operation_id=op, owner=owner, generation=generation,
                                            spec=spec, evidence_dir=str(evidence),
                                            candidate_hash=candidate_hash or self.candidate_hash)

    def interrupter(self, *args, **kwargs):
        """Stand in for the recorder and interrupt the caller instead."""
        raise KeyboardInterrupt("the controller was interrupted")

    # assertions ------------------------------------------------------------

    def state_of(self, op):
        return self.scheduler._operations[op].state

    def events_of(self, op, event_type):
        return [event for event in self.scheduler.events
                if event["operation_id"] == op and event["type"] == event_type]

    def track(self, op):
        """Remember the process group a supervised run of ``op`` reports."""
        for event in self.scheduler.events:
            if event["operation_id"] == op and event["type"] == "run.started":
                self.pids.append(event["payload"]["process_identity"]["pid"])

    def kill_started_groups(self):
        for pid in self.pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    def ledger(self):
        """Freeze everything a rejected call must not change."""
        return (self.scheduler.events,
                {op: (o.state, o.owner, o.generation, o.candidate_hash, dict(o.usage))
                 for op, o in self.scheduler._operations.items()},
                dict(self.scheduler._resource_holds),
                {request: root.stopped for request, root in self.scheduler._requests.items()})

    def assert_ledger_unchanged(self, before):
        self.assertEqual(self.ledger(), before, "a rejected call changed the ledger")

    def assert_reviewed(self, op, outcome, evidence, status="STOPPED"):
        self.assertEqual(outcome["state"], "REVIEW_PENDING")
        self.assertEqual(self.state_of(op), "REVIEW_PENDING")
        self.assertEqual((outcome["decision"]["status"], outcome["decision"]["replay_allowed"]), (status, True))
        started = self.events_of(op, "run.started")
        self.assertEqual(len(started), 1)
        observation = json.loads((Path(evidence) / OBSERVATION_NAME).read_bytes())["observation"]
        self.assertEqual(started[0]["payload"]["process_identity"], observation["process_identity"])
        self.assertEqual(self.events_of(op, "run.finished_for_review")[0]["payload"], {"candidate": CANDIDATE})
        self.assertEqual(self.candidates, [str(evidence)])
        self.assertNotIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)

    @staticmethod
    def expected_digest(handoff, observation):
        payload = json.dumps({"handoff": handoff, "observation": observation},
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def write_handoff(self, evidence, spec):
        """Write exactly the handoff record ``record_supervised_run`` publishes."""
        content = {"schema": "sentinelmesh-handoff-v1", "request_id": spec.request_id, "run_id": spec.run_id,
                   "environment_sha256": spec.environment_sha256, "spec_sha256": spec.digest()}
        (Path(evidence) / HANDOFF_NAME).write_bytes(canonical_json(content))

    def assert_start_unknown(self, op, reason="START_UNKNOWN"):
        self.assertEqual(self.state_of(op), "START_UNKNOWN")
        self.assertEqual(self.events_of(op, "scheduler.start_unknown")[0]["payload"], {"reason": reason})
        self.assertTrue(self.scheduler._requests["req-1"].stopped)
        self.assertIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)

    # a candidate that cannot be produced ----------------------------------

    def test_a_candidate_hash_that_cannot_be_computed_leaves_the_run_start_unknown(self):
        # Baseline: the same run with a working callback ends in review.
        op, evidence = self.operation()
        self.assert_reviewed(op, self.run_op(op, self.spec(op, PRINT_OK), evidence), evidence)
        # The changed case needs its own ledger: a reviewed operation stops the
        # request until its usage is final.
        self.scheduler = self.new_scheduler()
        self.candidates = []
        # One change: the callback raises instead of returning the candidate.
        op2, evidence2 = self.operation()
        calls = []

        def vanishing(evidence_dir):
            calls.append(evidence_dir)
            raise ValueError("the candidate vanished")

        with self.assertRaises(ValueError):
            self.run_op(op2, self.spec(op2, PRINT_OK), evidence2, candidate_hash=vanishing)
        self.assert_start_unknown(op2, "START_UNKNOWN")
        # The start is not undone: the process existed and its identity is recorded.
        started = self.events_of(op2, "run.started")
        self.assertEqual(len(started), 1)
        observation = json.loads((evidence2 / OBSERVATION_NAME).read_bytes())["observation"]
        self.assertEqual(started[0]["payload"]["process_identity"], observation["process_identity"])
        self.assertEqual(self.events_of(op2, "run.finished_for_review"), [])
        self.assertEqual(calls, [str(evidence2)])
        self.assertIsNone(self.scheduler._operations[op2].candidate_hash)

    def test_a_candidate_hash_that_is_not_a_hash_leaves_the_run_start_unknown(self):
        op, evidence = self.operation()
        self.assert_reviewed(op, self.run_op(op, self.spec(op, PRINT_OK), evidence), evidence)
        # The changed case needs its own ledger (see the test above).
        self.scheduler = self.new_scheduler()
        self.candidates = []
        # One change: the callback returns text that is not a SHA-256.
        op2, evidence2 = self.operation()
        with self.assertRaises(ContractError) as ctx:
            self.run_op(op2, self.spec(op2, PRINT_OK), evidence2, candidate_hash=lambda directory: "deadbeef")
        self.assertEqual(ctx.exception.code, "E_CLEANUP_OR_CANDIDATE")
        self.assert_start_unknown(op2, "START_UNKNOWN")
        self.assertEqual(len(self.events_of(op2, "run.started")), 1)
        self.assertEqual(self.events_of(op2, "run.finished_for_review"), [])
        self.assertIsNone(self.scheduler._operations[op2].candidate_hash)

    # an interrupt inside the recorder -------------------------------------

    def test_an_interrupt_before_any_record_is_not_started(self):
        # Baseline: the supervisor refused the input, so no record was written.
        environment = {"LANG": "C", "PATH": "/usr/bin:/bin", "SERVICE_API_TOKEN": "secret-value"}
        op, evidence = self.operation()
        outcome = self.run_op(op, self.spec(op, "pass", environment=environment), evidence, environment=environment)
        self.assertEqual((outcome["state"], outcome["error"]), ("NOT_STARTED", "E_RUNTIME_ENVIRONMENT"))
        self.assertEqual(os.listdir(evidence), [])
        self.assertIsNone(self.scheduler._requests["req-1"].stopped)
        # One change: the recorder is interrupted instead of refusing.
        op2, evidence2 = self.operation(task="task-2")
        with mock.patch.object(supervised_operation, "record_supervised_run", self.interrupter):
            with self.assertRaises(KeyboardInterrupt):
                self.run_op(op2, self.spec(op2, "pass"), evidence2)
        self.assertEqual(self.state_of(op2), "NOT_STARTED")
        self.assertEqual(os.listdir(evidence2), [])
        self.assertEqual(self.events_of(op2, "run.started"), [])
        self.assertEqual(self.events_of(op2, "scheduler.not_started")[0]["payload"],
                         {"evidence": evidence_digest(str(evidence2))})
        self.assertNotIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        self.assertIsNone(self.scheduler._requests["req-1"].stopped)

    def test_an_interrupt_between_the_handoff_and_the_observation_is_start_unknown(self):
        # Baseline: a handoff record on disk whose observation is missing -- the
        # evidence an interrupted run leaves -- is a START_UNKNOWN decision when
        # the record is the one the module wrote.
        op, evidence = self.handed_off()
        spec = self.spec(op, PRINT_OK)
        self.record(spec, evidence)
        os.unlink(evidence / OBSERVATION_NAME)
        self.assertEqual(replay_decision(spec, str(evidence))["status"], "START_UNKNOWN")
        # The changed case needs its own ledger: the baseline's operation is still in flight
        # (it holds task-1 and its resource), so a second one needs a fresh claim.
        self.scheduler = self.new_scheduler()
        op2, evidence2 = self.operation()
        spec = self.spec(op2, "pass")
        self.write_handoff(evidence2, spec)
        with mock.patch.object(supervised_operation, "record_supervised_run", self.interrupter):
            with self.assertRaises(KeyboardInterrupt):
                self.run_op(op2, spec, evidence2)
        self.assertEqual(self.state_of(op2), "START_UNKNOWN")
        self.assertEqual(self.events_of(op2, "scheduler.start_unknown")[0]["payload"], {"reason": "START_UNKNOWN"})
        self.assertEqual(self.events_of(op2, "run.started"), [])
        self.assertEqual(os.listdir(evidence2), [HANDOFF_NAME])
        self.assertTrue(self.scheduler._requests["req-1"].stopped)
        self.assertIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        # The record the bridge decided from is the one the module writes: any
        # other bytes would make the decision IDENTITY_MISMATCH or a refusal.
        self.assertEqual(replay_decision(spec, str(evidence2))["status"], "START_UNKNOWN")

    # evidence_digest over every file state ---------------------------------

    def test_evidence_digest_hashes_the_records_a_real_run_left(self):
        op, evidence = self.operation()
        self.run_op(op, self.spec(op, PRINT_OK), evidence)
        self.assertEqual(evidence_digest(str(evidence)), self.expected_digest(
            hashlib.sha256((evidence / HANDOFF_NAME).read_bytes()).hexdigest(),
            hashlib.sha256((evidence / OBSERVATION_NAME).read_bytes()).hexdigest()))

    def test_evidence_digest_reports_an_unreadable_record(self):
        def records(name):
            directory = self.work / name
            directory.mkdir()
            (directory / HANDOFF_NAME).write_bytes(HANDOFF_BYTES)
            (directory / OBSERVATION_NAME).write_bytes(OBSERVATION_BYTES)
            return directory

        digest_of = self.expected_digest
        intact = hashlib.sha256(OBSERVATION_BYTES).hexdigest()
        with self.subTest(record="nothing was recorded"):
            empty = self.work / "empty"
            empty.mkdir()
            self.assertEqual(evidence_digest(str(empty)), digest_of(None, None))
        with self.subTest(record="only the handoff was recorded"):
            partial = records("partial")
            os.unlink(partial / OBSERVATION_NAME)
            self.assertEqual(evidence_digest(str(partial)), digest_of(hashlib.sha256(HANDOFF_BYTES).hexdigest(), None))
        # Every case below changes exactly one record's file state; the other
        # record keeps digesting by content, which is the baseline.
        with self.subTest(record="the record is a directory"):
            directory = records("directory")
            os.unlink(directory / HANDOFF_NAME)
            (directory / HANDOFF_NAME).mkdir()
            self.assertEqual(evidence_digest(str(directory)), digest_of("unreadable", intact))
        with self.subTest(record="the record is a symlink"):
            link = records("symlink")
            os.unlink(link / HANDOFF_NAME)
            os.symlink(link / OBSERVATION_NAME, link / HANDOFF_NAME)
            self.assertEqual(evidence_digest(str(link)), digest_of("unreadable", intact))
        with self.subTest(record="the record is a fifo"):
            fifo = records("fifo")
            os.unlink(fifo / HANDOFF_NAME)
            os.mkfifo(fifo / HANDOFF_NAME)
            self.assertEqual(evidence_digest(str(fifo)), digest_of("unreadable", intact))
        with self.subTest(record="the record is one byte past the ceiling"):
            oversized = records("oversized")
            (oversized / HANDOFF_NAME).write_bytes(b"x" * (CEILING + 1))
            self.assertEqual(evidence_digest(str(oversized)), digest_of("unreadable", intact))

    def test_evidence_digest_reads_a_record_of_exactly_the_ceiling(self):
        directory = self.work / "ceiling"
        directory.mkdir()
        (directory / HANDOFF_NAME).write_bytes(b"x" * CEILING)
        (directory / OBSERVATION_NAME).write_bytes(OBSERVATION_BYTES)
        self.assertEqual(evidence_digest(str(directory)), self.expected_digest(
            hashlib.sha256(b"x" * CEILING).hexdigest(), hashlib.sha256(OBSERVATION_BYTES).hexdigest()))

    def test_evidence_digest_does_not_raise_for_a_missing_directory(self):
        missing = self.work / "never-created"
        self.assertEqual(evidence_digest(str(missing)), self.expected_digest(None, None))

    # what is accepted before anything happens -------------------------------

    def test_a_spec_that_is_not_the_invocation_is_refused_before_the_handoff(self):
        # Baseline: the same run_id with an InvocationSpec runs.
        op, evidence = self.operation()
        self.assert_reviewed(op, self.run_op(op, self.spec(op, PRINT_OK), evidence), evidence)
        # The rejections need a claimable request (see above) and one task per
        # case: an operation that never handed off still holds its task.
        self.scheduler = self.new_scheduler()
        self.candidates = []
        for index, wrong in enumerate(("text", "mapping", "another type"), start=1):
            with self.subTest(spec=wrong):
                op2, evidence2 = self.operation(task="task-%d" % index, resource="src/%d.py" % index)
                before = self.ledger()
                if wrong == "text":
                    value = "not a spec"
                elif wrong == "mapping":
                    value = {"run_id": "x"}
                else:
                    value = self.other_spec(op2)
                with self.assertRaises(RuntimeErrorCode) as ctx:
                    self.run_op(op2, value, evidence2)
                self.assertEqual(ctx.exception.code, "E_RUNTIME_INVOCATION")
                self.assertEqual((self.state_of(op2), os.listdir(evidence2)), ("RESERVED", []))
                self.assert_ledger_unchanged(before)

    def test_the_returned_decision_is_a_copy_of_the_record_on_disk(self):
        op, evidence = self.operation()
        seen = []
        real = supervised_operation.replay_decision

        def spy(spec, evidence_dir):
            decision = real(spec, evidence_dir)
            seen.append(decision)
            return decision

        with mock.patch.object(supervised_operation, "replay_decision", spy):
            outcome = self.run_op(op, self.spec(op, PRINT_OK), evidence)
        self.assertEqual(sorted(outcome), ["decision", "error", "exit_code", "operation_id", "state", "stop_reason"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(outcome["decision"], seen[0])
        self.assertIsNot(outcome["decision"], seen[0])
        outcome["decision"]["status"] = "MUTATED"
        self.assertEqual(seen[0]["status"], "STOPPED")

    # recovery ---------------------------------------------------------------

    def test_recovering_a_spec_for_another_run_is_refused(self):
        # Baseline: the operation's own spec recovers after a rollover, from the
        # handoff record the module writes (no observation: the start is unknown).
        op, evidence = self.handed_off()
        spec = self.spec(op, "pass")
        self.write_handoff(evidence, spec)
        self.scheduler.rollover()
        outcome = self.recover(op, spec, evidence)
        self.assertEqual((outcome["state"], self.state_of(op)), ("START_UNKNOWN", "START_UNKNOWN"))
        self.assertEqual(sorted(outcome), ["decision", "operation_id", "state"])
        # One change: a spec that names another run.
        self.scheduler = self.new_scheduler()
        op2, evidence2 = self.handed_off(task="task-2")
        self.scheduler.rollover()
        spec2 = self.spec(op2, "pass")
        self.write_handoff(evidence2, spec2)
        before = self.ledger()
        with self.assertRaises(RuntimeErrorCode) as ctx:
            self.recover(op2, self.spec("op-other"), evidence2)
        self.assertEqual(ctx.exception.code, "E_RUNTIME_INVOCATION")
        self.assert_ledger_unchanged(before)
        self.assertEqual(self.state_of(op2), "HANDOFF_ACCEPTED")
        self.assertIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        # The operation's own spec still recovers it.
        outcome2 = self.recover(op2, spec2, evidence2)
        self.assertEqual((outcome2["state"], self.state_of(op2)), ("START_UNKNOWN", "START_UNKNOWN"))

    def test_a_candidate_hash_that_raises_leaves_the_recovery_untouched(self):
        # Baseline: a finished run recovers into review with the candidate.
        op, evidence = self.handed_off()
        spec = self.spec(op, PRINT_OK)
        self.record(spec, evidence)
        self.scheduler.rollover()
        outcome = self.recover(op, spec, evidence)
        self.assertEqual((outcome["state"], self.state_of(op)), ("REVIEW_PENDING", "REVIEW_PENDING"))
        self.assertEqual(self.candidates, [str(evidence)])
        # One change: the new generation's callback raises.
        self.scheduler = self.new_scheduler()
        self.candidates = []
        op2, evidence2 = self.handed_off(task="task-2")
        spec2 = self.spec(op2, PRINT_OK)
        self.record(spec2, evidence2)
        self.scheduler.rollover()
        calls = []

        def vanishing(evidence_dir):
            calls.append(evidence_dir)
            raise ValueError("the candidate vanished")

        before = self.ledger()
        with self.assertRaises(ValueError):
            self.recover(op2, spec2, evidence2, owner="owner-2", generation=2, candidate_hash=vanishing)
        self.assertEqual(calls, [str(evidence2)])
        self.assert_ledger_unchanged(before)
        self.assertEqual(self.state_of(op2), "HANDOFF_ACCEPTED")
        # The recovery still works once a candidate can be produced.
        outcome2 = self.recover(op2, spec2, evidence2, owner="owner-2", generation=2)
        self.assertEqual((outcome2["state"], self.state_of(op2)), ("REVIEW_PENDING", "REVIEW_PENDING"))
        self.assertEqual(self.candidates, [str(evidence2)])
        self.assertEqual(self.scheduler.events[-1]["payload"]["candidate"], CANDIDATE)

    def test_recovering_an_operation_that_was_never_fenced_out_raises(self):
        # Baseline: the same operation recovers once the controller rolled over.
        op, evidence = self.handed_off()
        spec = self.spec(op, "pass")
        self.write_handoff(evidence, spec)
        self.scheduler.rollover()
        outcome = self.recover(op, spec, evidence)
        self.assertEqual(outcome["state"], "START_UNKNOWN")
        # One change: an operation of the current generation is recovered.
        self.scheduler = self.new_scheduler()
        op2, evidence2 = self.handed_off(task="task-2")
        before = self.ledger()
        with self.assertRaises(ContractError) as ctx:
            self.recover(op2, self.spec(op2), evidence2, owner="owner-1", generation=1)
        self.assertEqual(ctx.exception.code, "E_RECOVERY_NOT_FENCED")
        self.assertEqual((self.state_of(op2), self.scheduler.generation), ("HANDOFF_ACCEPTED", 1))
        self.assert_ledger_unchanged(before)
        self.assertIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        self.assertEqual(self.events_of(op2, "scheduler.recovered"), [])

    def test_a_recovery_whose_records_are_damaged_stays_start_unknown(self):
        # Baseline: the intact records of the same run recover as finished.
        op, evidence = self.handed_off()
        spec = self.spec(op, PRINT_OK)
        self.record(spec, evidence)
        self.scheduler.rollover()
        outcome = self.recover(op, spec, evidence)
        self.assertEqual((outcome["state"], outcome["decision"]["replay_allowed"]), ("REVIEW_PENDING", True))
        # One change: the observation record is damaged before the recovery.
        self.scheduler = self.new_scheduler()
        op2, evidence2 = self.handed_off(task="task-2")
        spec2 = self.spec(op2, PRINT_OK)
        self.record(spec2, evidence2)
        (evidence2 / OBSERVATION_NAME).write_bytes(b"not a record\n")
        self.scheduler.rollover()
        outcome2 = self.recover(op2, spec2, evidence2, owner="owner-2", generation=2)
        self.assertEqual((outcome2["state"], outcome2["decision"]), ("START_UNKNOWN", None))
        self.assertEqual(self.state_of(op2), "START_UNKNOWN")
        self.assertEqual(self.scheduler.events[-1]["payload"]["outcome"], "START_UNKNOWN")
        self.assertEqual(self.candidates, [str(evidence)])


if __name__ == "__main__":
    unittest.main()
