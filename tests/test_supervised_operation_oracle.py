"""Acceptance oracle for wiring supervised runs into the Scheduler.

Planner-provided; executors must not modify this file.  Cases start real local
``python`` process trees on macOS (no provider CLI, no credentials, no network)
and check the user's decisions in `.agent/design/scheduler-supervisor-wiring.md`:

* a run that ended with confirmed cleanup, whether it succeeded, failed or
  timed out, goes to REVIEW_PENDING with its process identity recorded;
* an unconfirmed cleanup is START_UNKNOWN (reason CLEANUP_UNCONFIRMED), keeps
  its holds and stops the request;
* a handoff proven not started (exec failure, rejected input) is NOT_STARTED;
* a refused handoff starts nothing;
* after a controller rollover the new generation recovers in-flight
  operations from the evidence on disk.

Each case kills whatever it started, even when an assertion fails.
"""
from __future__ import annotations

import hashlib
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

from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.scheduler import ContractError, Scheduler
from sentinelmesh.supervised_operation import (
    evidence_digest,
    recover_supervised_operation,
    run_supervised_operation,
)
from sentinelmesh.supervised_record import HANDOFF_NAME, OBSERVATION_NAME, record_supervised_run

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
CANDIDATE = "c" * 64
IGNORE_TERM_SLEEP = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
ESCAPED_PARENT = ("import os, sys, time\n"
                  "r, w = os.pipe()\n"
                  "if os.fork() == 0:\n"
                  "    os._exit(0)\n"
                  "os.close(w)\n"
                  "os.read(r, 1)\n"
                  "time.sleep(0.2)\n"
                  "os.setsid()\n"
                  "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
                  "os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
                  "time.sleep(30)\n")
ZOMBIE_GROUP_LEADER = ("import os, subprocess, sys, time\n"
                       "subprocess.Popen([sys.executable, '-c', %r, sys.argv[1]])\n"
                       "while not os.path.exists(sys.argv[1]):\n"
                       "    time.sleep(0.02)\n" % ESCAPED_PARENT)
PID_THEN_SLEEP = ("import os, sys, time\n"
                  "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
                  "os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
                  "time.sleep(30)\n")
CONTROLLER = ("import json, sys\n"
              "from sentinelmesh.runtime import InvocationSpec\n"
              "from sentinelmesh.supervised_record import record_supervised_run\n"
              "a = json.loads(sys.argv[1])\n"
              "spec = InvocationSpec(a['request_id'], a['run_id'], tuple(a['command']), a['cwd'],\n"
              "                      a['environment_sha256'], a['timeout'])\n"
              "record_supervised_run(spec, environment=a['environment'], evidence_dir=a['evidence'],\n"
              "                      worktree_id='wt-1')\n")


