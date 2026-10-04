"""Executor tests for the native POSIX supervisor (posix-supervisor contract).

These cases cover what the planner oracle leaves open: the order between
adjacent validation rules, the platform guard, the evidence rollback when only
the second file can be created, allowlist typing, uppercase-substring credential
matching, descriptor hygiene, the unconfirmed-cleanup path, the bounded stop and
reap windows (interrupts absorbed, refused signals tolerated, poll interval),
lifecycle generations and the absence of environment values in the returned
records.

Convention followed throughout: every rejection case first proves that a legal
baseline run succeeds, then changes exactly one input (or, for the ordering
cases, violates exactly the two adjacent rules under test) and asserts one
specific error code, so a rejection can never be attributed to an earlier rule.
Every case cleans up the process group it started.
"""
from __future__ import annotations

import dataclasses
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

import sentinelmesh.posix_supervisor as posix_supervisor
from sentinelmesh.posix_supervisor import (
    _CREDENTIAL_MARKERS,
    _KILL_WAIT_SECONDS,
    _REAP_POLL_SECONDS,
    _REAP_SECONDS,
    _TERM_GRACE_SECONDS,
    SupervisedRun,
    supervise,
)
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, Lifecycle, ProcessIdentity, RuntimeErrorCode, reconcile

PY = os.path.realpath(sys.executable)
# Long enough for a baseline run that exits immediately, short enough that a
# leaked process is still killed by the test's own cleanup.
BASE_TIMEOUT = 10
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
OK_CODE = "import sys; sys.stdout.write('ok'); sys.stderr.write('bad')"
GARBAGE = b"\x00\x01\x02 not an executable image\n"
# A leader that refuses SIGTERM and sleeps, so the grace wait runs its full second
# and only the SIGKILL ends it.
IGNORE_TERM_SLEEP = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
# Same leader, but it reports readiness first: a cancellation can then be injected
# once the process really exists.
IGNORE_TERM_READY = ("import signal, sys, time\n"
                     "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                     "open(sys.argv[1], 'w').close()\n"
                     "time.sleep(30)\n")
