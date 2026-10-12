"""Planner oracle for run-cli-recover (`.agent/design/controller-cli.md`, sections 3, 5 and 7).

``smctl run recover`` takes over what a controller that died left in the ledger,
through ``restart_recovery.recover_after_restart`` exactly once: one rollover,
every claim that never reached a handoff cancelled, every handoff never decided
settled from its records, and the report written as it is.  Every request stays
stopped with ``RECOVERY_REQUIRED``; what the recovery could not decide is the
operator's, through ``smctl ledger settle``, ``resolve`` and ``resume``.  The
conventions are ``smctl ledger``'s.  The last case is the request-resume
oracle's crash loop with product commands and real processes: a ``smctl run
once`` controller is killed mid-run (``TerminateProcess`` on Windows,
``SIGKILL`` elsewhere), ``smctl run recover`` takes over, the operator enters
the usage, abandons the run and resumes the request, and ``smctl run once`` runs
again in the new generation.  The child processes are local ``python -c``
programs; no provider CLI, no credentials, no network.

``once`` also learns one check (review of run-cli-once attempt-001, F4): an
environment whose names or values the invocation's digest refuses is refused by
the job check, before any claim, instead of stranding a RESERVED operation.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from sentinelmesh import cli
from sentinelmesh.durable_ledger import open_ledger
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec
from sentinelmesh.supervised_operation import evidence_digest
from sentinelmesh.supervised_record import HANDOFF_NAME, OBSERVATION_NAME, record_supervised_run

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
CAPS = {"dispatches": 9, "tokens": 500, "cost": 200}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
CANDIDATE, EVIDENCE = "c" * 64, "e" * 64
OPERATOR, NOTE = "ops-1", "checked the process table and the evidence directory"
REFUSAL_KEYS = {"error", "detail", "operation_id"}
REPORT_KEYS = {"command", "generation", "operations"}
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


# A child that reports its pid in ``started`` (written whole, then renamed) and
# waits for ``release``: the controller is killed while it waits.
WAIT_FOR_RELEASE = ("import os, sys, time" + NL
                    + "started, release = sys.argv[1:3]" + NL
                    + "open(started + '.tmp', 'w').write(str(os.getpid()))" + NL
                    + "os.rename(started + '.tmp', started)" + NL
                    + "deadline = time.monotonic() + 60" + NL
                    + "while not os.path.exists(release) and time.monotonic() < deadline:" + NL
                    + "    time.sleep(0.01)" + NL)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pid_in(path: Path):
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text else None


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` still runs; on Windows through the process handle (``os.kill`` would terminate it)."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


class RecoverCliCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="run-cli-recover-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.ledger = str(self.work / "ledger.db")
        self.count = 0
        self.dirs = 0
        self.init()

    # -- driving the CLI --------------------------------------------------- #

    def smctl(self, *argv):
        """Run ``smctl`` in this process: (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main([str(a) for a in argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv):
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, err), (0, ""), "smctl %s failed: %s" % (argv, err))
        self.assertTrue(out.endswith(NL) and out.count(NL) == 1, "one JSON line expected")
        return json.loads(out)

    def refused(self, expected, *argv, operation_id=None):
        """A refusal: exit 3, nothing on stdout, one JSON line on stderr with the three keys."""
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, out), (3, ""), "smctl %s should be refused: %s" % (argv, err))
        self.assertTrue(err.endswith(NL) and err.count(NL) == 1, "one JSON line expected on stderr")
        error = json.loads(err)
        self.assertEqual(set(error), REFUSAL_KEYS)
        self.assertEqual((error["error"], error["operation_id"]), (expected, operation_id), error)
        self.assertIn(type(error["detail"]), (str, type(None)))
        return error

    def ledger_refused(self, expected, *argv):
        """A refusal of ``smctl ledger``: its own two keys."""
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, out), (3, ""), "smctl %s should be refused: %s" % (argv, err))
        error = json.loads(err)
        self.assertEqual(error["error"], expected, error)
        return error

    def usage_error(self, *argv):
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, out), (2, ""), "smctl %s should be a usage error" % (argv,))
        self.assertNotEqual(err, "")

    def real(self, *argv, timeout=120):
        """Run ``smctl`` as a real process from the repository root."""
        return subprocess.run([PY, "-m", "sentinelmesh.cli", *[str(a) for a in argv]], cwd=str(REPO_ROOT),
                              capture_output=True, text=True, timeout=timeout)

    # -- files and jobs ------------------------------------------------------ #

    def write_json(self, name, value) -> str:
        path = self.work / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def spec(self):
        return {"request_id": "req-1", "repo_id": "repo-1", "caps": dict(CAPS), "concurrency": 4,
                "tasks": [{"task_id": "task-%d" % n, "caps": dict(CAPS), "max_attempts": 3} for n in range(1, 13)]}

    def init(self):
        return self.ok("run", "init", self.ledger, "--spec", self.write_json("spec.json", self.spec()))

    def job(self, code=None, *args, **overrides):
        self.count += 1
        task = overrides.pop("task_id", "task-%d" % self.count)
        job = {"request_id": "req-1", "task_id": task, "plan": 1, "idempotency_key": "idem-%d" % self.count,
               "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["src/%s.py" % task],
               "adapter_id": "offline", "role": "executor", "profile_id": "stub-profile",
               "command": [PY, "-c", emit(COMPLETE) if code is None else code, *args], "capabilities": [],
               "cwd": str(self.cwd), "environment": dict(BASE_ENV), "allowed_secret_names": [],
               "timeout_seconds": 20, "usage_dimensions": ["tokens", "cost"]}
        job.update(overrides)
        return job

    def evidence_dir(self) -> Path:
        self.dirs += 1
        path = self.work / ("evidence-%d" % self.dirs)
        path.mkdir()
        return path

    def once(self, job, evidence, *extra, owner="owner-1"):
        return self.ok("run", "once", self.ledger, "--job", self.write_json("job-%d.json" % self.count, job),
                       "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", owner, *extra)

    def once_refused(self, expected, job, evidence, *extra, owner="owner-1", operation_id=None):
        return self.refused(expected, "run", "once", self.ledger, "--job",
                            self.write_json("job-%d.json" % self.count, job), "--evidence", str(evidence),
                            "--worktree-id", "wt-1", "--owner", owner, *extra, operation_id=operation_id)

    def recover(self, *extra, owner="owner-2"):
        result = self.ok("run", "recover", self.ledger, "--owner", owner, *extra)
        self.assertEqual(set(result), REPORT_KEYS)
        self.assertEqual(result["command"], "recover")
        return result

    def stranded(self):
        """A claim refused after the claim: a RESERVED operation with nothing started."""
        evidence = self.evidence_dir()
        code, out, err = self.smctl("run", "once", self.ledger, "--job",
                                    self.write_json("strand-%d.json" % self.count,
                                                    self.job(usage_dimensions=["tokens", "gpu"])),
                                    "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1")
        self.assertEqual((code, out), (3, ""))
        error = json.loads(err)
        self.assertEqual(error["error"], "E_ADAPTER_SCHEMA")
        self.assertEqual(self.operation(error["operation_id"])["state"], "RESERVED")
        return error["operation_id"]

    def undecided_handoff(self, *, complete=True):
        """A handoff of generation 1 whose run was recorded in full but never settled.

        What a controller that died between the observation record and ``finish``
        leaves behind: the state is HANDOFF_ACCEPTED, the descriptor is in the
        ledger and both records are on disk.
        """
        self.count += 1
        task = "task-%d" % self.count
        evidence = self.evidence_dir()
        ledger = open_ledger(self.ledger)
        try:
            op = ledger.claim("req-1", task, 1, "idem-%d" % self.count, "owner-1", ledger.generation, 1,
                              RESERVATION, ["src/%s.py" % task])["operation_id"]
            spec = InvocationSpec("req-1", op, (PY, "-c", emit(COMPLETE)), str(self.cwd),
                                  environment_digest(BASE_ENV), 20)
            descriptor = {"spec": {"request_id": "req-1", "run_id": op, "command": list(spec.command),
                                   "cwd": spec.cwd, "environment_sha256": spec.environment_sha256,
                                   "timeout_seconds": 20},
                          "evidence_dir": str(evidence), "worktree_id": "wt-1"}
            ledger.handoff(op, "owner-1", ledger.generation, run=descriptor)
            record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
        finally:
            ledger.close()
        self.assertEqual(sorted(os.listdir(evidence)), [HANDOFF_NAME, OBSERVATION_NAME, "stderr.log", "stdout.log"])
        self.assertEqual(self.operation(op)["state"], "HANDOFF_ACCEPTED")
        return op, evidence

    # -- reading the ledger -------------------------------------------------- #

    def digest(self) -> str:
        return sha256_of(Path(self.ledger).read_bytes())

    def operation(self, operation_id):
        ledger = open_ledger(self.ledger)
        try:
            return ledger.operation(operation_id)
        finally:
            ledger.close()

    def generation(self) -> int:
        ledger = open_ledger(self.ledger)
        try:
            return ledger.generation
        finally:
            ledger.close()

    def events_of(self, operation_id, event_type):
        ledger = open_ledger(self.ledger)
        try:
            return [e for e in ledger.events if e["operation_id"] == operation_id and e["type"] == event_type]
        finally:
            ledger.close()

    def event_types(self):
        ledger = open_ledger(self.ledger)
        try:
            return [e["type"] for e in ledger.events]
        finally:
            ledger.close()

    def request(self):
        status = self.ok("ledger", "status", self.ledger)
        self.assertEqual([r["request_id"] for r in status["requests"]], ["req-1"])
        return status["generation"], status["requests"][0]

    def settle_and_resume(self, op):
        """What a recovered review still needs from the operator: its usage, then the resume.

        The recovery settles no usage, so the request also holds USAGE_UNKNOWN
        once the first dimension is entered and the other is still unknown;
        the resume names both holds back.
        """
        for dimension, amount in (("tokens", "12"), ("cost", "3")):
            self.ok("ledger", "settle", self.ledger, "--operation", op, "--dimension", dimension, "--amount",
                    amount, "--evidence", EVIDENCE)
        self.assertEqual(self.request()[1]["stop_reasons"], ["RECOVERY_REQUIRED", "USAGE_UNKNOWN"])
        self.resume("RECOVERY_REQUIRED", "USAGE_UNKNOWN")

    def resume(self, *reasons):
        argv = ["ledger", "resume", self.ledger, "--request", "req-1"]
        for reason in reasons:
            argv += ["--reason", reason]
        return self.ok(*argv, "--operator", OPERATOR, "--note", NOTE)