def pid_in(path: Path):
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text else None


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class SupervisedOperationOracle(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="supervised-operation-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.scheduler = Scheduler()
        self.scheduler.seed_request("req-1", "repo-1", {"dispatches": 5, "tokens": 100}, concurrency=4)
        self.scheduler.seed_task("req-1", "task-1", {"dispatches": 5, "tokens": 100}, max_attempts=2)
        self.candidates = []
        self.count = 0

    def candidate_hash(self, evidence_dir):
        self.candidates.append(evidence_dir)
        return CANDIDATE

    def operation(self):
        self.count += 1
        claim = self.scheduler.claim("req-1", "task-1", 1, "idem-%d" % self.count, "owner-1", 1, 1,
                                     {"dispatches": 1, "tokens": 40}, ["src/a.py"])
        evidence = self.work / ("evidence-%d" % self.count)
        evidence.mkdir()
        return claim["operation_id"], evidence

    def spec(self, op, code, *args, timeout=10, command=None, environment=None):
        argv = command if command is not None else (PY, "-c", code, *args)
        return InvocationSpec("req-1", op, argv, str(self.cwd), environment_digest(environment or BASE_ENV), timeout)

    def run_op(self, op, spec, evidence, environment=None, **kwargs):
        outcome = run_supervised_operation(self.scheduler, operation_id=op, owner="owner-1", generation=1, spec=spec,
                                           environment=environment or BASE_ENV, evidence_dir=str(evidence),
                                           worktree_id="wt-1", candidate_hash=self.candidate_hash, **kwargs)
        return outcome

    def kill_group(self, pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def kill_pid_in(self, path: Path):
        pid = pid_in(path)
        if pid is not None:
            self.kill_group(pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def state_of(self, op):
        return self.scheduler._operations[op].state

    def events_of(self, op, event_type):
        return [event for event in self.scheduler.events if event["operation_id"] == op and event["type"] == event_type]

    def assert_reviewed(self, op, outcome, evidence, status):
        self.assertEqual(outcome["state"], "REVIEW_PENDING")
        self.assertEqual(self.state_of(op), "REVIEW_PENDING")
        self.assertEqual((outcome["decision"]["status"], outcome["decision"]["replay_allowed"]), (status, True))
        started = self.events_of(op, "run.started")
        self.assertEqual(len(started), 1)
        identity = started[0]["payload"]["process_identity"]
        observation = json.loads((evidence / OBSERVATION_NAME).read_bytes())["observation"]
        self.assertEqual(identity, observation["process_identity"])
        self.assertEqual(self.events_of(op, "run.finished_for_review")[0]["payload"], {"candidate": CANDIDATE})
        self.assertEqual(self.candidates, [str(evidence)])
        self.assertNotIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)

    # runs that end with confirmed cleanup ----------------------------------

    def test_a_completed_run_goes_to_review_with_its_identity(self):
        op, evidence = self.operation()
        outcome = self.run_op(op, self.spec(op, "print('ok')"), evidence)
        self.assertEqual((outcome["stop_reason"], outcome["exit_code"], outcome["error"]), ("completed", 0, None))
        self.assert_reviewed(op, outcome, evidence, "STOPPED")

    def test_a_failed_run_also_goes_to_review(self):
        op, evidence = self.operation()
        outcome = self.run_op(op, self.spec(op, "import sys; sys.exit(3)"), evidence)
        self.assertEqual((outcome["stop_reason"], outcome["exit_code"]), ("completed", 3))
        self.assert_reviewed(op, outcome, evidence, "FAILED")

    def test_a_timeout_with_confirmed_cleanup_goes_to_review(self):
        op, evidence = self.operation()
        outcome = self.run_op(op, self.spec(op, IGNORE_TERM_SLEEP, timeout=1), evidence)
        self.assertEqual((outcome["stop_reason"], outcome["exit_code"]), ("timeout_killed", -signal.SIGKILL))
        self.assert_reviewed(op, outcome, evidence, "FAILED")

    # unconfirmed cleanup, not started, refused handoff ----------------------

    def test_an_unconfirmed_cleanup_is_start_unknown_and_keeps_its_holds(self):
        escaped = self.work / "escaped.pid"
        self.addCleanup(self.kill_pid_in, escaped)
        op, evidence = self.operation()
        outcome = self.run_op(op, self.spec(op, ZOMBIE_GROUP_LEADER, str(escaped)), evidence)
        self.assertIsNotNone(pid_in(escaped), "the escaped parent never reported its pid")
        self.assertEqual((outcome["state"], self.state_of(op)), ("START_UNKNOWN", "START_UNKNOWN"))
        self.assertEqual((outcome["decision"]["status"], outcome["decision"]["replay_allowed"]), ("STOPPED", False))
        self.assertEqual(len(self.events_of(op, "run.started")), 1)
        self.assertEqual(self.events_of(op, "scheduler.start_unknown")[0]["payload"], {"reason": "CLEANUP_UNCONFIRMED"})
        self.assertTrue(self.scheduler._requests["req-1"].stopped)
        self.assertIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        self.assertEqual(self.candidates, [])

    def test_an_exec_failure_is_not_started_and_releases_its_holds(self):
        garbage = self.cwd / "not-a-program"
        garbage.write_bytes(b"\x00\x01\x02 not an executable image\n")
        garbage.chmod(0o755)
        op, evidence = self.operation()
        outcome = self.run_op(op, self.spec(op, None, command=(str(garbage),)), evidence)
        self.assertEqual((outcome["state"], self.state_of(op), outcome["stop_reason"]),
                         ("NOT_STARTED", "NOT_STARTED", "start_failed"))
        self.assertEqual(self.events_of(op, "scheduler.not_started")[0]["payload"],
                         {"evidence": evidence_digest(str(evidence))})
        self.assertEqual(self.events_of(op, "run.started"), [])
        self.assertNotIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        snapshot = self.scheduler.snapshot("req-1")
        self.assertEqual((snapshot["dispatches"]["known"], snapshot["tokens"]["held"]), (1, 0))
        self.assertIsNone(self.scheduler._requests["req-1"].stopped)

    def test_a_rejected_input_is_not_started_without_records(self):
        environment = {"LANG": "C", "PATH": "/usr/bin:/bin", "SERVICE_API_TOKEN": "secret-value"}
        marker = self.work / "started"
        op, evidence = self.operation()
        spec = self.spec(op, "import sys; open(sys.argv[1], 'w').close()", str(marker), environment=environment)
        outcome = self.run_op(op, spec, evidence, environment=environment)
        time.sleep(0.5)
        self.assertFalse(marker.exists(), "a process started although the input was rejected")
        self.assertEqual((outcome["state"], outcome["error"]), ("NOT_STARTED", "E_RUNTIME_ENVIRONMENT"))
        self.assertEqual(os.listdir(evidence), [])
        self.assertEqual(self.events_of(op, "scheduler.not_started")[0]["payload"],
                         {"evidence": evidence_digest(str(evidence))})

    def test_a_refused_handoff_starts_nothing(self):
        marker = self.work / "started"
        op, evidence = self.operation()
        self.scheduler.stop("req-1")
        spec = self.spec(op, "import sys; open(sys.argv[1], 'w').close()", str(marker))
        with self.assertRaises(ContractError) as ctx:
            self.run_op(op, spec, evidence)
        self.assertEqual(ctx.exception.code, "E_STOPPED")
        time.sleep(0.5)
        self.assertFalse(marker.exists())
        self.assertEqual((self.state_of(op), os.listdir(evidence)), ("RESERVED", []))

    def test_a_spec_for_another_run_is_refused_before_the_handoff(self):
        op, evidence = self.operation()
        other = self.spec("op-other", "pass")
        with self.assertRaises(RuntimeErrorCode) as ctx:
            self.run_op(op, other, evidence)
        self.assertEqual(ctx.exception.code, "E_RUNTIME_INVOCATION")
        self.assertEqual((self.state_of(op), os.listdir(evidence)), ("RESERVED", []))

    def test_evidence_digest_hashes_the_two_records(self):
        op, evidence = self.operation()
        self.run_op(op, self.spec(op, "pass"), evidence)
        expected = {"handoff": hashlib.sha256((evidence / HANDOFF_NAME).read_bytes()).hexdigest(),
                    "observation": hashlib.sha256((evidence / OBSERVATION_NAME).read_bytes()).hexdigest()}
        self.assertEqual(evidence_digest(str(evidence)),
                         hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        empty = self.work / "empty"
        empty.mkdir()
        self.assertEqual(evidence_digest(str(empty)), hashlib.sha256(
            json.dumps({"handoff": None, "observation": None}, sort_keys=True, separators=(",", ":")).encode()).hexdigest())

    # recovery after a rollover ---------------------------------------------

    def handed_off(self):
        op, evidence = self.operation()
        self.scheduler.handoff(op, "owner-1", 1)
        return op, evidence

    def recover(self, op, spec, evidence):
        return recover_supervised_operation(self.scheduler, operation_id=op, owner="owner-2", generation=2, spec=spec,
                                            evidence_dir=str(evidence), candidate_hash=self.candidate_hash)

    def test_a_controller_killed_mid_run_recovers_as_start_unknown(self):
        leader = self.work / "leader.pid"
        self.addCleanup(self.kill_pid_in, leader)
        op, evidence = self.handed_off()
        spec = self.spec(op, PID_THEN_SLEEP, str(leader), timeout=30)
        arguments = {"request_id": spec.request_id, "run_id": spec.run_id, "command": list(spec.command),
                     "cwd": spec.cwd, "environment_sha256": spec.environment_sha256, "timeout": spec.timeout_seconds,
                     "environment": BASE_ENV, "evidence": str(evidence)}
        controller = subprocess.Popen([PY, "-c", CONTROLLER, json.dumps(arguments)], cwd=str(REPO_ROOT),
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(controller.wait, 10)
        self.addCleanup(lambda: controller.poll() is None and controller.kill())
        deadline = time.monotonic() + 15
        while pid_in(leader) is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(pid_in(leader), "the supervised leader never started")
        controller.kill()
        controller.wait(10)
        self.assertEqual(self.scheduler.rollover(), 2)
        outcome = self.recover(op, spec, evidence)
        self.assertEqual((outcome["state"], self.state_of(op)), ("START_UNKNOWN", "START_UNKNOWN"))
        self.assertEqual(outcome["decision"]["status"], "START_UNKNOWN")
        self.assertEqual(self.events_of(op, "scheduler.recovered")[0]["payload"],
                         {"from_generation": 1, "outcome": "START_UNKNOWN", "evidence": evidence_digest(str(evidence)),
                          "state": "START_UNKNOWN"})
        self.assertIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)

    def test_a_finished_run_whose_controller_died_recovers_into_review(self):
        op, evidence = self.handed_off()
        spec = self.spec(op, "print('done')")
        record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
        self.scheduler.rollover()
        outcome = self.recover(op, spec, evidence)
        self.assertEqual((outcome["state"], self.state_of(op)), ("REVIEW_PENDING", "REVIEW_PENDING"))
        self.assertEqual(self.candidates, [str(evidence)])
        self.assertNotIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)

    def test_a_not_started_run_recovers_as_not_started(self):
        garbage = self.cwd / "not-a-program"
        garbage.write_bytes(b"\x00\x01\x02 not an executable image\n")
        garbage.chmod(0o755)
        op, evidence = self.handed_off()
        spec = self.spec(op, None, command=(str(garbage),))
        record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
        self.scheduler.rollover()
        outcome = self.recover(op, spec, evidence)
        self.assertEqual((outcome["state"], self.state_of(op)), ("NOT_STARTED", "NOT_STARTED"))
        self.assertNotIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)

    def test_damaged_evidence_recovers_as_start_unknown(self):
        op, evidence = self.handed_off()
        spec = self.spec(op, "pass")
        (evidence / OBSERVATION_NAME).write_bytes(b"not a record\n")
        self.scheduler.rollover()
        outcome = self.recover(op, spec, evidence)
        self.assertEqual((outcome["state"], outcome["decision"]), ("START_UNKNOWN", None))
        self.assertEqual(self.state_of(op), "START_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
