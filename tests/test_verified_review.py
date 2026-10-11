"""Executor tests for ``verified-review`` (`.agent/design/stream-trust-boundary.md`, section 3 item 5).

These cover what the planner oracle does not: the review hook's own contract (how
often it runs, and what happens to the operation when the review it precedes is
refused), the two reasons a stream that moved stops a review when only one of the
two streams is involved, a recovery whose records refuse to give up their stream
facts at all, the order of ``read_stream``'s checks when nothing is on disk, and a
run that reserves no usage dimension.  Every case starts real local ``python``
processes and cleans up the ones it starts.

Each rejection case first runs a legal baseline on a ledger and then changes
exactly one thing, so a refusal is attributed to the rule under test and not to
another rule that happens to fire first.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sentinelmesh.supervised_operation as bridge
from sentinelmesh.adapter_protocol import AdapterError, Invocation, OfflineAdapter
from sentinelmesh.adapter_run import read_stream, run_adapter_operation
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.posix_supervisor import SupervisedRun
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.scheduler import Scheduler
from sentinelmesh.supervised_record import OBSERVATION_NAME, recorded_streams, record_supervised_run
from sentinelmesh.supervised_operation import recover_supervised_operation, run_supervised_operation

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
CANDIDATE = "c" * 64
CAPS = {"dispatches": 5, "tokens": 100, "cost": 50}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}

# Writes to both streams, so each one has bytes to digest.
BOTH_STREAMS = "import os\nos.write(1, b'leader\\n')\nos.write(2, b'err\\n')\n"
# The leader writes both streams; a grandchild leaves the group with setsid()
# before the leader exits (no zombie, so cleanup is confirmed) and touches only
# stderr, and only once the gate exists.  argv: pidfile, gate, done, mode, dir.
STDERR_ESCAPE = ("import os, sys, time\n"
                 "pidfile, gate, done, mode, directory = sys.argv[1:6]\n"
                 "middle = os.fork()\n"
                 "if middle == 0:\n"
                 "    if os.fork() == 0:\n"
                 "        os.setsid()\n"
                 "        open(pidfile + '.tmp', 'w').write(str(os.getpid()))\n"
                 "        os.rename(pidfile + '.tmp', pidfile)\n"
                 "        deadline = time.monotonic() + 30\n"
                 "        while not os.path.exists(gate) and time.monotonic() < deadline:\n"
                 "            time.sleep(0.01)\n"
                 "        if mode == 'stderr-append':\n"
                 "            os.write(2, b'late\\n')\n"
                 "        elif mode == 'stderr-replace':\n"
                 "            os.rename(os.path.join(directory, 'stderr.log'),\n"
                 "                      os.path.join(directory, 'moved.log'))\n"
                 "            open(os.path.join(directory, 'stderr.log'), 'wb').write(b'forged\\n')\n"
                 "        open(done, 'w').close()\n"
                 "        os._exit(0)\n"
                 "    os._exit(0)\n"
                 "os.waitpid(middle, 0)\n"
                 "while not os.path.exists(pidfile):\n"
                 "    time.sleep(0.01)\n")
# A grandchild that leaves a zombie in the leader's process group, so the group
# never empties and the cleanup cannot be confirmed.
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
                       "subprocess.Popen([sys.executable, '-c', %r, sys.argv[1]])\n"
                       "while not os.path.exists(sys.argv[1]):\n"
                       "    time.sleep(0.02)\n" % ESCAPED_PARENT)


class FinishRefused(Exception):
    """Raised instead of the review, so the bridge's refusal path is observable."""


class RefusingScheduler(Scheduler):
    """A ledger that refuses every review, after the hook and the candidate ran."""

    def finish(self, *args, **kwargs):
        raise FinishRefused("finish refused")


class GateScheduler(Scheduler):
    """Opens the escapee's gate from ``mark_running`` and waits until it has written.

    The bridge calls ``mark_running`` after the supervisor returned -- so after
    the reap and after the streams were digested -- and before it checks them:
    whatever the escapee writes then is a write after the reap that the check has
    to see.
    """

    gate = None
    done = None

    def mark_running(self, *args, **kwargs):
        super().mark_running(*args, **kwargs)
        if self.gate is not None:
            self.gate.touch()
            wait_for(self.done)


