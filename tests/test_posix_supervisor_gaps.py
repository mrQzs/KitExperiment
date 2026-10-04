"""Planner follow-up cases for the native POSIX supervisor.

The review of posix-supervisor a002 found three rules that no test guarded:

* the reap poll probes the group at least every 0.05 s (v1 rule 7).  Mutant
  S-M26 survived because the executor test compares each sleep with the module
  constant, so raising the constant raised the bound with it;
* a wait interrupted at its deadline still sees a leader that CPython reaped
  while handling the interrupt (v2 rule 3).  The attempt sent a SIGKILL and
  reported ``timeout_killed`` for a leader that had already exited;
* an interrupt never extends a deadline (v2 rule 3).  Mutants S-M31 and S-M32
  survived because the probes only checked that no window ended early.

Each case fails under its defect.  The interrupt cases drive the private
helpers with in-memory stand-ins and a patched ``os.killpg``, so they send no
signal and start no process; an interrupt that escapes fails only its own case.
"""
from __future__ import annotations

import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh.posix_supervisor import _REAP_SECONDS, _reap_group, _stop_group, _wait_for_leader, supervise
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
POLL_BOUND = 0.05
FAKE_PGID = 999_999


class LeaderReapedAtTheDeadline:
    """Popen stand-in for CPython's interrupt handling in ``Popen.wait``.

    The interrupt arrives during the wait; CPython keeps waiting until the
    deadline, reaps the leader there and re-raises the interrupt anyway.
    """

    pid = FAKE_PGID
    returncode = None

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        time.sleep(max(0.0, timeout))
        self.returncode = -signal.SIGTERM
        raise KeyboardInterrupt

    def poll(self):
        return self.returncode


class LeaderThatNeverExits:
    """Popen stand-in whose first waits are interrupted after 0.2 s each."""

    pid = FAKE_PGID
    returncode = None

    def __init__(self, interrupts):
        self.interrupts = interrupts
        self.timeouts = []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        if self.interrupts:
            self.interrupts -= 1
            time.sleep(min(timeout, 0.2))
            raise KeyboardInterrupt
        time.sleep(timeout)
        raise subprocess.TimeoutExpired("leader", timeout)

    def poll(self):
        return None


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class PosixSupervisorGaps(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="supervisor-gaps-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        (self.work / "evidence").mkdir()

    def kill_group(self, pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def without_escaping_interrupts(self, func, *args):
        try:
            return func(*args)
        except KeyboardInterrupt:
            self.fail("an interrupt escaped %s()" % func.__name__)

    def test_reap_poll_probes_at_least_every_fifty_milliseconds(self):
        # The probe never sees an empty group, so the poll runs its whole window;
        # the median keeps a loaded machine's occasional oversleep from failing it.
        real_killpg = os.killpg
        probes = []

        def never_empty(pgid, sig):
            if sig == 0:
                probes.append(time.monotonic())
                return None
            return real_killpg(pgid, sig)

        spec = InvocationSpec("req-1", "run-1", (PY, "-c", "pass"), str(self.work), environment_digest(BASE_ENV), 10)
        with mock.patch.object(os, "killpg", never_empty):
            run = supervise(spec, environment=BASE_ENV, evidence_dir=str(self.work / "evidence"), worktree_id="wt-1")
        self.addCleanup(self.kill_group, run.identity.pid)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertIs(run.observation["cleanup_confirmed"], False)
        # probes[0] is the check before the reap's SIGKILL; the poll starts at probes[1].
        gaps = [later - earlier for earlier, later in zip(probes[1:], probes[2:])]
        self.assertGreaterEqual(len(gaps), 10, "the poll did not keep probing during its window")
        self.assertLessEqual(statistics.median(gaps), POLL_BOUND * 1.5,
                             "the reap poll probes the group less often than every 0.05 s")

    def test_an_interrupt_at_the_deadline_still_sees_a_reaped_leader(self):
        self.assertIs(self.without_escaping_interrupts(_wait_for_leader, LeaderReapedAtTheDeadline(), 0.05), True)
        sent = []
        with mock.patch.object(os, "killpg", lambda pgid, sig: sent.append((pgid, sig))):
            reason = self.without_escaping_interrupts(_stop_group, LeaderReapedAtTheDeadline(), "timeout")
        # The leader exited inside the grace window: no SIGKILL, no timeout_killed.
        self.assertEqual(reason, "timeout")
        self.assertEqual(sent, [(FAKE_PGID, signal.SIGTERM)])

    def test_an_interrupt_never_extends_the_leader_wait(self):
        leader = LeaderThatNeverExits(interrupts=2)
        started = time.monotonic()
        exited = self.without_escaping_interrupts(_wait_for_leader, leader, 0.5)
        elapsed = time.monotonic() - started
        self.assertIs(exited, False)
        # Two interrupted attempts of 0.2 s each, then the rest of the window.
        self.assertEqual(len(leader.timeouts), 3, "the wait did not resume after each interrupt")
        self.assertLessEqual(leader.timeouts[1], 0.5 - 0.2 + 0.01)
        self.assertLessEqual(leader.timeouts[2], 0.5 - 0.4 + 0.01)
        self.assertLess(elapsed, 0.5 + 0.25)

    def test_an_interrupt_never_extends_the_reap_poll(self):
        probes = []
        interrupted = []

        def group_that_never_empties(pgid, sig):
            if sig == 0:
                probes.append(time.monotonic())
                if len(probes) == 25 and not interrupted:
                    interrupted.append(time.monotonic())
                    raise KeyboardInterrupt
            return None

        started = time.monotonic()
        with mock.patch.object(os, "killpg", group_that_never_empties):
            result = self.without_escaping_interrupts(_reap_group, FAKE_PGID)
        elapsed = time.monotonic() - started
        self.assertEqual(result, (True, False))
        self.assertEqual(len(interrupted), 1, "the poll ended before the interrupt was injected")
        after = [moment for moment in probes if moment > interrupted[0]]
        self.assertGreaterEqual(len(after), 5, "the poll did not resume after the interrupt")
        self.assertLess(elapsed, _REAP_SECONDS + 0.5)


if __name__ == "__main__":
    unittest.main()