class RecoverOracle(RecoverCliCase):
    def test_recover_with_nothing_in_flight_rolls_over_and_holds_every_request(self):
        before = self.ok("ledger", "status", self.ledger)
        self.assertEqual((before["generation"], before["requests"][0]["stopped"]), (1, False))
        result = self.recover()
        self.assertEqual(result, {"command": "recover", "generation": 2, "operations": []})
        generation, request = self.request()
        self.assertEqual((generation, request["stopped"], request["stop_reasons"], request["attention"]),
                         (2, True, ["RECOVERY_REQUIRED"], []))
        # Until the operator resumes the request, a run is the ledger's refusal:
        # nothing is claimed and nothing is started.
        evidence = self.evidence_dir()
        self.once_refused("E_STOPPED", self.job(), evidence)
        self.assertEqual(os.listdir(evidence), [])
        # A second recovery rolls over again and still has nothing to act on.
        self.assertEqual(self.recover(), {"command": "recover", "generation": 3, "operations": []})
        self.assertEqual(self.request()[1]["stop_reasons"], ["RECOVERY_REQUIRED"])
        self.assertEqual(self.resume("RECOVERY_REQUIRED")["previous_reasons"], ["RECOVERY_REQUIRED"])
        result = self.once(self.job(), self.evidence_dir())
        self.assertEqual((result["outcome"]["state"], result["operation"]["generation"]), ("REVIEW_PENDING", 3))
        self.assertEqual(self.event_types().count("controller.fenced"), 2)

    def test_recover_cancels_a_claim_that_never_reached_a_handoff(self):
        reviewed = self.once(self.job(), self.evidence_dir())["operation_id"]
        stranded = self.stranded()
        _, request = self.request()
        self.assertEqual(request["snapshot"]["tokens"]["held"], 40)
        result = self.recover()
        self.assertEqual(result["generation"], 2)
        self.assertEqual(result["operations"], [{"operation_id": stranded, "action": "cancelled",
                                                 "state": "CANCELLED_NOT_STARTED"}])
        self.assertEqual(self.operation(stranded)["state"], "CANCELLED_NOT_STARTED")
        # The run the dead controller settled itself is not touched or reported.
        self.assertEqual(self.operation(reviewed)["state"], "REVIEW_PENDING")
        _, request = self.request()
        self.assertEqual((request["snapshot"]["tokens"]["held"], request["snapshot"]["tokens"]["known"],
                          request["attention"], request["stop_reasons"]), (0, 12, [], ["RECOVERY_REQUIRED"]))
        # Two stranded claims come back in claim order, and a second recovery
        # finds nothing left.
        self.resume("RECOVERY_REQUIRED")
        first, second = self.stranded(), self.stranded()
        self.assertEqual([(o["operation_id"], o["action"]) for o in self.recover()["operations"]],
                         [(first, "cancelled"), (second, "cancelled")])
        self.assertEqual(self.recover()["operations"], [])

    def test_a_handoff_never_decided_is_settled_from_its_records(self):
        op, evidence = self.undecided_handoff()
        expected = evidence_digest(str(evidence))
        result = self.recover()
        self.assertEqual(result["operations"], [{"operation_id": op, "action": "recovered",
                                                 "state": "REVIEW_PENDING"}])
        operation = self.operation(op)
        self.assertEqual((operation["state"], operation["owner"], operation["generation"]),
                         ("REVIEW_PENDING", "owner-2", 2))
        # Without --candidate the candidate is the records' own digest, exactly
        # as for ``once``; with it, the operator's value.
        recovered = self.events_of(op, "scheduler.recovered")
        self.assertEqual(len(recovered), 1)
        payload = recovered[0]["payload"]
        self.assertEqual((payload["outcome"], payload["state"], payload["owner"], payload["from_generation"],
                          payload["candidate"]), ("FINISHED", "REVIEW_PENDING", "owner-2", 1, expected))
        # The recovery settles no usage: the review is there, its cost is the operator's.
        self.ledger_refused("E_RESUME_USAGE_UNKNOWN", "ledger", "resume", self.ledger, "--request", "req-1",
                            "--reason", "RECOVERY_REQUIRED", "--operator", OPERATOR, "--note", NOTE)
        self.settle_and_resume(op)
        op, evidence = self.undecided_handoff()
        result = self.recover("--candidate", CANDIDATE)
        self.assertEqual(result["operations"], [{"operation_id": op, "action": "recovered",
                                                 "state": "REVIEW_PENDING"}])
        self.assertEqual(self.events_of(op, "scheduler.recovered")[0]["payload"]["candidate"], CANDIDATE)
        # Records that no longer describe the files are no evidence: START_UNKNOWN,
        # for the operator to decide.
        self.settle_and_resume(op)
        op, evidence = self.undecided_handoff()
        (evidence / "stdout.log").write_bytes(b"replaced after the run" + NL.encode())
        result = self.recover("--candidate", CANDIDATE)
        self.assertEqual(result["operations"], [{"operation_id": op, "action": "recovered",
                                                 "state": "START_UNKNOWN"}])
        self.assertEqual(self.request()[1]["attention"][0]["operation_id"], op)
        self.assertNotIn("candidate", self.events_of(op, "scheduler.recovered")[0]["payload"])

    def test_recover_refusals_and_usage_errors_change_nothing(self):
        before = self.digest()
        self.refused("E_LEDGER_MISSING", "run", "recover", str(self.work / "absent.db"), "--owner", "owner-2")
        # An owner the recovery cannot hand operations to is refused before the
        # rollover: the generation and every request stay as they were.
        self.refused("E_RECOVERY_INPUT", "run", "recover", self.ledger, "--owner", "")
        self.assertEqual((self.digest(), self.generation()), (before, 1))
        self.usage_error("run", "recover")
        self.usage_error("run", "recover", self.ledger)
        self.usage_error("run", "recover", self.ledger, "--owner", "o", "--candidate", "zz")
        self.usage_error("run", "recover", self.ledger, "--owner", "o", "--candidate", "C" * 64)
        self.usage_error("run", "recover", self.ledger, "--owner", "o", "--extra")
        self.assertEqual((self.digest(), self.generation()), (before, 1))
        code, out, err = self.smctl("run", "--help")
        self.assertEqual(code, 0)
        self.assertIn("recover", out)

    def test_once_refuses_an_environment_the_digest_refuses_before_any_claim(self):
        before = self.digest()
        evidence = self.evidence_dir()
        for name, environment in (("a name with =", dict(BASE_ENV, **{"BAD=NAME": "x"})),
                                  ("an empty name", dict(BASE_ENV, **{"": "x"})),
                                  ("a control character", dict(BASE_ENV, MARK="x" + chr(0) + "y"))):
            with self.subTest(case=name):
                error = self.once_refused("E_RUN_JOB", self.job(environment=environment), evidence)
                self.assertIn("environment", error["detail"])
                self.assertEqual(self.digest(), before, "a refused job changed the ledger")
                self.assertEqual(os.listdir(evidence), [])
        ledger = open_ledger(self.ledger)
        try:
            self.assertEqual(ledger.operation_ids(), ())
        finally:
            ledger.close()


