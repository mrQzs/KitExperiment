"""Acceptance oracle for the native POSIX supervisor (posix-supervisor contract).

Planner-provided; executors must not modify this file.  Every case starts real
local ``python`` process trees (no provider CLI, no credentials, no network)
on macOS and checks the supervisor's observable contract: explicit environment
only, credential names rejected unless allowlisted, output in exclusive
evidence files, group-wide timeout, cancellation and straggler cleanup, the
lifecycle record, and an observation that ``reconcile`` accepts.  Each case
kills whatever it started, even when an assertion fails.
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
from unittest import mock

from sentinelmesh.posix_supervisor import SupervisedRun, supervise
from sentinelmesh.process_identity import boot_id, environment_digest, file_sha256
from sentinelmesh.runtime import InvocationSpec, Lifecycle, ProcessIdentity, RuntimeErrorCode, reconcile

PY = os.path.realpath(sys.executable)
TIMEOUT = 1
# timeout + SIGTERM grace (1 s) + kill wait (2 s) + reap (2 s), with room for a busy machine
WALL_BOUND = TIMEOUT + 8.0
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
IGNORE_TERM_SLEEP = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"


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


@unittest.skipIf(os.name != "posix", "native POSIX supervisor (process groups and signals); the Windows wiring has its own cases in tests/test_win_wiring*.py")
class PosixSupervisorOracle(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="supervisor-oracle-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.evidence = self.work / "evidence"
        self.evidence.mkdir()
        self.cwd = self.work / "tree"
        self.cwd.mkdir()

    def spec(self, code, *args, environment=None, timeout=TIMEOUT, command=None):
        env = BASE_ENV if environment is None else environment
        argv = command if command is not None else (PY, "-c", code, *args)
        return InvocationSpec("req-1", "run-1", argv, str(self.cwd), environment_digest(env), timeout)

    def run_spec(self, spec, environment=None, **kwargs):
        env = BASE_ENV if environment is None else environment
        started = time.monotonic()
        run = supervise(spec, environment=env, evidence_dir=str(self.evidence), worktree_id="wt-1", **kwargs)
        wall = time.monotonic() - started
        if run.identity is not None:
            self.addCleanup(self.kill_group, run.identity.pid)
        return run, wall

    def kill_group(self, pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def code_of(self, func, *args, **kwargs):
        with self.assertRaises(RuntimeErrorCode) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def assert_started_observation(self, run, state):
        observation = run.observation
        self.assertEqual(set(observation), {"run_id", "environment_sha256", "state", "process_identity",
                                            "cleanup_confirmed"})
        self.assertEqual((observation["run_id"], observation["state"]), ("run-1", state))
        self.assertEqual(observation["process_identity"], {
            "pid": run.identity.pid, "boot_id": run.identity.boot_id,
            "executable_sha256": run.identity.executable_sha256, "worktree_id": "wt-1"})
        json.dumps(observation)  # the observation crosses a JSON boundary

    def test_completed_run_writes_exclusive_evidence_and_a_stopped_observation(self):
        spec = self.spec("import sys; print('out'); sys.stderr.write('err')")
        run, wall = self.run_spec(spec)
        self.assertIsInstance(run, SupervisedRun)
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((run.stop_reason, run.exit_code, run.stragglers_killed), ("completed", 0, False))
        self.assertEqual(Path(run.stdout_path), self.evidence / "stdout.log")
        self.assertEqual(Path(run.stderr_path), self.evidence / "stderr.log")
        self.assertEqual(Path(run.stdout_path).read_bytes(), b"out\n")
        self.assertEqual(Path(run.stderr_path).read_bytes(), b"err")
        self.assertEqual(oct(os.stat(run.stdout_path).st_mode & 0o777), oct(0o600))
        self.assertIsInstance(run.identity, ProcessIdentity)
        self.assertEqual(run.identity.boot_id, boot_id())
        self.assertEqual(run.identity.executable_sha256, file_sha256(PY))
        self.assertEqual(run.lifecycle, Lifecycle("STOPPED", 4, run.identity))
        self.assert_started_observation(run, "STOPPED")
        self.assertIs(run.observation["cleanup_confirmed"], True)
        record = reconcile(spec, run.observation)
        self.assertEqual((record["status"], record["replay_allowed"]), ("STOPPED", True))
        self.assertEqual(run.environment_names, ("LANG", "PATH"))

    def test_nonzero_exit_is_a_failed_observation(self):
        run, _ = self.run_spec(self.spec("import sys; sys.exit(3)"))
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 3))
        self.assertEqual(run.lifecycle.state, "FAILED")
        self.assert_started_observation(run, "FAILED")

    def test_child_sees_exactly_the_explicit_environment(self):
        # /usr/bin/env prints the block it received from execve; a Python child would add
        # LC_CTYPE (PEP 538) and macOS adds __CF_USER_TEXT_ENCODING inside the child.
        environment = {"LANG": "C", "PATH": "/usr/bin:/bin", "SENTINEL_FLAG": "on"}
        spec = self.spec(None, environment=environment, command=("/usr/bin/env",))
        run, _ = self.run_spec(spec, environment=environment)
        received = dict(line.split("=", 1) for line in Path(run.stdout_path).read_text().splitlines())
        self.assertEqual(received, environment)

    def test_timeout_escalates_to_a_group_kill(self):
        leader = ("import subprocess, sys, signal, time\n"
                  "subprocess.Popen([sys.executable, '-c', %r])\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "time.sleep(30)\n" % IGNORE_TERM_SLEEP)
        run, wall = self.run_spec(self.spec(leader))
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((run.stop_reason, run.exit_code), ("timeout_killed", -signal.SIGKILL))
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(run.lifecycle.state, "FAILED")
        self.assert_started_observation(run, "FAILED")
        self.assertTrue(group_gone_within(run.identity.pid, 3.0))

    def test_timeout_obeyed_by_the_leader_is_graceful(self):
        run, wall = self.run_spec(self.spec("import time; time.sleep(30)"))
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((run.stop_reason, run.exit_code), ("timeout", -signal.SIGTERM))
        self.assertIs(run.observation["cleanup_confirmed"], True)

    def test_stragglers_after_a_normal_exit_are_reaped(self):
        leader = ("import subprocess, sys\n"
                  "subprocess.Popen([sys.executable, '-c', %r])\n"
                  "print('leader-done')\n" % IGNORE_TERM_SLEEP)
        run, wall = self.run_spec(self.spec(leader))
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((run.stop_reason, run.exit_code, run.stragglers_killed), ("completed", 0, True))
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(run.lifecycle.state, "STOPPED")
        self.assertTrue(group_gone_within(run.identity.pid, 3.0))

    def test_cancellation_stops_the_group_without_reraising(self):
        ready = self.work / "ready"
        leader = ("import os, signal, sys, time\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "open(sys.argv[1], 'w').close()\n"
                  "time.sleep(30)\n")
        real_wait = subprocess.Popen.wait
        calls = []

        def interrupt_first(proc, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                raise KeyboardInterrupt
            return real_wait(proc, *args, **kwargs)

        with mock.patch.object(subprocess.Popen, "wait", interrupt_first):
            run, wall = self.run_spec(self.spec(leader, str(ready), timeout=30))
        self.assertTrue(ready.exists(), "the leader was not ready before the interrupt")
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((run.stop_reason, run.exit_code), ("cancelled", -signal.SIGKILL))
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assert_started_observation(run, "FAILED")

    def test_exec_failure_is_not_started(self):
        garbage = self.cwd / "not-a-program"
        garbage.write_bytes(b"\x00\x01\x02 not an executable image\n")
        garbage.chmod(0o755)
        spec = self.spec(None, command=(str(garbage),))
        run, _ = self.run_spec(spec)
        self.assertEqual((run.stop_reason, run.exit_code, run.identity), ("start_failed", None, None))
        self.assertEqual(run.lifecycle, Lifecycle("FAILED", 2, None))
        self.assertEqual(run.observation, {"run_id": "run-1", "environment_sha256": spec.environment_sha256,
                                           "state": "NOT_STARTED"})
        record = reconcile(spec, run.observation)
        self.assertEqual((record["status"], record["replay_allowed"]), ("NOT_STARTED", True))

    def test_credential_names_need_an_explicit_allowlist(self):
        environment = {"LANG": "C", "PATH": "/usr/bin:/bin", "SERVICE_API_TOKEN": "secret-value"}
        spec = self.spec("import os; print(len(os.environ['SERVICE_API_TOKEN']))", environment=environment)
        self.assertEqual(self.code_of(supervise, spec, environment=environment, evidence_dir=str(self.evidence),
                                      worktree_id="wt-1"), "E_RUNTIME_ENVIRONMENT")
        self.assertEqual(list(self.evidence.iterdir()), [], "no evidence file before validation passes")
        run, _ = self.run_spec(spec, environment=environment, allowed_secret_names=frozenset({"SERVICE_API_TOKEN"}))
        self.assertEqual(Path(run.stdout_path).read_text(), "12\n")
        self.assertIn("SERVICE_API_TOKEN", run.environment_names)
        self.assertNotIn("secret-value", repr(run))

    def test_invalid_inputs_fail_before_anything_starts(self):
        spec = self.spec("print('never')")
        cases = [
            ({"environment": {"LANG": "C", "PATH": "/evil"}}, "E_RUNTIME_ENVIRONMENT"),
            ({"worktree_id": ""}, "E_RUNTIME_IDENTITY"),
            ({"evidence_dir": str(self.work / "missing")}, "E_RUNTIME_EVIDENCE"),
            ({"evidence_dir": "relative/evidence"}, "E_RUNTIME_EVIDENCE"),
            ({"allowed_secret_names": ["SERVICE_API_TOKEN"]}, "E_RUNTIME_ENVIRONMENT"),
        ]
        for change, code in cases:
            with self.subTest(change=change):
                kwargs = {"environment": BASE_ENV, "evidence_dir": str(self.evidence), "worktree_id": "wt-1"}
                kwargs.update(change)
                self.assertEqual(self.code_of(supervise, spec, **kwargs), code)
        relative_cwd = InvocationSpec("req-1", "run-1", (PY, "-c", "pass"), "tree", environment_digest(BASE_ENV), 1)
        missing_cwd = InvocationSpec("req-1", "run-1", (PY, "-c", "pass"), str(self.work / "nope"),
                                     environment_digest(BASE_ENV), 1)
        for bad in (relative_cwd, missing_cwd):
            with self.subTest(cwd=bad.cwd):
                self.assertEqual(self.code_of(supervise, bad, environment=BASE_ENV, evidence_dir=str(self.evidence),
                                              worktree_id="wt-1"), "E_RUNTIME_INVOCATION")
        self.assertEqual(self.code_of(supervise, "spec", environment=BASE_ENV, evidence_dir=str(self.evidence),
                                      worktree_id="wt-1"), "E_RUNTIME_INVOCATION")
        self.assertEqual(list(self.evidence.iterdir()), [], "no evidence file after rejected inputs")

    def test_existing_evidence_files_are_never_overwritten(self):
        (self.evidence / "stdout.log").write_bytes(b"earlier run")
        self.assertEqual(self.code_of(supervise, self.spec("print('x')"), environment=BASE_ENV,
                                      evidence_dir=str(self.evidence), worktree_id="wt-1"), "E_RUNTIME_EVIDENCE")
        self.assertEqual((self.evidence / "stdout.log").read_bytes(), b"earlier run")


if __name__ == "__main__":
    unittest.main()
