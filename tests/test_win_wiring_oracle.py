"""Planner oracle for win-wiring (`.agent/design/windows-supervisor.md`, sections 3.1, 6 W11-W12 and 7 item 4).

The last of the four Windows supervisor contracts: `sentinelmesh.supervised_record`
chooses the supervisor by platform on every call (``os.name == "nt"`` is the Job
Object, ``"posix"`` the process group, anything else ``E_RUNTIME_PLATFORM``),
`sentinelmesh.supervised_operation` asks the same module's ``streams_match``, and
the two layers above (`run_supervised_operation`, `run_adapter_operation`,
recovery) run end to end on Windows with real local ``python`` process trees.

A file identity on Windows (``st_dev``, ``st_ino``) is 64 bits wide and does not
fit the canonical JSON integer profile (2**53 - 1) the records are written in,
so an identity beyond the profile is recorded as its decimal text and read back
as the number; every other integer stays a number and every record written
before this rule keeps its bytes.

No provider CLI, no credentials, no network; every native case kills whatever it
started, even when an assertion fails.
"""
from __future__ import annotations

import hashlib
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
import sentinelmesh.supervised_operation as supervised_operation
import sentinelmesh.supervised_record as supervised_record
import sentinelmesh.windows_supervisor as windows_supervisor
from sentinelmesh.adapter_protocol import Invocation, OfflineAdapter
from sentinelmesh.adapter_run import run_adapter_operation
from sentinelmesh.core import MAX_SAFE_INTEGER, canonical_json
from sentinelmesh.durable_ledger import create_ledger, open_ledger
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.restart_recovery import recover_after_restart
from sentinelmesh.runtime import InvocationSpec, Lifecycle, RuntimeErrorCode
from sentinelmesh.scheduler import Scheduler
from sentinelmesh.supervised_operation import recover_supervised_operation, run_supervised_operation
from sentinelmesh.supervised_record import (
    HANDOFF_NAME,
    OBSERVATION_NAME,
    SupervisedRun,
    native_supervisor,
    record_supervised_run,
    recorded_streams,
    replay_decision,
)
from sentinelmesh.windows_supervisor import TERMINATED_EXIT_CODE

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
CAPS = {"dispatches": 5, "tokens": 100, "cost": 50}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
DIMENSIONS = ("tokens", "cost")
CANDIDATE = "c" * 64
TIMEOUT = 1
# timeout + kill wait (2 s) + reap (2 s), with room for a busy machine
WALL_BOUND = TIMEOUT + 7.0
IMPORT_CHECK = ("import sys, sentinelmesh.supervised_record, sentinelmesh.supervised_operation" + NL
                + "import sentinelmesh.adapter_run, sentinelmesh.restart_recovery" + NL
                + "assert 'ctypes' not in sys.modules, 'ctypes'" + NL
                + "print('IMPORT_SAFE_OK')" + NL)
# Writes bytes text mode would mangle (CR LF, 0x1A) on stdout and a line on stderr.
STDOUT_BYTES = bytes([111, 117, 116, 13, 10, 26, 120, 10])
STDERR_BYTES = bytes([101, 114, 114, 10])
WRITE_BOTH = ("import os" + NL
              + "os.write(1, bytes(%r))" % list(STDOUT_BYTES) + NL
              + "os.write(2, bytes(%r))" % list(STDERR_BYTES) + NL)
# A leader that spawns a grandchild sleeping for a minute, reports its pid on
# stdout and then sleeps itself: what a timeout has to end as a whole.
TREE_THEN_SLEEP = ("import subprocess, sys, time" + NL
                   + "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])" + NL
                   + "sys.stdout.write(str(p.pid)); sys.stdout.flush()" + NL
                   + "time.sleep(60)" + NL)
# A leader that marks a file once it runs, then sleeps (for the cancellation case).
MARK_THEN_SLEEP = ("import sys, time" + NL
                   + "open(sys.argv[1], 'w').close()" + NL
                   + "time.sleep(60)" + NL)
STILL_ACTIVE = 259


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


def facts_of(path: Path) -> dict:
    info = os.lstat(path)
    data = path.read_bytes()
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": len(data), "sha256": sha256_of(data)}


def events_of(scheduler, op, event_type):
    return [e for e in scheduler.events if e["operation_id"] == op and e["type"] == event_type]


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


class AppendingScheduler(Scheduler):
    """A same-account writer that touches one evidence file from ``mark_running``.

    The bridge calls ``mark_running`` after the supervisor returned - after the
    job was reaped and the streams digested - and before it checks them, so what
    is written here is a write after the reap that the check has to see.
    """

    evidence = None
    mode = None

    def mark_running(self, *args, **kwargs):
        super().mark_running(*args, **kwargs)
        if self.evidence is None:
            return
        path = self.evidence / "stderr.log"
        if self.mode == "append":
            with open(path, "ab") as handle:
                handle.write(bytes([108, 97, 116, 101, 10]))
        elif self.mode == "replace":
            os.replace(path, self.evidence / "moved.log")
            path.write_bytes(bytes([102, 111, 114, 103, 101, 100, 10]))


class WiringCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="win-wiring-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.count = 0

    def evidence_dir(self) -> Path:
        self.count += 1
        path = self.work / ("evidence-%d" % self.count)
        path.mkdir()
        return path

    def spec(self, code, *args, run_id="run-1", timeout=10, command=None, environment=None):
        env = BASE_ENV if environment is None else environment
        argv = command if command is not None else (PY, "-c", code, *args)
        return InvocationSpec("req-1", run_id, argv, str(self.cwd), environment_digest(env), timeout)

    def not_started_run(self, spec) -> SupervisedRun:
        """A `SupervisedRun` a supervisor returns when the process could not start."""
        lifecycle = Lifecycle().transition("STARTING").transition("FAILED")
        observation = {"run_id": spec.run_id, "environment_sha256": spec.environment_sha256,
                       "state": "NOT_STARTED"}
        return SupervisedRun("start_failed", None, lifecycle, observation, None, "", "", False, (), 0.0)

    def code_of(self, func, *args, **kwargs):
        with self.assertRaises(RuntimeErrorCode) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code


class SelectionOracle(WiringCase):
    """The platform choice, on any platform, through fakes that start nothing."""

    def test_the_supervisor_is_chosen_by_os_name_on_every_call(self):
        for name, expected in (("nt", windows_supervisor), ("posix", posix_supervisor)):
            with mock.patch.object(supervised_record.os, "name", name):
                self.assertIs(native_supervisor(), expected)
        with mock.patch.object(supervised_record.os, "name", "java"):
            self.assertEqual(self.code_of(native_supervisor), "E_RUNTIME_PLATFORM")
        self.assertIs(native_supervisor(), windows_supervisor if os.name == "nt" else posix_supervisor)
        self.assertIs(supervised_operation.native_supervisor, native_supervisor)
        self.assertIs(supervised_record.SupervisedRun, posix_supervisor.SupervisedRun)
        self.assertIs(windows_supervisor.SupervisedRun, posix_supervisor.SupervisedRun)

    def test_supervise_reaches_the_platform_module_with_the_arguments_unchanged(self):
        spec, hook, env, names = object(), (lambda: None), {"A": "1"}, frozenset({"X"})
        self.assertIsNot(supervised_record.supervise, posix_supervisor.supervise)
        self.assertIsNot(supervised_record.supervise, windows_supervisor.supervise)
        for name, chosen, other in (("nt", windows_supervisor, posix_supervisor),
                                    ("posix", posix_supervisor, windows_supervisor)):
            with self.subTest(platform=name):
                calls = []

                def chosen_fake(spec_, **kwargs):
                    calls.append((spec_, kwargs))
                    return "the run"

                def other_fake(*args, **kwargs):
                    raise AssertionError("the other platform's supervisor was called")

                with mock.patch.object(chosen, "supervise", chosen_fake), \
                        mock.patch.object(other, "supervise", other_fake), \
                        mock.patch.object(supervised_record.os, "name", name):
                    result = supervised_record.supervise(spec, environment=env, evidence_dir="d",
                                                         worktree_id="w", allowed_secret_names=names,
                                                         before_start=hook)
                    self.assertEqual(result, "the run")
                    self.assertEqual(len(calls), 1)
                    self.assertIs(calls[0][0], spec)
                    self.assertEqual(set(calls[0][1]), {"environment", "evidence_dir", "worktree_id",
                                                        "allowed_secret_names", "before_start"})
                    self.assertIs(calls[0][1]["environment"], env)
                    self.assertEqual((calls[0][1]["evidence_dir"], calls[0][1]["worktree_id"]), ("d", "w"))
                    self.assertIs(calls[0][1]["allowed_secret_names"], names)
                    self.assertIs(calls[0][1]["before_start"], hook)
                    # The two optional arguments default the way the supervisors do.
                    supervised_record.supervise(spec, environment=env, evidence_dir="d", worktree_id="w")
                    self.assertEqual(calls[1][1]["allowed_secret_names"], frozenset())
                    self.assertIsNone(calls[1][1]["before_start"])

    def test_record_supervised_run_goes_through_the_module_level_supervise(self):
        spec = self.spec("pass")
        evidence = self.evidence_dir()
        seen = []

        def fake(spec_, **kwargs):
            seen.append((spec_, kwargs))
            kwargs["before_start"]()
            return self.not_started_run(spec_)

        with mock.patch.object(supervised_record, "supervise", fake):
            run = record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1",
                                        allowed_secret_names=frozenset({"TOKEN"}))
        self.assertEqual(run.stop_reason, "start_failed")
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][0], spec)
        self.assertEqual((seen[0][1]["environment"], seen[0][1]["evidence_dir"], seen[0][1]["worktree_id"],
                          seen[0][1]["allowed_secret_names"]),
                         (BASE_ENV, str(evidence), "wt-1", frozenset({"TOKEN"})))
        self.assertTrue(callable(seen[0][1]["before_start"]))
        self.assertEqual(sorted(os.listdir(evidence)), [HANDOFF_NAME, OBSERVATION_NAME])
        self.assertEqual(replay_decision(spec, str(evidence))["status"], "NOT_STARTED")

    def test_an_unsupported_platform_is_refused_before_any_record_or_process(self):
        spec = self.spec("pass")
        evidence = self.evidence_dir()
        with mock.patch.object(supervised_record.os, "name", "java"):
            self.assertEqual(self.code_of(record_supervised_run, spec, environment=BASE_ENV,
                                          evidence_dir=str(evidence), worktree_id="wt-1"), "E_RUNTIME_PLATFORM")
        self.assertEqual(os.listdir(evidence), [])
        scheduler = Scheduler()
        scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        scheduler.seed_task("req-1", "task-1", CAPS, max_attempts=3)
        op = scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, RESERVATION,
                             ["src/task-1.py"])["operation_id"]
        bridge_evidence = self.evidence_dir()
        with mock.patch.object(supervised_record.os, "name", "java"):
            outcome = run_supervised_operation(scheduler, operation_id=op, owner="owner-1", generation=1,
                                               spec=self.spec("pass", run_id=op), environment=BASE_ENV,
                                               evidence_dir=str(bridge_evidence), worktree_id="wt-1",
                                               candidate_hash=lambda directory: CANDIDATE)
        self.assertEqual((outcome["error"], outcome["stop_reason"], outcome["exit_code"]),
                         ("E_RUNTIME_PLATFORM", None, None))
        self.assertEqual((outcome["state"], scheduler.operation(op)["state"]), ("NOT_STARTED", "NOT_STARTED"))
        self.assertEqual(os.listdir(bridge_evidence), [])
        self.assertEqual(events_of(scheduler, op, "run.started"), [])

    def test_streams_match_reaches_the_platform_module_and_is_used_by_name(self):
        self.assertIsNot(supervised_operation.streams_match, posix_supervisor.streams_match)
        self.assertIsNot(supervised_operation.streams_match, windows_supervisor.streams_match)
        facts = {"stdout": {"device": 1, "inode": 2, "bytes": 0, "sha256": sha256_of(b"")},
                 "stderr": {"device": 1, "inode": 3, "bytes": 0, "sha256": sha256_of(b"")}}
        for name, chosen, other in (("nt", windows_supervisor, posix_supervisor),
                                    ("posix", posix_supervisor, windows_supervisor)):
            with self.subTest(platform=name):
                calls = []

                def chosen_fake(evidence_dir, streams):
                    calls.append((evidence_dir, streams))
                    return True

                def other_fake(evidence_dir, streams):
                    raise AssertionError("the other platform's streams_match was called")

                with mock.patch.object(chosen, "streams_match", chosen_fake), \
                        mock.patch.object(other, "streams_match", other_fake), \
                        mock.patch.object(supervised_record.os, "name", name):
                    self.assertIs(supervised_operation.streams_match("d", facts), True)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], "d")
                self.assertIs(calls[0][1], facts)
        # The bridge asks through its own module name, so a caller can see the question.
        seen = []

        def by_name(evidence_dir, streams):
            seen.append((evidence_dir, streams))
            return True

        with mock.patch.object(supervised_operation, "streams_match", by_name):
            self.assertIsNone(supervised_operation._stream_reason("d", facts))
        self.assertEqual(seen, [("d", facts)])

    def test_importing_the_wiring_loads_no_ctypes(self):
        completed = subprocess.run([PY, "-c", IMPORT_CHECK], cwd=str(REPO_ROOT), capture_output=True, text=True,
                                   timeout=60)
        self.assertEqual((completed.returncode, completed.stdout.strip()), (0, "IMPORT_SAFE_OK"), completed.stderr)


