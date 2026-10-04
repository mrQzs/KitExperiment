"""POSIX timeout and cleanup oracle for the legacy SubprocessRunner.

Planner-provided acceptance oracle for runner-posix-cleanup; executors must not
modify this file.  Each case starts real local ``python -c`` process trees
(no provider CLI, no credentials, no network) and checks two measured defects
of the legacy runner on macOS: the timeout was not enforced while a descendant
held an output pipe, and cleanup was reported ``verified`` while descendants
survived.  Every case kills whatever it started, even when an assertion fails.
"""
from __future__ import annotations

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

from sentinelmesh.runtime import Invocation, SubprocessRunner

PY = sys.executable
TIMEOUT = 1
# timeout + SIGTERM grace (1 s) + drain (2 s) + reap (2 s), with room for a busy machine
WALL_BOUND = TIMEOUT + 8.0
IGNORE_TERM_SLEEP = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_gone_within(pgid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


@unittest.skipIf(os.name == "nt", "POSIX process groups only; Windows Job Object cleanup stays NOT_RUN")
class RunnerPosixCleanupOracle(unittest.TestCase):
    def run_tree(self, code: str, *args: str):
        invocation = Invocation((PY, "-c", code, *args), str(Path.cwd()), {}, TIMEOUT)
        started = time.monotonic()
        result = SubprocessRunner().run(invocation)
        wall = time.monotonic() - started
        if result.pid:
            self.addCleanup(self.kill_group, result.pid)
        return result, wall

    def kill_group(self, pgid: int) -> None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def kill_pid(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_timeout_is_enforced_while_a_descendant_holds_the_pipes(self):
        leader = ("import subprocess, sys, signal, time\n"
                  "subprocess.Popen([sys.executable, '-c', %r])\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "time.sleep(30)\n" % IGNORE_TERM_SLEEP)
        result, wall = self.run_tree(leader)
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual(result.stop_reason, "timeout_killed")
        self.assertEqual(result.cleanup, "verified")
        self.assertTrue(group_gone_within(result.pid, 3.0))

    def test_descendant_detached_from_the_pipes_is_killed_with_the_group(self):
        leader = ("import subprocess, sys, signal, time\n"
                  "subprocess.Popen([sys.executable, '-c', %r], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "time.sleep(30)\n" % IGNORE_TERM_SLEEP)
        result, wall = self.run_tree(leader)
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual(result.stop_reason, "timeout_killed")
        self.assertEqual(result.cleanup, "verified")
        self.assertTrue(group_gone_within(result.pid, 3.0))

    def test_escaped_descendant_holding_a_pipe_is_reported_incomplete(self):
        pid_dir = Path(tempfile.mkdtemp(prefix="runner-oracle-"))
        self.addCleanup(shutil.rmtree, pid_dir, True)
        pid_file = pid_dir / "escaped.pid"
        escaped = ("import os, signal, sys, time\n"
                   "os.setsid()\n"
                   "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                   "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                   "time.sleep(30)\n")
        leader = ("import os, subprocess, sys, signal, time\n"
                  "subprocess.Popen([sys.executable, '-c', %r, sys.argv[1]])\n"
                  "while not os.path.exists(sys.argv[1]):\n"
                  "    time.sleep(0.02)\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "time.sleep(30)\n" % escaped)
        result, wall = self.run_tree(leader, str(pid_file))
        if pid_file.exists() and pid_file.read_text().strip():
            self.addCleanup(self.kill_pid, int(pid_file.read_text().strip()))
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual(result.stop_reason, "timeout_killed")
        self.assertEqual(result.cleanup, "incomplete")

    def test_normal_exit_with_a_surviving_group_member_is_incomplete(self):
        leader = ("import subprocess, sys\n"
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],"
                  " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                  "print('leader-done')\n")
        result, wall = self.run_tree(leader)
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((result.stop_reason, result.exit_code), ("completed", 0))
        self.assertIn(b"leader-done", result.stdout)
        self.assertEqual(result.cleanup, "incomplete")

    def test_clean_normal_exit_is_verified(self):
        result, wall = self.run_tree("print('ok')")
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((result.stop_reason, result.exit_code, result.cleanup), ("completed", 0, "verified"))
        self.assertIn(b"ok", result.stdout)

    def test_leader_that_obeys_sigterm_keeps_the_timeout_reason(self):
        result, wall = self.run_tree("import time; time.sleep(30)")
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((result.stop_reason, result.cleanup), ("timeout", "verified"))
        self.assertTrue(group_gone_within(result.pid, 3.0))

    def test_cancellation_uses_the_same_bounded_cleanup(self):
        # Interrupt only once the whole tree ignores SIGTERM, so the case cannot pass by
        # killing the leader before it set up its descendant.
        ready_dir = Path(tempfile.mkdtemp(prefix="runner-oracle-"))
        self.addCleanup(shutil.rmtree, ready_dir, True)
        grandchild = ("import signal, sys, time\n"
                      "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                      "open(sys.argv[1] + '/grandchild', 'w').close()\n"
                      "time.sleep(30)\n")
        leader = ("import os, subprocess, sys, signal, time\n"
                  "subprocess.Popen([sys.executable, '-c', %r, sys.argv[1]])\n"
                  "while not os.path.exists(sys.argv[1] + '/grandchild'):\n"
                  "    time.sleep(0.02)\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "open(sys.argv[1] + '/leader', 'w').close()\n"
                  "time.sleep(30)\n" % grandchild)
        real_communicate = subprocess.Popen.communicate
        calls = []

        def interrupt_first(proc, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                deadline = time.monotonic() + 10
                while not (ready_dir / "leader").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                raise KeyboardInterrupt
            return real_communicate(proc, *args, **kwargs)

        with mock.patch.object(subprocess.Popen, "communicate", interrupt_first):
            result, wall = self.run_tree(leader, str(ready_dir))
        self.assertTrue((ready_dir / "leader").exists(), "process tree was not ready before the interrupt")
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual(result.cleanup, "verified")
        self.assertTrue(group_gone_within(result.pid, 3.0))

if __name__ == "__main__":
    unittest.main()