class CrashToResumeCliOracle(RecoverCliCase):
    def test_a_killed_controller_is_recovered_resolved_resumed_and_runs_again(self):
        reviewed = self.once(self.job(), self.evidence_dir())["operation_id"]
        started, release = self.work / "started", self.work / "release"
        self.addCleanup(lambda: release.touch())
        evidence = self.evidence_dir()
        job = self.write_json("crash.json", self.job(WAIT_FOR_RELEASE, str(started), str(release)))
        controller = subprocess.Popen([PY, "-m", "sentinelmesh.cli", "run", "once", self.ledger, "--job", job,
                                       "--evidence", str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1"],
                                      cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: controller.poll() is None and controller.kill())
        deadline = time.monotonic() + 30
        while pid_in(started) is None and time.monotonic() < deadline and controller.poll() is None:
            time.sleep(0.01)
        child = pid_in(started)
        self.assertIsNotNone(child, "the supervised child never started: %r" % (controller.poll(),))
        # The controller dies mid-run: TerminateProcess on Windows, SIGKILL elsewhere.
        controller.kill()
        out, err = controller.communicate(timeout=60)
        self.assertNotEqual(controller.returncode, 0, err)
        self.assertEqual(out, "", "a killed controller reported an outcome")
        if os.name == "nt":
            # The Job Object ends the child with its controller, before any release.
            self.assertTrue(gone_within(child, 10.0), "the supervised child outlived its controller")
        release.touch()
        self.assertTrue(gone_within(child, 30.0), "the supervised child never exited")
        # The handoff is on disk, the observation is not: the run was never decided.
        self.assertEqual(sorted(os.listdir(evidence)), [HANDOFF_NAME, "stderr.log", "stdout.log"])
        _, request = self.request()
        self.assertEqual((request["stopped"], request["snapshot"]["tokens"]["held"]), (False, 40))

        # Another controller process recovers: the run is START_UNKNOWN, the
        # request is held, and the reviewed run is left alone.
        completed = self.real("run", "recover", self.ledger, "--owner", "owner-2")
        self.assertEqual((completed.returncode, completed.stderr, completed.stdout.count(NL)), (0, "", 1))
        report = json.loads(completed.stdout)
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(report["generation"], 2)
        self.assertEqual([(o["action"], o["state"]) for o in report["operations"]],
                         [("recovered", "START_UNKNOWN")])
        crashed = report["operations"][0]["operation_id"]
        self.assertNotEqual(crashed, reviewed)
        self.assertEqual(self.operation(reviewed)["state"], "REVIEW_PENDING")
        generation, request = self.request()
        self.assertEqual((generation, request["stopped"], request["stop_reasons"]), (2, True, ["RECOVERY_REQUIRED"]))
        self.assertEqual([(a["operation_id"], a["state"], a["reasons"]) for a in request["attention"]],
                         [(crashed, "START_UNKNOWN", ["start_unknown", "usage_unknown"])])
        self.once_refused("E_STOPPED", self.job(), self.evidence_dir(), owner="owner-2")
        self.ledger_refused("E_RESUME_UNKNOWN", "ledger", "resume", self.ledger, "--request", "req-1", "--reason",
                            "RECOVERY_REQUIRED", "--operator", OPERATOR, "--note", NOTE)

        # The operator enters what the run cost, one dimension at a time -- the
        # first entry leaves the other unknown, which the ledger records as one
        # more hold -- and abandons it: the bridge recorded a run descriptor, so
        # the run may not be called unstarted.
        self.ok("ledger", "settle", self.ledger, "--operation", crashed, "--dimension", "tokens", "--amount", "9",
                "--evidence", EVIDENCE)
        self.ok("ledger", "settle", self.ledger, "--operation", crashed, "--dimension", "cost", "--amount", "2",
                "--evidence", EVIDENCE)
        self.assertEqual(self.request()[1]["stop_reasons"], ["RECOVERY_REQUIRED", "USAGE_UNKNOWN"])
        self.ledger_refused("E_RESOLVE_CONFLICT", "ledger", "resolve", self.ledger, "--operation", crashed,
                            "--resolution", "NOT_STARTED", "--operator", OPERATOR, "--evidence", EVIDENCE,
                            "--note", NOTE)
        resolved = self.ok("ledger", "resolve", self.ledger, "--operation", crashed, "--resolution", "ABANDONED",
                           "--operator", OPERATOR, "--evidence", EVIDENCE, "--note", NOTE)
        self.assertEqual((resolved["operation"]["state"], resolved["operation"]["generation"]), ("ABANDONED", 2))
        self.ledger_refused("E_STOP_REASON_CHANGED", "ledger", "resume", self.ledger, "--request", "req-1",
                            "--reason", "RECOVERY_REQUIRED", "--operator", OPERATOR, "--note", NOTE)
        resumed = self.resume("RECOVERY_REQUIRED", "USAGE_UNKNOWN")
        self.assertEqual(resumed["previous_reasons"], ["RECOVERY_REQUIRED", "USAGE_UNKNOWN"])
        generation, request = self.request()
        self.assertEqual((request["stopped"], request["stop_reasons"], request["attention"]), (False, [], []))

        # The abandoned task is claimed again as a new attempt, in the new
        # generation, and runs to review through the product command.
        again = self.once(self.job(task_id=self.operation(crashed)["task_id"]), self.evidence_dir(), owner="owner-2")
        self.assertEqual((again["outcome"]["state"], again["operation"]["generation"], again["operation"]["owner"]),
                         ("REVIEW_PENDING", 2, "owner-2"))
        _, request = self.request()
        tokens = request["snapshot"]["tokens"]
        self.assertEqual((tokens["known"], tokens["held"], tokens["unknown_operations"]), (12 + 9 + 12, 0, 0))
        types = self.event_types()
        self.assertEqual([t for t in types if t in ("controller.fenced", "scheduler.unknown_resolved",
                                                    "request.resumed")],
                         ["controller.fenced", "scheduler.unknown_resolved", "request.resumed"])


if __name__ == "__main__":
    unittest.main()
