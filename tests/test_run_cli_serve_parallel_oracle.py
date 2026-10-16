"""Planner oracle for serve-parallel (`.agent/design/serve-parallel.md`, sections 3, 5 and 6).

``smctl run serve --parallel N`` walks a request exactly as ``serve`` without it
does -- the same checks, skips, derived keys, evidence directories and stops, the
claims made one at a time by the main thread in seeding order -- but hands each
claimed run to a thread of its own and goes on claiming while fewer than N runs
are in flight.  A claim refused with ``E_CONCURRENCY``, ``E_RESOURCE_BUSY`` or
``E_BUDGET_INSUFFICIENT`` while runs of this call are in flight waits for one of
them to end and claims the same task again; with none in flight it stops the walk
as it does without ``--parallel``.  Every stop -- a refusal, ``--max-runs``,
``--max-seconds``, the first Ctrl-C -- ends the claims and waits for the runs in
flight before the report, which lists the runs in claim order and carries
``"parallel": N``; every refusal is one stderr line, in claim order, the first
being the report's own.  A second Ctrl-C is not absorbed: the runs still in
flight are left to ``smctl run recover``, as a killed controller leaves them.
Two jobs whose working directories are one directory must share a resource key.
Without ``--parallel`` nothing changes: `tests/test_run_cli_serve_oracle.py`
still pins that walk.

The children are local ``python -c`` programs writing the offline adapter's
protocol and leaving marker files that show which runs overlapped.  Ctrl-C is
delivered in process with ``_thread.interrupt_main()``.  No provider CLI, no
credentials, no network.
"""
from __future__ import annotations

import _thread
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh import cli, run_cli
from sentinelmesh.adapter_protocol import AdapterError
from sentinelmesh.scheduler import ContractError
from sentinelmesh.durable_ledger import LedgerError, open_ledger
from sentinelmesh.supervised_operation import evidence_digest
from sentinelmesh.supervised_record import HANDOFF_NAME, OBSERVATION_NAME

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
CAPS = {"dispatches": 20, "tokens": 1000, "cost": 500}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
CANDIDATE, EVIDENCE = "c" * 64, "e" * 64
OPERATOR, NOTE = "ops-1", "read the candidate"
REPORT_KEYS = {"command", "request", "runs", "skipped", "stopped", "refusal", "parallel"}
RUN_KEYS = {"task_id", "attempt", "operation_id", "state", "evidence_dir"}
REFUSAL_KEYS = {"error", "detail", "operation_id"}
STILL_ACTIVE = 259


def ev(seq, typ, payload=None):
    return {"seq": seq, "event_id": "e%d" % seq, "type": typ, "source": "offline",
            "payload": {} if payload is None else payload}


COMPLETE = [ev(0, "run_started"), ev(1, "assistant"), ev(2, "usage", {"tokens": 12, "cost": 3}),
            ev(3, "run_finished")]


def emit(events):
    """Python code that writes each event as one LF-terminated JSON line, byte for byte."""
    return ("import json, sys" + NL
            + "for event in json.loads(%r):" % json.dumps(events) + NL
            + "    sys.stdout.buffer.write((json.dumps(event) + chr(10)).encode())" + NL
            + "sys.stdout.buffer.flush()" + NL)


# A child that leaves marker files in the directory ``marks`` and then writes a
# complete offline run:
#   <me>.saw    "1" when <watch>.end already existed as it started, else "0";
#   <me>.start  its pid, written whole (renamed into place);
#   <me>.met    "1" when <meet>.start appeared within 20 seconds, else "0";
#   <me>.end    written after its pause, just before the run is written.
# ``-`` means no watch, or no meet.
STEP = ("import os, sys, time" + NL
        + "marks, me, meet, pause, watch = sys.argv[1:6]" + NL
        + "def put(name, text):" + NL
        + "    tmp = os.path.join(marks, name + '.tmp')" + NL
        + "    with open(tmp, 'w') as handle:" + NL
        + "        handle.write(text)" + NL
        + "    os.replace(tmp, os.path.join(marks, name))" + NL
        + "put(me + '.saw', '1' if watch != '-' and os.path.exists(os.path.join(marks, watch + '.end')) else '0')" + NL
        + "put(me + '.start', str(os.getpid()))" + NL
        + "if meet != '-':" + NL
        + "    deadline = time.monotonic() + 20" + NL
        + "    while not os.path.exists(os.path.join(marks, meet + '.start')) and time.monotonic() < deadline:" + NL
        + "        time.sleep(0.01)" + NL
        + "    put(me + '.met', '1' if os.path.exists(os.path.join(marks, meet + '.start')) else '0')" + NL
        + "time.sleep(float(pause))" + NL
        + "put(me + '.end', str(time.time()))" + NL) + emit(COMPLETE)

# A child that writes its pid to ``started`` (renamed into place) and waits, at
# most 30 seconds, for ``release`` to exist; then it writes a complete run.
HOLD = ("import os, sys, time" + NL
        + "started, release = sys.argv[1:3]" + NL
        + "with open(started + '.tmp', 'w') as handle:" + NL
        + "    handle.write(str(os.getpid()))" + NL
        + "os.replace(started + '.tmp', started)" + NL
        + "deadline = time.monotonic() + 30" + NL
        + "while not os.path.exists(release) and time.monotonic() < deadline:" + NL
        + "    time.sleep(0.01)" + NL) + emit(COMPLETE)


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


