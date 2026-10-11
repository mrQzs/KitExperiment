"""Executor tests for win-wiring (`.agent/design/windows-supervisor.md`, sections 3.1 and 7 item 4).

The wiring itself - which supervisor a platform gets, how a file identity is
written and read back - is pinned by the oracle case by case; these cases cover
the two layers above it end to end on Windows, with real local ``python``
process trees (no provider CLI, no credentials, no network): a handoff that is
already on disk, a run that exits non-zero, an adapter that refuses what the run
wrote, a cleanup nobody could confirm, a credential-shaped environment name, and
a timeout that ends the run and still settles the usage it wrote.  Every case
kills whatever it started, even when an assertion fails, and every refusal is
preceded by the legal baseline it departs from by one change.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sentinelmesh.windows_supervisor as windows_supervisor
from sentinelmesh.adapter_protocol import AdapterError, Invocation, OfflineAdapter
from sentinelmesh.adapter_run import run_adapter_operation
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.scheduler import Scheduler
from sentinelmesh.supervised_operation import run_supervised_operation
from sentinelmesh.supervised_record import (
    HANDOFF_NAME,
    OBSERVATION_NAME,
    record_supervised_run,
)
from sentinelmesh.windows_supervisor import TERMINATED_EXIT_CODE

PY = os.path.realpath(sys.executable)
NL = chr(10)
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
CAPS = {"dispatches": 5, "tokens": 100, "cost": 50}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
DIMENSIONS = ("tokens", "cost")
CANDIDATE = "c" * 64
TIMEOUT = 1
# timeout + kill wait (2 s) + reap (2 s), with room for a busy machine
WALL_BOUND = TIMEOUT + 7.0
STILL_ACTIVE = 259
# Bytes text mode would mangle (CR LF, 0x1A) on stdout, and one line on stderr.
OUT_BYTES = bytes([111, 117, 116, 13, 10, 26, 120, 10])
ERR_BYTES = bytes([101, 114, 114, 10])
WRITE_BOTH = ("import os" + NL
              + "os.write(1, bytes(%r))" % list(OUT_BYTES) + NL
              + "os.write(2, bytes(%r))" % list(ERR_BYTES) + NL)
# Writes both streams and then fails: a run that demonstrably happened and did
# not succeed, which is what goes to review rather than to a replay.
WRITE_THEN_EXIT3 = WRITE_BOTH + "import sys" + NL + "sys.exit(3)" + NL


def ev(seq, typ, payload=None):
    return {"seq": seq, "event_id": "e%d" % seq, "type": typ, "source": "offline",
            "payload": {} if payload is None else payload}


COMPLETE = [ev(0, "run_started"), ev(1, "assistant"), ev(2, "usage", {"tokens": 12, "cost": 3}),
            ev(3, "run_finished")]


def emit(events, after=""):
    """Python code that writes each event as one LF-terminated JSON line, byte for byte."""
    return ("import json, sys" + NL
            + "for event in json.loads(%r):" % json.dumps(events) + NL
            + "    sys.stdout.buffer.write((json.dumps(event) + chr(10)).encode())" + NL
            + "sys.stdout.buffer.flush()" + NL) + after


def stream_bytes(events) -> bytes:
    return b"".join(json.dumps(event).encode() + bytes([10]) for event in events)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RefusingAdapter(OfflineAdapter):
    """Refuses the stream with a code of its own, the way a provider adapter would."""

    def run(self, invocation, events):
        raise AdapterError("E_ADAPTER_SOURCE", "event source does not match adapter", seq=1)


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


@unittest.skipIf(os.name != "nt", "native Windows wiring: real Job Object process trees")
class WindowsWiringCases(unittest.TestCase):
    """The record, the bridge and the adapter path, end to end on Windows."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="win-wiring-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.count = 0
        self.candidates = []
        self.scheduler = Scheduler()
        self.scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        for task in ("task-1", "task-2", "task-3"):
            self.scheduler.seed_task("req-1", task, CAPS, max_attempts=5)

    # helpers ---------------------------------------------------------------

    def evidence_dir(self) -> Path:
        self.count += 1
        path = self.work / ("evidence-%d" % self.count)
        path.mkdir()
        return path

    def spec(self, code, *args, run_id="run-1", timeout=10, environment=None):
        env = BASE_ENV if environment is None else environment
        argv = (PY, "-c", code, *args)
        return InvocationSpec("req-1", run_id, argv, str(self.cwd), environment_digest(env), timeout)

    def operation(self, task="task-1") -> str:
        claim = self.scheduler.claim("req-1", task, 1, "idem-%d" % self.count, "owner-1", 1, 1,
                                     RESERVATION, ["src/%s.py" % task])
        return claim["operation_id"]

    def candidate_hash(self, evidence_dir):
        self.candidates.append(evidence_dir)
        return CANDIDATE

    def events(self, op, event_type):
        return [event for event in self.scheduler.events if event["operation_id"] == op
                and event["type"] == event_type]

    def state_of(self, op):
        return self.scheduler.operation(op)["state"]

    def track(self, op):
        """Kill whatever this operation started, even when a case fails."""
        for event in self.events(op, "run.started"):
            self.addCleanup(kill_pid, event["payload"]["process_identity"]["pid"])

    def run_op(self, op, spec, evidence, **kwargs):
        outcome = run_supervised_operation(
            self.scheduler, operation_id=op, owner="owner-1", generation=1, spec=spec,
            environment=kwargs.pop("environment", BASE_ENV), evidence_dir=str(evidence),
            worktree_id="wt-1", candidate_hash=self.candidate_hash, **kwargs)
        self.track(op)
        return outcome

    def invocation(self, op, code, *args):
        return Invocation("req-1", op, "executor", "stub-profile", (PY, "-c", code, *args), (),
                          environment_digest(BASE_ENV), None)

    def run_adapter(self, invocation, evidence, adapter=None, **kwargs):
        kwargs.setdefault("usage_dimensions", DIMENSIONS)
        kwargs.setdefault("timeout_seconds", 10)
        outcome = run_adapter_operation(self.scheduler, adapter or OfflineAdapter(), invocation,
                                        cwd=str(self.cwd), environment=BASE_ENV, owner="owner-1",
                                        generation=1, evidence_dir=str(evidence), worktree_id="wt-1",
                                        candidate_hash=self.candidate_hash, **kwargs)
        self.track(invocation.run_id)
        return outcome

    # the record ------------------------------------------------------------

    def test_a_handoff_that_is_already_there_starts_no_process(self):
        spec = self.spec(WRITE_BOTH)
        # The legal baseline first: the same invocation from an empty directory
        # records both files and runs to its end.
        clean = self.evidence_dir()
        run = record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(clean), worktree_id="wt-1")
        self.addCleanup(kill_pid, run.identity.pid)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertEqual(sorted(os.listdir(clean)),
                         [HANDOFF_NAME, OBSERVATION_NAME, "stderr.log", "stdout.log"])
        # One thing changed: a handoff is already in the evidence directory.
        evidence = self.evidence_dir()
        planted = bytes([104, 97, 110, 100, 111, 102, 102, 10])
        (evidence / HANDOFF_NAME).write_bytes(planted)
        with self.assertRaises(RuntimeErrorCode) as raised:
            record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
        self.assertEqual(raised.exception.code, "E_RUNTIME_EVIDENCE")
        # No process was started, so there is no run to report and no evidence of
        # one: the two stream files the supervisor rolled back are gone, and the
        # planted handoff is exactly what it was.
        self.assertIsNone(getattr(raised.exception, "run", None))
        self.assertEqual(sorted(os.listdir(evidence)), [HANDOFF_NAME])
        self.assertEqual((evidence / HANDOFF_NAME).read_bytes(), planted)

    # the bridge ------------------------------------------------------------

    def test_a_run_that_exits_non_zero_is_reviewed_from_its_records(self):
        op = self.operation()
        evidence = self.evidence_dir()
        outcome = self.run_op(op, self.spec(WRITE_THEN_EXIT3, run_id=op), evidence)
        self.assertEqual((outcome["state"], self.state_of(op)), ("REVIEW_PENDING", "REVIEW_PENDING"))
        self.assertEqual((outcome["stop_reason"], outcome["exit_code"], outcome["error"]),
                         ("completed", 3, None))
        self.assertEqual((outcome["decision"]["status"], outcome["decision"]["replay_allowed"]),
                         ("FAILED", True))
        self.assertEqual(len(self.events(op, "run.finished_for_review")), 1)
        self.assertEqual(self.events(op, "run.finished_for_review")[0]["payload"], {"candidate": CANDIDATE})
        self.assertIn(str(evidence), self.candidates)
        self.assertNotIn(("repo-1", "src/task-1.py"), self.scheduler._resource_holds)
        # The identity the ledger records for the start is the one the record
        # carries, so a review is decided from the run that did happen.
        started = self.events(op, "run.started")
        self.assertEqual(len(started), 1)
        observation = json.loads((evidence / OBSERVATION_NAME).read_bytes())["observation"]
        self.assertEqual(started[0]["payload"]["process_identity"], observation["process_identity"])
        self.assertEqual(((evidence / "stdout.log").read_bytes(), (evidence / "stderr.log").read_bytes()),
                         (OUT_BYTES, ERR_BYTES))

    def test_a_credential_name_outside_the_allowlist_starts_nothing(self):
        environment = dict(BASE_ENV, SERVICE_API_TOKEN="s3cret-value")
        op = self.operation()
        evidence = self.evidence_dir()
        outcome = self.run_op(op, self.spec(WRITE_BOTH, run_id=op, environment=environment), evidence,
                              environment=environment)
        self.assertEqual((outcome["error"], outcome["state"], self.state_of(op)),
                         ("E_RUNTIME_ENVIRONMENT", "NOT_STARTED", "NOT_STARTED"))
        self.assertEqual((outcome["stop_reason"], outcome["exit_code"]), (None, None))
        self.assertEqual(os.listdir(evidence), [])
        self.assertEqual(self.events(op, "run.started"), [])
        self.assertNotIn(("repo-1", "src/task-1.py"), self.scheduler._resource_holds)
        # The same invocation with that one name allowlisted runs and reviews.
        allowed = self.operation("task-2")
        evidence = self.evidence_dir()
        outcome = self.run_op(allowed, self.spec(WRITE_BOTH, run_id=allowed, environment=environment),
                              evidence, environment=environment,
                              allowed_secret_names=frozenset({"SERVICE_API_TOKEN"}))
        self.assertEqual((outcome["state"], outcome["error"], outcome["exit_code"]),
                         ("REVIEW_PENDING", None, 0))

    # the adapter path ------------------------------------------------------

    def test_an_adapter_that_refuses_the_stream_records_no_usage(self):
        # The legal baseline first: the same stream reduced by the offline
        # adapter settles the usage against the supervisor's own digest.
        accepted = self.operation()
        baseline = self.run_adapter(self.invocation(accepted, emit(COMPLETE)), self.evidence_dir())
        self.assertEqual(baseline["state"], "REVIEW_PENDING")
        self.assertEqual((baseline["result_error"], baseline["usage_recorded"], baseline["usage_skipped"]),
                         (None, {"tokens": "APPLIED", "cost": "APPLIED"}, []))
        # One thing changed: the adapter refuses what the run wrote.
        op = self.operation("task-2")
        evidence = self.evidence_dir()
        outcome = self.run_adapter(self.invocation(op, emit(COMPLETE)), evidence, adapter=RefusingAdapter())
        self.assertEqual((outcome["state"], outcome["stream"]["status"]), ("REVIEW_PENDING", "ok"))
        self.assertIsNone(outcome["result"])
        self.assertEqual(outcome["result_error"], "E_ADAPTER_SOURCE")
        self.assertEqual((outcome["usage_recorded"], outcome["usage_skipped"]), ({}, ["tokens", "cost"]))
        self.assertEqual([event["payload"].get("dimension") for event in self.scheduler.events
                          if event["operation_id"] == op and event["type"] == "usage.observed"], [])
        self.assertEqual((evidence / "stdout.log").read_bytes(), stream_bytes(COMPLETE))

    def test_an_unconfirmed_cleanup_settles_no_usage_and_keeps_its_holds(self):
        # The legal baseline first: the same run with a cleanup the job confirms
        # is reviewed and its usage settled.
        accepted = self.operation()
        evidence = self.evidence_dir()
        baseline = self.run_adapter(self.invocation(accepted, emit(COMPLETE)), evidence)
        self.assertEqual(baseline["state"], "REVIEW_PENDING")
        self.assertEqual(baseline["usage_recorded"], {"tokens": "APPLIED", "cost": "APPLIED"})
        self.assertNotIn(("repo-1", "src/task-1.py"), self.scheduler._resource_holds)
        # One thing changed: the job still reports an active process, so nothing
        # about the cleanup is confirmed.  The baseline's own candidate hash is
        # forgotten, so what is left is what this run asked for.
        self.candidates = []
        op = self.operation("task-2")
        evidence = self.evidence_dir()
        with mock.patch.object(windows_supervisor._Job, "active_processes", lambda self_: 1):
            outcome = self.run_adapter(self.invocation(op, emit(COMPLETE)), evidence)
        self.assertEqual((outcome["state"], self.state_of(op)), ("START_UNKNOWN", "START_UNKNOWN"))
        self.assertEqual([event["payload"]["reason"] for event in self.events(op, "scheduler.start_unknown")],
                         ["CLEANUP_UNCONFIRMED"])
        self.assertEqual(self.events(op, "run.finished_for_review"), [])
        # The run wrote a complete, digestible stream and nothing was recorded
        # from it: the usage would be settled against a digest nobody can
        # account for.
        self.assertEqual((outcome["stream"]["status"], outcome["stream"]["sha256"]),
                         ("ok", sha256_of(stream_bytes(COMPLETE))))
        self.assertEqual((outcome["usage_recorded"], outcome["usage_skipped"]), ({}, ["tokens", "cost"]))
        self.assertEqual(self.candidates, [])
        self.assertIn(("repo-1", "src/task-2.py"), self.scheduler._resource_holds)
        self.assertTrue(self.scheduler._requests["req-1"].stopped)

    def test_a_timeout_ends_the_run_and_settles_the_usage_it_wrote(self):
        op = self.operation()
        evidence = self.evidence_dir()
        # The stream is complete before the sleep, so the usage of a run the
        # timeout ended is still settled against what the run wrote.
        started = time.monotonic()
        outcome = self.run_adapter(self.invocation(op, emit(COMPLETE) + "import time" + NL
                                                   + "time.sleep(30)" + NL), evidence,
                                   timeout_seconds=TIMEOUT)
        wall = time.monotonic() - started
        self.assertEqual((outcome["stop_reason"], outcome["exit_code"]),
                         ("timeout_killed", TERMINATED_EXIT_CODE))
        self.assertEqual((outcome["state"], outcome["error"]), ("REVIEW_PENDING", None))
        self.assertEqual(outcome["usage_recorded"], {"tokens": "APPLIED", "cost": "APPLIED"})
        self.assertEqual(outcome["usage_skipped"], [])
        self.assertEqual((evidence / "stdout.log").read_bytes(), stream_bytes(COMPLETE))
        pid = self.events(op, "run.started")[0]["payload"]["process_identity"]["pid"]
        self.assertTrue(pid_gone_within(pid, 3.0), "the leader outlived the job")
        self.assertLess(wall, WALL_BOUND)


if __name__ == "__main__":
    unittest.main()