def ev(seq, typ, payload=None):
    return {"seq": seq, "event_id": "e%d" % seq, "type": typ, "source": "offline",
            "payload": {} if payload is None else payload}


def emit(events):
    """Python code that prints each event as one JSON line."""
    return ("import json, sys\n"
            "for event in json.loads(%r):\n"
            "    sys.stdout.write(json.dumps(event) + '\\n')\n"
            "sys.stdout.flush()\n" % json.dumps(events))


COMPLETE = [ev(0, "run_started"), ev(1, "usage", {"tokens": 12, "cost": 3}), ev(2, "run_finished")]


def facts_of(path: Path) -> dict:
    info = os.lstat(path)
    data = path.read_bytes()
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def wait_for(path: Path, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError("%s never appeared" % path.name)
        time.sleep(0.01)


def events_of(scheduler, op, event_type):
    return [e for e in scheduler.events if e["operation_id"] == op and e["type"] == event_type]


class Base(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="verified-review-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.fresh_ledger()
        self.count = 0

    def fresh_ledger(self, scheduler_class=Scheduler):
        """A new ledger: a case that stops the request needs one for the next case."""
        self.scheduler = scheduler_class()
        self.scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        for task in ("task-1", "task-2"):
            self.scheduler.seed_task("req-1", task, CAPS, max_attempts=5)
        self.calls = []

    def candidate_hash(self, evidence_dir):
        self.calls.append(("candidate_hash", evidence_dir, self.scheduler.operation(self.op)["state"]))
        return CANDIDATE

    def before_finish(self, run):
        finished = [e for e in self.scheduler.events if e["type"] == "run.finished_for_review"]
        self.calls.append(("before_finish", run, self.scheduler.operation(self.op)["state"], len(finished)))

    def operation(self, task="task-1"):
        self.count += 1
        claim = self.scheduler.claim("req-1", task, 1, "idem-%d" % self.count, "owner-1", 1, 1, RESERVATION,
                                     ["src/%s.py" % task])
        evidence = self.work / ("evidence-%d" % self.count)
        evidence.mkdir()
        self.op = claim["operation_id"]
        return self.op, evidence

    def spec(self, op, code, *args, command=None):
        argv = command if command is not None else (PY, "-c", code, *args)
        return InvocationSpec("req-1", op, argv, str(self.cwd), environment_digest(BASE_ENV), 10)

    def run_op(self, op, spec, evidence, **kwargs):
        kwargs.setdefault("before_finish", self.before_finish)
        return run_supervised_operation(self.scheduler, operation_id=op, owner="owner-1", generation=1, spec=spec,
                                        environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1",
                                        candidate_hash=self.candidate_hash, **kwargs)

    def escape(self, mode):
        """Names and cleanup for one descendant that waits for its gate, then writes."""
        names = [self.work / ("%s-%d%s" % (mode, self.count, suffix)) for suffix in (".pid", ".gate", ".done")]
        self.addCleanup(self.kill_pid_in, names[0])
        # Let a waiting escapee go once the case is over, so it exits on its own.
        self.addCleanup(lambda gate=names[1]: gate.touch())
        return names

    def kill_pid_in(self, path: Path):
        try:
            pid = int(path.read_text().strip())
        except (OSError, ValueError):
            return
        for kill in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
            try:
                kill()
            except (ProcessLookupError, PermissionError):
                pass

    def state_of(self, op):
        return self.scheduler.operation(op)["state"]

    def assert_unknown(self, op, outcome, reason, task="task-1"):
        self.assertEqual((outcome["state"], self.state_of(op)), ("START_UNKNOWN", "START_UNKNOWN"))
        events = events_of(self.scheduler, op, "scheduler.start_unknown")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["reason"], reason)
        self.assertEqual(events_of(self.scheduler, op, "run.finished_for_review"), [])
        self.assertTrue(("repo-1", "src/%s.py" % task) in self.scheduler._resource_holds,
                        "the holds were released")

    def recorded_run(self, code, *args):
        """One recorded run of a generation that has since been rolled over."""
        op, evidence = self.operation()
        self.scheduler.handoff(op, "owner-1", 1)
        spec = self.spec(op, code, *args)
        record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
        self.scheduler.rollover()
        return op, spec, evidence

    def recover(self, op, spec, evidence):
        return recover_supervised_operation(self.scheduler, operation_id=op, owner="owner-2", generation=2,
                                            spec=spec, evidence_dir=str(evidence), candidate_hash=self.candidate_hash)


@unittest.skipIf(os.name != "posix", "native POSIX supervisor (process groups and signals); the Windows wiring has its own cases in tests/test_win_wiring*.py")
class BridgeHookTests(Base):
    def test_before_finish_runs_exactly_once_on_the_review_path(self):
        with self.subTest(case="a clean run"):
            op, evidence = self.operation()
            outcome = self.run_op(op, self.spec(op, BOTH_STREAMS), evidence)
            self.assertEqual((outcome["state"], self.state_of(op)), ("REVIEW_PENDING", "REVIEW_PENDING"))
            self.assertEqual([call[0] for call in self.calls], ["before_finish", "candidate_hash"])
            _name, run, state, finished = self.calls[0]
            self.assertIs(type(run), SupervisedRun)
            self.assertEqual((state, finished), ("RUNNING", 0), "before_finish ran across the finish")
            self.assertEqual(self.calls[1][1:], (str(evidence), "RUNNING"))
        with self.subTest(case="an unconfirmed cleanup"):
            # The hook belongs to the review path alone: a run that cannot be
            # classified never reaches it, whatever the streams look like.
            self.fresh_ledger()
            escaped = self.work / "escaped.pid"
            self.addCleanup(self.kill_pid_in, escaped)
            op, evidence = self.operation("task-2")
            outcome = self.run_op(op, self.spec(op, ZOMBIE_GROUP_LEADER, str(escaped)), evidence)
            self.assertEqual((outcome["state"], self.state_of(op)), ("START_UNKNOWN", "START_UNKNOWN"))
            events = events_of(self.scheduler, op, "scheduler.start_unknown")
            self.assertEqual(events[0]["payload"]["reason"], "CLEANUP_UNCONFIRMED")
            self.assertEqual(self.calls, [], "before_finish ran outside the review path")

    def test_a_refused_review_after_before_finish_is_start_unknown_and_reraised(self):
        with self.subTest(case="legal baseline"):
            op, evidence = self.operation()
            self.assertEqual(self.run_op(op, self.spec(op, BOTH_STREAMS), evidence)["state"], "REVIEW_PENDING")
            self.assertEqual([call[0] for call in self.calls], ["before_finish", "candidate_hash"])
        with self.subTest(case="finish refuses"):
            self.fresh_ledger(RefusingScheduler)
            op, evidence = self.operation()
            with self.assertRaises(FinishRefused):
                self.run_op(op, self.spec(op, BOTH_STREAMS), evidence)
            # The hook already ran and the candidate was produced: what is missing
            # is only the review, so the operation is unknown rather than finished.
            self.assertEqual(self.state_of(op), "START_UNKNOWN")
            events = events_of(self.scheduler, op, "scheduler.start_unknown")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["payload"]["reason"], "START_UNKNOWN")
            self.assertEqual([call[0] for call in self.calls], ["before_finish", "candidate_hash"])
            self.assertEqual(events_of(self.scheduler, op, "run.finished_for_review"), [])
            self.assertTrue(("repo-1", "src/task-1.py") in self.scheduler._resource_holds,
                            "the holds were released")

    def test_an_interrupting_hook_is_start_unknown_and_reraised(self):
        # The interrupt is the caller's to see, but the hook it interrupted leaves
        # an operation that cannot be shown to have produced a reviewable
        # candidate, and the event it produced inside the hook still stands.
        op, evidence = self.operation()

        def interrupt(run):
            raise KeyboardInterrupt("interrupted inside before_finish")

        with self.assertRaises(KeyboardInterrupt):
            self.run_op(op, self.spec(op, BOTH_STREAMS), evidence, before_finish=interrupt)
        self.assertEqual(self.state_of(op), "START_UNKNOWN")
        events = events_of(self.scheduler, op, "scheduler.start_unknown")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["reason"], "START_UNKNOWN")
        self.assertEqual(self.calls, [], "the review ran after the hook refused")


