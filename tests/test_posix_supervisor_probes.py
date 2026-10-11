"""Planner probes for posix-supervisor v2 (review of attempt-001).

Planner-provided; executors must not modify this file.  The review found that
the observation lost the leader's outcome when cleanup was unconfirmed, that a
``PermissionError`` from a group kill escaped ``supervise()`` (macOS answers
EPERM for a group whose only members are unreaped zombies, measured
2026-10-04), and that a second interrupt during the stop or reap phase escaped
as well.  The interrupt cases send a real SIGINT to this process only after a
handshake shows the supervisor is inside the window under test, through a
handler that is disarmed as soon as ``supervise()`` returns.  Each case kills
whatever it started, even when an assertion fails.
"""
from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh.posix_supervisor import supervise
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, Lifecycle, RuntimeErrorCode, reconcile

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
IGNORE_TERM_SLEEP = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
# A parent that leaves the supervised group after its child has exited but before
# reaping it: the group is left holding only that zombie, so killpg answers EPERM.
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
                       "open(sys.argv[2], 'w').write(str(os.getpid()))\n"
                       "subprocess.Popen([sys.executable, '-c', %r, sys.argv[1]])\n"
                       "while not os.path.exists(sys.argv[1]):\n"
                       "    time.sleep(0.02)\n" % ESCAPED_PARENT)
# Records SIGTERM in a file and keeps running, so the grace wait lasts its full second.
TERM_RECORDING_LEADER = ("import os, signal, sys, time\n"
                         "signal.signal(signal.SIGTERM, lambda *_: open(sys.argv[2], 'w').close())\n"
                         "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                         "while True:\n"
                         "    time.sleep(0.05)\n")


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


class InterruptSender(threading.Thread):
    """Send SIGINT to this process once per gate, in order, while armed."""

    def __init__(self, gates):
        super().__init__(daemon=True)
        self.gates = list(gates)
        self.done = threading.Event()
        self.sent_at = []

    def run(self):
        deadline = time.monotonic() + 20.0
        for gate in self.gates:
            while not gate():
                if self.done.is_set() or time.monotonic() >= deadline:
                    return
                time.sleep(0.01)
            if self.done.is_set():
                return
            self.sent_at.append(time.monotonic())
            os.kill(os.getpid(), signal.SIGINT)


