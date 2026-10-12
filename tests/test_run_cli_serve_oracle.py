"""Planner oracle for serve-loop (`.agent/design/serve-loop.md`, sections 3, 5 and 6).

``smctl run serve`` drains one request: it walks the request's tasks in seeding
order, claims each task that can be claimed with the task's own job file
(``<task_id>.json`` under ``--jobs``) and the derived idempotency key
``<job key>#<attempt>``, runs ``once``'s chain exactly once per claim with the
evidence directory ``<root>/<task_id>/<attempt>``, and ends when a whole pass
claimed nothing (``drained``), when ``--max-runs`` or ``--max-seconds`` is
reached before a claim, when a ``KeyboardInterrupt`` lands between runs
(``interrupted``) or when a refusal means the ledger must not move
(``refused``, exit 3, the refusal also on stderr).  Tasks that are done, out
of attempts, without a job, or refused as ``E_ACTIVE_TASK`` / ``E_TASK_DONE`` /
``E_ATTEMPTS`` are skipped with that reason.  stdout is always exactly one
report line.  The child processes are local ``python -c`` programs writing the
offline adapter's protocol; no provider CLI, no credentials, no network.

A replayed claim cannot be produced through the product commands (every
operation of a task pushes the derived attempt number past any key it used), so
that stop is pinned in process with a ledger proxy whose ``claim`` replays.
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
from unittest import mock

from sentinelmesh import cli, run_cli
from sentinelmesh.durable_ledger import open_ledger
from sentinelmesh.supervised_record import HANDOFF_NAME, OBSERVATION_NAME

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
CAPS = {"dispatches": 20, "tokens": 1000, "cost": 500}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
CANDIDATE, EVIDENCE = "c" * 64, "e" * 64
OPERATOR, NOTE = "ops-1", "read the candidate"
REPORT_KEYS = {"command", "request", "runs", "skipped", "stopped", "refusal"}
RUN_KEYS = {"task_id", "attempt", "operation_id", "state", "evidence_dir"}
REFUSAL_KEYS = {"error", "detail", "operation_id"}
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


def pid_in(path: Path):
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text else None


def pid_alive(pid: int) -> bool:
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
    handle = k32.OpenProcess(0x1000, False, pid)
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


class _Later:
    """An operation id read from the ledger after the command ran: the id serve claimed at ``index``."""

    def __init__(self, case, index):
        self.case, self.index = case, index

    def resolve(self):
        return self.case.operations()[self.index]["operation_id"]


class ServeCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="run-cli-serve-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.ledger = str(self.work / "ledger.db")
        self.jobs = self.work / "jobs"
        self.jobs.mkdir()
        self.evidence = self.work / "evidence"
        self.roots = 0
        self.init()

    # -- driving the CLI --------------------------------------------------- #

    def smctl(self, *argv):
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

    def usage_error(self, *argv):
        code, out, err = self.smctl(*argv)
        self.assertEqual((code, out), (2, ""), "smctl %s should be a usage error" % (argv,))
        self.assertNotEqual(err, "")

    def real(self, *argv, timeout=120):
        return subprocess.run([PY, "-m", "sentinelmesh.cli", *[str(a) for a in argv]], cwd=str(REPO_ROOT),
                              capture_output=True, text=True, timeout=timeout)

    # -- files --------------------------------------------------------------- #

    def write_json(self, path, value) -> str:
        Path(path).write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def spec(self, tasks=3, **overrides):
        spec = {"request_id": "req-1", "repo_id": "repo-1", "caps": dict(CAPS), "concurrency": 4,
                "tasks": [{"task_id": "task-%d" % n, "caps": dict(CAPS), "max_attempts": 2}
                          for n in range(1, tasks + 1)]}
        spec.update(overrides)
        return spec

    def init(self, **overrides):
        return self.ok("run", "init", self.ledger, "--spec",
                       self.write_json(self.work / "spec.json", self.spec(**overrides)))

    def job(self, task_id, code=None, *args, **overrides):
        job = {"request_id": "req-1", "task_id": task_id, "plan": 1, "idempotency_key": "idem-" + task_id,
               "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["src/%s.py" % task_id],
               "adapter_id": "offline", "role": "executor", "profile_id": "stub-profile",
               "command": [PY, "-c", emit(COMPLETE) if code is None else code, *args], "capabilities": [],
               "cwd": str(self.cwd), "environment": dict(BASE_ENV), "allowed_secret_names": [],
               "timeout_seconds": 10, "usage_dimensions": ["tokens", "cost"]}
        job.update(overrides)
        return job

    def write_jobs(self, *jobs, names=None):
        """Write each job as ``<task_id>.json`` (or under the given names) in the jobs directory."""
        for index, job in enumerate(jobs):
            name = (names or [None] * len(jobs))[index] or (job["task_id"] + ".json")
            self.write_json(self.jobs / name, job)

    def all_jobs(self):
        self.write_jobs(*(self.job("task-%d" % n) for n in range(1, 4)))

    def root(self) -> str:
        self.roots += 1
        return str(self.evidence / ("root-%d" % self.roots))

    def made_root(self) -> str:
        path = Path(self.root())
        path.mkdir(parents=True)
        return str(path)

    def once(self, job, owner="owner-1"):
        """``smctl run once`` with ``job`` as it is, in its own fresh evidence directory."""
        self.roots += 1
        return self.ok("run", "once", self.ledger, "--job", self.write_json(self.work / ("once-%d.json" % self.roots), job),
                       "--evidence", self.made_root(), "--worktree-id", "wt-1", "--owner", owner,
                       "--candidate", CANDIDATE)

    # -- serve --------------------------------------------------------------- #

    def serve_argv(self, *extra, root=None, owner="owner-1", request="req-1", candidate=CANDIDATE):
        return ("run", "serve", self.ledger, "--request", request, "--jobs", str(self.jobs), "--evidence",
                root or self.root(), "--worktree-id", "wt-1", "--owner", owner,
                *(("--candidate", candidate) if candidate else ()), *extra)

    def serve(self, *extra, **kw):
        report = self.ok(*self.serve_argv(*extra, **kw))
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual((report["command"], report["request"], report["refusal"]), ("serve", "req-1", None))
        self.assertIn(report["stopped"], ("drained", "max_runs", "max_seconds", "interrupted"))
        for run in report["runs"]:
            self.assertEqual(set(run), RUN_KEYS)
        return report

    def serve_refused(self, expected, *extra, operation_id=None, **kw):
        """A refusal: exit 3, the report on stdout with ``stopped: refused``, the refusal alone on stderr."""
        code, out, err = self.smctl(*self.serve_argv(*extra, **kw))
        self.assertEqual(code, 3, (out, err))
        self.assertTrue(out.endswith(NL) and out.count(NL) == 1 and err.endswith(NL) and err.count(NL) == 1,
                        (out, err))
        report, error = json.loads(out), json.loads(err)
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(set(error), REFUSAL_KEYS)
        self.assertEqual((report["stopped"], report["refusal"]), ("refused", error))
        if isinstance(operation_id, _Later):
            operation_id = operation_id.resolve()
        self.assertEqual((error["error"], error["operation_id"]), (expected, operation_id), error)
        self.assertIn(type(error["detail"]), (str, type(None)))
        return report

    def verdict(self, operation_id, decision="--accept", candidate=CANDIDATE):
        return self.ok("ledger", "verdict", self.ledger, "--operation", operation_id, "--candidate", candidate,
                       decision, "--evidence", EVIDENCE, "--operator", OPERATOR, "--note", NOTE)

    # -- the ledger ---------------------------------------------------------- #

    def operations(self):
        ledger = open_ledger(self.ledger)
        try:
            return [ledger.operation(op) for op in ledger.operation_ids()]
        finally:
            ledger.close()

    def digest(self) -> str:
        return hashlib.sha256(Path(self.ledger).read_bytes()).hexdigest()


class ServeOracle(ServeCase):
    def test_serve_drains_the_request_in_seeding_order_and_a_second_pass_claims_nothing(self):
        self.all_jobs()
        root = self.root()
        report = self.serve(root=root)
        self.assertEqual((report["stopped"], report["skipped"]), ("drained", []))
        self.assertEqual([(r["task_id"], r["attempt"], r["state"]) for r in report["runs"]],
                         [("task-1", 1, "REVIEW_PENDING"), ("task-2", 1, "REVIEW_PENDING"),
                          ("task-3", 1, "REVIEW_PENDING")])
        self.assertEqual([r["evidence_dir"] for r in report["runs"]],
                         [os.path.join(root, "task-%d" % n, "1") for n in range(1, 4)])
        for run in report["runs"]:
            self.assertEqual(sorted(os.listdir(run["evidence_dir"])),
                             [HANDOFF_NAME, OBSERVATION_NAME, "stderr.log", "stdout.log"])
        operations = self.operations()
        self.assertEqual([(o["task_id"], o["state"], o["owner"], o["generation"]) for o in operations],
                         [("task-%d" % n, "REVIEW_PENDING", "owner-1", 1) for n in range(1, 4)])
        self.assertEqual([r["operation_id"] for r in report["runs"]], [o["operation_id"] for o in operations])
        # The second serve claims nothing: every task waits for its verdict.
        again = self.serve()
        self.assertEqual((again["runs"], again["stopped"]), ([], "drained"))
        self.assertEqual(again["skipped"], [{"task_id": "task-%d" % n, "reason": "E_ACTIVE_TASK"} for n in range(1, 4)])
        self.assertEqual(len(self.operations()), 3)

    def test_the_attempt_number_derives_the_key_and_the_evidence_dir_and_verdicts_steer_the_next_pass(self):
        self.all_jobs()
        first = self.serve()
        ops = {r["task_id"]: r["operation_id"] for r in first["runs"]}
        self.verdict(ops["task-1"], "--accept")
        self.verdict(ops["task-2"], "--reject")
        root = self.root()
        second = self.serve(root=root)
        self.assertEqual([(r["task_id"], r["attempt"], r["state"], r["evidence_dir"]) for r in second["runs"]],
                         [("task-2", 2, "REVIEW_PENDING", os.path.join(root, "task-2", "2"))])
        self.assertEqual(second["skipped"], [{"task_id": "task-1", "reason": "done"},
                                             {"task_id": "task-3", "reason": "E_ACTIVE_TASK"}])
        self.assertEqual(second["stopped"], "drained")
        # The second attempt is a new operation under a new key: nothing was replayed.
        self.assertEqual([o["task_id"] for o in self.operations()], ["task-1", "task-2", "task-3", "task-2"])
        self.verdict(second["runs"][0]["operation_id"], "--reject")
        third = self.serve()
        self.assertEqual((third["runs"], third["stopped"]), ([], "drained"))
        self.assertEqual(third["skipped"], [{"task_id": "task-1", "reason": "done"},
                                            {"task_id": "task-2", "reason": "attempts"},
                                            {"task_id": "task-3", "reason": "E_ACTIVE_TASK"}])
        self.assertEqual(len(self.operations()), 4)

    def test_a_task_without_a_job_is_skipped_and_the_others_run(self):
        self.write_jobs(self.job("task-1"), self.job("task-3"))
        report = self.serve(candidate=None)
        self.assertEqual([r["task_id"] for r in report["runs"]], ["task-1", "task-3"])
        self.assertEqual((report["skipped"], report["stopped"]), ([{"task_id": "task-2", "reason": "no_job"}], "drained"))
        # Without --candidate the candidate is the evidence records' own digest, as for once.
        from sentinelmesh.supervised_operation import evidence_digest
        run = report["runs"][0]
        self.verdict(run["operation_id"], "--accept", candidate=evidence_digest(run["evidence_dir"]))
        self.assertEqual(self.operations()[0]["state"], "ACCEPTED")

    def test_serve_refuses_bad_jobs_before_opening_the_ledger_and_unknown_tasks_before_any_claim(self):
        before = self.digest()
        shutil.rmtree(self.jobs)
        self.serve_refused("E_RUN_JOBS")
        self.jobs.mkdir()
        self.serve_refused("E_RUN_JOBS")  # no job file at all
        self.write_jobs(self.job("task-1"), self.job("task-2", request_id="req-2"))
        self.assertIn("task-2.json", self.serve_refused("E_RUN_JOBS")["refusal"]["detail"])
        self.write_jobs(self.job("task-1"), self.job("task-2"), names=[None, "task-9.json"])
        self.serve_refused("E_RUN_JOBS")  # task_id is not the file name
        bad = self.job("task-2")
        del bad["cwd"]
        shutil.rmtree(self.jobs)
        self.jobs.mkdir()
        self.write_jobs(self.job("task-1"), bad)
        self.assertIn("task-2.json", self.serve_refused("E_RUN_JOBS")["refusal"]["detail"])
        shutil.rmtree(self.jobs)
        self.jobs.mkdir()
        self.write_jobs(self.job("task-1"), self.job("task-9"))
        report = self.serve_refused("E_RUN_JOBS")
        self.assertIn("task-9", report["refusal"]["detail"])
        self.assertEqual((report["runs"], report["skipped"]), ([], []))
        shutil.rmtree(self.jobs)
        self.jobs.mkdir()
        self.write_jobs(self.job("task-1", request_id="req-9"))
        self.serve_refused("E_REQUEST_UNKNOWN", request="req-9")
        self.assertEqual((self.digest(), self.operations()), (before, []))

    def test_refusals_that_mean_the_ledger_must_not_move_stop_the_loop(self):
        self.all_jobs()
        self.ok("ledger", "stop", self.ledger, "--request", "req-1", "--reason", "OPERATOR_STOP")
        report = self.serve_refused("E_STOPPED")
        self.assertEqual((report["runs"], report["skipped"], self.operations()), ([], [], []))
        self.ok("ledger", "resume", self.ledger, "--request", "req-1", "--reason", "OPERATOR_STOP",
                "--operator", OPERATOR, "--note", NOTE)
        # The idempotency index is per request: once used the key serve derives for task-2's first
        # attempt on task-1, so serve's claim for task-2 is a conflict (the intent names another task).
        self.verdict(self.once(self.job("task-1", idempotency_key="idem-task-2#1"))["operation_id"], "--reject")
        report = self.serve_refused("E_IDEMPOTENCY_CONFLICT")
        self.assertEqual([(r["task_id"], r["attempt"], r["state"]) for r in report["runs"]],
                         [("task-1", 2, "REVIEW_PENDING")])
        self.assertEqual(report["skipped"], [])
        self.assertEqual([o["task_id"] for o in self.operations()], ["task-1", "task-1"])

    def test_a_replayed_claim_stops_the_loop_and_names_the_operation(self):
        self.all_jobs()
        real_open = run_cli.open_ledger
        calls = []

        class Replaying:
            """A ledger whose second claim replays the first."""

            def __init__(self, ledger):
                self._ledger = ledger
                self._first = None

            def __getattr__(self, name):
                return getattr(self._ledger, name)

            def claim(self, *args, **kwargs):
                calls.append(args[1])
                if self._first is not None:
                    return {"operation_id": self._first["operation_id"], "state": "REVIEW_PENDING", "replayed": True}
                self._first = self._ledger.claim(*args, **kwargs)
                return self._first

        with mock.patch.object(run_cli, "open_ledger", lambda path: Replaying(real_open(path))):
            code, out, err = self.smctl(*self.serve_argv())
        report, error = json.loads(out), json.loads(err)
        self.assertEqual((code, calls), (3, ["task-1", "task-2"]))
        first = self.operations()[0]["operation_id"]
        self.assertEqual((error["error"], error["operation_id"]), ("E_RUN_CLAIM_REPLAYED", first))
        self.assertEqual((report["stopped"], report["refusal"]), ("refused", error))
        self.assertEqual([(r["task_id"], r["state"]) for r in report["runs"]], [("task-1", "REVIEW_PENDING")])
        self.assertEqual(len(self.operations()), 1)

    def test_a_refusal_after_a_claim_names_the_operation_and_stops_with_it_reserved(self):
        self.write_jobs(self.job("task-1"), self.job("task-2", usage_dimensions=["tokens", "gpu"]), self.job("task-3"))
        report = self.serve_refused("E_ADAPTER_SCHEMA", operation_id=self.operations_after(1))
        self.assertEqual([(r["task_id"], r["state"]) for r in report["runs"]], [("task-1", "REVIEW_PENDING")])
        self.assertEqual([(o["task_id"], o["state"]) for o in self.operations()],
                         [("task-1", "REVIEW_PENDING"), ("task-2", "RESERVED")])

    def operations_after(self, index):
        """The operation id the next serve will leave at ``index``: resolved lazily by serve_refused's caller."""
        return _Later(self, index)

    def test_a_run_the_runtime_refuses_is_an_outcome_the_loop_goes_past(self):
        self.write_jobs(self.job("task-1"), self.job("task-2", cwd="tree"), self.job("task-3"))
        report = self.serve()
        self.assertEqual([(r["task_id"], r["attempt"], r["state"]) for r in report["runs"]],
                         [("task-1", 1, "REVIEW_PENDING"), ("task-2", 1, "NOT_STARTED"), ("task-3", 1, "REVIEW_PENDING")])
        self.assertEqual((report["stopped"], report["skipped"]), ("drained", []))
        self.assertEqual(os.listdir(report["runs"][1]["evidence_dir"]), [])
        # A run that never started used an attempt, and this call does not retry it: the next call may.
        again = self.serve()
        self.assertEqual([(r["task_id"], r["attempt"], r["state"]) for r in again["runs"]], [("task-2", 2, "NOT_STARTED")])

    def test_max_runs_and_max_seconds_are_checked_before_a_claim_never_inside_a_run(self):
        self.all_jobs()
        report = self.serve("--max-runs", "2")
        self.assertEqual(([r["task_id"] for r in report["runs"]], report["skipped"], report["stopped"]),
                         (["task-1", "task-2"], [], "max_runs"))
        report = self.serve("--max-seconds", "0")
        self.assertEqual((report["runs"], report["skipped"], report["stopped"]), ([], [], "max_seconds"))
        # A fresh ledger whose first task takes longer than the budget: the run completes
        # (the budget never reaches into a run), and the next claim is not made.
        self.ledger = str(self.work / "slow.db")
        self.init()
        self.write_jobs(self.job("task-1", "import time; time.sleep(1.3)" + NL + emit(COMPLETE)), self.job("task-2"),
                        self.job("task-3"))
        started = time.monotonic()
        report = self.serve("--max-seconds", "1")
        self.assertEqual(([(r["task_id"], r["state"]) for r in report["runs"]], report["skipped"], report["stopped"]),
                         ([("task-1", "REVIEW_PENDING")], [], "max_seconds"))
        self.assertGreaterEqual(time.monotonic() - started, 1.2)
        self.usage_error(*self.serve_argv("--max-runs", "0"))
        self.usage_error(*self.serve_argv("--max-seconds", "-1"))
        self.usage_error("run", "serve", self.ledger, "--jobs", str(self.jobs))

    def test_the_evidence_root_is_laid_out_per_task_and_attempt_and_never_reused(self):
        self.all_jobs()
        root = self.root()
        os.makedirs(os.path.join(root, "task-2", "1"))
        report = self.serve_refused("E_RUN_EVIDENCE", root=root)
        self.assertEqual([r["task_id"] for r in report["runs"]], ["task-1"])
        self.assertEqual(len(self.operations()), 1)
        self.assertIn(os.path.join(root, "task-2", "1"), report["refusal"]["detail"])

    def test_a_keyboard_interrupt_between_runs_ends_the_loop_as_interrupted(self):
        self.all_jobs()
        ticks = [0.0, 0.0, 0.0]

        def clock():
            if not ticks:
                raise KeyboardInterrupt
            return ticks.pop(0)
        with mock.patch.object(run_cli, "_clock", clock):
            try:
                report = self.serve("--max-seconds", "100")
            except KeyboardInterrupt:  # an interrupt between runs must be absorbed, not escape
                self.fail("the KeyboardInterrupt between runs escaped serve")
        self.assertEqual(([r["task_id"] for r in report["runs"]], report["stopped"]),
                         (["task-1", "task-2"], "interrupted"))
        self.assertEqual(len(self.operations()), 2)

    def test_serve_runs_through_a_real_smctl_process(self):
        self.all_jobs()
        completed = self.real(*self.serve_argv())
        self.assertEqual((completed.returncode, completed.stderr, completed.stdout.count(NL)), (0, "", 1))
        report = json.loads(completed.stdout)
        self.assertEqual(([r["task_id"] for r in report["runs"]], report["stopped"]),
                         (["task-1", "task-2", "task-3"], "drained"))

    def test_a_killed_serve_leaves_exactly_one_undecided_operation_for_recover(self):
        started, release = self.work / "started", self.work / "release"
        self.addCleanup(lambda: release.touch())
        self.write_jobs(self.job("task-1"), self.job("task-2", WAIT_FOR_RELEASE, str(started), str(release)),
                        self.job("task-3"))
        controller = subprocess.Popen([PY, "-m", "sentinelmesh.cli", *[str(a) for a in self.serve_argv()]],
                                      cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: controller.poll() is None and controller.kill())
        deadline = time.monotonic() + 30
        while pid_in(started) is None and time.monotonic() < deadline and controller.poll() is None:
            time.sleep(0.01)
        child = pid_in(started)
        self.assertIsNotNone(child, "the second task's child never started: %r" % (controller.poll(),))
        controller.kill()
        out, err = controller.communicate(timeout=60)
        self.assertNotEqual(controller.returncode, 0, err)
        self.assertEqual(out, "", "a killed serve reported")
        if os.name == "nt":
            self.assertTrue(gone_within(child, 10.0), "the supervised child outlived its controller")
        release.touch()
        self.assertTrue(gone_within(child, 30.0))
        self.assertEqual([(o["task_id"], o["state"]) for o in self.operations()][:1], [("task-1", "REVIEW_PENDING")])
        self.assertEqual(len(self.operations()), 2)
        completed = self.real("run", "recover", self.ledger, "--owner", "owner-2")
        self.assertEqual((completed.returncode, completed.stderr), (0, ""))
        recovered = json.loads(completed.stdout)
        self.assertEqual([(o["action"], o["state"]) for o in recovered["operations"]],
                         [("recovered", "START_UNKNOWN")])
        self.assertEqual([(o["task_id"], o["state"]) for o in self.operations()],
                         [("task-1", "REVIEW_PENDING"), ("task-2", "START_UNKNOWN")])
        self.serve_refused("E_STOPPED")


if __name__ == "__main__":
    unittest.main()