# A leader that exits at once but leaves a straggler behind in its group.
STRAGGLER_LEADER = ("import subprocess, sys\n"
                    "subprocess.Popen([sys.executable, '-c', %r])\n"
                    "print('leader-done')\n" % IGNORE_TERM_SLEEP)


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class PosixSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="supervisor-exec-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.evidence = self.work / "evidence"
        self.evidence.mkdir()
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.garbage = self.cwd / "not-a-program"
        self.garbage.write_bytes(GARBAGE)
        self.garbage.chmod(0o755)

    # helpers ---------------------------------------------------------------

    def spec(self, code, *args, environment=None, timeout=BASE_TIMEOUT, command=None):
        env = BASE_ENV if environment is None else environment
        argv = command if command is not None else (PY, "-c", code, *args)
        return InvocationSpec("req-1", "run-1", argv, str(self.cwd), environment_digest(env), timeout)

    def call(self, spec, environment=None, **kwargs):
        env = BASE_ENV if environment is None else environment
        return supervise(spec, environment=env, evidence_dir=str(self.evidence), worktree_id="wt-1", **kwargs)

    def clear_evidence(self):
        for name in ("stdout.log", "stderr.log"):
            path = self.evidence / name
            if path.exists():
                os.unlink(path)

    def ok_run(self, code=OK_CODE, environment=None, keep_evidence=True, **kwargs):
        """Run a legal baseline and return it; the caller may inspect the result."""
        spec = self.spec(code, environment=environment)
        started = time.monotonic()
        run = self.call(spec, environment, **kwargs)
        self.assertLess(time.monotonic() - started, 30.0, "baseline run did not finish in time")
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertEqual(run.lifecycle, Lifecycle("STOPPED", 4, run.identity))
        self.addCleanup(self.kill_group, run.identity.pid)
        if not keep_evidence:
            self.clear_evidence()
        return run

    def reject(self, expected_code, func, *args, **kwargs):
        """Assert one specific rejection code and that no evidence file was created."""
        before = sorted(path.name for path in self.evidence.iterdir())
        with self.assertRaises(RuntimeErrorCode) as ctx:
            func(*args, **kwargs)
        after = sorted(path.name for path in self.evidence.iterdir())
        self.assertEqual(after, before, "a rejected call must not create evidence files")
        self.assertEqual(ctx.exception.code, expected_code)

    def kill_group(self, pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def never_empty_group(self):
        """Return a patch where the signal-0 probe always answers "still alive"."""
        real_killpg = os.killpg

        def never_empty(pgid, sig):
            if sig == 0:
                # Pretend the process group keeps a member forever; every real
                # signal is still delivered to the real group.
                return 0
            return real_killpg(pgid, sig)

        return mock.patch.object(os, "killpg", never_empty)

    def interrupt_first_wait(self, ready):
        """Return a patch that raises ``KeyboardInterrupt`` in the first wait."""
        real_wait = subprocess.Popen.wait

        def interrupt_first(proc, *args, **kwargs):
            if proc.returncode is None and not ready.exists():
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                raise KeyboardInterrupt
            return real_wait(proc, *args, **kwargs)

        return mock.patch.object(subprocess.Popen, "wait", interrupt_first)

    def start_failed_run(self):
        """One run whose exec fails: an evidence pair, no process."""
        spec = self.spec(None, command=(str(self.garbage),))
        run = self.call(spec)
        self.assertEqual(run.stop_reason, "start_failed")
        self.clear_evidence()
        return run

    # 1. public interface ---------------------------------------------------

    def test_public_surface_field_order_and_constants(self):
        self.assertEqual(
            [field.name for field in dataclasses.fields(SupervisedRun)],
            ["stop_reason", "exit_code", "lifecycle", "observation", "identity", "stdout_path",
             "stderr_path", "stragglers_killed", "environment_names", "duration_seconds"],
        )
        self.assertEqual((_TERM_GRACE_SECONDS, _KILL_WAIT_SECONDS, _REAP_SECONDS), (1.0, 2.0, 2.0))
        self.assertEqual(_CREDENTIAL_MARKERS, ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "AUTH"))
        run = self.ok_run()
        self.assertEqual(run.environment_names, ("LANG", "PATH"))
        self.assertIsInstance(run.duration_seconds, float)
        self.assertGreaterEqual(run.duration_seconds, 0.0)
        self.assertIsInstance(run.identity, ProcessIdentity)
        self.assertEqual(Path(run.stdout_path), self.evidence / "stdout.log")
        self.assertEqual(Path(run.stderr_path), self.evidence / "stderr.log")

    # 2. validation order ---------------------------------------------------

    def test_bad_spec_beats_an_environment_mismatch(self):
        self.ok_run()  # legal baseline: the same inputs start and finish a run
        self.clear_evidence()
        wrong_env = {"LANG": "C", "PATH": "/evil"}
        self.reject("E_RUNTIME_INVOCATION", self.call, "spec", wrong_env)

    def test_bad_cwd_beats_a_missing_evidence_dir(self):
        self.ok_run()  # legal baseline
        self.clear_evidence()
        relative = InvocationSpec("req-1", "run-1", (PY, "-c", "pass"), "tree", environment_digest(BASE_ENV), 1)
        missing = InvocationSpec("req-1", "run-1", (PY, "-c", "pass"), str(self.work / "nope"),
                                 environment_digest(BASE_ENV), 1)
        for bad, target in ((relative, "relative/evidence"), (missing, str(self.work / "missing"))):
            with self.subTest(cwd=bad.cwd):
                self.reject("E_RUNTIME_INVOCATION", supervise, bad, environment=BASE_ENV,
                            evidence_dir=target, worktree_id="wt-1")

    def test_credential_name_beats_an_empty_worktree_id(self):
        environment = {"LANG": "C", "PATH": "/usr/bin:/bin", "SERVICE_API_TOKEN": "secret-value"}
        spec = self.spec("pass", environment=environment)
        # Legal baseline: the same environment starts once the name is allowlisted
        # and the worktree id is usable.
        run = self.ok_run("pass", environment=environment, allowed_secret_names=frozenset({"SERVICE_API_TOKEN"}),
                          keep_evidence=False)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        # Both rules violated at once: the credential rule (6) fires, not the
        # worktree-id rule (7) that would fire if the ordering were reversed.
        self.reject("E_RUNTIME_ENVIRONMENT", supervise, spec, environment=environment,
                    evidence_dir=str(self.evidence), worktree_id="")
        # The same input with only rule 7 violated isolates that rule, so the
        # combined rejection above cannot be a mis-ordered worktree-id check.
        base_spec = self.spec("pass")
        self.reject("E_RUNTIME_IDENTITY", supervise, base_spec, environment=BASE_ENV,
                    evidence_dir=str(self.evidence), worktree_id="")

    def test_platform_guard(self):
        self.ok_run()  # legal baseline on this POSIX platform
        self.clear_evidence()
        with mock.patch.object(os, "name", "nt"):
            self.reject("E_RUNTIME_PLATFORM", self.call, self.spec("pass"))

    # 3. evidence files -----------------------------------------------------

    def test_second_evidence_file_rejection_removes_the_first(self):
        self.ok_run()  # legal baseline creates the pair normally
        self.clear_evidence()
        (self.evidence / "stderr.log").write_bytes(b"earlier stderr")
        spec = self.spec("print('never')")
        self.reject("E_RUNTIME_EVIDENCE", self.call, spec)
        self.assertFalse((self.evidence / "stdout.log").exists(), "the rolled back stdout file was left behind")
        self.assertEqual((self.evidence / "stderr.log").read_bytes(), b"earlier stderr",
                         "a pre-existing evidence file must not be touched")
        # The first-file rejection leaves the directory untouched as well.
        self.clear_evidence()
        (self.evidence / "stdout.log").write_bytes(b"earlier stdout")
        self.reject("E_RUNTIME_EVIDENCE", self.call, spec)
        self.assertEqual((self.evidence / "stdout.log").read_bytes(), b"earlier stdout")

    # 4. credential allowlist ----------------------------------------------

    def test_allowlist_must_hold_text(self):
        self.ok_run()  # legal baseline
        self.clear_evidence()
        spec = self.spec("pass")
        for bad in (frozenset({"SERVICE_API_TOKEN", 1}), {"A", None}):
            with self.subTest(allowlist=bad):
                self.reject("E_RUNTIME_ENVIRONMENT", supervise, spec, environment=BASE_ENV,
                            evidence_dir=str(self.evidence), worktree_id="wt-1", allowed_secret_names=bad)

    def test_allowlisted_name_absent_from_the_environment_is_harmless(self):
        run = self.ok_run(allowed_secret_names=frozenset({"NOT_PRESENT_TOKEN"}))
        self.assertEqual(run.environment_names, ("LANG", "PATH"))
        self.assertEqual(Path(run.stdout_path).read_text(), "ok")

    def test_credential_match_is_an_uppercase_substring(self):
        for name in ("my_Token", "MONKEY", "AUTH", "COOKIE_VALUE"):
            with self.subTest(name=name):
                environment = {"LANG": "C", "PATH": "/usr/bin:/bin", name: "value"}
                spec = self.spec("pass", environment=environment)
                self.reject("E_RUNTIME_ENVIRONMENT", supervise, spec, environment=environment,
                            evidence_dir=str(self.evidence), worktree_id="wt-1")
                run = self.ok_run("pass", environment=environment, allowed_secret_names=frozenset({name}),
                                  keep_evidence=False)
                self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        # A name that merely shares letters with a marker, without containing the
        # marker as a substring, is not credential-shaped.
        environment = {"LANG": "C", "PATH": "/usr/bin:/bin", "OKAY": "1", "LANGISH": "2"}
        run = self.ok_run("pass", environment=environment, keep_evidence=False)
        self.assertEqual(run.environment_names, ("LANG", "LANGISH", "OKAY", "PATH"))

    # 5. descriptor hygiene -------------------------------------------------

    @unittest.skipUnless(os.path.isdir("/dev/fd"), "/dev/fd is the POSIX descriptor directory")
    def test_no_descriptor_leak_across_runs(self):
        def fd_count():
            return len(os.listdir("/dev/fd"))

        self.assertEqual(fd_count(), fd_count(), "/dev/fd is not a stable measurement on this platform")
        before = fd_count()
        for _ in range(3):
            self.ok_run(keep_evidence=False)
        for _ in range(2):
            self.start_failed_run()
        self.assertEqual(fd_count(), before, "supervise leaked file descriptors")

    # 6. lifecycle generations ---------------------------------------------

    def test_start_failure_lifecycle_and_observation(self):
        run = self.start_failed_run()
        self.assertIsNone(run.identity)
        self.assertIsNone(run.exit_code)
        self.assertFalse(run.stragglers_killed)
        self.assertEqual(run.lifecycle, Lifecycle("FAILED", 2, None))
        self.assertEqual(set(run.observation), {"run_id", "environment_sha256", "state"})
        self.assertEqual(run.observation["state"], "NOT_STARTED")
        self.assertEqual(Path(run.stdout_path), self.evidence / "stdout.log")
        self.assertEqual(Path(run.stderr_path), self.evidence / "stderr.log")
        self.assertIsInstance(run.duration_seconds, float)
        self.assertGreaterEqual(run.duration_seconds, 0.0)
        spec = self.spec(None, command=(str(self.garbage),))
        record = reconcile(spec, run.observation)
        self.assertEqual((record["status"], record["replay_allowed"]), ("NOT_STARTED", True))

    # 7. unconfirmed cleanup ------------------------------------------------

    def assert_unconfirmed(self, spec, run, state):
        """One run whose group was never confirmed empty keeps the leader's outcome."""
        self.assertIs(run.observation["cleanup_confirmed"], False)
        self.assertIs(run.stragglers_killed, True)
        self.assertEqual(set(run.observation), {"run_id", "environment_sha256", "state",
                                                "process_identity", "cleanup_confirmed"})
        # Rule 8 of the v1 contract: the observation keeps the leader's outcome;
        # only the lifecycle records that the start state is unknown, so
        # ``reconcile`` still sees the outcome and refuses the replay on the
        # cleanup fact instead of on a rewritten state.
        self.assertEqual(run.observation["state"], state)
        self.assertEqual(run.lifecycle, Lifecycle("START_UNKNOWN", 4, run.identity))
        record = reconcile(spec, run.observation)
        self.assertEqual((record["status"], record["replay_allowed"]), (state, False))

    def test_unconfirmed_cleanup_keeps_the_stopped_outcome(self):
        spec = self.spec(OK_CODE)
        with self.never_empty_group():
            started = time.monotonic()
            run = self.call(spec)
            wall = time.monotonic() - started
        self.addCleanup(self.kill_group, run.identity.pid)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assert_unconfirmed(spec, run, "STOPPED")
        # The unconfirmed group is polled for at most the documented window.
        self.assertLess(wall, _REAP_SECONDS + 5.0)

    def test_unconfirmed_cleanup_keeps_the_cancelled_outcome(self):
        ready = self.work / "ready"
        spec = self.spec(IGNORE_TERM_READY, str(ready), timeout=30)
        with self.never_empty_group(), self.interrupt_first_wait(ready):
            run = self.call(spec)
        self.addCleanup(self.kill_group, run.identity.pid)
        self.assertTrue(ready.exists(), "the leader was not ready before the interrupt")
        # A cancellation still escalates: the leader refuses SIGTERM, so the
        # group is SIGKILLed and the leader's outcome is a failed run.
        self.assertEqual((run.stop_reason, run.exit_code), ("cancelled", -signal.SIGKILL))
        self.assert_unconfirmed(spec, run, "FAILED")

    def test_unconfirmed_cleanup_keeps_the_killed_outcome(self):
        spec = self.spec(IGNORE_TERM_SLEEP, timeout=1)
        with self.never_empty_group():
            run = self.call(spec)
        self.addCleanup(self.kill_group, run.identity.pid)
        self.assertEqual((run.stop_reason, run.exit_code), ("timeout_killed", -signal.SIGKILL))
        self.assert_unconfirmed(spec, run, "FAILED")

    # 8. stop and reap windows ----------------------------------------------

    def test_interrupt_during_the_wait_after_the_kill_is_absorbed(self):
        """An interrupt inside the post-SIGKILL window is absorbed, not re-raised."""
        real_wait = subprocess.Popen.wait
        waits = []
        interrupted = []

        def interrupt_after_the_kill(proc, *args, **kwargs):
            waits.append(time.monotonic())
            # The leader refuses SIGTERM: the first wait times out, the grace
            # wait times out, the group is killed, and only the wait after the
            # kill is interrupted once.
            if len(waits) == 3:
                interrupted.append(time.monotonic())
                raise KeyboardInterrupt
            return real_wait(proc, *args, **kwargs)

        spec = self.spec(IGNORE_TERM_SLEEP, timeout=1)
        with mock.patch.object(subprocess.Popen, "wait", interrupt_after_the_kill):
            run = self.call(spec)
        self.addCleanup(self.kill_group, run.identity.pid)
        self.assertEqual(len(interrupted), 1, "the wait after the kill was not interrupted")
        self.assertEqual((run.stop_reason, run.exit_code), ("timeout_killed", -signal.SIGKILL))
        self.assertIs(run.stragglers_killed, False)
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(run.observation["state"], "FAILED")
        self.assertEqual(run.lifecycle, Lifecycle("FAILED", 4, run.identity))

    def test_reap_poll_never_sleeps_longer_than_the_interval_bound(self):
        """Every sleep of the leftover poll stays inside the documented bound."""
        sleeps = []

        class recording_time:
            @staticmethod
            def monotonic():
                return time.monotonic()

            @staticmethod
            def sleep(seconds):
                sleeps.append(seconds)
                time.sleep(seconds)

        spec = self.spec(OK_CODE)
        with self.never_empty_group(), mock.patch.object(posix_supervisor, "time", recording_time):
            run = self.call(spec)
        self.addCleanup(self.kill_group, run.identity.pid)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertIs(run.observation["cleanup_confirmed"], False)
        self.assertGreaterEqual(len(sleeps), 2, "the leftover poll did not cycle during its window")
        for seconds in sleeps:
            self.assertLessEqual(seconds, _REAP_POLL_SECONDS,
                                 "a leftover poll slept longer than the interval bound")
        self.assertLessEqual(sum(sleeps), _REAP_SECONDS + _REAP_POLL_SECONDS,
                             "the leftover poll slept past its window")

    def test_permission_error_from_the_reap_kill_does_not_escape(self):
        """A refused SIGKILL in the reap phase still reports the leftovers."""
        real_killpg = os.killpg

        def kill_denied(pgid, sig):
            result = real_killpg(pgid, sig)
            if sig == signal.SIGKILL:
                # The kernel refuses the signal (macOS answers EPERM for a group
                # of unreaped zombies); the report of the kill is still refused
                # only after the real signal has been delivered.
                raise PermissionError("operation not permitted")
            return result

        spec = self.spec(STRAGGLER_LEADER)
        with mock.patch.object(os, "killpg", kill_denied):
            run = self.call(spec)
        self.addCleanup(self.kill_group, run.identity.pid)
        # The leader exited on its own, so the only SIGKILL of this run is the
        # one the reap phase sends to a group that still holds a straggler.
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertIs(run.stragglers_killed, True)
        self.assertIs(run.observation["cleanup_confirmed"], True,
                      "the delivered SIGKILL emptied the group, so the reap confirmed it")
        self.assertEqual(run.observation["state"], "STOPPED")
        self.assertEqual(run.lifecycle, Lifecycle("STOPPED", 4, run.identity))

    # 9. no environment value leaks ----------------------------------------

    def test_allowlisted_credential_values_never_reach_the_records(self):
        secret = "sekret-value-1234"
        environment = {"LANG": "C", "PATH": "/usr/bin:/bin", "SERVICE_API_TOKEN": secret}
        code = "import os, sys; sys.stdout.write(os.environ['SERVICE_API_TOKEN'])"
        run = self.ok_run(code, environment=environment, allowed_secret_names=frozenset({"SERVICE_API_TOKEN"}))
        self.assertEqual(Path(run.stdout_path).read_text(), secret, "the child did receive its environment")
        self.assertIn("SERVICE_API_TOKEN", run.environment_names)
        self.assertNotIn(secret, repr(run))
        for value in run.observation.values():
            self.assertNotIn(secret, str(value))
        self.assertNotIn(secret, json.dumps(run.observation))
        self.assertNotIn(secret, repr(run.identity))
        self.assertNotIn(secret, repr(run.lifecycle))


if __name__ == "__main__":
    unittest.main()