@unittest.skipIf(os.name != "posix", "native POSIX supervisor (process groups and signals); the Windows wiring has its own cases in tests/test_win_wiring*.py")
class PosixSupervisorProbes(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="supervisor-probes-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.runs = 0

    def spec(self, code, *args, timeout=10):
        return InvocationSpec("req-1", "run-1", (PY, "-c", code, *args), str(self.cwd),
                              environment_digest(BASE_ENV), timeout)

    def evidence_dir(self) -> Path:
        self.runs += 1
        path = self.work / ("evidence-%d" % self.runs)
        path.mkdir()
        return path

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

    def supervise_once(self, spec, sender=None):
        """Run supervise(); return (run, escaped exception or None, wall, end time)."""
        armed = [True]

        def on_sigint(signum, frame):
            if armed[0]:
                raise KeyboardInterrupt

        previous = signal.signal(signal.SIGINT, on_sigint)
        self.addCleanup(signal.signal, signal.SIGINT, previous)
        if sender is not None:
            self.addCleanup(sender.join, 5.0)
            self.addCleanup(sender.done.set)
            sender.start()
        run = escaped = None
        started = time.monotonic()
        try:
            try:
                run = supervise(spec, environment=BASE_ENV, evidence_dir=str(self.evidence_dir()),
                                worktree_id="wt-1")
            finally:
                armed[0] = False
                ended = time.monotonic()
                if sender is not None:
                    sender.done.set()
        except (KeyboardInterrupt, OSError) as exc:
            escaped = exc
        if run is not None and run.identity is not None:
            self.addCleanup(self.kill_group, run.identity.pid)
        return run, escaped, ended - started, ended

    def assert_returned(self, run, escaped):
        if escaped is not None:
            self.fail("supervise() raised %s instead of returning a result" % type(escaped).__name__)
        self.assertIsNotNone(run)

    def assert_unconfirmed(self, spec, run, state):
        # Rule 8: the observation keeps the leader's outcome; only the lifecycle
        # records that the start state is unknown while cleanup is unconfirmed.
        self.assertIs(run.observation["cleanup_confirmed"], False)
        self.assertEqual(run.observation["state"], state)
        self.assertEqual(run.lifecycle, Lifecycle("START_UNKNOWN", 4, run.identity))
        record = reconcile(spec, run.observation)
        self.assertEqual((record["status"], record["replay_allowed"]), (state, False))

    def test_unconfirmed_cleanup_keeps_the_leader_outcome(self):
        # A signal-0 probe that keeps answering EPERM never confirms the cleanup.
        real_killpg = os.killpg

        def probe_denied(pgid, sig):
            if sig == 0:
                raise PermissionError("operation not permitted")
            return real_killpg(pgid, sig)

        for code, exit_code, state in (("pass", 0, "STOPPED"), ("import sys; sys.exit(3)", 3, "FAILED")):
            with self.subTest(state=state):
                spec = self.spec(code)
                with mock.patch.object(os, "killpg", probe_denied):
                    run, escaped, wall, _ = self.supervise_once(spec)
                self.assert_returned(run, escaped)
                self.assertEqual((run.stop_reason, run.exit_code, run.stragglers_killed),
                                 ("completed", exit_code, True))
                self.assert_unconfirmed(spec, run, state)
                self.assertLess(wall, 2.0 + 5.0)

    def test_timeout_kill_signals_the_group_before_any_probe(self):
        real_killpg = os.killpg
        calls = []

        def recording(pgid, sig):
            calls.append((pgid, sig))
            return real_killpg(pgid, sig)

        with mock.patch.object(os, "killpg", recording):
            run, escaped, _, _ = self.supervise_once(self.spec(IGNORE_TERM_SLEEP, timeout=1))
        self.assert_returned(run, escaped)
        pgid = run.identity.pid
        self.assertEqual((run.stop_reason, run.exit_code), ("timeout_killed", -signal.SIGKILL))
        self.assertIs(run.stragglers_killed, False)
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(calls, [(pgid, signal.SIGTERM), (pgid, signal.SIGKILL), (pgid, 0)])

    def test_permission_error_from_the_stop_phase_kill_does_not_escape(self):
        # The SIGKILL is delivered, then reported as EPERM.
        real_killpg = os.killpg
        targets = []

        def kill_reported_denied(pgid, sig):
            if pgid not in targets:
                targets.append(pgid)
                self.addCleanup(self.kill_group, pgid)
            result = real_killpg(pgid, sig)
            if sig == signal.SIGKILL:
                raise PermissionError("operation not permitted")
            return result

        with mock.patch.object(os, "killpg", kill_reported_denied):
            run, escaped, _, _ = self.supervise_once(self.spec(IGNORE_TERM_SLEEP, timeout=1))
        self.assert_returned(run, escaped)
        self.assertEqual((run.stop_reason, run.exit_code), ("timeout_killed", -signal.SIGKILL))
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(run.observation["state"], "FAILED")
        self.assertEqual(run.lifecycle, Lifecycle("FAILED", 4, run.identity))

    def zombie_group_spec(self):
        escaped_pid = self.work / "escaped.pid"
        leader_pid = self.work / "leader.pid"
        self.addCleanup(self.kill_pid_in, escaped_pid)
        return self.spec(ZOMBIE_GROUP_LEADER, str(escaped_pid), str(leader_pid)), escaped_pid, leader_pid

    def assert_zombie_group_left(self, run, escaped_pid):
        # The scenario held: only the zombie is left, and it goes once its parent dies.
        with self.assertRaises(PermissionError):
            os.killpg(run.identity.pid, 0)
        self.kill_pid_in(escaped_pid)
        self.assertTrue(group_gone_within(run.identity.pid, 3.0))

    def test_zombie_only_group_is_unconfirmed_without_raising(self):
        spec, escaped_pid, _ = self.zombie_group_spec()
        run, escaped, wall, _ = self.supervise_once(spec)
        self.assertIsNotNone(pid_in(escaped_pid), "the escaped parent never reported its pid")
        self.assert_returned(run, escaped)
        self.assertEqual((run.stop_reason, run.exit_code, run.stragglers_killed), ("completed", 0, True))
        self.assert_unconfirmed(spec, run, "STOPPED")
        self.assertLess(wall, 10.0)
        self.assert_zombie_group_left(run, escaped_pid)

    def test_second_interrupt_during_the_grace_wait_is_absorbed(self):
        ready = self.work / "ready.pid"
        term = self.work / "term"
        self.addCleanup(lambda: pid_in(ready) and self.kill_group(pid_in(ready)))
        sender = InterruptSender([ready.exists, term.exists])
        spec = self.spec(TERM_RECORDING_LEADER, str(ready), str(term), timeout=30)
        run, escaped, wall, ended = self.supervise_once(spec, sender)
        self.assertEqual(len(sender.sent_at), 2, "both interrupts must have been sent")
        self.assert_returned(run, escaped)
        self.assertEqual((run.stop_reason, run.exit_code), ("cancelled", -signal.SIGKILL))
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(run.observation["state"], "FAILED")
        self.assertEqual(run.lifecycle, Lifecycle("FAILED", 4, run.identity))
        # The grace wait kept its deadline (1 s after SIGTERM) despite the interrupt.
        self.assertGreater(ended - sender.sent_at[1], 0.5)
        self.assertLess(wall, 15.0)

    def test_interrupt_during_the_reap_poll_is_absorbed(self):
        spec, escaped_pid, leader_pid = self.zombie_group_spec()
        reaped_at = []

        def leader_reaped_a_while_ago():
            # kill(pid, 0) succeeds on an unreaped zombie and fails once the
            # supervisor has reaped the leader, i.e. once the reap phase began.
            if not reaped_at:
                pid = pid_in(leader_pid)
                if pid is None or pid_in(escaped_pid) is None:
                    return False
                try:
                    os.kill(pid, 0)
                    return False
                except ProcessLookupError:
                    reaped_at.append(time.monotonic())
            return time.monotonic() - reaped_at[0] >= 0.3

        sender = InterruptSender([leader_reaped_a_while_ago])
        run, escaped, wall, ended = self.supervise_once(spec, sender)
        self.assert_returned(run, escaped)
        self.assertEqual(len(sender.sent_at), 1, "the interrupt must have been sent during the reap poll")
        self.assertEqual((run.stop_reason, run.exit_code, run.stragglers_killed), ("completed", 0, True))
        self.assert_unconfirmed(spec, run, "STOPPED")
        # The poll kept its deadline (2 s after it began) despite the interrupt.
        self.assertGreater(ended - sender.sent_at[0], 1.0)
        self.assertLess(wall, 10.0)
        self.assert_zombie_group_left(run, escaped_pid)

    def test_existing_relative_cwd_is_rejected(self):
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.work)
        self.assertTrue(os.path.isdir("tree"))
        spec = InvocationSpec("req-1", "run-1", (PY, "-c", "pass"), "tree", environment_digest(BASE_ENV), 10)
        evidence = self.evidence_dir()
        with self.assertRaises(RuntimeErrorCode) as ctx:
            supervise(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
        self.assertEqual(ctx.exception.code, "E_RUNTIME_INVOCATION")
        self.assertEqual(list(evidence.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
