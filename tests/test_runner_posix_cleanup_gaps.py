"""Planner follow-up cases for SubprocessRunner POSIX cleanup.

The review of runner-posix-cleanup a001 found four rules that neither the
oracle nor the executor tests guarded (mutants M02, M07, M11 and M13 survived):
SIGTERM must reach the whole group, a PermissionError must not count as a gone
group, the leader must be reaped when a pipe stays open, and a graceful timeout
must not duplicate output.  Each case below fails under its mutant.  Cases start
only local ``python -c`` processes and kill what they started.
"""
from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh.runtime import Invocation, SubprocessRunner

PY = sys.executable
TIMEOUT = 1
WALL_BOUND = TIMEOUT + 8.0


@unittest.skipIf(os.name == "nt", "POSIX process groups only; Windows Job Object cleanup stays NOT_RUN")
class RunnerPosixCleanupGaps(unittest.TestCase):
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

    def test_sigterm_reaches_every_group_member(self):
        # Both processes obey SIGTERM and the descendant holds the pipes: only a
        # group-wide SIGTERM ends the tree inside the grace window (M02).
        leader = ("import subprocess, sys, time\n"
                  "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                  "time.sleep(30)\n")
        result, wall = self.run_tree(leader)
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((result.stop_reason, result.cleanup), ("timeout", "verified"))

    def test_permission_error_on_the_group_probe_is_not_a_clean_group(self):
        # A group that cannot be signalled may still exist (M07).
        real_killpg = os.killpg

        def probe_denied(pgid, sig):
            if sig == 0:
                raise PermissionError("operation not permitted")
            return real_killpg(pgid, sig)

        with mock.patch.object(os, "killpg", probe_denied):
            result, wall = self.run_tree("print('ok')")
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((result.stop_reason, result.exit_code), ("completed", 0))
        self.assertEqual(result.cleanup, "incomplete")

    def test_leader_is_reaped_when_an_escaped_process_keeps_a_pipe(self):
        # The killed leader still gets a real exit status (M11).
        pid_dir = Path(tempfile.mkdtemp(prefix="runner-gaps-"))
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
        self.assertEqual((result.stop_reason, result.cleanup), ("timeout_killed", "incomplete"))
        self.assertEqual(result.exit_code, -signal.SIGKILL)

    def test_zombie_leader_with_an_escaped_pipe_holder_does_not_raise(self):
        # The leader obeys SIGTERM and becomes an unreaped zombie while an escaped
        # descendant keeps the pipe open; macOS then answers the group SIGKILL with
        # EPERM, which must not escape run() (found reviewing posix-supervisor a001).
        pid_dir = Path(tempfile.mkdtemp(prefix="runner-gaps-"))
        self.addCleanup(shutil.rmtree, pid_dir, True)
        pid_file = pid_dir / "escaped.pid"
        escaped = ("import os, signal, sys, time\n"
                   "os.setsid()\n"
                   "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                   "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                   "time.sleep(30)\n")
        leader = ("import os, subprocess, sys, time\n"
                  "subprocess.Popen([sys.executable, '-c', %r, sys.argv[1]])\n"
                  "while not os.path.exists(sys.argv[1]):\n"
                  "    time.sleep(0.02)\n"
                  "time.sleep(30)\n" % escaped)
        result, wall = self.run_tree(leader, str(pid_file))
        if pid_file.exists() and pid_file.read_text().strip():
            self.addCleanup(self.kill_pid, int(pid_file.read_text().strip()))
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((result.stop_reason, result.cleanup), ("timeout_killed", "incomplete"))
        self.assertEqual(result.exit_code, -signal.SIGTERM)

    def test_graceful_timeout_keeps_every_byte_exactly_once(self):
        # Output written before SIGTERM is returned once, not duplicated (M13).
        leader = ("import sys, time\n"
                  "sys.stdout.write('early\\n'); sys.stdout.flush()\n"
                  "sys.stderr.write('warn\\n'); sys.stderr.flush()\n"
                  "time.sleep(30)\n")
        result, wall = self.run_tree(leader)
        self.assertLess(wall, WALL_BOUND)
        self.assertEqual((result.stop_reason, result.cleanup), ("timeout", "verified"))
        self.assertEqual((result.stdout, result.stderr), (b"early\n", b"warn\n"))


if __name__ == "__main__":
    unittest.main()