@unittest.skipIf(os.name != "posix", "native POSIX supervisor (process groups and signals); the Windows wiring has its own cases in tests/test_win_wiring*.py")
class BridgeStreamTests(Base):
    def test_one_stderr_stream_that_moved_stops_the_review(self):
        with self.subTest(case="legal baseline"):
            # The same escapee, its gate opened only once the bridge is back.
            op, evidence = self.operation()
            pidfile, gate, done = self.escape("stderr-append")
            outcome = self.run_op(op, self.spec(op, BOTH_STREAMS + STDERR_ESCAPE, str(pidfile), str(gate), str(done),
                                                "stderr-append", str(evidence)), evidence)
            self.assertEqual(outcome["state"], "REVIEW_PENDING")
            gate.touch()
            wait_for(done)
        for case, mode, reason in (("stderr replaced by another file", "stderr-replace", "START_UNKNOWN"),
                                   ("stderr appended to the pinned file", "stderr-append", "CLEANUP_UNCONFIRMED")):
            with self.subTest(case=case):
                self.fresh_ledger(GateScheduler)
                op, evidence = self.operation()
                pidfile, gate, done = self.escape(mode)
                self.scheduler.gate, self.scheduler.done = gate, done
                try:
                    outcome = self.run_op(op, self.spec(op, BOTH_STREAMS + STDERR_ESCAPE, str(pidfile), str(gate),
                                                        str(done), mode, str(evidence)), evidence)
                finally:
                    self.scheduler.gate = self.scheduler.done = None
                self.assertTrue(done.exists(), "the escapee never wrote")
                self.assertEqual(outcome["decision"]["status"], "STOPPED")
                self.assert_unknown(op, outcome, reason)
                self.assertEqual(self.calls, [], "the review path ran")
                # The cleanup itself was fine: replay is allowed, so it was the
                # stderr stream alone that stopped the review.
                self.assertEqual(outcome["decision"]["replay_allowed"], True)


