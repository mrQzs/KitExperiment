"""Behaviour tests for the bounded POSIX shutdown of the legacy SubprocessRunner.

Complements ``tests/test_runner_posix_cleanup_oracle.py`` with the cases the
oracle does not cover: a clean run that also delivers stdin, a start failure,
the exit code of a SIGKILL escalation, and the output the leader wrote before
the shutdown.  Every case starts real local ``python -c`` process trees (no
provider CLI, no credentials, no network), kills whatever it started even when
an assertion fails, and is skipped on Windows where the Job Object cleanup path
stays NOT_RUN.
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

from sentinelmesh.runtime import Invocation, SubprocessRunner

PY = sys.executable
TIMEOUT = 1
# timeout + SIGTERM grace (1 s) + drain (2 s) + reap (2 s), with room for a busy machine
WALL_BOUND = TIMEOUT + 8.0


@unittest.skipIf(os.name == "nt", "POSIX process groups only; Windows Job Object cleanup stays NOT_RUN")
class RunnerPosixCleanupTests(unittest.TestCase):
    def run_tree(self, code: str, *args: str, input_bytes: bytes | None = None, timeout: float = TIMEOUT):
        invocation = Invocation((PY, "-c", code, *args), str(Path.cwd()), {}, timeout)
        started = time.monotonic()
        result = SubprocessRunner().run(invocation, input_bytes=input_bytes)
        wall = time.monotonic() - started
        if result.pid:
            self.addCleanup(self.kill_group, result.pid)
        return result, wall

    def kill_group(self, pgid: int) -> None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_clean_exit_delivers_input_and_captures_output(self):
        code = "import sys; sys.stdout.buffer.write(b'out:' + sys.stdin.buffer.read())"
        result, wall = self.run_tree(code, input_bytes=b"in")
        self.assertEqual((result.started, result.exit_code), (True, 0))
        self.assertEqual(result.stdout, b"out:in")
        self.assertEqual(result.stderr, b"")
        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.cleanup, "verified")
        self.assertLess(wall, WALL_BOUND)

    def test_missing_executable_is_not_started(self):
        invocation = Invocation(("./kitexperiment-missing-executable",), str(Path.cwd()), {}, TIMEOUT)
        result = SubprocessRunner().run(invocation)
        self.assertFalse(result.started)
        self.assertIsNone(result.exit_code)
        self.assertIsNone(result.pid)
        self.assertEqual(result.stdout, b"")
        self.assertTrue(result.stderr)
        self.assertEqual(result.stop_reason, "start_failed")
        self.assertEqual(result.cleanup, "not_started")

    def test_sigkill_escalation_reports_the_signal_exit_code(self):
        code = ("import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "time.sleep(30)\n")
        result, wall = self.run_tree(code)
        self.assertEqual(result.exit_code, -signal.SIGKILL)
        self.assertGreaterEqual(result.duration_seconds, TIMEOUT)
        self.assertEqual(result.stop_reason, "timeout_killed")
        self.assertEqual(result.cleanup, "verified")
        self.assertLess(wall, WALL_BOUND)

    def test_output_written_before_the_kill_is_kept(self):
        code = ("import sys, signal, time\n"
                "sys.stdout.write('early')\n"
                "sys.stdout.flush()\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "time.sleep(30)\n")
        result, wall = self.run_tree(code)
        self.assertEqual(result.stdout, b"early")
        self.assertEqual(result.stderr, b"")
        self.assertEqual(result.stop_reason, "timeout_killed")
        self.assertEqual(result.cleanup, "verified")
        self.assertLess(wall, WALL_BOUND)

    def test_escaped_descendant_holding_a_pipe_keeps_output_and_is_incomplete(self):
        # A descendant that left the process group keeps an output pipe open, so the
        # parent's pipes cannot be drained inside the bounded window: the bytes read
        # before the kill are kept and cleanup stays incomplete.
        pid_dir = Path(tempfile.mkdtemp(prefix="runner-posix-"))
        self.addCleanup(shutil.rmtree, pid_dir, True)
        pid_file = pid_dir / "escaped.pid"
        escaped = ("import os, signal, sys, time\n"
                   "os.setsid()\n"
                   "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                   "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                   "time.sleep(30)\n")
        leader = ("import os, subprocess, sys, signal, time\n"
                  "sys.stdout.write('leader-out')\n"
                  "sys.stdout.flush()\n"
                  "subprocess.Popen([sys.executable, '-c', %r, sys.argv[1]])\n"
                  "while not os.path.exists(sys.argv[1]):\n"
                  "    time.sleep(0.02)\n"
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                  "time.sleep(30)\n" % escaped)
        result, wall = self.run_tree(leader, str(pid_file))
        if pid_file.exists() and pid_file.read_text().strip():
            self.addCleanup(self.kill_pid, int(pid_file.read_text().strip()))
        self.assertEqual(result.stop_reason, "timeout_killed")
        self.assertEqual(result.cleanup, "incomplete")
        self.assertEqual(result.stdout, b"leader-out")
        self.assertLess(wall, WALL_BOUND)

    def kill_pid(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_reaped_group_means_the_timeout_reason_is_kept(self):
        # A leader that obeys SIGTERM must not be escalated to SIGKILL, so the
        # timeout reason stays "timeout" while the group is confirmed empty.
        result, wall = self.run_tree("import time; time.sleep(30)")
        self.assertEqual(result.stop_reason, "timeout")
        self.assertEqual(result.cleanup, "verified")
        self.assertTrue(self.group_gone_within(result.pid, 3.0))
        self.assertLess(wall, WALL_BOUND)

    def group_gone_within(self, pgid: int, seconds: float) -> bool:
        """Return whether the process group is gone within ``seconds``."""
        deadline = time.monotonic() + seconds
        while True:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
