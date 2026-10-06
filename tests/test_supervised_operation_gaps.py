"""Planner follow-up cases for supervised-operation-bridge (review of attempt-001).

The review reproduced a fail-open the contract left open: the supervised child
can write into the evidence directory, so a NOT_STARTED observation it forged
was read back and released a run that did start.  On the live path the bridge
now puts what the supervisor returned first and requires the records to agree.
These cases check that, plus rules no test guarded: a post-start recording
failure, a candidate hash interrupted, and the ``now`` and
``allowed_secret_names`` pass-through.  Each case kills whatever it started and
restores any permission it changed.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sentinelmesh.supervised_operation as bridge
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.scheduler import ContractError, Scheduler
from sentinelmesh.supervised_operation import run_supervised_operation
from sentinelmesh.supervised_record import OBSERVATION_NAME, record_supervised_run

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
CANDIDATE = "c" * 64
FORGE_NOT_STARTED = ("import json, os, sys\n"
                     "d = sys.argv[1]; h = json.load(open(os.path.join(d, 'handoff.json')))\n"
                     "o = {'schema': 'sentinelmesh-observation-v1', 'spec_sha256': h['spec_sha256'],\n"
                     "     'observation': {'run_id': h['run_id'], 'environment_sha256': h['environment_sha256'],\n"
                     "                     'state': 'NOT_STARTED'}}\n"
                     "open(os.path.join(d, 'observation.json'), 'wb').write(\n"
                     "    (json.dumps(o, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\\n').encode())\n")
LOCK_DIRECTORY = "import os, sys; os.chmod(sys.argv[1], 0o500)"
DELETE_HANDOFF_AND_LOCK = ("import os, sys\n"
                           "os.unlink(os.path.join(sys.argv[1], 'handoff.json'))\n"
                           "os.chmod(sys.argv[1], 0o500)\n")


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class SupervisedOperationGaps(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="supervised-bridge-gaps-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.scheduler = Scheduler()
        self.scheduler.seed_request("req-1", "repo-1", {"dispatches": 5, "tokens": 100}, concurrency=4)
        self.scheduler.seed_task("req-1", "task-1", {"dispatches": 5, "tokens": 100}, max_attempts=2)
        self.candidates = []

    def candidate_hash(self, evidence_dir):
        self.candidates.append(evidence_dir)
        return CANDIDATE

    def operation(self):
        op = self.scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 40},
                                  ["src/a.py"])["operation_id"]
        evidence = self.work / "evidence"
        evidence.mkdir()
        # A child may lock the directory; give it back before the tree is removed.
        self.addCleanup(os.chmod, evidence, 0o700)
        return op, evidence

    def spec(self, op, code, *args, command=None, environment=None):
        argv = command if command is not None else (PY, "-c", code, *args)
        return InvocationSpec("req-1", op, argv, str(self.cwd), environment_digest(environment or BASE_ENV), 10)

    def run_op(self, op, spec, evidence, environment=None, **kwargs):
        return run_supervised_operation(self.scheduler, operation_id=op, owner="owner-1", generation=1, spec=spec,
                                        environment=environment or BASE_ENV, evidence_dir=str(evidence),
                                        worktree_id="wt-1", candidate_hash=self.candidate_hash, **kwargs)

    def state_of(self, op):
        return self.scheduler._operations[op].state

    def types_of(self, op):
        return [event["type"] for event in self.scheduler.events if event["operation_id"] == op]

    def last_payload(self, op, event_type):
        return [event["payload"] for event in self.scheduler.events
                if event["operation_id"] == op and event["type"] == event_type][-1]

    def assert_held_and_unknown(self, op, outcome, reason):
        self.assertEqual((outcome["state"], self.state_of(op)), ("START_UNKNOWN", "START_UNKNOWN"))
        self.assertEqual(self.last_payload(op, "scheduler.start_unknown"), {"reason": reason})
        self.assertNotIn("scheduler.not_started", self.types_of(op))
        self.assertIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        self.assertTrue(self.scheduler._requests["req-1"].stopped)
        self.assertEqual(self.candidates, [])

    # the child writes into the evidence directory ---------------------------

    def test_a_forged_not_started_observation_never_releases_a_run_that_started(self):
        op, evidence = self.operation()
        outcome = self.run_op(op, self.spec(op, FORGE_NOT_STARTED, str(evidence)), evidence)
        self.assertEqual((outcome["error"], outcome["stop_reason"]), ("E_RUNTIME_EVIDENCE", "completed"))
        self.assertEqual(outcome["decision"]["status"], "NOT_STARTED", "the forged record was read back")
        self.assert_held_and_unknown(op, outcome, "START_UNKNOWN")
        self.assertIn("process_identity", self.last_payload(op, "run.started"))

    def test_a_deleted_handoff_and_a_locked_directory_never_release_a_run(self):
        op, evidence = self.operation()
        outcome = self.run_op(op, self.spec(op, DELETE_HANDOFF_AND_LOCK, str(evidence)), evidence)
        self.assertEqual((outcome["error"], outcome["stop_reason"]), ("E_RUNTIME_EVIDENCE", "completed"))
        self.assertEqual(outcome["decision"]["status"], "NOT_STARTED", "both records were gone")
        self.assert_held_and_unknown(op, outcome, "START_UNKNOWN")

    def test_an_observation_that_cannot_be_written_is_start_unknown(self):
        op, evidence = self.operation()
        outcome = self.run_op(op, self.spec(op, LOCK_DIRECTORY, str(evidence)), evidence)
        self.assertEqual((outcome["error"], outcome["stop_reason"], outcome["exit_code"]),
                         ("E_RUNTIME_EVIDENCE", "completed", 0))
        self.assert_held_and_unknown(op, outcome, "START_UNKNOWN")

    def test_the_recorder_attaches_the_finished_run_to_its_error(self):
        op, evidence = self.operation()
        with self.assertRaises(RuntimeErrorCode) as ctx:
            record_supervised_run(self.spec(op, LOCK_DIRECTORY, str(evidence)), environment=BASE_ENV,
                                  evidence_dir=str(evidence), worktree_id="wt-1")
        self.assertEqual(ctx.exception.code, "E_RUNTIME_EVIDENCE")
        run = ctx.exception.run
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertIsNotNone(run.identity)

    # what the supervisor returned disagrees with the records ----------------

    def run_with_observation(self, change):
        real = bridge.record_supervised_run

        def altered(*args, **kwargs):
            run = real(*args, **kwargs)
            return dataclasses.replace(run, observation=dict(run.observation, **change))

        op, evidence = self.operation()
        with mock.patch.object(bridge, "record_supervised_run", altered):
            outcome = self.run_op(op, self.spec(op, "pass"), evidence)
        return op, outcome

    def test_an_unconfirmed_cleanup_in_memory_is_never_reviewed(self):
        op, outcome = self.run_with_observation({"cleanup_confirmed": False})
        self.assertEqual((outcome["decision"]["status"], outcome["decision"]["replay_allowed"]), ("STOPPED", True))
        self.assert_held_and_unknown(op, outcome, "CLEANUP_UNCONFIRMED")

    def test_an_outcome_the_records_contradict_is_start_unknown(self):
        op, outcome = self.run_with_observation({"state": "FAILED"})
        self.assertEqual(outcome["decision"]["status"], "STOPPED")
        self.assert_held_and_unknown(op, outcome, "START_UNKNOWN")

    def test_an_exec_failure_whose_observation_cannot_be_written_is_not_started(self):
        garbage = self.cwd / "not-a-program"
        garbage.write_bytes(b"\x00\x01\x02 not an executable image\n")
        garbage.chmod(0o755)
        real_link = os.link

        def refuse_the_observation(source, target):
            if str(target).endswith(OBSERVATION_NAME):
                raise OSError(28, "No space left on device")
            return real_link(source, target)

        op, evidence = self.operation()
        with mock.patch.object(os, "link", refuse_the_observation):
            outcome = self.run_op(op, self.spec(op, None, command=(str(garbage),)), evidence)
        self.assertEqual((outcome["error"], outcome["stop_reason"]), ("E_RUNTIME_EVIDENCE", "start_failed"))
        self.assertEqual((outcome["state"], self.state_of(op)), ("NOT_STARTED", "NOT_STARTED"))
        self.assertNotIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)

    def test_a_run_whose_own_observation_failed_is_never_finished(self):
        # The observation reaches the disk, but its publish reports a failure:
        # the records agree with the run, yet this call did not record them.
        real_link = os.link

        def published_then_refused(source, target):
            result = real_link(source, target)
            if str(target).endswith(OBSERVATION_NAME):
                raise OSError(5, "Input/output error")
            return result

        op, evidence = self.operation()
        with mock.patch.object(os, "link", published_then_refused):
            outcome = self.run_op(op, self.spec(op, "pass"), evidence)
        self.assertEqual((outcome["decision"]["status"], outcome["decision"]["replay_allowed"]), ("STOPPED", True))
        self.assertEqual(outcome["error"], "E_RUNTIME_EVIDENCE")
        self.assert_held_and_unknown(op, outcome, "START_UNKNOWN")

    # candidate hash and pass-through -----------------------------------------

    def test_an_interrupted_candidate_hash_is_start_unknown_and_re_raised(self):
        interrupt = KeyboardInterrupt("candidate hash interrupted")

        def interrupted(evidence_dir):
            raise interrupt

        op, evidence = self.operation()
        with self.assertRaises(KeyboardInterrupt) as ctx:
            run_supervised_operation(self.scheduler, operation_id=op, owner="owner-1", generation=1,
                                     spec=self.spec(op, "pass"), environment=BASE_ENV, evidence_dir=str(evidence),
                                     worktree_id="wt-1", candidate_hash=interrupted)
        self.assertIs(ctx.exception, interrupt)
        self.assertEqual(self.state_of(op), "START_UNKNOWN")
        self.assertIn("run.started", self.types_of(op))

    def test_now_reaches_the_handoff(self):
        op, evidence = self.operation()
        with self.assertRaises(ContractError) as ctx:
            self.run_op(op, self.spec(op, "pass"), evidence, now=30)  # the claim's lease ends at 30
        self.assertEqual(ctx.exception.code, "E_HANDOFF_STATE")
        self.assertEqual((self.state_of(op), os.listdir(evidence)), ("RESERVED", []))

    def test_allowed_secret_names_reach_the_supervisor(self):
        environment = {"LANG": "C", "PATH": "/usr/bin:/bin", "SERVICE_API_TOKEN": "secret-value"}
        op, evidence = self.operation()
        outcome = self.run_op(op, self.spec(op, "pass", environment=environment), evidence, environment=environment,
                              allowed_secret_names=frozenset({"SERVICE_API_TOKEN"}))
        self.assertEqual((outcome["state"], outcome["error"]), ("REVIEW_PENDING", None))


    # an evidence directory that is not a string (adapter-supervised-run review, F2) ---------

    def test_an_evidence_directory_that_is_not_a_string_is_refused_before_the_handoff(self):
        op, evidence = self.operation()
        before = (self.scheduler.events, dict(self.scheduler._resource_holds))
        for value in (None, 123, evidence):
            with self.subTest(evidence_dir=repr(value)):
                with self.assertRaises(RuntimeErrorCode) as ctx:
                    run_supervised_operation(self.scheduler, operation_id=op, owner="owner-1", generation=1,
                                             spec=self.spec(op, "pass"), environment=BASE_ENV, evidence_dir=value,
                                             worktree_id="wt-1", candidate_hash=self.candidate_hash)
                self.assertEqual(ctx.exception.code, "E_RUNTIME_EVIDENCE")
                self.assertEqual((self.scheduler.events, dict(self.scheduler._resource_holds)), before)
                self.assertEqual((self.state_of(op), os.listdir(evidence)), ("RESERVED", []))
        # A string the supervisor rejects still settles as a handoff proven not to have started.
        outcome = run_supervised_operation(self.scheduler, operation_id=op, owner="owner-1", generation=1,
                                           spec=self.spec(op, "pass"), environment=BASE_ENV,
                                           evidence_dir=str(self.work / "missing"), worktree_id="wt-1",
                                           candidate_hash=self.candidate_hash)
        self.assertEqual((outcome["state"], outcome["error"]), ("NOT_STARTED", "E_RUNTIME_EVIDENCE"))

    def test_recovery_refuses_an_evidence_directory_that_is_not_a_string(self):
        op, evidence = self.operation()
        self.scheduler.handoff(op, "owner-1", 1)
        self.scheduler.rollover()
        before = self.scheduler.events
        with self.assertRaises(RuntimeErrorCode) as ctx:
            bridge.recover_supervised_operation(self.scheduler, operation_id=op, owner="owner-2", generation=2,
                                                spec=self.spec(op, "pass"), evidence_dir=None,
                                                candidate_hash=self.candidate_hash)
        self.assertEqual(ctx.exception.code, "E_RUNTIME_EVIDENCE")
        self.assertEqual(self.scheduler.events, before)
        self.assertEqual((self.state_of(op), self.scheduler._operations[op].generation), ("HANDOFF_ACCEPTED", 1))


if __name__ == "__main__":
    unittest.main()