@unittest.skipIf(os.name != "posix", "native POSIX supervisor (process groups and signals); the Windows wiring has its own cases in tests/test_win_wiring*.py")
class RecoveryStreamTests(Base):
    def test_streams_that_cannot_be_read_from_the_record_stop_the_review(self):
        with self.subTest(case="legal baseline"):
            op, spec, evidence = self.recorded_run(BOTH_STREAMS)
            outcome = self.recover(op, spec, evidence)
            self.assertEqual((outcome["state"], self.state_of(op)), ("REVIEW_PENDING", "REVIEW_PENDING"))
            self.assertEqual([call[0] for call in self.calls], ["candidate_hash"])
        with self.subTest(case="the record refuses to give up its streams"):
            self.fresh_ledger()
            op, spec, evidence = self.recorded_run(BOTH_STREAMS)
            broken = RuntimeErrorCode("E_RUNTIME_EVIDENCE", "the observation record could not be read")

            def refuse(*args, **kwargs):
                raise broken

            with mock.patch.object(bridge, "recorded_streams", refuse):
                outcome = self.recover(op, spec, evidence)
            # A record that cannot be read for its streams is exactly as
            # inconsistent as one that does not match: no review, and no raise.
            self.assertEqual((outcome["state"], self.state_of(op)), ("START_UNKNOWN", "START_UNKNOWN"))
            self.assertEqual(outcome["decision"]["status"], "STOPPED")
            self.assertEqual(self.calls, [], "the candidate hash was computed")
            recovered = events_of(self.scheduler, op, "scheduler.recovered")
            self.assertEqual(recovered[0]["payload"]["outcome"], "START_UNKNOWN")
        with self.subTest(case="a damaged observation record"):
            # Real evidence, no patching: neither the decision nor the streams can
            # be read from it, and recovery stays a question, not a crash.
            self.fresh_ledger()
            op, spec, evidence = self.recorded_run(BOTH_STREAMS)
            with open(evidence / OBSERVATION_NAME, "wb") as handle:
                handle.write(b'{"schema": "sentinelmesh-observation-v2"')
            with self.assertRaises(RuntimeErrorCode) as refused:
                recorded_streams(spec, str(evidence))
            self.assertEqual(refused.exception.code, "E_RUNTIME_EVIDENCE")
            outcome = self.recover(op, spec, evidence)
            self.assertEqual((outcome["state"], self.state_of(op)), ("START_UNKNOWN", "START_UNKNOWN"))
            self.assertIsNone(outcome["decision"], "a decision was read from a damaged record")
            self.assertEqual(self.calls, [], "the candidate hash was computed")