class RecordIdentityOracle(WiringCase):
    """Identities beyond the canonical JSON profile in the observation record, on any platform."""

    def started_observation(self, spec):
        return {"run_id": spec.run_id, "environment_sha256": spec.environment_sha256, "state": "STOPPED",
                "process_identity": {"pid": 4242, "boot_id": "boot-1", "executable_sha256": "0" * 64,
                                     "worktree_id": "wt-1"},
                "cleanup_confirmed": True}

    def write_records(self, spec, evidence, streams):
        """Write a handoff and an observation the module's own way; return the observation bytes."""
        supervised_record._write_record(str(evidence), HANDOFF_NAME, supervised_record._handoff_content(spec))
        supervised_record._write_record(
            str(evidence), OBSERVATION_NAME,
            supervised_record._observation_content(spec, self.started_observation(spec), streams))
        return (evidence / OBSERVATION_NAME).read_bytes()

    def test_identities_inside_the_profile_stay_numbers_and_beyond_it_become_text(self):
        inside = (0, 1, 7, MAX_SAFE_INTEGER, -1, -MAX_SAFE_INTEGER)
        beyond = (MAX_SAFE_INTEGER + 1, 2**63 + 5, 2**64 - 1, -(MAX_SAFE_INTEGER + 1), -(2**63))
        for value in inside:
            with self.subTest(value=value):
                written = supervised_record._record_identity(value)
                self.assertIs(type(written), int)
                self.assertEqual(written, value)
                self.assertEqual(supervised_record._recorded_identity(written), value)
        for value in beyond:
            with self.subTest(value=value):
                written = supervised_record._record_identity(value)
                self.assertEqual(written, str(value))
                self.assertIs(type(written), str)
                decoded = supervised_record._recorded_identity(written)
                self.assertIs(type(decoded), int)
                self.assertEqual(decoded, value)

    def test_a_recorded_identity_has_exactly_one_form(self):
        refused = ("7", "0", "-0", "", "-", str(MAX_SAFE_INTEGER), str(-MAX_SAFE_INTEGER),
                   "+" + str(MAX_SAFE_INTEGER + 1), "0" + str(MAX_SAFE_INTEGER + 1),
                   " " + str(MAX_SAFE_INTEGER + 1), str(MAX_SAFE_INTEGER + 1) + " ",
                   str(MAX_SAFE_INTEGER + 1) + ".0", "1e16", "0x20000000000000", str(2**64), str(-(2**64)),
                   "9" * 25, True, False, 1.0, float(MAX_SAFE_INTEGER + 1), None, [MAX_SAFE_INTEGER + 1],
                   # fullwidth digits are digits to str.isdigit, not to a record
                   "".join(chr(0xFF10 + int(d)) for d in str(MAX_SAFE_INTEGER + 1)))
        for value in refused:
            with self.subTest(value=value):
                self.assertIsNone(supervised_record._recorded_identity(value))
        for value in (0, 5, -5, MAX_SAFE_INTEGER, -MAX_SAFE_INTEGER):
            with self.subTest(value=value):
                self.assertEqual(supervised_record._recorded_identity(value), value)

    def test_a_record_round_trips_identities_beyond_the_profile(self):
        spec = self.spec("pass")
        evidence = self.evidence_dir()
        streams = {"stdout": {"device": 2**63 + 5, "inode": MAX_SAFE_INTEGER + 1, "bytes": 3,
                              "sha256": sha256_of(b"abc")},
                   "stderr": {"device": -(2**63), "inode": 2**64 - 1, "bytes": 0, "sha256": sha256_of(b"")}}
        raw = self.write_records(spec, evidence, streams)
        self.assertIn(b'"device":"9223372036854775813"', raw)
        self.assertIn(b'"inode":"9007199254740992"', raw)
        self.assertIn(b'"device":"-9223372036854775808"', raw)
        self.assertIn(b'"inode":"18446744073709551615"', raw)
        self.assertEqual(raw, canonical_json({
            "schema": "sentinelmesh-observation-v2", "spec_sha256": spec.digest(),
            "observation": self.started_observation(spec),
            "streams": {"stdout": {"device": "9223372036854775813", "inode": "9007199254740992", "bytes": 3,
                                   "sha256": sha256_of(b"abc")},
                        "stderr": {"device": "-9223372036854775808", "inode": "18446744073709551615", "bytes": 0,
                                   "sha256": sha256_of(b"")}}}))
        recorded = recorded_streams(spec, str(evidence))
        self.assertEqual(recorded, streams)
        for fact in recorded.values():
            self.assertIs(type(fact["device"]), int)
            self.assertIs(type(fact["inode"]), int)
        self.assertEqual((replay_decision(spec, str(evidence))["status"],
                          replay_decision(spec, str(evidence))["replay_allowed"]), ("STOPPED", True))
        # Identities inside the profile are written exactly as before.
        small = self.evidence_dir()
        small_streams = {"stdout": {"device": 5, "inode": 7, "bytes": 0, "sha256": sha256_of(b"")}, "stderr": None}
        raw = self.write_records(spec, small, small_streams)
        self.assertIn(b'"device":5,"inode":7', raw)
        self.assertEqual(recorded_streams(spec, str(small)), small_streams)

    def test_a_record_whose_identity_is_in_the_wrong_form_is_damaged_evidence(self):
        spec = self.spec("pass")
        fact = {"device": 5, "inode": 7, "bytes": 0, "sha256": sha256_of(b"")}
        for case, stdout in (("a number inside the profile as text", dict(fact, inode="7")),
                             ("a device inside the profile as text", dict(fact, device="5")),
                             ("a negative inode as text", dict(fact, inode="-" + str(MAX_SAFE_INTEGER + 1))),
                             ("a signed inode", dict(fact, inode="+" + str(MAX_SAFE_INTEGER + 1))),
                             ("a leading zero", dict(fact, inode="0" + str(MAX_SAFE_INTEGER + 1))),
                             ("beyond 64 bits", dict(fact, inode=str(2**64))),
                             ("a byte count as text", dict(fact, bytes=str(MAX_SAFE_INTEGER + 1)))):
            with self.subTest(case=case):
                evidence = self.evidence_dir()
                supervised_record._write_record(str(evidence), HANDOFF_NAME,
                                                supervised_record._handoff_content(spec))
                content = {"schema": "sentinelmesh-observation-v2", "spec_sha256": spec.digest(),
                           "observation": self.started_observation(spec),
                           "streams": {"stdout": stdout, "stderr": None}}
                (evidence / OBSERVATION_NAME).write_bytes(canonical_json(content))
                self.assertEqual(self.code_of(recorded_streams, spec, str(evidence)), "E_RUNTIME_EVIDENCE")
                self.assertEqual(self.code_of(replay_decision, spec, str(evidence)), "E_RUNTIME_EVIDENCE")
        with self.subTest(case="a number beyond the profile as a JSON number"):
            evidence = self.evidence_dir()
            raw = self.write_records(spec, evidence, {"stdout": fact, "stderr": None})
            self.assertIn(b'"inode":7,', raw)
            (evidence / OBSERVATION_NAME).write_bytes(raw.replace(b'"inode":7,', b'"inode":9007199254740992,'))
            self.assertEqual(self.code_of(recorded_streams, spec, str(evidence)), "E_RUNTIME_EVIDENCE")
        with self.subTest(case="a negative device as text is a device"):
            evidence = self.evidence_dir()
            self.write_records(spec, evidence, {"stdout": dict(fact, device=-(MAX_SAFE_INTEGER + 1)),
                                                "stderr": None})
            self.assertEqual(recorded_streams(spec, str(evidence))["stdout"]["device"], -(MAX_SAFE_INTEGER + 1))