def wait_for(paths, seconds=30.0) -> bool:
    deadline = time.monotonic() + seconds
    while not all(Path(p).exists() for p in paths):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


class ParallelCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="run-cli-serve-parallel-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.ledger = str(self.work / "ledger.db")
        self.jobs = self.work / "jobs"
        self.jobs.mkdir()
        self.evidence = self.work / "evidence"
        self.marks = self.work / "marks"
        self.marks.mkdir()
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

    def spec(self, tasks=3, task_caps=None, **overrides):
        spec = {"request_id": "req-1", "repo_id": "repo-1", "caps": dict(CAPS), "concurrency": 4,
                "tasks": [{"task_id": "task-%d" % n, "caps": dict(task_caps or CAPS), "max_attempts": 2}
                          for n in range(1, tasks + 1)]}
        spec.update(overrides)
        return spec

    def init(self, **overrides):
        return self.ok("run", "init", self.ledger, "--spec",
                       self.write_json(self.work / "spec.json", self.spec(**overrides)))

    def tree(self, name) -> str:
        """A working directory of its own for ``name``: parallel jobs must not share one by accident."""
        path = self.work / ("tree-" + name)
        path.mkdir(exist_ok=True)
        return str(path)

    def job(self, task_id, code=None, *args, **overrides):
        job = {"request_id": "req-1", "task_id": task_id, "plan": 1, "idempotency_key": "idem-" + task_id,
               "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["src/%s.py" % task_id],
               "adapter_id": "offline", "role": "executor", "profile_id": "stub-profile",
               "command": [PY, "-c", emit(COMPLETE) if code is None else code, *args], "capabilities": [],
               "cwd": self.tree(task_id), "environment": dict(BASE_ENV), "allowed_secret_names": [],
               "timeout_seconds": 30, "usage_dimensions": ["tokens", "cost"]}
        job.update(overrides)
        return job

    def step(self, task_id, meet="-", pause=0.0, watch="-", **overrides):
        return self.job(task_id, STEP, str(self.marks), task_id, meet, str(pause), watch, **overrides)

    def hold(self, task_id, **overrides):
        return self.job(task_id, HOLD, str(self.marks / (task_id + ".start")), str(self.marks / "release"),
                        **overrides)

    def mark(self, name):
        path = self.marks / name
        return path.read_text() if path.exists() else None

    def write_jobs(self, *jobs):
        for job in jobs:
            self.write_json(self.jobs / (job["task_id"] + ".json"), job)

    def reset_jobs(self, *jobs):
        shutil.rmtree(self.jobs)
        self.jobs.mkdir()
        self.write_jobs(*jobs)

    def root(self) -> str:
        self.roots += 1
        return str(self.evidence / ("root-%d" % self.roots))

    # -- serve --------------------------------------------------------------- #

    def serve_argv(self, *extra, root=None, owner="owner-1", request="req-1", candidate=CANDIDATE, parallel=2):
        return ("run", "serve", self.ledger, "--request", request, "--jobs", str(self.jobs), "--evidence",
                root or self.root(), "--worktree-id", "wt-1", "--owner", owner,
                *(("--candidate", candidate) if candidate else ()),
                *(("--parallel", str(parallel)) if parallel is not None else ()), *extra)

    def check_report(self, report, parallel, request="req-1"):
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual((report["command"], report["request"], report["parallel"]), ("serve", request, parallel))
        for run in report["runs"]:
            self.assertEqual(set(run), RUN_KEYS)

    def serve(self, *extra, parallel=2, **kw):
        report = self.ok(*self.serve_argv(*extra, parallel=parallel, **kw))
        self.check_report(report, parallel)
        self.assertIsNone(report["refusal"])
        self.assertIn(report["stopped"], ("drained", "max_runs", "max_seconds", "interrupted"))
        return report

    def serve_refused(self, *extra, lines=1, parallel=2, **kw):
        """A refusal: exit 3, the report with ``stopped: refused``, one stderr line per refusal."""
        code, out, err = self.smctl(*self.serve_argv(*extra, parallel=parallel, **kw))
        self.assertEqual(code, 3, (out, err))
        self.assertTrue(out.endswith(NL) and out.count(NL) == 1, out)
        self.assertTrue(err.endswith(NL) and err.count(NL) == lines, err)
        report = json.loads(out)
        errors = [json.loads(line) for line in err.splitlines()]
        self.check_report(report, parallel, kw.get("request", "req-1"))
        for error in errors:
            self.assertEqual(set(error), REFUSAL_KEYS)
        self.assertEqual((report["stopped"], report["refusal"]), ("refused", errors[0]))
        return report, errors

    def verdict(self, operation_id, decision="--accept", candidate=CANDIDATE):
        return self.ok("ledger", "verdict", self.ledger, "--operation", operation_id, "--candidate", candidate,
                       decision, "--evidence", EVIDENCE, "--operator", OPERATOR, "--note", NOTE)

    def interrupt_later(self, ready, times=1, gap=0.5, release_after=None):
        """From another thread: once every path in ``ready`` exists, interrupt the main thread
        ``times`` times, ``gap`` seconds apart; then, ``release_after`` seconds later, let the
        HOLD children go (``None``: the test releases them itself)."""
        def body():
            wait_for(ready)
            for n in range(times):
                if n:
                    time.sleep(gap)
                _thread.interrupt_main()
            if release_after is not None:
                time.sleep(release_after)
                (self.marks / "release").touch()
        helper = threading.Thread(target=body, daemon=True)
        helper.start()
        self.addCleanup(lambda: (self.marks / "release").touch() if self.marks.exists() else None)
        return helper

    # -- the ledger ---------------------------------------------------------- #

    def operations(self):
        ledger = open_ledger(self.ledger)
        try:
            return [ledger.operation(op) for op in ledger.operation_ids()]
        finally:
            ledger.close()

    def digest(self) -> str:
        return hashlib.sha256(Path(self.ledger).read_bytes()).hexdigest()


