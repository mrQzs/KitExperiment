"""Executor tests for win-supervise (`.agent/design/windows-supervisor.md`, section 3.5).

Native Windows Job Object supervision, exercised end to end with real local
``python`` process trees (no provider CLI, no credentials, no network).  The
oracle pins each rule's boundary; these cases cover the whole mechanism - a
normal run's stream facts and handle census, a three-level tree ended by a
timeout, a straggler reaped after a normal exit, a cancellation, an unconfirmed
cleanup, a hook rollback and a refused second run - and every case kills whatever
it started, even when an assertion fails.  The Win32 helpers below are the same
ones the oracle uses.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sentinelmesh.windows_supervisor as windows_supervisor
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode, reconcile
from sentinelmesh.windows_supervisor import TERMINATED_EXIT_CODE, streams_match, supervise

PY = os.path.realpath(sys.executable)
NL = chr(10)
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
TIMEOUT = 1
STILL_ACTIVE = 259

# Process trees built by nesting one level's source into the next through
# ``repr``; each level reports the pid it spawned and then sleeps for a minute.
GRANDCHILD_SLEEP = "import time; time.sleep(60)"
CHILD_THEN_SLEEP = ("import subprocess, sys, time" + NL
                    + "p = subprocess.Popen([sys.executable, '-c', " + repr(GRANDCHILD_SLEEP) + "])" + NL
                    + "sys.stdout.write(str(p.pid) + ' '); sys.stdout.flush()" + NL
                    + "time.sleep(60)" + NL)
# A leader that starts a child (which starts a grandchild), reports the child
# pid, and sleeps: a timeout must end all three at once.
LEADER_TREE_THEN_SLEEP = ("import subprocess, sys, time" + NL
                          + "p = subprocess.Popen([sys.executable, '-c', " + repr(CHILD_THEN_SLEEP) + "])" + NL
                          + "sys.stdout.write(str(p.pid) + ' '); sys.stdout.flush()" + NL
                          + "time.sleep(60)" + NL)
# A leader that leaves a grandchild behind and exits non-zero at once.
LEADER_LEAVE_EXIT2 = ("import subprocess, sys" + NL
                      + "p = subprocess.Popen([sys.executable, '-c', " + repr(GRANDCHILD_SLEEP) + "])" + NL
                      + "sys.stdout.write(str(p.pid) + ' '); sys.stdout.flush()" + NL
                      + "sys.exit(2)" + NL)
# A leader with a grandchild that marks a file once it runs, then sleeps.
LEADER_MARK_TREE = ("import subprocess, sys, time" + NL
                    + "p = subprocess.Popen([sys.executable, '-c', " + repr(GRANDCHILD_SLEEP) + "])" + NL
                    + "sys.stdout.write(str(p.pid) + ' '); sys.stdout.flush()" + NL
                    + "open(sys.argv[1], 'w').close()" + NL
                    + "time.sleep(60)" + NL)
# A leader that marks a file once it runs, then sleeps (no descendant).
MARK_THEN_SLEEP = ("import sys, time" + NL
                   + "open(sys.argv[1], 'w').close()" + NL
                   + "time.sleep(60)" + NL)
# A leader that writes ~100 KB of stdout carrying a CR LF and a 0x1A byte, plus
# ten bytes of stderr, then exits cleanly.
BIG = ("import sys" + NL
       + "out = bytearray(b'Z' * (100 * 1024))" + NL
       + "out[10:12] = b'\\r\\n'" + NL
       + "out[500] = 26" + NL
       + "sys.stdout.buffer.write(bytes(out)); sys.stdout.buffer.flush()" + NL
       + "sys.stderr.buffer.write(b'0123456789'); sys.stderr.buffer.flush()" + NL)


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


def interrupt_when_ready(ready: Path):
    """Return a ``Popen.wait`` stand-in that raises once ``ready`` exists.

    Only the first call interrupts (the leader's own wait); every later call -
    the one `_wait_for_leader` makes while stopping the job - is the real wait,
    so the leader is actually reaped after it is terminated.
    """
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

    return interrupt_first


@unittest.skipIf(os.name != "nt", "native Windows Job Object supervision")
class WinSuperviseCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="win-supervise-exec-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.evidence = self.work / "evidence"
        self.evidence.mkdir()
        self.cwd = self.work / "tree"
        self.cwd.mkdir()

    def spec(self, code, *args, timeout=TIMEOUT):
        argv = (PY, "-c", code, *args)
        return InvocationSpec("req-1", "run-1", argv, str(self.cwd), environment_digest(BASE_ENV), timeout)

    def supervise(self, spec, **kwargs):
        run = supervise(spec, environment=BASE_ENV, evidence_dir=str(self.evidence), worktree_id="wt-1", **kwargs)
        if run.identity is not None:
            self.addCleanup(kill_pid, run.identity.pid)
        return run

    def independent_fact(self, path):
        """The four stream keys recomputed straight from the file on disk."""
        data = Path(path).read_bytes()
        info = os.stat(path)
        return {"device": info.st_dev, "inode": info.st_ino, "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}

    def reported_pids(self, run):
        tokens = Path(run.stdout_path).read_bytes().split()
        pids = [int(token) for token in tokens]
        for pid in pids:
            self.addCleanup(kill_pid, pid)
        return pids

    def test_completed_run_records_large_streams_and_returns_its_handles(self):
        # A warm-up run loads ctypes and whatever the first call lazily opens.
        warm = self.work / "warm"
        warm.mkdir()
        warm_run = supervise(self.spec("pass"), environment=BASE_ENV, evidence_dir=str(warm), worktree_id="wt-1")
        del warm_run
        before = handle_count()

        run = self.supervise(self.spec(BIG))

        self.assertIsInstance(run, windows_supervisor.SupervisedRun)
        self.assertEqual((run.stop_reason, run.exit_code, run.stragglers_killed), ("completed", 0, False))
        self.assertEqual(run.lifecycle.state, "STOPPED")
        self.assertEqual(Path(run.stdout_path), self.evidence / "stdout.log")
        self.assertEqual(Path(run.stderr_path), self.evidence / "stderr.log")
        # Each stream fact equals the four keys recomputed independently, and the
        # 0x1A byte and the CR LF survived the O_BINARY round trip.
        self.assertEqual(run.streams["stdout"], self.independent_fact(run.stdout_path))
        self.assertEqual(run.streams["stderr"], self.independent_fact(run.stderr_path))
        self.assertEqual((run.streams["stdout"]["bytes"], run.streams["stderr"]["bytes"]), (100 * 1024, 10))
        self.assertIs(streams_match(str(self.evidence), run.streams), True)
        self.assertEqual(run.environment_names, ("LANG", "PATH"))
        self.assertTrue(pid_gone_within(run.identity.pid, 3.0))
        del run
        self.assertEqual(handle_count(), before, "a handle leaked")

    def test_timeout_ends_a_three_level_tree_at_once(self):
        run = self.supervise(self.spec(LEADER_TREE_THEN_SLEEP, timeout=2))
        descendants = self.reported_pids(run)
        self.assertEqual(len(descendants), 2, "the leader did not report both descendant pids")

        self.assertEqual((run.stop_reason, run.exit_code), ("timeout_killed", TERMINATED_EXIT_CODE))
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(run.lifecycle.state, "FAILED")
        # The leader, the child and the grandchild are all gone, and the timeout -
        # not the kill or the reap - is what took the time.
        for pid in (run.identity.pid, *descendants):
            self.assertTrue(pid_gone_within(pid, 3.0), f"pid {pid} outlived the job")
        self.assertGreaterEqual(run.duration_seconds, 2)
        self.assertLess(run.duration_seconds, 2 + 1.5)

    def test_a_straggler_after_a_normal_exit_is_reaped(self):
        run = self.supervise(self.spec(LEADER_LEAVE_EXIT2, timeout=5))
        (grandchild,) = self.reported_pids(run)

        self.assertEqual((run.stop_reason, run.exit_code, run.stragglers_killed), ("completed", 2, True))
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(run.lifecycle.state, "FAILED")
        self.assertTrue(pid_gone_within(grandchild, 3.0), "the straggler outlived the run")

    def test_cancellation_ends_the_leader_and_its_grandchild(self):
        ready = self.work / "ready"
        with mock.patch.object(subprocess.Popen, "wait", interrupt_when_ready(ready)):
            run = self.supervise(self.spec(LEADER_MARK_TREE, str(ready), timeout=30))
        (grandchild,) = self.reported_pids(run)

        self.assertTrue(ready.exists(), "the leader never ran before the interrupt")
        self.assertEqual((run.stop_reason, run.exit_code), ("cancelled", TERMINATED_EXIT_CODE))
        self.assertEqual(run.observation["state"], "FAILED")
        self.assertTrue(pid_gone_within(run.identity.pid, 3.0))
        self.assertTrue(pid_gone_within(grandchild, 3.0), "the grandchild outlived the cancellation")

    def test_an_unconfirmed_cleanup_blocks_a_replay(self):
        ready = self.work / "ready"
        spec = self.spec(MARK_THEN_SLEEP, str(ready), timeout=30)
        with mock.patch.object(subprocess.Popen, "wait", interrupt_when_ready(ready)):
            with mock.patch.object(windows_supervisor._Job, "active_processes", return_value=1):
                run = self.supervise(spec)

        self.assertEqual(run.stop_reason, "cancelled")
        self.assertIs(run.observation["cleanup_confirmed"], False)
        self.assertEqual(run.lifecycle.state, "START_UNKNOWN")
        self.assertIs(reconcile(spec, run.observation)["replay_allowed"], False)
        # The whole reap window was spent before giving up on the cleanup.
        self.assertGreaterEqual(run.duration_seconds, windows_supervisor._REAP_SECONDS)

    def test_a_hook_that_writes_then_raises_keeps_only_its_own_file(self):
        note = self.evidence / "hook-note.txt"
        sentinel = KeyboardInterrupt("hook refused")

        def hook():
            note.write_bytes(b"the hook ran")
            raise sentinel

        before = handle_count()
        with self.assertRaises(KeyboardInterrupt) as ctx:
            supervise(self.spec("pass"), environment=BASE_ENV, evidence_dir=str(self.evidence),
                      worktree_id="wt-1", before_start=hook)
        # The very exception the hook raised comes back out, the two evidence
        # files are gone (so the pins and write descriptors were closed before the
        # removal), the hook's own file is left exactly as it wrote it, and no
        # handle was leaked.
        self.assertIs(ctx.exception, sentinel)
        self.assertEqual(sorted(entry.name for entry in self.evidence.iterdir()), ["hook-note.txt"])
        self.assertEqual(note.read_bytes(), b"the hook ran")
        self.assertEqual(handle_count(), before, "a handle leaked")

    def test_a_second_run_in_the_same_directory_is_refused(self):
        first = self.supervise(self.spec("import sys; sys.stdout.write('first')"))
        stdout_bytes = (self.evidence / "stdout.log").read_bytes()
        stderr_bytes = (self.evidence / "stderr.log").read_bytes()
        listing = sorted(os.listdir(self.evidence))

        second = self.spec("import sys; sys.stdout.write('second')")
        with self.assertRaises(RuntimeErrorCode) as ctx:
            supervise(second, environment=BASE_ENV, evidence_dir=str(self.evidence), worktree_id="wt-1")

        self.assertEqual(ctx.exception.code, "E_RUNTIME_EVIDENCE")
        # The first run's evidence is untouched and nothing new was created.
        self.assertEqual((self.evidence / "stdout.log").read_bytes(), stdout_bytes)
        self.assertEqual((self.evidence / "stderr.log").read_bytes(), stderr_bytes)
        self.assertEqual(sorted(os.listdir(self.evidence)), listing)


if __name__ == "__main__":
    unittest.main()