@unittest.skipIf(os.name != "nt", "native Windows wiring: real Job Object process trees")
class WindowsWiringOracle(WiringCase):
    """The record, the bridge, the adapter path and the recovery, end to end on Windows."""

    def setUp(self):
        super().setUp()
        self.candidates = []
        self.fresh_scheduler()

    def fresh_scheduler(self, scheduler_class=Scheduler):
        self.scheduler = scheduler_class()
        self.scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        for task in ("task-1", "task-2", "task-3"):
            self.scheduler.seed_task("req-1", task, CAPS, max_attempts=5)
        return self.scheduler

    def candidate_hash(self, evidence_dir):
        self.candidates.append(evidence_dir)
        return CANDIDATE

    def operation(self, task="task-1"):
        evidence = self.evidence_dir()
        claim = self.scheduler.claim("req-1", task, 1, "idem-%d" % self.count, "owner-1", 1, 1, RESERVATION,
                                     ["src/%s.py" % task])
        return claim["operation_id"], evidence

    def run_op(self, op, spec, evidence, **kwargs):
        outcome = run_supervised_operation(self.scheduler, operation_id=op, owner="owner-1", generation=1,
                                           spec=spec, environment=BASE_ENV, evidence_dir=str(evidence),
                                           worktree_id="wt-1", candidate_hash=self.candidate_hash, **kwargs)
        self.track(op)
        return outcome

    def invocation(self, op, code, *args):
        return Invocation("req-1", op, "executor", "stub-profile", (PY, "-c", code, *args), (),
                          environment_digest(BASE_ENV), None)

    def run_adapter(self, invocation, evidence, adapter=None, **kwargs):
        kwargs.setdefault("usage_dimensions", DIMENSIONS)
        kwargs.setdefault("timeout_seconds", 10)
        outcome = run_adapter_operation(self.scheduler, adapter or OfflineAdapter(), invocation, cwd=str(self.cwd),
                                        environment=BASE_ENV, owner="owner-1", generation=1,
                                        evidence_dir=str(evidence), worktree_id="wt-1",
                                        candidate_hash=self.candidate_hash, **kwargs)
        self.track(invocation.run_id)
        return outcome

    def track(self, op):
        for event in events_of(self.scheduler, op, "run.started"):
            self.addCleanup(kill_pid, event["payload"]["process_identity"]["pid"])

    def usage_of(self, op, dimension):
        usage = self.scheduler._operations[op].usage.get(dimension)
        return None if usage is None else (usage.call_key, usage.revision, usage.amount, usage.final,
                                           usage.evidence_hash)

    def state_of(self, op):
        return self.scheduler.operation(op)["state"]

    def assert_reviewed(self, op, outcome, evidence, status):
        self.assertEqual((outcome["state"], self.state_of(op)), ("REVIEW_PENDING", "REVIEW_PENDING"))
        self.assertEqual((outcome["decision"]["status"], outcome["decision"]["replay_allowed"]), (status, True))
        started = events_of(self.scheduler, op, "run.started")
        self.assertEqual(len(started), 1)
        observation = json.loads((evidence / OBSERVATION_NAME).read_bytes())["observation"]
        self.assertEqual(started[0]["payload"]["process_identity"], observation["process_identity"])
        self.assertEqual(events_of(self.scheduler, op, "run.finished_for_review")[0]["payload"],
                         {"candidate": CANDIDATE})
        self.assertIn(str(evidence), self.candidates)
        self.assertNotIn(("repo-1", "src/task-1.py"), self.scheduler._resource_holds)

    def assert_unknown(self, op, outcome, reason):
        self.assertEqual((outcome["state"], self.state_of(op)), ("START_UNKNOWN", "START_UNKNOWN"))
        unknown = events_of(self.scheduler, op, "scheduler.start_unknown")
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["payload"]["reason"], reason)
        self.assertEqual(events_of(self.scheduler, op, "run.finished_for_review"), [])
        self.assertIn(("repo-1", "src/task-1.py"), self.scheduler._resource_holds, "the holds were released")
        self.assertEqual(self.candidates, [])

    # the record -----------------------------------------------------------

    def test_a_recorded_run_on_windows_is_a_job_object_run_with_exact_records(self):
        spec = self.spec(WRITE_BOTH)
        evidence = self.evidence_dir()
        run = record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
        self.addCleanup(kill_pid, run.identity.pid)
        self.assertEqual((run.stop_reason, run.exit_code, run.lifecycle.state), ("completed", 0, "STOPPED"))
        self.assertRegex(run.identity.boot_id, "^[0-9a-fA-F-]{36}:[0-9]+$")
        self.assertEqual(sorted(os.listdir(evidence)), [HANDOFF_NAME, OBSERVATION_NAME, "stderr.log", "stdout.log"])
        self.assertEqual(((evidence / "stdout.log").read_bytes(), (evidence / "stderr.log").read_bytes()),
                         (STDOUT_BYTES, STDERR_BYTES))
        self.assertEqual(run.streams, {"stdout": facts_of(evidence / "stdout.log"),
                                       "stderr": facts_of(evidence / "stderr.log")})
        for fact in run.streams.values():
            for key in ("device", "inode", "bytes"):
                self.assertIs(type(fact[key]), int)
        # The record is the module's own content in canonical form, identities included.
        raw = (evidence / OBSERVATION_NAME).read_bytes()
        self.assertEqual(raw, canonical_json(supervised_record._observation_content(spec, run.observation,
                                                                                    run.streams)))
        parsed = json.loads(raw)["streams"]
        for name, fact in run.streams.items():
            for key in ("device", "inode"):
                expected_type = int if abs(fact[key]) <= MAX_SAFE_INTEGER else str
                self.assertIs(type(parsed[name][key]), expected_type, (name, key, fact[key]))
                self.assertEqual(str(parsed[name][key]), str(fact[key]))
        self.assertEqual(recorded_streams(spec, str(evidence)), run.streams)
        self.assertIs(supervised_operation.streams_match(str(evidence), recorded_streams(spec, str(evidence))), True)
        self.assertIs(windows_supervisor.streams_match(str(evidence), run.streams), True)
        decision = replay_decision(spec, str(evidence))
        self.assertEqual((decision["status"], decision["replay_allowed"]), ("STOPPED", True))
        self.assertTrue(pid_gone_within(run.identity.pid, 3.0))
        # The recovery question goes through the bridge's module name with the recorded facts.
        seen = []

        def by_name(evidence_dir, streams):
            seen.append((evidence_dir, streams))
            return True

        with mock.patch.object(supervised_operation, "streams_match", by_name):
            self.assertIs(supervised_operation._recorded_streams_match(spec, str(evidence)), True)
        self.assertEqual(seen, [(str(evidence), run.streams)])

    # the bridge -----------------------------------------------------------

    def test_a_timeout_on_windows_ends_the_tree_and_goes_to_review(self):
        op, evidence = self.operation()
        started = time.monotonic()
        outcome = self.run_op(op, self.spec(TREE_THEN_SLEEP, run_id=op, timeout=TIMEOUT), evidence)
        wall = time.monotonic() - started
        grandchild = int((evidence / "stdout.log").read_bytes().split()[0])
        self.addCleanup(kill_pid, grandchild)
        self.assertEqual((outcome["stop_reason"], outcome["exit_code"], outcome["error"]),
                         ("timeout_killed", TERMINATED_EXIT_CODE, None))
        self.assertLess(wall, WALL_BOUND)
        self.assert_reviewed(op, outcome, evidence, "FAILED")
        self.assertTrue(pid_gone_within(grandchild, 3.0), "the grandchild outlived the job")
        leader = events_of(self.scheduler, op, "run.started")[0]["payload"]["process_identity"]["pid"]
        self.assertTrue(pid_gone_within(leader, 3.0))

    def test_an_image_that_cannot_start_is_not_started_and_releases_its_holds(self):
        garbage = self.cwd / "not-a-program.exe"
        garbage.write_bytes(bytes([0, 1, 2]) + b" not an executable image" + NL.encode())
        op, evidence = self.operation()
        outcome = self.run_op(op, self.spec(None, run_id=op, command=(str(garbage),)), evidence)
        self.assertEqual((outcome["state"], self.state_of(op), outcome["stop_reason"], outcome["exit_code"],
                          outcome["error"]), ("NOT_STARTED", "NOT_STARTED", "start_failed", None, None))
        self.assertEqual((outcome["decision"]["status"], outcome["decision"]["replay_allowed"]),
                         ("NOT_STARTED", True))
        self.assertEqual(events_of(self.scheduler, op, "run.started"), [])
        self.assertNotIn(("repo-1", "src/task-1.py"), self.scheduler._resource_holds)
        self.assertEqual(sorted(os.listdir(evidence)), [HANDOFF_NAME, OBSERVATION_NAME, "stderr.log", "stdout.log"])
        self.assertIsNone(self.scheduler._requests["req-1"].stopped)

    def test_a_cancelled_run_on_windows_is_reported_and_reviewed(self):
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

        op, evidence = self.operation()
        with mock.patch.object(subprocess.Popen, "wait", interrupt_first):
            outcome = self.run_op(op, self.spec(MARK_THEN_SLEEP, str(ready), run_id=op, timeout=30), evidence)
        self.assertTrue(ready.exists(), "the leader was not running before the interrupt")
        self.assertEqual((outcome["stop_reason"], outcome["exit_code"]), ("cancelled", TERMINATED_EXIT_CODE))
        self.assert_reviewed(op, outcome, evidence, "FAILED")

    def test_a_same_account_write_after_the_reap_stops_the_review(self):
        for mode, reason in (("append", "CLEANUP_UNCONFIRMED"), ("replace", "START_UNKNOWN")):
            with self.subTest(mode=mode):
                self.fresh_scheduler(AppendingScheduler)
                self.candidates = []
                op, evidence = self.operation()
                self.scheduler.evidence, self.scheduler.mode = evidence, mode
                try:
                    outcome = self.run_op(op, self.spec(WRITE_BOTH, run_id=op), evidence)
                finally:
                    self.scheduler.evidence = None
                # The cleanup itself was confirmed: it is the stream alone that stopped the review.
                self.assertEqual((outcome["decision"]["status"], outcome["decision"]["replay_allowed"]),
                                 ("STOPPED", True))
                self.assert_unknown(op, outcome, reason)
                self.assertTrue(self.scheduler._requests["req-1"].stopped)

    def test_an_unconfirmed_cleanup_on_windows_is_start_unknown_and_keeps_its_holds(self):
        op, evidence = self.operation()
        with mock.patch.object(windows_supervisor._Job, "active_processes", lambda self_: 1):
            outcome = self.run_op(op, self.spec(WRITE_BOTH, run_id=op), evidence)
        self.assertEqual((outcome["stop_reason"], outcome["exit_code"]), ("completed", 0))
        self.assertEqual((outcome["decision"]["status"], outcome["decision"]["replay_allowed"]), ("STOPPED", False))
        self.assert_unknown(op, outcome, "CLEANUP_UNCONFIRMED")
        self.assertTrue(self.scheduler._requests["req-1"].stopped)

    # the adapter path -------------------------------------------------------

    def test_an_adapter_run_on_windows_settles_usage_against_the_supervisors_digest(self):
        op, evidence = self.operation()
        outcome = self.run_adapter(self.invocation(op, emit(COMPLETE)), evidence)
        expected = stream_bytes(COMPLETE)
        self.assertEqual((evidence / "stdout.log").read_bytes(), expected)
        self.assertEqual((outcome["state"], outcome["stop_reason"], outcome["exit_code"], outcome["error"]),
                         ("REVIEW_PENDING", "completed", 0, None))
        self.assertEqual(outcome["stream"], {"status": "ok", "sha256": sha256_of(expected), "bytes": len(expected)})
        self.assertEqual(outcome["result"]["usage"], {"tokens": 12, "cost": 3})
        self.assertEqual((outcome["result_error"], outcome["usage_recorded"], outcome["usage_skipped"]),
                         (None, {"tokens": "APPLIED", "cost": "APPLIED"}, []))
        self.assertEqual(self.usage_of(op, "tokens"), ("local:%s:tokens" % op, 1, 12, True, sha256_of(expected)))
        self.assertEqual(self.usage_of(op, "cost"), ("local:%s:cost" % op, 1, 3, True, sha256_of(expected)))
        self.assertEqual(recorded_streams(InvocationSpec("req-1", op, (PY, "-c", emit(COMPLETE)), str(self.cwd),
                                                         environment_digest(BASE_ENV), 10), str(evidence))
                         ["stdout"]["sha256"], sha256_of(expected))
        types = [(e["type"], e["payload"].get("dimension")) for e in self.scheduler.events
                 if e["operation_id"] == op]
        self.assertEqual(types, [("scheduler.claimed", None), ("scheduler.handoff_accepted", None),
                                 ("run.started", None), ("usage.observed", "tokens"), ("usage.observed", "cost"),
                                 ("run.finished_for_review", None)])

    def test_the_stream_ceilings_reach_read_stream_on_the_run_path(self):
        expected = stream_bytes(COMPLETE)
        for case, kwargs, status in (("the byte ceiling at the stream size", {"max_stream_bytes": len(expected)}, "ok"),
                                     ("one byte under the stream", {"max_stream_bytes": len(expected) - 1},
                                      "too_large"),
                                     ("the line ceiling under the first line", {"max_line_bytes": 40}, "invalid")):
            with self.subTest(case=case):
                self.fresh_scheduler()
                op, evidence = self.operation()
                outcome = self.run_adapter(self.invocation(op, emit(COMPLETE)), evidence, **kwargs)
                self.assertEqual((evidence / "stdout.log").read_bytes(), expected)
                self.assertEqual((outcome["state"], outcome["stream"]["status"]), ("REVIEW_PENDING", status))
                if status == "ok":
                    self.assertEqual(outcome["usage_recorded"], {"tokens": "APPLIED", "cost": "APPLIED"})
                else:
                    self.assertEqual((outcome["usage_recorded"], outcome["usage_skipped"], outcome["result"]),
                                     ({}, ["tokens", "cost"], None))
                    self.assertIsNone(self.usage_of(op, "tokens"))
                if status == "invalid":
                    self.assertEqual((outcome["stream"]["line"], outcome["stream"]["code"]), (1, "E_JSON_LIMIT"))

    def test_a_run_through_the_wiring_gives_back_its_handles(self):
        # CPython keeps a few handles after its first failed CreateProcess, and the
        # first run warms every lazy binding: both are paid before the baseline.
        garbage = self.cwd / "not-a-program.exe"
        garbage.write_bytes(bytes([0, 1, 2]) + NL.encode())
        try:
            subprocess.Popen([str(garbage)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError:
            pass
        op, evidence = self.operation()
        self.run_adapter(self.invocation(op, emit(COMPLETE)), evidence)
        before = handle_count()
        self.fresh_scheduler()
        op, evidence = self.operation()
        outcome = self.run_adapter(self.invocation(op, emit(COMPLETE)), evidence)
        self.assertEqual(outcome["usage_recorded"], {"tokens": "APPLIED", "cost": "APPLIED"})
        self.assertEqual(handle_count(), before)

    # recovery ---------------------------------------------------------------

    def test_recovery_on_windows_reviews_a_finished_run_from_its_records(self):
        path = str(self.work / "ledger.db")
        ledger = create_ledger(path)
        self.addCleanup(lambda: ledger.close())
        ledger.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        for task in ("task-1", "task-2"):
            ledger.seed_task("req-1", task, CAPS, max_attempts=5)
        ops = {}
        for task in ("task-1", "task-2"):
            op = ledger.claim("req-1", task, 1, "idem-" + task, "owner-1", 1, 1, RESERVATION,
                              ["src/%s.py" % task])["operation_id"]
            evidence = self.evidence_dir()
            spec = self.spec(WRITE_BOTH, run_id=op)
            ledger.handoff(op, "owner-1", 1, run={"spec": {
                "request_id": spec.request_id, "run_id": spec.run_id, "command": list(spec.command),
                "cwd": spec.cwd, "environment_sha256": spec.environment_sha256,
                "timeout_seconds": spec.timeout_seconds}, "evidence_dir": str(evidence), "worktree_id": "wt-1"})
            run = record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
            self.addCleanup(kill_pid, run.identity.pid)
            ops[task] = (op, evidence, spec)
        # One run's stderr was replaced since the records were written.
        replaced_op, replaced_evidence, _ = ops["task-2"]
        os.replace(replaced_evidence / "stderr.log", replaced_evidence / "moved.log")
        (replaced_evidence / "stderr.log").write_bytes(STDERR_BYTES)
        report = recover_after_restart(ledger, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(report, {"generation": 2, "operations": [
            {"operation_id": ops["task-1"][0], "action": "recovered", "state": "REVIEW_PENDING"},
            {"operation_id": replaced_op, "action": "recovered", "state": "START_UNKNOWN"}]})
        self.assertEqual(self.candidates, [str(ops["task-1"][1])])
        expected = {op: ledger.operation(op) for op, _, _ in ops.values()}
        ledger.close()
        reopened = open_ledger(path)
        self.addCleanup(lambda: reopened.close())
        self.assertEqual({op: reopened.operation(op) for op in expected}, expected)
        self.assertEqual(reopened.operation(ops["task-1"][0])["state"], "REVIEW_PENDING")
        # The in-memory bridge recovery agrees, on a run whose records are intact.
        scheduler = self.fresh_scheduler()
        op, evidence = self.operation()
        spec = self.spec(WRITE_BOTH, run_id=op)
        scheduler.handoff(op, "owner-1", 1)
        run = record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
        self.addCleanup(kill_pid, run.identity.pid)
        scheduler.rollover()
        outcome = recover_supervised_operation(scheduler, operation_id=op, owner="owner-2", generation=2,
                                               spec=spec, evidence_dir=str(evidence),
                                               candidate_hash=self.candidate_hash)
        self.assertEqual((outcome["state"], self.state_of(op)), ("REVIEW_PENDING", "REVIEW_PENDING"))


if __name__ == "__main__":
    unittest.main()
