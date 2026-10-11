"""Planner oracle for win-supervise (`.agent/design/windows-supervisor.md`, section 3.5).

The third of the four Windows supervisor contracts: ``windows_supervisor.supervise``
starts one explicitly described process ``CREATE_SUSPENDED``, puts it into a Job
Object with ``KILL_ON_JOB_CLOSE``, resumes it, waits, ends the whole job with
``TerminateJobObject`` on timeout or cancellation (exit code
``TERMINATED_EXIT_CODE``), reaps by the job's member count and returns the same
`SupervisedRun` the POSIX supervisor returns.

Every native case starts real local ``python`` process trees (no provider CLI, no
credentials, no network) and kills whatever it started, even when an assertion
fails.  The handle census (``GetProcessHandleCount``) stands in for the POSIX
``/dev/fd`` descriptor count: a run must give back the job, process, thread and
file handles it took.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sentinelmesh.posix_supervisor as posix_supervisor
import sentinelmesh.windows_supervisor as windows_supervisor
from sentinelmesh.process_identity import boot_id, environment_digest, file_sha256
from sentinelmesh.runtime import InvocationSpec, Lifecycle, ProcessIdentity, RuntimeErrorCode, reconcile
from sentinelmesh.windows_supervisor import TERMINATED_EXIT_CODE, SupervisedRun, streams_match, supervise

PY = os.path.realpath(sys.executable)
NL = chr(10)
TIMEOUT = 1
# timeout + kill wait (2 s) + reap (2 s), with room for a busy machine
WALL_BOUND = TIMEOUT + 7.0
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
IMPORT_CHECK = ("import sys, sentinelmesh.windows_supervisor" + NL
                + "assert 'ctypes' not in sys.modules, 'ctypes'" + NL
                + "print('IMPORT_SAFE_OK')" + NL)
# A leader that spawns a grandchild sleeping for a minute, reports its pid on
# stdout and then sleeps itself: what a timeout has to end as a whole.
TREE_THEN_SLEEP = ("import subprocess, sys, time" + NL
                   + "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])" + NL
                   + "sys.stdout.write(str(p.pid)); sys.stdout.flush()" + NL
                   + "time.sleep(60)" + NL)
# A leader that leaves a grandchild behind and exits at once.
TREE_THEN_EXIT = ("import subprocess, sys" + NL
                  + "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])" + NL
                  + "sys.stdout.write(str(p.pid)); sys.stdout.flush()" + NL)
# A leader that marks a file once it runs, then sleeps (for the cancellation case).
MARK_THEN_SLEEP = ("import sys, time" + NL
                   + "open(sys.argv[1], 'w').close()" + NL
                   + "time.sleep(60)" + NL)
STILL_ACTIVE = 259


def _k32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetProcessHandleCount.restype = wintypes.BOOL
    k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    return ctypes, wintypes, k32


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` is still running (a pid that cannot be opened counts as gone)."""
    ctypes, wintypes, k32 = _k32()
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def pid_gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def kill_pid(pid: int) -> None:
    ctypes, wintypes, k32 = _k32()
    handle = k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if handle:
        k32.TerminateProcess(handle, 1)
        k32.CloseHandle(handle)


def handle_count() -> int:
    ctypes, wintypes, k32 = _k32()
    count = wintypes.DWORD()
    assert k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(count))
    return count.value


class SupervisorCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="win-supervise-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.evidence = self.work / "evidence"
        self.evidence.mkdir()
        self.cwd = self.work / "tree"
        self.cwd.mkdir()

    def spec(self, code, *args, environment=None, timeout=TIMEOUT, command=None):
        env = BASE_ENV if environment is None else environment
        argv = command if command is not None else (PY, "-c", code, *args)
        return InvocationSpec("req-1", "run-1", argv, str(self.cwd), environment_digest(env), timeout)

    def run_spec(self, spec, environment=None, evidence=None, **kwargs):
        env = BASE_ENV if environment is None else environment
        started = time.monotonic()
        run = supervise(spec, environment=env, evidence_dir=str(evidence or self.evidence), worktree_id="wt-1",
                        **kwargs)
        wall = time.monotonic() - started
        if run.identity is not None:
            self.addCleanup(kill_pid, run.identity.pid)
        return run, wall

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
        json.dumps(observation)

    def grandchild_pid(self, run) -> int:
        return int(Path(run.stdout_path).read_bytes().split()[0])


