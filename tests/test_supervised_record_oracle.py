"""Acceptance oracle for supervised start records and the replay gate.

Planner-provided; executors must not modify this file.  Cases start real local
``python`` process trees on macOS (no provider CLI, no credentials, no network)
and check the end-to-end contract of posix-reconcile-integration:

* a timeout whose cleanup was confirmed may be replayed;
* a run whose cleanup was not confirmed may not;
* a controller that died after the start leaves START_UNKNOWN, which may not;
* nothing started (rejected input, failed handoff, exec failure) may be replayed;
* records for another invocation, or damaged records, never authorize a replay.

Each case kills whatever it started, even when an assertion fails.
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

from sentinelmesh.posix_supervisor import supervise
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode, reconcile
from sentinelmesh.supervised_record import (
    HANDOFF_NAME,
    OBSERVATION_NAME,
    record_supervised_run,
    replay_decision,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
IGNORE_TERM_SLEEP = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
# A parent that leaves the supervised group after its child has exited but before
# reaping it: the group keeps only that zombie, so the cleanup is never confirmed.
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
# Writes its pid, then runs until killed.
PID_THEN_SLEEP = ("import os, sys, time\n"
                  "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
                  "os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
                  "time.sleep(30)\n")
# A controller process that records one supervised run from JSON arguments.
CONTROLLER = ("import json, sys\n"
              "from sentinelmesh.runtime import InvocationSpec\n"
              "from sentinelmesh.supervised_record import record_supervised_run\n"
              "a = json.loads(sys.argv[1])\n"
              "spec = InvocationSpec(a['request_id'], a['run_id'], tuple(a['command']), a['cwd'],\n"
              "                      a['environment_sha256'], a['timeout'])\n"
              "record_supervised_run(spec, environment=a['environment'], evidence_dir=a['evidence'],\n"
              "                      worktree_id='wt-1')\n")


def canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def group_gone_within(pgid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def pid_in(path: Path):
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text else None


def wait_for(path: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


@unittest.skipIf(os.name != "posix", "native POSIX supervisor (process groups and signals); the Windows wiring has its own cases in tests/test_win_wiring*.py")
class SupervisedRecordOracle(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="supervised-record-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.dirs = 0

    def evidence_dir(self) -> Path:
        self.dirs += 1
        path = self.work / ("evidence-%d" % self.dirs)
        path.mkdir()
        return path

    def spec(self, code, *args, timeout=10, command=None, run_id="run-1"):
        argv = command if command is not None else (PY, "-c", code, *args)
        return InvocationSpec("req-1", run_id, argv, str(self.cwd), environment_digest(BASE_ENV), timeout)

    def record(self, spec, evidence, environment=None, **kwargs):
        run = record_supervised_run(spec, environment=BASE_ENV if environment is None else environment,
                                    evidence_dir=str(evidence), worktree_id="wt-1", **kwargs)
        if run.identity is not None:
            self.addCleanup(self.kill_group, run.identity.pid)
        return run

    def kill_group(self, pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def kill_pid_in(self, path: Path):
        pid = pid_in(path)
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def code_of(self, func, *args, **kwargs):
        with self.assertRaises(RuntimeErrorCode) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def decision(self, spec, evidence):
        return replay_decision(spec, str(evidence))

    def assert_decision(self, record, spec, status, replay_allowed, observed):
        self.assertEqual(record, {"status": status, "run_id": spec.run_id,
                                  "expected_environment_sha256": spec.environment_sha256,
                                  "observed_environment_sha256": observed, "replay_allowed": replay_allowed})

    # end-to-end replay decisions --------------------------------------------

    def test_completed_run_records_both_files_and_allows_replay(self):
        spec = self.spec("print('ok')")
        evidence = self.evidence_dir()
        run = self.record(spec, evidence)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertEqual(sorted(os.listdir(evidence)),
                         [HANDOFF_NAME, OBSERVATION_NAME, "stderr.log", "stdout.log"])
        for name in (HANDOFF_NAME, OBSERVATION_NAME):
            self.assertEqual(oct(os.stat(evidence / name).st_mode & 0o777), oct(0o600))
        self.assertEqual((evidence / HANDOFF_NAME).read_bytes(), canonical({
            "schema": "sentinelmesh-handoff-v1", "request_id": "req-1", "run_id": "run-1",
            "environment_sha256": spec.environment_sha256, "spec_sha256": spec.digest()}))
        # Schema v2 (supervisor-stream-facts) adds the stream facts next to the
        # unchanged observation.
        self.assertEqual((evidence / OBSERVATION_NAME).read_bytes(), canonical({
            "schema": "sentinelmesh-observation-v2", "spec_sha256": spec.digest(),
            "observation": dict(run.observation), "streams": run.streams}))
        record = self.decision(spec, evidence)
        self.assertEqual(record, dict(reconcile(spec, run.observation)))
        self.assert_decision(record, spec, "STOPPED", True, spec.environment_sha256)

    def test_timeout_with_confirmed_cleanup_allows_replay(self):
        spec = self.spec(IGNORE_TERM_SLEEP, timeout=1)
        evidence = self.evidence_dir()
        run = self.record(spec, evidence)
        self.assertEqual((run.stop_reason, run.exit_code), ("timeout_killed", -signal.SIGKILL))
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assert_decision(self.decision(spec, evidence), spec, "FAILED", True, spec.environment_sha256)

    def test_unconfirmed_cleanup_blocks_replay(self):
        escaped_pid = self.work / "escaped.pid"
        self.addCleanup(self.kill_pid_in, escaped_pid)
        spec = self.spec(ZOMBIE_GROUP_LEADER, str(escaped_pid))
        evidence = self.evidence_dir()
        run = self.record(spec, evidence)
        self.assertIsNotNone(pid_in(escaped_pid), "the escaped parent never reported its pid")
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertIs(run.observation["cleanup_confirmed"], False)
        self.assert_decision(self.decision(spec, evidence), spec, "STOPPED", False, spec.environment_sha256)
        self.kill_pid_in(escaped_pid)
        self.assertTrue(group_gone_within(run.identity.pid, 3.0))

    def test_controller_killed_after_the_start_leaves_start_unknown(self):
        leader_pid = self.work / "leader.pid"
        self.addCleanup(lambda: pid_in(leader_pid) and self.kill_group(pid_in(leader_pid)))
        spec = self.spec(PID_THEN_SLEEP, str(leader_pid), timeout=30)
        evidence = self.evidence_dir()
        arguments = {"request_id": spec.request_id, "run_id": spec.run_id, "command": list(spec.command),
                     "cwd": spec.cwd, "environment_sha256": spec.environment_sha256,
                     "timeout": spec.timeout_seconds, "environment": BASE_ENV, "evidence": str(evidence)}
        controller = subprocess.Popen([PY, "-c", CONTROLLER, json.dumps(arguments)], cwd=str(REPO_ROOT),
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL)
        self.addCleanup(controller.wait, 10)
        self.addCleanup(lambda: controller.poll() is None and controller.kill())
        self.assertTrue(wait_for(leader_pid, 15.0), "the supervised leader never started")
        controller.kill()
        controller.wait(10)
        self.assertTrue((evidence / HANDOFF_NAME).exists())
        self.assertFalse((evidence / OBSERVATION_NAME).exists())
        self.assert_decision(self.decision(spec, evidence), spec, "START_UNKNOWN", False, None)

    def test_exec_failure_is_not_started_and_may_be_replayed(self):
        garbage = self.cwd / "not-a-program"
        garbage.write_bytes(b"\x00\x01\x02 not an executable image\n")
        garbage.chmod(0o755)
        spec = self.spec(None, command=(str(garbage),))
        evidence = self.evidence_dir()
        run = self.record(spec, evidence)
        self.assertEqual(run.stop_reason, "start_failed")
        self.assertEqual(json.loads((evidence / OBSERVATION_NAME).read_bytes())["observation"],
                         {"run_id": "run-1", "environment_sha256": spec.environment_sha256, "state": "NOT_STARTED"})
        self.assert_decision(self.decision(spec, evidence), spec, "NOT_STARTED", True, spec.environment_sha256)

    # nothing started ---------------------------------------------------------

    def test_rejected_input_records_nothing_and_may_be_replayed(self):
        environment = {"LANG": "C", "PATH": "/usr/bin:/bin", "SERVICE_API_TOKEN": "secret-value"}
        spec = InvocationSpec("req-1", "run-1", (PY, "-c", "pass"), str(self.cwd), environment_digest(environment), 10)
        evidence = self.evidence_dir()
        self.assertEqual(self.code_of(record_supervised_run, spec, environment=environment,
                                      evidence_dir=str(evidence), worktree_id="wt-1"), "E_RUNTIME_ENVIRONMENT")
        self.assertEqual(os.listdir(evidence), [])
        self.assert_decision(self.decision(spec, evidence), spec, "NOT_STARTED", True, None)

    def test_a_handoff_that_cannot_be_recorded_starts_nothing(self):
        marker = self.work / "started"
        spec = self.spec("import sys; open(sys.argv[1], 'w').close()", str(marker))
        evidence = self.evidence_dir()
        (evidence / HANDOFF_NAME).write_bytes(b"earlier handoff\n")
        self.assertEqual(self.code_of(record_supervised_run, spec, environment=BASE_ENV,
                                      evidence_dir=str(evidence), worktree_id="wt-1"), "E_RUNTIME_EVIDENCE")
        time.sleep(0.5)
        self.assertFalse(marker.exists(), "a process started although the handoff failed")
        self.assertEqual(sorted(os.listdir(evidence)), [HANDOFF_NAME])
        self.assertEqual((evidence / HANDOFF_NAME).read_bytes(), b"earlier handoff\n")

    def test_before_start_runs_once_after_the_evidence_files_and_rolls_them_back(self):
        marker = self.work / "started"
        spec = self.spec("import sys; open(sys.argv[1], 'w').close()", str(marker))
        evidence = self.evidence_dir()
        seen = []

        class Refused(Exception):
            pass

        def refuse():
            seen.append(sorted(os.listdir(evidence)))
            raise Refused("handoff refused")

        with self.assertRaises(Refused):
            supervise(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1", before_start=refuse)
        self.assertEqual(seen, [["stderr.log", "stdout.log"]])
        time.sleep(0.5)
        self.assertFalse(marker.exists(), "a process started although before_start raised")
        self.assertEqual(os.listdir(evidence), [])
        self.assertEqual(self.code_of(supervise, spec, environment=BASE_ENV, evidence_dir=str(evidence),
                                      worktree_id="wt-1", before_start="not callable"), "E_RUNTIME_INVOCATION")
        self.assertEqual(os.listdir(evidence), [])

    # records that never authorize a replay -----------------------------------

    def test_records_for_another_invocation_are_an_identity_mismatch(self):
        spec = self.spec("print('a')")
        other = self.spec("print('b')")
        evidence = self.evidence_dir()
        self.record(spec, evidence)
        self.assert_decision(self.decision(other, evidence), other, "IDENTITY_MISMATCH", False,
                             spec.environment_sha256)
        handoff_only = self.evidence_dir()
        shutil.copy(evidence / HANDOFF_NAME, handoff_only / HANDOFF_NAME)
        self.assert_decision(self.decision(spec, handoff_only), spec, "START_UNKNOWN", False, None)
        self.assert_decision(self.decision(other, handoff_only), other, "IDENTITY_MISMATCH", False,
                             spec.environment_sha256)
        # A matching handoff next to an observation recorded for another invocation.
        other_evidence = self.evidence_dir()
        self.record(other, other_evidence)
        mixed = self.evidence_dir()
        shutil.copy(evidence / HANDOFF_NAME, mixed / HANDOFF_NAME)
        shutil.copy(other_evidence / OBSERVATION_NAME, mixed / OBSERVATION_NAME)
        self.assert_decision(self.decision(spec, mixed), spec, "IDENTITY_MISMATCH", False,
                             spec.environment_sha256)

    def test_damaged_or_inconsistent_records_fail_closed(self):
        spec = self.spec("print('ok')")
        source = self.evidence_dir()
        self.record(spec, source)
        handoff = (source / HANDOFF_NAME).read_bytes()
        observation = (source / OBSERVATION_NAME).read_bytes()
        parsed = json.loads(observation)
        extra = dict(parsed, note="x")
        inner_extra = dict(parsed, observation=dict(parsed["observation"], note="x"))
        duplicate = observation.replace(b'{"observation":', b'{"schema":"x","observation":', 1)
        cases = {
            "observation without handoff": {OBSERVATION_NAME: observation},
            "not canonical": {HANDOFF_NAME: handoff, OBSERVATION_NAME: observation.replace(b",", b", ", 1)},
            "extra key": {HANDOFF_NAME: handoff, OBSERVATION_NAME: canonical(extra)},
            # reconcile would ignore the extra key and allow the replay
            "extra key inside the observation": {HANDOFF_NAME: handoff, OBSERVATION_NAME: canonical(inner_extra)},
            "duplicate key": {HANDOFF_NAME: handoff, OBSERVATION_NAME: duplicate},
            "not JSON": {HANDOFF_NAME: b"handoff\n"},
        }
        for label, files in cases.items():
            with self.subTest(case=label):
                evidence = self.evidence_dir()
                for name, data in files.items():
                    (evidence / name).write_bytes(data)
                self.assertEqual(self.code_of(self.decision, spec, evidence), "E_RUNTIME_EVIDENCE")
        with self.subTest(case="symlinked handoff"):
            evidence = self.evidence_dir()
            os.symlink(source / HANDOFF_NAME, evidence / HANDOFF_NAME)
            self.assertEqual(self.code_of(self.decision, spec, evidence), "E_RUNTIME_EVIDENCE")
        with self.subTest(case="handoff larger than 64 KiB"):
            # A canonical, correctly shaped handoff for this very spec, only too large.
            big = InvocationSpec("r" * 70000, "run-1", (PY, "-c", "pass"), str(self.cwd),
                                 environment_digest(BASE_ENV), 10)
            evidence = self.evidence_dir()
            (evidence / HANDOFF_NAME).write_bytes(canonical({
                "schema": "sentinelmesh-handoff-v1", "request_id": big.request_id, "run_id": big.run_id,
                "environment_sha256": big.environment_sha256, "spec_sha256": big.digest()}))
            self.assertGreater(os.path.getsize(evidence / HANDOFF_NAME), 65536)
            self.assertEqual(self.code_of(self.decision, big, evidence), "E_RUNTIME_EVIDENCE")

    def test_replay_decision_rejects_bad_inputs(self):
        spec = self.spec("pass")
        self.assertEqual(self.code_of(replay_decision, "spec", str(self.evidence_dir())), "E_RUNTIME_INVOCATION")
        self.assertEqual(self.code_of(replay_decision, spec, "relative/evidence"), "E_RUNTIME_EVIDENCE")
        self.assertEqual(self.code_of(replay_decision, spec, str(self.work / "missing")), "E_RUNTIME_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