class ParallelOracle(ParallelCase):
    # -- overlap, width and order ------------------------------------------- #

    def test_runs_overlap_through_a_real_smctl_process_and_the_report_keeps_the_claim_order(self):
        self.ledger = str(self.work / "two.db")
        self.init(tasks=2)
        self.write_jobs(self.step("task-1", meet="task-2"), self.step("task-2", meet="task-1"))
        root = self.root()
        completed = self.real(*self.serve_argv(root=root))
        self.assertEqual((completed.returncode, completed.stderr, completed.stdout.count(NL)), (0, "", 1),
                         completed.stderr)
        report = json.loads(completed.stdout)
        self.check_report(report, 2)
        self.assertEqual((report["stopped"], report["skipped"], report["refusal"]), ("drained", [], None))
        self.assertEqual([(r["task_id"], r["attempt"], r["state"], r["evidence_dir"]) for r in report["runs"]],
                         [("task-%d" % n, 1, "REVIEW_PENDING", os.path.join(root, "task-%d" % n, "1"))
                          for n in (1, 2)])
        # Each child saw the other one start: the two runs were in flight at the same time.
        self.assertEqual((self.mark("task-1.met"), self.mark("task-2.met")), ("1", "1"))
        for run in report["runs"]:
            self.assertEqual(sorted(os.listdir(run["evidence_dir"])),
                             [HANDOFF_NAME, OBSERVATION_NAME, "stderr.log", "stdout.log"])
        operations = self.operations()
        self.assertEqual([(o["task_id"], o["state"], o["owner"], o["generation"]) for o in operations],
                         [("task-1", "REVIEW_PENDING", "owner-1", 1), ("task-2", "REVIEW_PENDING", "owner-1", 1)])
        self.assertEqual([r["operation_id"] for r in report["runs"]], [o["operation_id"] for o in operations])

    def test_the_width_caps_the_runs_in_flight_and_runs_are_reported_in_claim_order(self):
        # task-2 outlives task-1, and task-3 (no pause) ends before task-2: the report is still
        # in claim order, and task-3 started only after task-1 ended and freed a slot.
        self.write_jobs(self.step("task-1", meet="task-2", pause=0.3), self.step("task-2", meet="task-1", pause=1.5),
                        self.step("task-3", watch="task-1"))
        report = self.serve()
        self.assertEqual(([(r["task_id"], r["state"]) for r in report["runs"]], report["stopped"]),
                         ([("task-1", "REVIEW_PENDING"), ("task-2", "REVIEW_PENDING"), ("task-3", "REVIEW_PENDING")],
                          "drained"))
        self.assertEqual((self.mark("task-1.met"), self.mark("task-2.met"), self.mark("task-3.saw")), ("1", "1", "1"))
        self.assertLess(float(self.mark("task-3.end")), float(self.mark("task-2.end")))

    def test_parallel_one_runs_one_task_at_a_time_and_reports_its_width(self):
        self.write_jobs(self.step("task-1", pause=0.4), self.step("task-2", watch="task-1"))
        report = self.serve(parallel=1)
        self.assertEqual(([r["task_id"] for r in report["runs"]], report["skipped"], report["stopped"]),
                         (["task-1", "task-2"], [{"task_id": "task-3", "reason": "no_job"}], "drained"))
        self.assertEqual(self.mark("task-2.saw"), "1")

    # -- waiting out what this call's own runs hold ------------------------- #

    def test_a_request_concurrency_below_the_width_makes_the_walk_wait(self):
        self.ledger = str(self.work / "narrow.db")
        self.init(concurrency=1)
        self.write_jobs(self.step("task-1", pause=0.5), self.step("task-2", watch="task-1"))
        report = self.serve()
        self.assertEqual(([r["task_id"] for r in report["runs"]], report["stopped"]), (["task-1", "task-2"], "drained"))
        self.assertEqual(self.mark("task-2.saw"), "1")

    def test_a_shared_resource_orders_its_runs_and_the_tasks_after_it_wait_their_turn(self):
        # task-1 and task-2 share a resource (and so may share a directory); task-3 is free but
        # comes after task-2 in seeding order, so it is claimed only after task-2 is.
        shared = self.tree("shared")
        self.write_jobs(self.step("task-1", pause=0.5, cwd=shared, resources=["wt-a", "src/task-1.py"]),
                        self.step("task-2", watch="task-1", cwd=shared, resources=["src/task-2.py", "wt-a"]),
                        self.step("task-3", watch="task-1"))
        report = self.serve()
        self.assertEqual(([r["task_id"] for r in report["runs"]], report["skipped"], report["stopped"]),
                         (["task-1", "task-2", "task-3"], [], "drained"))
        self.assertEqual((self.mark("task-2.saw"), self.mark("task-3.saw")), ("1", "1"))

    def test_a_budget_held_by_runs_in_flight_is_waited_out(self):
        # The request may expose 100 tokens; each claim reserves 40 and a run uses 12.  task-3's
        # claim does not fit while task-1 and task-2 are in flight, and fits once task-1 is settled.
        self.ledger = str(self.work / "budget.db")
        self.init(caps={"dispatches": 20, "tokens": 100, "cost": 500}, task_caps={"dispatches": 20, "tokens": 100, "cost": 500})
        self.write_jobs(self.step("task-1", pause=0.4), self.step("task-2", pause=1.5),
                        self.step("task-3", watch="task-1"))
        report = self.serve(parallel=3)
        self.assertEqual(([r["task_id"] for r in report["runs"]], report["stopped"]),
                         (["task-1", "task-2", "task-3"], "drained"))
        self.assertEqual(self.mark("task-3.saw"), "1")

    def real_refused(self):
        """``serve --parallel 2`` as a real process with a one-minute timeout: a walk that waited
        for a run of its own that does not exist fails by the timeout instead of hanging the suite."""
        completed = self.real(*self.serve_argv(), timeout=60)
        self.assertEqual((completed.returncode, completed.stdout.count(NL), completed.stderr.count(NL)), (3, 1, 1),
                         completed.stderr)
        report, error = json.loads(completed.stdout), json.loads(completed.stderr)
        self.assertEqual((report["stopped"], report["refusal"], report["parallel"]), ("refused", error, 2))
        return report, error

    def test_with_none_of_its_runs_in_flight_a_wait_code_stops_the_walk(self):
        # Too small a budget for even one claim.
        poor = {"dispatches": 20, "tokens": 30, "cost": 500}
        self.ledger = str(self.work / "poor.db")
        self.init(caps=poor, task_caps=poor)
        self.write_jobs(self.job("task-1"), self.job("task-2"))
        report, error = self.real_refused()
        self.assertEqual((error["error"], error["operation_id"]), ("E_BUDGET_INSUFFICIENT", None))
        self.assertEqual((report["runs"], report["skipped"], self.operations()), ([], [], []))
        # Another controller's operation holds the request's only slot.
        self.ledger = str(self.work / "foreign.db")
        self.init(concurrency=1)
        ledger = open_ledger(self.ledger)
        try:
            ledger.claim("req-1", "task-3", 1, "foreign", "owner-9", ledger.generation, 1, dict(RESERVATION),
                         ["foreign"], 0)
        finally:
            ledger.close()
        report, error = self.real_refused()
        self.assertEqual((report["runs"], error["error"], error["operation_id"]), ([], "E_CONCURRENCY", None))
        self.assertEqual([(o["task_id"], o["state"]) for o in self.operations()], [("task-3", "RESERVED")])

    # -- the same walk as without --parallel -------------------------------- #

    def test_the_parallel_walk_skips_and_derives_what_the_walk_without_it_does(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"), self.job("task-3"))
        first = self.serve()
        self.assertEqual(([(r["task_id"], r["attempt"]) for r in first["runs"]], first["skipped"]),
                         ([("task-1", 1), ("task-2", 1), ("task-3", 1)], []))
        ops = {r["task_id"]: r["operation_id"] for r in first["runs"]}
        self.verdict(ops["task-1"], "--accept")
        self.verdict(ops["task-2"], "--reject")
        root = self.root()
        second = self.serve(root=root)
        self.assertEqual([(r["task_id"], r["attempt"], r["state"], r["evidence_dir"]) for r in second["runs"]],
                         [("task-2", 2, "REVIEW_PENDING", os.path.join(root, "task-2", "2"))])
        self.assertEqual((second["skipped"], second["stopped"]),
                         ([{"task_id": "task-1", "reason": "done"}, {"task_id": "task-3", "reason": "E_ACTIVE_TASK"}],
                          "drained"))
        self.verdict(second["runs"][0]["operation_id"], "--reject")
        third = self.serve()
        self.assertEqual((third["runs"], third["stopped"]), ([], "drained"))
        self.assertEqual(third["skipped"], [{"task_id": "task-1", "reason": "done"},
                                            {"task_id": "task-2", "reason": "attempts"},
                                            {"task_id": "task-3", "reason": "E_ACTIVE_TASK"}])
        self.assertEqual([o["task_id"] for o in self.operations()], ["task-1", "task-2", "task-3", "task-2"])

    def test_a_task_without_a_job_is_skipped_and_the_candidate_defaults_to_the_records_digest(self):
        # task-3's relative working directory is refused by the runtime: an outcome, not a refusal.
        self.write_jobs(self.job("task-1"), self.job("task-3", cwd="tree"))
        report = self.serve(candidate=None)
        self.assertEqual(([(r["task_id"], r["state"]) for r in report["runs"]], report["skipped"], report["stopped"]),
                         ([("task-1", "REVIEW_PENDING"), ("task-3", "NOT_STARTED")],
                          [{"task_id": "task-2", "reason": "no_job"}], "drained"))
        run = report["runs"][0]
        self.verdict(run["operation_id"], "--accept", candidate=evidence_digest(run["evidence_dir"]))
        self.assertEqual(self.operations()[0]["state"], "ACCEPTED")

    def test_the_derived_key_is_the_one_without_parallel(self):
        # The idempotency index is per request: a ``once`` that used, on task-1, the key serve derives
        # for task-2's first attempt makes serve's claim for task-2 a conflict, which stops the walk.
        once_root = self.evidence / "once"
        once_root.mkdir(parents=True)
        once = self.ok("run", "once", self.ledger, "--job",
                       self.write_json(self.work / "once.json", self.job("task-1", idempotency_key="idem-task-2#1")),
                       "--evidence", str(once_root), "--worktree-id", "wt-1", "--owner", "owner-1",
                       "--candidate", CANDIDATE)
        self.verdict(once["operation_id"], "--reject")
        self.write_jobs(self.step("task-1", pause=0.4), self.job("task-2"), self.job("task-3"))
        report, errors = self.serve_refused()
        self.assertEqual((errors[0]["error"], errors[0]["operation_id"]), ("E_IDEMPOTENCY_CONFLICT", None))
        self.assertEqual([(r["task_id"], r["attempt"], r["state"]) for r in report["runs"]],
                         [("task-1", 2, "REVIEW_PENDING")])
        self.assertEqual([o["task_id"] for o in self.operations()], ["task-1", "task-1"])

    def test_the_checks_before_the_walk_refuse_as_they_do_without_parallel(self):
        before = self.digest()
        self.write_jobs(self.job("task-1"), self.job("task-9"))
        report, errors = self.serve_refused()
        self.assertEqual(errors[0]["error"], "E_RUN_JOBS")
        self.assertIn("task-9", errors[0]["detail"])
        self.assertEqual((report["runs"], report["skipped"]), ([], []))
        self.reset_jobs(self.job("task-1", request_id="req-9"))
        self.assertEqual(self.serve_refused(request="req-9")[1][0]["error"], "E_REQUEST_UNKNOWN")
        self.assertEqual((self.digest(), self.operations()), (before, []))
        self.reset_jobs(self.job("task-1"), self.job("task-2"))
        self.ok("ledger", "stop", self.ledger, "--request", "req-1", "--reason", "OPERATOR_STOP")
        report, errors = self.serve_refused()
        self.assertEqual((errors[0]["error"], report["runs"], self.operations()), ("E_STOPPED", [], []))

    # -- one working directory ---------------------------------------------- #

    def test_jobs_sharing_a_working_directory_without_a_common_resource_are_refused_before_the_ledger_opens(self):
        tree = self.tree("one")
        spellings = {"as written": tree, "with a dot-dot": os.path.join(tree, "sub", "..")}
        if os.name == "nt":
            spellings["in upper case"] = tree.upper()
        before = self.digest()
        for label, other in spellings.items():
            with self.subTest(spelling=label):
                self.reset_jobs(self.job("task-1", cwd=tree), self.job("task-3"), self.job("task-2", cwd=other))
                report, errors = self.serve_refused()
                self.assertEqual((errors[0]["error"], errors[0]["operation_id"]), ("E_RUN_JOBS", None))
                self.assertIn("task-1", errors[0]["detail"])
                self.assertIn("task-2", errors[0]["detail"])
                self.assertEqual((report["runs"], report["skipped"]), ([], []))
        self.assertEqual((self.digest(), self.operations()), (before, []))
        # A broken ledger path is not even opened: the directory check comes first.
        self.reset_jobs(self.job("task-1", cwd=tree), self.job("task-2", cwd=tree))
        self.ledger = str(self.work / "missing.db")
        self.assertEqual(self.serve_refused()[1][0]["error"], "E_RUN_JOBS")
        self.ledger = str(self.work / "ledger.db")
        # Without --parallel the walk is the old one, which does not check directories.
        report = self.ok(*self.serve_argv(parallel=None))
        self.assertEqual(([r["task_id"] for r in report["runs"]], report["stopped"]), (["task-1", "task-2"], "drained"))
        self.assertNotIn("parallel", report)

    def test_jobs_sharing_a_working_directory_and_a_resource_run_one_after_the_other(self):
        tree = self.tree("one")
        self.write_jobs(self.step("task-1", pause=0.4, cwd=tree, resources=["wt"]),
                        self.step("task-2", watch="task-1", cwd=tree, resources=["wt"]))
        report = self.serve()
        self.assertEqual(([r["task_id"] for r in report["runs"]], report["stopped"]), (["task-1", "task-2"], "drained"))
        self.assertEqual(self.mark("task-2.saw"), "1")

    # -- stops end the claims, the runs in flight end before the report ------ #

    def test_a_refusal_inside_a_run_stops_the_claims_and_the_other_runs_end_before_the_report(self):
        self.write_jobs(self.step("task-1", pause=1.0), self.job("task-2", usage_dimensions=["tokens", "gpu"]),
                        self.job("task-3"))
        report, errors = self.serve_refused()
        operations = self.operations()
        self.assertEqual([(o["task_id"], o["state"]) for o in operations],
                         [("task-1", "REVIEW_PENDING"), ("task-2", "RESERVED")])
        self.assertEqual((errors[0]["error"], errors[0]["operation_id"]),
                         ("E_ADAPTER_SCHEMA", operations[1]["operation_id"]))
        self.assertEqual([(r["task_id"], r["state"]) for r in report["runs"]], [("task-1", "REVIEW_PENDING")])
        self.assertEqual(report["skipped"], [])

    def test_every_refusal_is_one_stderr_line_in_claim_order(self):
        self.write_jobs(self.job("task-1"), self.job("task-2"), self.job("task-3"))
        barrier = threading.Barrier(2, timeout=20)

        def refuse(*args, **kwargs):
            barrier.wait()  # both runs are claimed and in flight before either refuses
            raise AdapterError("E_ADAPTER_SCHEMA", "refused by the oracle")
        with mock.patch.object(run_cli, "run_adapter_operation", refuse):
            report, errors = self.serve_refused(lines=2)
        operations = self.operations()
        self.assertEqual([(o["task_id"], o["state"]) for o in operations],
                         [("task-1", "RESERVED"), ("task-2", "RESERVED")])
        self.assertEqual([(e["error"], e["operation_id"]) for e in errors],
                         [("E_ADAPTER_SCHEMA", o["operation_id"]) for o in operations])
        self.assertEqual((report["runs"], report["skipped"]), ([], []))

    def test_a_refusal_in_a_run_comes_before_the_walks_own_refusal(self):
        # task-1's run refuses half a second after its claim; meanwhile the walk refuses task-2 (its
        # evidence directory exists).  The refusals are in claim order: task-1's first.
        self.write_jobs(self.job("task-1"), self.job("task-2"), self.job("task-3"))
        root = self.root()
        os.makedirs(os.path.join(root, "task-2", "1"))

        def late(*args, **kwargs):
            time.sleep(0.5)
            raise AdapterError("E_ADAPTER_SCHEMA", "refused by the oracle")
        with mock.patch.object(run_cli, "run_adapter_operation", late):
            report, errors = self.serve_refused(root=root, lines=2)
        first = self.operations()[0]["operation_id"]
        self.assertEqual([(e["error"], e["operation_id"]) for e in errors],
                         [("E_ADAPTER_SCHEMA", first), ("E_RUN_EVIDENCE", None)])
        self.assertEqual((report["runs"], [o["task_id"] for o in self.operations()]), ([], ["task-1"]))

    def test_a_refusal_before_a_claim_waits_for_the_runs_in_flight(self):
        self.write_jobs(self.step("task-1", pause=0.6), self.job("task-2"), self.job("task-3"))
        root = self.root()
        os.makedirs(os.path.join(root, "task-2", "1"))
        report, errors = self.serve_refused(root=root)
        self.assertEqual((errors[0]["error"], errors[0]["operation_id"]), ("E_RUN_EVIDENCE", None))
        self.assertIn(os.path.join(root, "task-2", "1"), errors[0]["detail"])
        self.assertEqual([(r["task_id"], r["state"]) for r in report["runs"]], [("task-1", "REVIEW_PENDING")])
        self.assertEqual([(o["task_id"], o["state"]) for o in self.operations()], [("task-1", "REVIEW_PENDING")])

    def test_a_replayed_claim_stops_the_walk_without_running_the_replayed_operation(self):
        self.write_jobs(self.step("task-1", pause=0.4), self.job("task-2"), self.job("task-3"))
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
                    return {"operation_id": self._first["operation_id"], "state": "HANDOFF_ACCEPTED", "replayed": True}
                self._first = self._ledger.claim(*args, **kwargs)
                return self._first

        root = self.root()
        with mock.patch.object(run_cli, "open_ledger", lambda path: Replaying(real_open(path))):
            report, errors = self.serve_refused(root=root)
        first = self.operations()[0]["operation_id"]
        self.assertEqual((calls, errors[0]["error"], errors[0]["operation_id"]),
                         (["task-1", "task-2"], "E_RUN_CLAIM_REPLAYED", first))
        self.assertEqual([(r["task_id"], r["state"]) for r in report["runs"]], [("task-1", "REVIEW_PENDING")])
        self.assertEqual(sorted(os.listdir(root)), ["task-1"])
        self.assertEqual(len(self.operations()), 1)

    def test_max_runs_and_max_seconds_stop_the_claims_and_the_runs_in_flight_end_first(self):
        self.write_jobs(self.step("task-1", pause=0.3), self.step("task-2", pause=0.3), self.job("task-3"))
        report = self.serve("--max-runs", "2")
        self.assertEqual(([(r["task_id"], r["state"]) for r in report["runs"]], report["stopped"]),
                         ([("task-1", "REVIEW_PENDING"), ("task-2", "REVIEW_PENDING")], "max_runs"))
        report = self.serve("--max-seconds", "0")
        self.assertEqual((report["runs"], report["skipped"], report["stopped"]), ([], [], "max_seconds"))
        self.ledger = str(self.work / "slow.db")
        self.init()
        self.reset_jobs(self.step("task-1", pause=1.3), self.step("task-2", pause=1.3), self.job("task-3"))
        started = time.monotonic()
        report = self.serve("--max-seconds", "1")
        self.assertEqual(([(r["task_id"], r["state"]) for r in report["runs"]], report["stopped"]),
                         ([("task-1", "REVIEW_PENDING"), ("task-2", "REVIEW_PENDING")], "max_seconds"))
        self.assertGreaterEqual(time.monotonic() - started, 1.2)
        self.assertEqual(len(self.operations()), 2)

    # -- Ctrl-C and a killed controller -------------------------------------- #

    def test_the_first_interrupt_stops_the_claims_and_waits_for_the_runs_in_flight(self):
        self.write_jobs(self.hold("task-1"), self.hold("task-2"), self.job("task-3"))
        self.interrupt_later([self.marks / "task-1.start", self.marks / "task-2.start"], release_after=1.0)
        try:
            report = self.serve()
        except KeyboardInterrupt:  # the first interrupt must be absorbed, not escape
            self.fail("the first KeyboardInterrupt escaped serve --parallel")
        self.assertEqual(([(r["task_id"], r["state"]) for r in report["runs"]], report["skipped"], report["stopped"]),
                         ([("task-1", "REVIEW_PENDING"), ("task-2", "REVIEW_PENDING")], [], "interrupted"))
        self.assertEqual([o["task_id"] for o in self.operations()], ["task-1", "task-2"])

    def test_an_interrupt_while_waiting_for_a_run_does_not_mark_it_ended(self):
        # Planner fix after the fresh check of ledger-catch-up (2026-10-16): on Python 3.12 a
        # KeyboardInterrupt inside Thread.join(timeout) marks a running thread as stopped, so a
        # drain built on join could stop waiting for a run that was still in flight.
        release = threading.Event()
        worker = threading.Thread(target=release.wait, args=(10,), daemon=True)
        worker.start()
        self.addCleanup(release.set)
        threading.Timer(0.25, _thread.interrupt_main).start()
        deadline = time.monotonic() + 5
        with self.assertRaises(KeyboardInterrupt):
            while time.monotonic() < deadline:
                run_cli._await_run([worker])
        self.assertTrue(worker.is_alive(), "an interrupted wait marked a running thread as ended")
        release.set()
        worker.join(5)

    def test_a_second_interrupt_is_not_absorbed_and_leaves_the_runs_in_flight_undecided(self):
        self.write_jobs(self.hold("task-1"), self.hold("task-2"), self.job("task-3"))
        before = set(threading.enumerate())
        helper = self.interrupt_later([self.marks / "task-1.start", self.marks / "task-2.start"], times=2, gap=0.5)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(KeyboardInterrupt):
                cli.main([str(a) for a in self.serve_argv()])
        # The second interrupt lands inside serve only when serve absorbed the first; a serve that let
        # the first one escape would get the second one here, in the test: catch it and say so.
        stray = False
        try:
            helper.join(10)
            time.sleep(0.3)
        except KeyboardInterrupt:
            stray = True
        self.assertFalse(stray, "the first interrupt escaped serve, so the second one landed in the test")
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        # Nothing decided the two runs before the ledger closed under them.
        self.assertEqual([(o["task_id"], o["state"]) for o in self.operations()],
                         [("task-1", "HANDOFF_ACCEPTED"), ("task-2", "HANDOFF_ACCEPTED")])
        children = [pid_in(self.marks / "task-1.start"), pid_in(self.marks / "task-2.start")]
        (self.marks / "release").touch()
        for child in children:
            self.assertTrue(gone_within(child, 30.0))
        for thread in set(threading.enumerate()) - before - {helper}:
            thread.join(30)
        self.assertEqual([(o["task_id"], o["state"]) for o in self.operations()],
                         [("task-1", "HANDOFF_ACCEPTED"), ("task-2", "HANDOFF_ACCEPTED")])

    def test_an_unexpected_error_in_a_run_is_raised_after_the_other_runs_end(self):
        self.write_jobs(self.step("task-1", pause=0.6), self.job("task-2"), self.job("task-3"))
        real_run = run_cli.run_adapter_operation

        def flaky(*args, **kwargs):
            if os.path.basename(os.path.dirname(kwargs["evidence_dir"])) == "task-2":
                raise RuntimeError("boom")
            return real_run(*args, **kwargs)
        with mock.patch.object(run_cli, "run_adapter_operation", flaky):
            code, out, err = self.smctl(*self.serve_argv())
        # Raised out of serve once task-1 ended, and turned by ``cli.main`` into its usage-style error.
        self.assertEqual((code, out, err), (2, "", "error: boom" + NL))
        self.assertEqual([(o["task_id"], o["state"]) for o in self.operations()],
                         [("task-1", "REVIEW_PENDING"), ("task-2", "RESERVED")])

    def test_a_killed_serve_leaves_every_run_in_flight_to_recover(self):
        self.write_jobs(self.hold("task-1"), self.hold("task-2"), self.job("task-3"))
        self.addCleanup(lambda: (self.marks / "release").touch() if self.marks.exists() else None)
        controller = subprocess.Popen([PY, "-m", "sentinelmesh.cli", *[str(a) for a in self.serve_argv()]],
                                      cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: controller.poll() is None and controller.kill())
        started = [self.marks / "task-1.start", self.marks / "task-2.start"]
        self.assertTrue(wait_for(started), "the two children never started: %r" % (controller.poll(),))
        children = [pid_in(path) for path in started]
        controller.kill()
        out, err = controller.communicate(timeout=60)
        self.assertNotEqual(controller.returncode, 0, err)
        self.assertEqual(out, "", "a killed serve reported")
        if os.name == "nt":
            for child in children:
                self.assertTrue(gone_within(child, 10.0), "a supervised child outlived its controller")
        (self.marks / "release").touch()
        for child in children:
            self.assertTrue(gone_within(child, 30.0))
        self.assertEqual([o["task_id"] for o in self.operations()], ["task-1", "task-2"])
        completed = self.real("run", "recover", self.ledger, "--owner", "owner-2")
        self.assertEqual((completed.returncode, completed.stderr), (0, ""))
        recovered = json.loads(completed.stdout)
        self.assertEqual([(o["action"], o["state"]) for o in recovered["operations"]],
                         [("recovered", "START_UNKNOWN"), ("recovered", "START_UNKNOWN")])
        self.assertEqual([(o["task_id"], o["state"]) for o in self.operations()],
                         [("task-1", "START_UNKNOWN"), ("task-2", "START_UNKNOWN")])

    # -- the command line ---------------------------------------------------- #

    def test_parallel_takes_a_positive_integer_written_in_digits(self):
        self.write_jobs(self.job("task-1"))
        for value in ("0", "-1", "2.0", " 2", "", "two"):
            with self.subTest(value=value):
                self.usage_error(*self.serve_argv("--parallel", value, parallel=None))
        self.assertEqual(self.operations(), [])


class ParallelCompletion(ParallelCase):
    """The planner's completion after attempt-001 (`.agent/reviews/serve-parallel/attempt-001.md`, F1 and F2).

    Both are races no product-level case can time, so they are pinned in process
    through the candidate's own helpers: ``_runs_in_flight`` counts the runs in
    flight and ``_await_run`` blocks for at most one poll.
    """

    def test_a_run_that_ends_between_two_counts_does_not_leave_the_walk_spinning(self):
        # F1: the claim is refused with a wait code while one run is counted in flight, and that run
        # has ended by the next count.  Counted twice, the wait would be for fewer than zero runs.
        class Ledger:
            generation = 1

            def task_ids(self, request):
                return ["task-1"]

            def task(self, request, task):
                return {"task_id": task, "max_attempts": 2, "attempts": 0, "done": False}

            def claim(self, *args):
                raise ContractError("E_RESOURCE_BUSY")

            def close(self):
                pass
        counts = iter([0, 1, 0])
        polls = []

        def await_run(threads):
            polls.append(1)
            if len(polls) > 200:
                raise RuntimeError("the walk spins waiting for a run that is not there")
        self.write_jobs(self.job("task-1"))
        with mock.patch.object(run_cli, "open_ledger", lambda path: Ledger()),                 mock.patch.object(run_cli, "_runs_in_flight", lambda threads: next(counts, 0)),                 mock.patch.object(run_cli, "_await_run", await_run):
            report, errors = self.serve_refused()
        self.assertEqual((errors[0]["error"], errors[0]["operation_id"], report["runs"]), ("E_RESOURCE_BUSY", None, []))

    def test_a_ledger_refusal_in_the_walk_waits_for_the_runs_in_flight(self):
        # F2: the task view of the second task fails while the first run is still in flight; the
        # report comes after that run ended, and names it.
        self.write_jobs(self.job("task-1"), self.job("task-2"))
        real_open = run_cli.open_ledger
        release = threading.Event()
        ended = []

        class Failing:
            def __init__(self, ledger):
                self._ledger, self._views = ledger, 0

            def __getattr__(self, name):
                return getattr(self._ledger, name)

            def task(self, *args):
                self._views += 1
                if self._views == 2:
                    threading.Timer(0.5, release.set).start()
                    raise LedgerError("E_LEDGER_UNAVAILABLE", "made unusable by the oracle")
                return self._ledger.task(*args)

        def run(*args, **kwargs):
            release.wait(10)
            ended.append(time.monotonic())
            return {"state": "REVIEW_PENDING"}
        with mock.patch.object(run_cli, "open_ledger", lambda path: Failing(real_open(path))),                 mock.patch.object(run_cli, "run_adapter_operation", run):
            report, errors = self.serve_refused()
            returned = time.monotonic()
        self.assertEqual((errors[0]["error"], errors[0]["operation_id"]), ("E_LEDGER_UNAVAILABLE", None))
        self.assertEqual([(r["task_id"], r["state"]) for r in report["runs"]], [("task-1", "REVIEW_PENDING")])
        self.assertTrue(ended and ended[0] <= returned)


if __name__ == "__main__":
    unittest.main()
