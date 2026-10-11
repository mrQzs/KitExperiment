"""Planner follow-up cases for supervised start records.

The review of posix-reconcile-integration a001 reproduced six gaps on the
accepted candidate, all left open by the contract:

* a stale ``observation.json`` already in the evidence directory survived the
  run's failed publish and was read back as its outcome: a run that did start
  was reported NOT_STARTED with ``replay_allowed=True``;
* a FIFO named like a record made ``replay_decision`` wait in ``open`` forever;
* a record that grew after ``fstat`` was read without a bound;
* a handoff too large to read back could still be written, and its run started;
* a close that failed after the write was ignored and the record published;
* a ``worktree_id`` with a C1 control passed the supervisor's check, then failed
  ``ProcessIdentity`` after the child had started, leaving it unsupervised.

Each case fails on the accepted candidate.  Cases that could start a process
use a leader that writes a marker, and kill whatever they started.
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

from sentinelmesh.posix_supervisor import supervise
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.supervised_record import HANDOFF_NAME, OBSERVATION_NAME, record_supervised_run, replay_decision

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
CEILING = 65536
# Writes its pid to argv[1], then runs until killed.
MARKER_LEADER = ("import os, sys, time\n"
                 "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
                 "os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
                 "time.sleep(30)\n")
DECIDE_IN_A_CHILD = ("import json, sys\n"
                     "from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode\n"
                     "from sentinelmesh.supervised_record import replay_decision\n"
                     "a = json.loads(sys.argv[1])\n"
                     "spec = InvocationSpec(a['request_id'], a['run_id'], tuple(a['command']), a['cwd'],\n"
                     "                      a['environment_sha256'], a['timeout'])\n"
                     "try:\n"
                     "    print(json.dumps(replay_decision(spec, a['evidence'])))\n"
                     "except RuntimeErrorCode as exc:\n"
                     "    print(exc.code)\n")


def canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def handoff_of(spec: InvocationSpec) -> dict:
    return {"schema": "sentinelmesh-handoff-v1", "request_id": spec.request_id, "run_id": spec.run_id,
            "environment_sha256": spec.environment_sha256, "spec_sha256": spec.digest()}


@unittest.skipIf(os.name != "posix", "native POSIX supervisor (process groups and signals); the Windows wiring has its own cases in tests/test_win_wiring*.py")
class SupervisedRecordGaps(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="supervised-gaps-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.marker = self.work / "leader.pid"
        self.addCleanup(self.kill_marked_group)
        self.dirs = 0

    def evidence_dir(self) -> Path:
        self.dirs += 1
        path = self.work / ("evidence-%d" % self.dirs)
        path.mkdir()
        return path

    def spec(self, request_id="req-1", command=None):
        argv = command if command is not None else (PY, "-c", MARKER_LEADER, str(self.marker))
        return InvocationSpec(request_id, "run-1", argv, str(self.cwd), environment_digest(BASE_ENV), 10)

    def kill_marked_group(self):
        try:
            pid = int(self.marker.read_text())
        except (OSError, ValueError):
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def code_of(self, func, *args, **kwargs):
        with self.assertRaises(RuntimeErrorCode) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def assert_nothing_started(self):
        time.sleep(0.5)
        self.assertFalse(self.marker.exists(), "a process started")

    def record(self, spec, evidence, **kwargs):
        return record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1",
                                     **kwargs)

    def test_a_stale_observation_blocks_the_start_and_never_allows_a_replay(self):
        spec = self.spec()
        stale = canonical({"schema": "sentinelmesh-observation-v1", "spec_sha256": spec.digest(),
                           "observation": {"run_id": spec.run_id, "environment_sha256": spec.environment_sha256,
                                           "state": "NOT_STARTED"}})
        evidence = self.evidence_dir()
        (evidence / OBSERVATION_NAME).write_bytes(stale)
        self.assertEqual(self.code_of(self.record, spec, evidence), "E_RUNTIME_EVIDENCE")
        self.assert_nothing_started()
        self.assertEqual(sorted(os.listdir(evidence)), [OBSERVATION_NAME])
        self.assertEqual((evidence / OBSERVATION_NAME).read_bytes(), stale)
        # An observation without its handoff is damaged evidence, never a replay.
        self.assertEqual(self.code_of(replay_decision, spec, str(evidence)), "E_RUNTIME_EVIDENCE")
        with self.subTest(case="dangling symlink in place of the observation"):
            linked = self.evidence_dir()
            os.symlink(self.work / "nowhere", linked / OBSERVATION_NAME)
            self.assertEqual(self.code_of(self.record, spec, linked), "E_RUNTIME_EVIDENCE")
            self.assert_nothing_started()
            self.assertEqual(sorted(os.listdir(linked)), [OBSERVATION_NAME])

    def test_a_fifo_record_is_refused_without_blocking(self):
        spec = self.spec()
        arguments = {"request_id": spec.request_id, "run_id": spec.run_id, "command": list(spec.command),
                     "cwd": spec.cwd, "environment_sha256": spec.environment_sha256, "timeout": spec.timeout_seconds}
        handoff_fifo = self.evidence_dir()
        os.mkfifo(handoff_fifo / HANDOFF_NAME)
        observation_fifo = self.evidence_dir()
        (observation_fifo / HANDOFF_NAME).write_bytes(canonical(handoff_of(spec)))
        os.mkfifo(observation_fifo / OBSERVATION_NAME)
        for evidence in (handoff_fifo, observation_fifo):
            with self.subTest(evidence=sorted(os.listdir(evidence))):
                # A child process, so a regression that blocks cannot hang the suite.
                try:
                    done = subprocess.run([PY, "-B", "-c", DECIDE_IN_A_CHILD, json.dumps(dict(arguments, evidence=str(evidence)))],
                                          cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=20)
                except subprocess.TimeoutExpired:
                    self.fail("replay_decision blocked on a FIFO")
                self.assertEqual(done.stdout.strip(), "E_RUNTIME_EVIDENCE", done.stderr[-500:])

    def test_a_record_that_grows_after_fstat_is_not_read_past_the_ceiling(self):
        big = self.spec(request_id="r" * 70000)
        raw = canonical(handoff_of(big))
        self.assertGreater(len(raw), CEILING)
        evidence = self.evidence_dir()
        (evidence / HANDOFF_NAME).write_bytes(raw)
        real_fstat = os.fstat

        def small_at_fstat(descriptor):
            fields = list(real_fstat(descriptor))
            fields[6] = 10  # st_size: the file looked small when it was checked
            return os.stat_result(fields)

        with mock.patch.object(os, "fstat", small_at_fstat):
            self.assertEqual(self.code_of(replay_decision, big, str(evidence)), "E_RUNTIME_EVIDENCE")

    def test_a_handoff_too_large_to_read_back_is_never_written(self):
        big = self.spec(request_id="r" * 70000)
        evidence = self.evidence_dir()
        self.assertEqual(self.code_of(self.record, big, evidence), "E_RUNTIME_EVIDENCE")
        self.assert_nothing_started()
        self.assertEqual(os.listdir(evidence), [])
        self.assertEqual(replay_decision(big, str(evidence))["status"], "NOT_STARTED")

    def test_a_handoff_whose_close_fails_is_not_published(self):
        spec = self.spec()
        evidence = self.evidence_dir()
        real_open, real_close = os.open, os.close
        partials = []

        def opening(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if str(path).endswith(".partial"):
                partials.append(descriptor)
            return descriptor

        def closing(descriptor):
            if descriptor in partials:
                partials.remove(descriptor)
                real_close(descriptor)
                raise OSError(5, "Input/output error")
            return real_close(descriptor)

        with mock.patch.object(os, "open", opening), mock.patch.object(os, "close", closing):
            self.assertEqual(self.code_of(self.record, spec, evidence), "E_RUNTIME_EVIDENCE")
        self.assert_nothing_started()
        self.assertEqual(os.listdir(evidence), [])

    def test_a_worktree_id_the_identity_refuses_is_rejected_before_the_start(self):
        spec = self.spec()
        evidence = self.evidence_dir()
        self.assertEqual(self.code_of(supervise, spec, environment=BASE_ENV, evidence_dir=str(evidence),
                                      worktree_id="wt\x85"), "E_RUNTIME_IDENTITY")
        self.assert_nothing_started()
        self.assertEqual(os.listdir(evidence), [])


if __name__ == "__main__":
    unittest.main()