@unittest.skipIf(os.name != "nt", "native Windows Job Object supervision")
class WindowsSupervisorOracle(SupervisorCase):
    def test_completed_run_writes_exclusive_evidence_and_a_stopped_observation(self):
        spec = self.spec("import sys; sys.stdout.buffer.write(b'out'); sys.stderr.buffer.write(b'err')")
        run, wall = self.run_spec(spec)
        self.assertIsInstance(run, SupervisedRun)
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((run.stop_reason, run.exit_code, run.stragglers_killed), ("completed", 0, False))
        self.assertEqual(Path(run.stdout_path), self.evidence / "stdout.log")
        self.assertEqual(Path(run.stderr_path), self.evidence / "stderr.log")
        self.assertEqual(Path(run.stdout_path).read_bytes(), b"out")
        self.assertEqual(Path(run.stderr_path).read_bytes(), b"err")
        self.assertIsInstance(run.identity, ProcessIdentity)
        self.assertEqual(run.identity.boot_id, boot_id())
        self.assertEqual(run.identity.executable_sha256, file_sha256(PY))
        self.assertEqual(run.lifecycle, Lifecycle("STOPPED", 4, run.identity))
        self.assert_started_observation(run, "STOPPED")
        self.assertIs(run.observation["cleanup_confirmed"], True)
        record = reconcile(spec, run.observation)
        self.assertEqual((record["status"], record["replay_allowed"]), ("STOPPED", True))
        self.assertEqual(run.environment_names, ("LANG", "PATH"))
        self.assertEqual(run.streams["stdout"]["bytes"], 3)
        self.assertEqual(run.streams["stderr"]["bytes"], 3)
        self.assertIs(streams_match(str(self.evidence), run.streams), True)
        self.assertTrue(pid_gone_within(run.identity.pid, 3.0))

    def test_nonzero_exit_is_a_failed_observation(self):
        run, _ = self.run_spec(self.spec("import os, sys; sys.stdout.write(str(os.getpid())); sys.exit(3)"))
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 3))
        self.assertEqual(int(Path(run.stdout_path).read_bytes()), run.identity.pid)
        self.assertEqual(run.lifecycle.state, "FAILED")
        self.assert_started_observation(run, "FAILED")
        self.assertIs(run.observation["cleanup_confirmed"], True)

    def test_child_sees_exactly_the_explicit_environment(self):
        # Windows upper-cases the names it hands a child; the values and the set
        # of names must be exactly the explicit mapping, nothing inherited.
        environment = dict(BASE_ENV, SENTINEL_FLAG="on")
        spec = self.spec("import json, os, sys; sys.stdout.write(json.dumps("
                         "{'env': dict(os.environ), 'cwd': os.getcwd(), 'pid': os.getpid()}))",
                         environment=environment)
        run, _ = self.run_spec(spec, environment=environment)
        received = json.loads(Path(run.stdout_path).read_bytes())
        self.assertEqual({name.upper(): value for name, value in received["env"].items()},
                         {name.upper(): value for name, value in environment.items()})
        # The child runs in the specification's directory, and the identity
        # names the child itself.
        self.assertEqual(os.path.normcase(os.path.realpath(received["cwd"])), os.path.normcase(str(self.cwd)))
        self.assertEqual(received["pid"], run.identity.pid)

    def test_timeout_ends_the_whole_job_at_once(self):
        run, wall = self.run_spec(self.spec(TREE_THEN_SLEEP))
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((run.stop_reason, run.exit_code), ("timeout_killed", TERMINATED_EXIT_CODE))
        self.assertEqual(TERMINATED_EXIT_CODE, 0xC000013A)
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(run.lifecycle.state, "FAILED")
        self.assert_started_observation(run, "FAILED")
        self.assertTrue(pid_gone_within(run.identity.pid, 3.0))
        self.assertTrue(pid_gone_within(self.grandchild_pid(run), 3.0), "the grandchild outlived the job")
        # The timeout itself, not the kill or the reap, is what took the time.
        self.assertGreaterEqual(run.duration_seconds, TIMEOUT)
        self.assertLess(run.duration_seconds, TIMEOUT + 1.5)

    def test_stragglers_after_a_normal_exit_are_reaped(self):
        run, wall = self.run_spec(self.spec(TREE_THEN_EXIT))
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((run.stop_reason, run.exit_code, run.stragglers_killed), ("completed", 0, True))
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(run.lifecycle.state, "STOPPED")
        self.assertTrue(pid_gone_within(self.grandchild_pid(run), 3.0), "the straggler outlived the run")

    def test_cancellation_ends_the_job_without_reraising(self):
        ready = self.work / "ready"
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
            run, wall = self.run_spec(self.spec(MARK_THEN_SLEEP, str(ready), timeout=30))
        self.assertTrue(ready.exists(), "the leader was not running before the interrupt")
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((run.stop_reason, run.exit_code), ("cancelled", TERMINATED_EXIT_CODE))
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assert_started_observation(run, "FAILED")
        self.assertTrue(pid_gone_within(run.identity.pid, 3.0))

    def test_an_image_that_cannot_start_is_not_started(self):
        garbage = self.cwd / "not-a-program.exe"
        garbage.write_bytes(bytes([0, 1, 2]) + b" not an executable image" + NL.encode())
        spec = self.spec(None, command=(str(garbage),))
        run, _ = self.run_spec(spec)
        self.assertEqual((run.stop_reason, run.exit_code, run.identity, run.streams), ("start_failed", None, None, None))
        self.assertEqual(run.lifecycle, Lifecycle("FAILED", 2, None))
        self.assertEqual(run.observation, {"run_id": "run-1", "environment_sha256": spec.environment_sha256,
                                           "state": "NOT_STARTED"})
        record = reconcile(spec, run.observation)
        self.assertEqual((record["status"], record["replay_allowed"]), ("NOT_STARTED", True))
        # The evidence files record the attempt and stayed empty.
        self.assertEqual(sorted(os.listdir(self.evidence)), ["stderr.log", "stdout.log"])
        self.assertEqual((self.evidence / "stdout.log").read_bytes(), b"")

    def test_a_process_that_cannot_join_the_job_never_runs(self):
        marker = self.work / "ran"
        ctypes, wintypes, k32 = _k32()
        k32.GetProcessId.restype = wintypes.DWORD
        k32.GetProcessId.argtypes = [wintypes.HANDLE]
        pids = []

        def refuse_after_a_while(job, process_handle):
            # Long enough for a running child to start Python and touch the
            # marker: only a suspended child stays still.
            pids.append(k32.GetProcessId(process_handle))
            time.sleep(0.5)
            return False

        with mock.patch.object(windows_supervisor._Job, "assign_and_resume", refuse_after_a_while):
            run, wall = self.run_spec(self.spec("import sys; open(sys.argv[1], 'w').close()", str(marker)))
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((run.stop_reason, run.exit_code, run.identity), ("start_failed", None, None))
        self.assertEqual(run.observation["state"], "NOT_STARTED")
        self.assertEqual(run.lifecycle, Lifecycle("FAILED", 2, None))
        # Terminated while still suspended: not one instruction of it ran, and
        # the process is gone rather than left suspended forever.
        self.assertEqual(len(pids), 1)
        self.assertTrue(pid_gone_within(pids[0], 3.0), "the refused process was left behind")
        time.sleep(0.2)
        self.assertFalse(marker.exists(), "a process that was not in the job ran anyway")

    def test_an_unconfirmed_cleanup_keeps_the_outcome_and_blocks_a_replay(self):
        cases = {
            "completed": (self.spec("pass"), "completed", 0, "STOPPED"),
            "timeout": (self.spec("import time; time.sleep(60)"), "timeout_killed", TERMINATED_EXIT_CODE, "FAILED"),
        }
        for label, (spec, stop_reason, exit_code, state) in cases.items():
            with self.subTest(case=label):
                evidence = self.work / ("evidence-" + label)
                evidence.mkdir()
                with mock.patch.object(windows_supervisor._Job, "active_processes", return_value=1):
                    run, wall = self.run_spec(spec, evidence=evidence)
                self.assertLess(wall, WALL_BOUND)
                self.assertEqual((run.stop_reason, run.exit_code, run.stragglers_killed), (stop_reason, exit_code, True))
                self.assertIs(run.observation["cleanup_confirmed"], False)
                self.assertEqual(run.observation["state"], state)
                self.assertEqual(run.lifecycle.state, "START_UNKNOWN")
                self.assertIs(reconcile(spec, run.observation)["replay_allowed"], False)
                # The reap window was spent in full before giving up.
                self.assertGreaterEqual(run.duration_seconds, posix_supervisor._REAP_SECONDS)
                self.assertIsNotNone(run.streams["stdout"], "the streams are still digested")

    def test_a_member_count_that_cannot_be_read_is_an_unconfirmed_cleanup(self):
        with mock.patch.object(windows_supervisor._Job, "active_processes", return_value=None):
            run, wall = self.run_spec(self.spec("pass"))
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((run.stop_reason, run.exit_code, run.stragglers_killed), ("completed", 0, False))
        self.assertIs(run.observation["cleanup_confirmed"], False)
        self.assertEqual(run.lifecycle.state, "START_UNKNOWN")

    def test_an_interrupt_during_the_reap_poll_is_absorbed(self):
        real_sleep = time.sleep
        interrupted = []

        def interrupt_once(seconds):
            if not interrupted:
                interrupted.append(seconds)
                raise KeyboardInterrupt
            real_sleep(seconds)

        real_active = windows_supervisor._Job.active_processes
        counts = []

        def one_member_twice(job):
            # The probe before the poll and the poll's first probe both see a
            # member, so the poll reaches its first sleep; that sleep is
            # interrupted and the poll must go on to the truth.
            value = real_active(job)
            counts.append(value)
            return 1 if len(counts) <= 2 else value

        # An interrupt that escapes would abort the whole test session instead
        # of failing this case, so it is caught here and reported as a failure.
        try:
            with mock.patch.object(windows_supervisor._Job, "active_processes", one_member_twice):
                with mock.patch.object(windows_supervisor.time, "sleep", interrupt_once):
                    run, wall = self.run_spec(self.spec("pass"))
        except KeyboardInterrupt:
            self.fail("the interrupt escaped the reap poll")
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual(interrupted, [posix_supervisor._REAP_POLL_SECONDS], "the poll did not sleep the bound once")
        self.assertEqual((run.stop_reason, run.stragglers_killed), ("completed", True))
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(run.lifecycle.state, "STOPPED")

    def test_every_run_gives_its_handles_back(self):
        # One warm-up run loads ctypes and whatever the first call lazily opens,
        # and one failing bare ``Popen`` pays CPython's one-time cost of a failed
        # ``CreateProcess`` (eight handles, measured by the review of
        # attempt-001): without it the "start failed" case below would depend on
        # whether an earlier case already failed a start in this process.
        self.run_spec(self.spec("pass"), evidence=self.evidence)
        garbage = self.cwd / "not-a-program.exe"
        garbage.write_bytes(bytes([0, 1, 2]) + b" not an executable image" + NL.encode())
        with self.assertRaises(OSError):
            subprocess.Popen([str(garbage)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        cases = {
            "completed": ("pass", {}),
            "timeout": (TREE_THEN_SLEEP, {}),
            "start failed": (None, {"command": (str(garbage),)}),
        }
        before = handle_count()
        for label, (code, extra) in cases.items():
            with self.subTest(case=label):
                evidence = self.work / ("evidence-" + label.replace(" ", "-"))
                evidence.mkdir()
                run, _ = self.run_spec(self.spec(code, **extra), evidence=evidence)
                del run
                self.assertEqual(handle_count(), before, "a handle leaked")

    def test_closing_the_job_ends_its_members(self):
        # KILL_ON_JOB_CLOSE: if the supervisor dies holding the job handle, the
        # kernel ends the tree.  Checked on the job itself: a member started
        # through the supervisor's own start path must die when the handle closes.
        api = windows_supervisor._win32()
        job = windows_supervisor._Job(api)
        stdout_fd = os.open(self.evidence / "stdout.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
        stderr_fd = os.open(self.evidence / "stderr.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600)
        try:
            proc = windows_supervisor._start_in_job(job, self.spec(TREE_THEN_SLEEP), PY, BASE_ENV, stdout_fd, stderr_fd)
        finally:
            os.close(stdout_fd)
            os.close(stderr_fd)
        self.addCleanup(kill_pid, proc.pid)
        deadline = time.monotonic() + 10
        while not (self.evidence / "stdout.log").read_bytes() and time.monotonic() < deadline:
            time.sleep(0.02)
        grandchild = int((self.evidence / "stdout.log").read_bytes().split()[0])
        self.addCleanup(kill_pid, grandchild)
        self.assertTrue(pid_alive(proc.pid) and pid_alive(grandchild), "the tree is not running")
        job.close()
        self.assertTrue(pid_gone_within(proc.pid, 3.0), "the leader survived the job handle")
        self.assertTrue(pid_gone_within(grandchild, 3.0), "the grandchild survived the job handle")
        proc.wait(5)

    def test_a_hook_that_raises_starts_nothing_and_removes_both_files(self):
        calls = []

        def hook():
            calls.append(1)
            raise ValueError("hook refused")

        before = handle_count()
        with self.assertRaises(ValueError) as ctx:
            supervise(self.spec("print('never')"), environment=BASE_ENV, evidence_dir=str(self.evidence),
                      worktree_id="wt-1", before_start=hook)
        self.assertEqual(str(ctx.exception), "hook refused")
        self.assertEqual(calls, [1])
        # On Windows an open file cannot be removed: both gone proves the pins
        # and the write descriptors were closed before the removal.
        self.assertEqual(os.listdir(self.evidence), [])
        self.assertEqual(handle_count(), before, "a handle leaked")

    def test_credential_names_need_an_explicit_allowlist(self):
        environment = dict(BASE_ENV, SERVICE_API_TOKEN="secret-value")
        spec = self.spec("import os, sys; sys.stdout.write(str(len(os.environ['SERVICE_API_TOKEN'])))",
                         environment=environment)
        self.assertEqual(self.code_of(supervise, spec, environment=environment, evidence_dir=str(self.evidence),
                                      worktree_id="wt-1"), "E_RUNTIME_ENVIRONMENT")
        self.assertEqual(list(self.evidence.iterdir()), [], "no evidence file before validation passes")
        run, _ = self.run_spec(spec, environment=environment, allowed_secret_names=frozenset({"SERVICE_API_TOKEN"}))
        self.assertEqual(Path(run.stdout_path).read_bytes(), b"12")
        self.assertIn("SERVICE_API_TOKEN", run.environment_names)
        self.assertNotIn("secret-value", repr(run))

    def test_invalid_inputs_fail_before_anything_starts(self):
        spec = self.spec("print('never')")
        cases = [
            ({"environment": dict(BASE_ENV, PATH=BASE_ENV["PATH"] + ";")}, "E_RUNTIME_ENVIRONMENT"),
            ({"worktree_id": ""}, "E_RUNTIME_IDENTITY"),
            ({"evidence_dir": str(self.work / "missing")}, "E_RUNTIME_EVIDENCE"),
            ({"evidence_dir": "relative" + os.sep + "evidence"}, "E_RUNTIME_EVIDENCE"),
            ({"allowed_secret_names": ["SERVICE_API_TOKEN"]}, "E_RUNTIME_ENVIRONMENT"),
            ({"before_start": "not callable"}, "E_RUNTIME_INVOCATION"),
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
        # A shim is not an executable on Windows (user decision, section 5 item 3).
        shim = self.cwd / "tool.cmd"
        shim.write_bytes(b"@echo off" + NL.encode())
        self.assertEqual(self.code_of(supervise, self.spec(None, command=(str(shim),)), environment=BASE_ENV,
                                      evidence_dir=str(self.evidence), worktree_id="wt-1"), "E_RUNTIME_IDENTITY")
        self.assertEqual(list(self.evidence.iterdir()), [], "no evidence file after rejected inputs")

    def test_the_check_order_is_the_posix_one(self):
        # A bad spec beats an environment mismatch; a bad cwd beats a missing
        # evidence directory; a credential name beats an empty worktree id.
        spec = self.spec("pass")
        self.assertEqual(self.code_of(supervise, "spec", environment={"LANG": "other"},
                                      evidence_dir=str(self.evidence), worktree_id="wt-1"), "E_RUNTIME_INVOCATION")
        bad_cwd = InvocationSpec("req-1", "run-1", (PY, "-c", "pass"), str(self.work / "nope"),
                                 environment_digest(BASE_ENV), 1)
        self.assertEqual(self.code_of(supervise, bad_cwd, environment=BASE_ENV,
                                      evidence_dir=str(self.work / "missing"), worktree_id="wt-1"),
                         "E_RUNTIME_INVOCATION")
        environment = dict(BASE_ENV, API_KEY="x")
        self.assertEqual(self.code_of(supervise, self.spec("pass", environment=environment), environment=environment,
                                      evidence_dir=str(self.evidence), worktree_id=""), "E_RUNTIME_ENVIRONMENT")
        # The evidence directory is checked before the hook's callability.
        self.assertEqual(self.code_of(supervise, spec, environment=BASE_ENV, evidence_dir=str(self.work / "missing"),
                                      worktree_id="wt-1", before_start="x"), "E_RUNTIME_EVIDENCE")

    def test_existing_evidence_files_are_never_overwritten(self):
        (self.evidence / "stdout.log").write_bytes(b"earlier run")
        self.assertEqual(self.code_of(supervise, self.spec("print('x')"), environment=BASE_ENV,
                                      evidence_dir=str(self.evidence), worktree_id="wt-1"), "E_RUNTIME_EVIDENCE")
        self.assertEqual((self.evidence / "stdout.log").read_bytes(), b"earlier run")
        self.assertEqual(sorted(os.listdir(self.evidence)), ["stdout.log"])


class WindowsSupervisorSurfaceOracle(SupervisorCase):
    """What holds on every platform: the guard, the surface, the import."""

    def test_platform_guard_comes_first(self):
        with mock.patch.object(windows_supervisor.os, "name", "posix"):
            self.assertEqual(self.code_of(supervise, "spec", environment=BASE_ENV, evidence_dir=str(self.evidence),
                                          worktree_id="wt-1"), "E_RUNTIME_PLATFORM")

    def test_public_surface(self):
        self.assertEqual(windows_supervisor.__all__, ["SupervisedRun", "TERMINATED_EXIT_CODE", "streams_match",
                                                      "supervise"])
        self.assertEqual(TERMINATED_EXIT_CODE, 0xC000013A)
        self.assertIs(windows_supervisor.SupervisedRun, posix_supervisor.SupervisedRun)
        self.assertEqual(windows_supervisor._REAP_SECONDS, posix_supervisor._REAP_SECONDS)
        self.assertEqual(windows_supervisor._REAP_POLL_SECONDS, posix_supervisor._REAP_POLL_SECONDS)
        self.assertFalse(hasattr(windows_supervisor, "_TERM_GRACE_SECONDS"), "Windows has no graceful stop")

    def test_a_process_joins_the_job_before_it_is_resumed(self):
        # The order is what keeps a descendant from escaping: a process resumed
        # before it is in the job could spawn a child outside it.  Pinned with a
        # recording API so the rule holds on every platform and regardless of
        # how fast a child starts.
        calls = []

        class FakeK32:
            def CreateJobObjectW(self, *args):
                return 41

            def SetInformationJobObject(self, *args):
                return 1

            def AssignProcessToJobObject(self, handle, process_handle):
                calls.append(("assign", handle, process_handle))
                return 0 if process_handle == 7 else 1

            def CloseHandle(self, handle):
                calls.append(("close", handle))
                return 1

        class FakeNtdll:
            def NtResumeProcess(self, process_handle):
                calls.append(("resume", process_handle))
                return 0

        class FakeInfo:
            def __init__(self):
                self.BasicLimitInformation = self

        class FakeApi:
            k32 = FakeK32()
            ntdll = FakeNtdll()
            extended_limit = FakeInfo
            basic_accounting = FakeInfo

            @staticmethod
            def byref(value):
                return value

            @staticmethod
            def sizeof(value):
                return 0

            @staticmethod
            def last_error():
                return 0

        job = windows_supervisor._Job(FakeApi())
        self.assertIs(job.assign_and_resume(9), True)
        self.assertEqual(calls, [("assign", 41, 9), ("resume", 9)])
        calls.clear()
        self.assertIs(job.assign_and_resume(7), False)
        self.assertEqual(calls, [("assign", 41, 7)], "a process that could not join the job was resumed")
        job.close()
        job.close()
        self.assertEqual(calls[-1:], [("close", 41)])
        self.assertEqual(calls.count(("close", 41)), 1)

    def test_importing_loads_no_ctypes(self):
        completed = subprocess.run([PY, "-c", IMPORT_CHECK], capture_output=True, text=True, timeout=120,
                                   cwd=str(Path(__file__).resolve().parent.parent))
        self.assertEqual((completed.returncode, completed.stdout.strip()), (0, "IMPORT_SAFE_OK"), completed.stderr)


if __name__ == "__main__":
    unittest.main()