@unittest.skipIf(os.name != "posix", "read_stream's POSIX branch (O_NOFOLLOW); the Windows branch has its own cases")
class ReadStreamTests(Base):
    def setUp(self):
        super().setUp()
        self.evidence = self.work / "facts"
        self.evidence.mkdir()
        self.path = self.evidence / "stdout.log"
        self.raw = b"".join((json.dumps(event) + "\n").encode() for event in COMPLETE)

    def test_read_stream_with_expected_reports_a_missing_stream(self):
        self.path.write_bytes(self.raw)
        good = facts_of(self.path)
        self.assertEqual(read_stream(str(self.evidence), expected=good)["status"], "ok", "legal baseline")
        self.path.unlink()
        self.assertEqual(read_stream(str(self.evidence), expected=good),
                         {"status": "missing", "sha256": None, "bytes": None, "events": None})
        # A stream that is not there answers nothing about the fact either, so the
        # fact's own shape is checked before the path is opened at all.
        malformed = dict(good, sha256=good["sha256"].upper())
        with self.assertRaises(AdapterError) as raised:
            read_stream(str(self.evidence), expected=malformed)
        self.assertEqual(raised.exception.code, "E_ADAPTER_SCHEMA")
        # And the ceiling is checked before the fact, so an out-of-range ceiling
        # names the ceiling even when the fact is malformed too.
        with self.assertRaises(AdapterError) as raised:
            read_stream(str(self.evidence), max_bytes=0, expected=malformed)
        self.assertIn("max_stream_bytes", str(raised.exception))


@unittest.skipIf(os.name != "posix", "native POSIX supervisor (process groups and signals); the Windows wiring has its own cases in tests/test_win_wiring*.py")
class AdapterTests(Base):
    def invocation(self, op, code, *args):
        return Invocation("req-1", op, "executor", "stub-profile", (PY, "-c", code, *args), (),
                          environment_digest(BASE_ENV), None)

    def run_op(self, invocation, evidence, **kwargs):
        return run_adapter_operation(self.scheduler, OfflineAdapter(), invocation, cwd=str(self.cwd),
                                     timeout_seconds=10, environment=BASE_ENV, owner="owner-1", generation=1,
                                     evidence_dir=str(evidence), worktree_id="wt-1",
                                     candidate_hash=self.candidate_hash, **kwargs)

    def usage_of(self, op, dimension):
        usage = self.scheduler._operations[op].usage.get(dimension)
        return None if usage is None else (usage.amount, usage.final, usage.evidence_hash)

    def test_a_run_that_reserves_no_dimension_records_nothing(self):
        with self.subTest(case="legal baseline"):
            op, evidence = self.operation()
            outcome = self.run_op(self.invocation(op, emit(COMPLETE)), evidence, usage_dimensions=("tokens",))
            self.assertEqual(outcome["state"], "REVIEW_PENDING")
            self.assertEqual(outcome["usage_recorded"], {"tokens": "APPLIED"})
            self.assertEqual(outcome["usage_skipped"], [])
            self.assertEqual(self.usage_of(op, "tokens")[0], 12)
        with self.subTest(case="no dimension at all"):
            self.fresh_ledger()
            op, evidence = self.operation()
            outcome = self.run_op(self.invocation(op, emit(COMPLETE)), evidence, usage_dimensions=())
            self.assertEqual(outcome["state"], "REVIEW_PENDING")
            self.assertEqual((outcome["usage_recorded"], outcome["usage_skipped"]), ({}, []))
            self.assertEqual(set(self.scheduler._operations[op].usage), {"dispatches"},
                             "a usage dimension was recorded anyway")
            self.assertEqual([call[0] for call in self.calls], ["candidate_hash"])
        with self.subTest(case="a dimension the operation does not reserve"):
            self.fresh_ledger()
            op, evidence = self.operation()
            before = list(self.scheduler.events)
            with self.assertRaises(AdapterError) as raised:
                self.run_op(self.invocation(op, emit(COMPLETE)), evidence, usage_dimensions=("energy",))
            self.assertEqual(raised.exception.code, "E_ADAPTER_SCHEMA")
            self.assertEqual(list(self.scheduler.events), before)
            self.assertEqual(self.state_of(op), "RESERVED")
            self.assertEqual(os.listdir(evidence), [])


if __name__ == "__main__":
    unittest.main()
