"""Planner oracle for verified-review (`.agent/design/stream-trust-boundary.md`, section 3 item 5).

Planner-provided; executors must not modify this file.

The supervisor now records the facts of both output streams after the reap.
This contract makes the two callers act on them:

* the bridge sends a run to review only when both files on disk are still
  exactly what the supervisor recorded in memory; a stream that changed after
  the reap is ``CLEANUP_UNCONFIRMED`` (a writer outlived the cleanup), a stream
  that is missing, replaced or could not be digested is ``START_UNKNOWN``;
* recovery after a rollover reviews a run only when the recorded streams still
  match the files;
* the bridge hands the in-memory run to a ``before_finish`` hook on the review
  path, and the adapter settles usage there, against the supervisor's digest,
  only when the bytes it reduced are exactly those;
* before the handoff, the specification must belong to the operation's request
  and every usage dimension must be reserved by the operation.

Every case starts real local ``python`` processes.
"""
from __future__ import annotations

import errno
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
from sentinelmesh.posix_supervisor import SupervisedRun, streams_match
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.scheduler import ContractError, Scheduler
from sentinelmesh.supervised_operation import recover_supervised_operation, run_supervised_operation
from sentinelmesh.supervised_record import OBSERVATION_NAME, record_supervised_run, recorded_streams

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
CANDIDATE = "c" * 64
CAPS = {"dispatches": 5, "tokens": 100, "cost": 50}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
DIMENSIONS = ("tokens", "cost")
OTHER_CAPS = {"dispatches": 5, "tokens": 100, "energy": 10}
GARBAGE = b"\x00\x01\x02 not an executable image\n"


def ev(seq, typ, payload=None):
    return {"seq": seq, "event_id": "e%d" % seq, "type": typ, "source": "offline",
            "payload": {} if payload is None else payload}


COMPLETE = [ev(0, "run_started"), ev(1, "assistant"), ev(2, "usage", {"tokens": 12, "cost": 3}), ev(3, "run_finished")]


def emit(events):
    """Python code that prints each event as one JSON line."""
    return ("import json, sys\n"
            "for event in json.loads(%r):\n"
            "    sys.stdout.write(json.dumps(event) + '\\n')\n"
            "sys.stdout.flush()\n" % json.dumps(events))


LEADER_ONLY = "import os\nos.write(1, b'leader\\n')\n"
# Writes to both streams, so each one has bytes to digest.
BOTH_STREAMS = "import os\nos.write(1, b'leader\\n')\nos.write(2, b'err\\n')\n"
# The leader writes ``prefix``; a grandchild leaves the group with setsid()
# before the leader exits (no zombie, so cleanup is confirmed) and writes only
# once the gate exists.  argv: pidfile, gate, done, mode.
ESCAPE = ("import os, sys, time\n"
          "pidfile, gate, done, mode = sys.argv[1:5]\n"
          "middle = os.fork()\n"
          "if middle == 0:\n"
          "    if os.fork() == 0:\n"
          "        os.setsid()\n"
          "        open(pidfile + '.tmp', 'w').write(str(os.getpid()))\n"
          "        os.rename(pidfile + '.tmp', pidfile)\n"
          "        deadline = time.monotonic() + 30\n"
          "        while not os.path.exists(gate) and time.monotonic() < deadline:\n"
          "            time.sleep(0.01)\n"
          "        if mode == 'stdout-append':\n"
          "            os.write(1, b'{\"forged\": true}\\n')\n"
          "        elif mode == 'stderr-append':\n"
          "            os.write(2, b'late\\n')\n"
          "        elif mode == 'stdout-rewrite':\n"
          "            os.pwrite(1, b' ', 0)\n"
          "        open(done, 'w').close()\n"
          "        os._exit(0)\n"
          "    os._exit(0)\n"
          "os.waitpid(middle, 0)\n"
          "while not os.path.exists(pidfile):\n"
          "    time.sleep(0.01)\n")
# The child moves its own stdout file aside and puts a forged one at the path.
RENAME_DURING_RUN = ("import os, sys\n"
                     "d = sys.argv[1]\n"
                     "os.write(1, b'first\\n')\n"
                     "os.rename(os.path.join(d, 'stdout.log'), os.path.join(d, 'moved.log'))\n"
                     "open(os.path.join(d, 'stdout.log'), 'wb').write(b'forged\\n')\n")
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


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def facts_of(path: Path) -> dict:
    info = os.lstat(path)
    data = path.read_bytes()
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": len(data), "sha256": sha(data)}


def wait_for(path: Path, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError("%s never appeared" % path.name)
        time.sleep(0.01)


class GateScheduler(Scheduler):
    """Opens the escapee's gate from ``mark_running`` and waits until it has written.

    The bridge calls ``mark_running`` after the supervisor returned (so after the
    reap and the digest) and before it checks the streams: whatever the escapee
    writes then is a write after the reap that the check must see.
    """

    gate = None
    done = None

    def mark_running(self, *args, **kwargs):
        super().mark_running(*args, **kwargs)
        if self.gate is not None:
            self.gate.touch()
            wait_for(self.done)


class Base(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="verified-review-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.fresh_ledger()
        self.count = 0

    def fresh_ledger(self):
        """A new scheduler: a case that stops the request needs one for the next case."""
        self.scheduler = GateScheduler()
        self.scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        # Another request whose budget has a dimension this request's does not.
        self.scheduler.seed_request("req-2", "repo-2", OTHER_CAPS, concurrency=4)
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

    def spec(self, op, code, *args, request_id="req-1", command=None, timeout=10):
        argv = command if command is not None else (PY, "-c", code, *args)
        return InvocationSpec(request_id, op, argv, str(self.cwd), environment_digest(BASE_ENV), timeout)

    def escape(self, mode):
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

    def events_of(self, op, event_type):
        return [e for e in self.scheduler.events if e["operation_id"] == op and e["type"] == event_type]

    def unknown_reason(self, op):
        events = self.events_of(op, "scheduler.start_unknown")
        self.assertEqual(len(events), 1)
        return events[0]["payload"]["reason"]

    def held(self, task="task-1"):
        return ("repo-1", "src/%s.py" % task) in self.scheduler._resource_holds

    def assert_unknown(self, op, outcome, reason):
        self.assertEqual((outcome["state"], self.state_of(op)), ("START_UNKNOWN", "START_UNKNOWN"))
        self.assertEqual(self.unknown_reason(op), reason)
        self.assertEqual(self.events_of(op, "run.finished_for_review"), [])
        self.assertEqual(self.calls, [], "the review path ran")
        self.assertTrue(self.held(), "the holds were released")
        self.assertEqual(self.scheduler._requests["req-1"].stopped, "START_UNKNOWN")


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class BridgeOracle(Base):
    def run_op(self, op, spec, evidence, **kwargs):
        kwargs.setdefault("before_finish", self.before_finish)
        return run_supervised_operation(self.scheduler, operation_id=op, owner="owner-1", generation=1, spec=spec,
                                        environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1",
                                        candidate_hash=self.candidate_hash, **kwargs)

    def test_a_clean_run_goes_to_review_and_before_finish_sees_the_run_first(self):
        op, evidence = self.operation()
        outcome = self.run_op(op, self.spec(op, LEADER_ONLY), evidence)
        self.assertEqual((outcome["state"], self.state_of(op)), ("REVIEW_PENDING", "REVIEW_PENDING"))
        self.assertEqual([call[0] for call in self.calls], ["before_finish", "candidate_hash"])
        _name, run, state, finished = self.calls[0]
        self.assertIs(type(run), SupervisedRun)
        self.assertEqual((state, finished), ("RUNNING", 0), "before_finish ran after the finish")
        self.assertEqual(run.streams, {"stdout": facts_of(evidence / "stdout.log"),
                                       "stderr": facts_of(evidence / "stderr.log")})
        self.assertEqual(self.calls[1][1:], (str(evidence), "RUNNING"))
        self.assertFalse(self.held())

    def test_writes_after_the_reap_stop_the_review_as_cleanup_unconfirmed(self):
        for mode in ("stdout-append", "stderr-append", "stdout-rewrite"):
            with self.subTest(mode=mode):
                self.fresh_ledger()
                op, evidence = self.operation()
                pidfile, gate, done = self.escape(mode)
                self.scheduler.gate, self.scheduler.done = gate, done
                try:
                    outcome = self.run_op(op, self.spec(op, LEADER_ONLY + ESCAPE, str(pidfile), str(gate),
                                                        str(done), mode), evidence)
                finally:
                    self.scheduler.gate = self.scheduler.done = None
                self.assertTrue(done.exists(), "the escapee never wrote")
                self.assertEqual(outcome["decision"]["status"], "STOPPED")
                self.assert_unknown(op, outcome, "CLEANUP_UNCONFIRMED")

    def test_an_escapee_that_writes_only_after_the_review_does_not_stop_it(self):
        # Legal baseline for the case above: the same program, the gate opened
        # only after the bridge returned.
        op, evidence = self.operation()
        pidfile, gate, done = self.escape("stdout-append")
        outcome = self.run_op(op, self.spec(op, LEADER_ONLY + ESCAPE, str(pidfile), str(gate), str(done),
                                            "stdout-append"), evidence)
        self.assertEqual(outcome["state"], "REVIEW_PENDING")
        gate.touch()
        wait_for(done)
        self.assertIs(streams_match(str(evidence), self.calls[0][1].streams), False,
                      "the later write is still detectable against the recorded facts")

    def test_a_stream_replaced_during_the_run_stops_the_review_as_start_unknown(self):
        op, evidence = self.operation()
        outcome = self.run_op(op, self.spec(op, RENAME_DURING_RUN, str(evidence)), evidence)
        self.assertEqual(outcome["decision"]["status"], "STOPPED")
        self.assert_unknown(op, outcome, "START_UNKNOWN")

    def test_a_stream_that_could_not_be_digested_stops_the_review_as_start_unknown(self):
        op, evidence = self.operation()

        def failing_pread(*args, **kwargs):
            raise OSError(errno.EIO, "Input/output error")

        with mock.patch.object(os, "pread", failing_pread):
            outcome = self.run_op(op, self.spec(op, BOTH_STREAMS), evidence)
        self.assertEqual(outcome["decision"]["status"], "STOPPED")
        self.assert_unknown(op, outcome, "START_UNKNOWN")

    def test_the_streams_are_checked_through_the_module_name(self):
        # A test (or a caller) can see the check: a refusal at that point alone
        # stops the review, with the files untouched.
        op, evidence = self.operation()
        with mock.patch.object(bridge, "streams_match", lambda directory, streams: False):
            outcome = self.run_op(op, self.spec(op, LEADER_ONLY), evidence)
        self.assert_unknown(op, outcome, "CLEANUP_UNCONFIRMED")

    def test_a_failing_before_finish_is_start_unknown_and_reraised(self):
        op, evidence = self.operation()

        def refuse(run):
            raise ValueError("before_finish refused")

        with self.assertRaises(ValueError):
            self.run_op(op, self.spec(op, LEADER_ONLY), evidence, before_finish=refuse)
        self.assertEqual(self.state_of(op), "START_UNKNOWN")
        self.assertEqual(self.unknown_reason(op), "START_UNKNOWN")
        self.assertEqual(self.calls, [], "candidate_hash ran after before_finish refused")
        self.assertTrue(self.held())

    def test_before_finish_runs_only_on_the_review_path(self):
        with self.subTest(case="an exec failure"):
            garbage = self.cwd / "not-a-program"
            garbage.write_bytes(GARBAGE)
            garbage.chmod(0o755)
            op, evidence = self.operation()
            outcome = self.run_op(op, self.spec(op, None, command=(str(garbage),)), evidence)
            self.assertEqual(outcome["state"], "NOT_STARTED")
            self.assertEqual(self.calls, [])
        with self.subTest(case="an unconfirmed cleanup"):
            escaped = self.work / "escaped.pid"
            self.addCleanup(self.kill_pid_in, escaped)
            op, evidence = self.operation("task-2")
            outcome = self.run_op(op, self.spec(op, ZOMBIE_GROUP_LEADER, str(escaped)), evidence)
            self.assertEqual(outcome["state"], "START_UNKNOWN")
            self.assertEqual(self.unknown_reason(op), "CLEANUP_UNCONFIRMED")
            self.assertEqual(self.calls, [])

    def test_refusals_before_the_handoff_start_nothing(self):
        op, evidence = self.operation()
        legal = dict(spec=self.spec(op, LEADER_ONLY))
        cases = {
            "a before_finish that is not callable": (dict(legal, before_finish=5), "E_RUNTIME_INVOCATION"),
            "a specification for another request": (dict(spec=self.spec(op, LEADER_ONLY, request_id="req-2")),
                                                    "E_RUNTIME_INVOCATION"),
            "a specification for a request that does not exist": (
                dict(spec=self.spec(op, LEADER_ONLY, request_id="req-9")), "E_RUNTIME_INVOCATION"),
        }
        before = self.scheduler.events
        for label, (arguments, code) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(RuntimeErrorCode) as ctx:
                    self.run_op(op, evidence=evidence, **arguments)
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(self.scheduler.events, before)
                self.assertEqual(self.state_of(op), "RESERVED")
                self.assertEqual(os.listdir(evidence), [])
        with self.subTest(case="an operation the scheduler does not know"):
            missing = "op-" + "0" * 32
            with self.assertRaises(ContractError) as ctx:
                self.run_op(missing, self.spec(missing, LEADER_ONLY), evidence)
            self.assertEqual(ctx.exception.code, "E_OPERATION_UNKNOWN")
            self.assertEqual((self.scheduler.events, os.listdir(evidence)), (before, []))
        # The legal call still works on the same ledger.
        self.assertEqual(self.run_op(op, evidence=evidence, **legal)["state"], "REVIEW_PENDING")

    # recovery after a rollover ---------------------------------------------

    def recorded_run(self, code, *args, **patches):
        op, evidence = self.operation()
        self.scheduler.handoff(op, "owner-1", 1)
        spec = self.spec(op, code, *args)
        with mock.patch.multiple(os, **patches) if patches else _null():
            record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
        self.scheduler.rollover()
        return op, spec, evidence

    def recover(self, op, spec, evidence):
        return recover_supervised_operation(self.scheduler, operation_id=op, owner="owner-2", generation=2,
                                            spec=spec, evidence_dir=str(evidence), candidate_hash=self.candidate_hash)

    def test_recovery_reviews_a_run_only_while_its_streams_match(self):
        with self.subTest(case="streams as recorded"):
            op, spec, evidence = self.recorded_run(LEADER_ONLY)
            outcome = self.recover(op, spec, evidence)
            self.assertEqual((outcome["state"], self.state_of(op)), ("REVIEW_PENDING", "REVIEW_PENDING"))
            self.assertEqual([call[0] for call in self.calls], ["candidate_hash"])
        self.fresh_ledger()
        with self.subTest(case="stdout appended after the record"):
            op, spec, evidence = self.recorded_run(LEADER_ONLY)
            with open(evidence / "stdout.log", "ab") as handle:
                handle.write(b"late\n")
            outcome = self.recover(op, spec, evidence)
            self.assertEqual((outcome["state"], self.state_of(op)), ("START_UNKNOWN", "START_UNKNOWN"))
            self.assertEqual(outcome["decision"]["status"], "STOPPED")
            self.assertEqual(self.calls, [])
            self.assertEqual(self.events_of(op, "scheduler.recovered")[0]["payload"]["outcome"], "START_UNKNOWN")
        self.fresh_ledger()
        with self.subTest(case="streams that could not be digested"):
            def failing_pread(*args, **kwargs):
                raise OSError(errno.EIO, "Input/output error")

            op, spec, evidence = self.recorded_run(BOTH_STREAMS, pread=failing_pread)
            self.assertEqual(recorded_streams(spec, str(evidence)), {"stdout": None, "stderr": None})
            outcome = self.recover(op, spec, evidence)
            self.assertEqual((outcome["state"], outcome["decision"]["status"]), ("START_UNKNOWN", "STOPPED"))
            self.assertEqual(self.calls, [])

    def test_recovery_refuses_a_specification_of_another_request(self):
        op, spec, evidence = self.recorded_run(LEADER_ONLY)
        foreign = self.spec(op, LEADER_ONLY, request_id="req-2")
        before = self.scheduler.events
        with self.assertRaises(RuntimeErrorCode) as ctx:
            self.recover(op, foreign, evidence)
        self.assertEqual(ctx.exception.code, "E_RUNTIME_INVOCATION")
        self.assertEqual(self.scheduler.events, before)
        self.assertEqual(self.state_of(op), "HANDOFF_ACCEPTED")


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class AdapterOracle(Base):
    def invocation(self, op, code, *args, request_id="req-1"):
        return Invocation(request_id, op, "executor", "stub-profile", (PY, "-c", code, *args), (),
                          environment_digest(BASE_ENV), None)

    def run_op(self, invocation, evidence, **kwargs):
        kwargs.setdefault("usage_dimensions", DIMENSIONS)
        return run_adapter_operation(self.scheduler, OfflineAdapter(), invocation, cwd=str(self.cwd),
                                     timeout_seconds=10, environment=BASE_ENV, owner="owner-1", generation=1,
                                     evidence_dir=str(evidence), worktree_id="wt-1",
                                     candidate_hash=self.candidate_hash, **kwargs)

    def usage_of(self, op, dimension):
        usage = self.scheduler._operations[op].usage.get(dimension)
        return None if usage is None else (usage.amount, usage.final, usage.evidence_hash)

    def test_usage_is_settled_against_the_supervisors_digest(self):
        op, evidence = self.operation()
        invocation = self.invocation(op, emit(COMPLETE))
        outcome = self.run_op(invocation, evidence)
        self.assertEqual(outcome["state"], "REVIEW_PENDING")
        recorded = recorded_streams(InvocationSpec("req-1", op, invocation.command, str(self.cwd),
                                                   invocation.environment_sha256, 10), str(evidence))
        self.assertEqual(recorded["stdout"], facts_of(evidence / "stdout.log"))
        digest = recorded["stdout"]["sha256"]
        self.assertEqual(outcome["stream"], {"status": "ok", "sha256": digest, "bytes": recorded["stdout"]["bytes"]})
        self.assertEqual((self.usage_of(op, "tokens"), self.usage_of(op, "cost")),
                         ((12, True, digest), (3, True, digest)))
        self.assertEqual(outcome["usage_recorded"], {"tokens": "APPLIED", "cost": "APPLIED"})
        self.assertEqual([call[0] for call in self.calls], ["candidate_hash"])

    def test_an_unreserved_usage_dimension_is_refused_before_the_handoff(self):
        op, evidence = self.operation()
        before = self.scheduler.events
        for dimensions in (("tokens", "energy"), ("energy",), ("cost", "tokens", "seconds")):
            with self.subTest(dimensions=dimensions):
                with self.assertRaises(AdapterError) as raised:
                    self.run_op(self.invocation(op, emit(COMPLETE)), evidence, usage_dimensions=dimensions)
                self.assertEqual(raised.exception.code, "E_ADAPTER_SCHEMA")
                self.assertEqual(self.scheduler.events, before)
                self.assertEqual(self.state_of(op), "RESERVED")
                self.assertEqual(os.listdir(evidence), [])
        with self.subTest(case="an operation the scheduler does not know"):
            missing = "op-" + "0" * 32
            with self.assertRaises(ContractError) as raised:
                self.run_op(self.invocation(missing, emit(COMPLETE)), evidence)
            self.assertEqual(raised.exception.code, "E_OPERATION_UNKNOWN")
            self.assertEqual((self.scheduler.events, os.listdir(evidence)), (before, []))
        with self.subTest(case="a dimension only another request has, named by an invocation for that request"):
            # The operation's own reservation decides, and it is checked before
            # the bridge looks at the request: the adapter refuses first.
            with self.assertRaises(AdapterError) as raised:
                self.run_op(self.invocation(op, emit(COMPLETE), request_id="req-2"), evidence,
                            usage_dimensions=("energy",))
            self.assertEqual(raised.exception.code, "E_ADAPTER_SCHEMA")
            self.assertEqual((self.scheduler.events, os.listdir(evidence)), (before, []))
        with self.subTest(case="an invocation for another request"):
            with self.assertRaises(RuntimeErrorCode) as raised:
                self.run_op(self.invocation(op, emit(COMPLETE), request_id="req-2"), evidence)
            self.assertEqual(raised.exception.code, "E_RUNTIME_INVOCATION")
            self.assertEqual((self.scheduler.events, os.listdir(evidence)), (before, []))
        # Legal baseline on the same ledger: every listed dimension is reserved.
        self.assertEqual(self.run_op(self.invocation(op, emit(COMPLETE)), evidence)["usage_recorded"],
                         {"tokens": "APPLIED", "cost": "APPLIED"})

    def test_an_escaped_writer_after_the_reap_records_no_usage(self):
        op, evidence = self.operation()
        pidfile, gate, done = self.escape("stdout-append")
        self.scheduler.gate, self.scheduler.done = gate, done
        try:
            outcome = self.run_op(self.invocation(op, emit(COMPLETE) + ESCAPE, str(pidfile), str(gate), str(done),
                                                  "stdout-append"), evidence)
        finally:
            self.scheduler.gate = self.scheduler.done = None
        self.assertEqual((outcome["state"], self.unknown_reason(op)), ("START_UNKNOWN", "CLEANUP_UNCONFIRMED"))
        self.assertEqual((outcome["usage_recorded"], outcome["usage_skipped"]), ({}, list(DIMENSIONS)))
        self.assertEqual((self.usage_of(op, "tokens"), self.usage_of(op, "cost")), (None, None))
        self.assertEqual(self.calls, [])

    def test_a_stream_changed_after_the_check_records_no_usage(self):
        # The bridge's check passes; the stream changes before the adapter reads
        # it.  Only the adapter's own comparison with the in-memory facts can
        # notice.
        op, evidence = self.operation()
        real = bridge.streams_match

        def check_then_append(directory, streams):
            result = real(directory, streams)
            with open(os.path.join(directory, "stdout.log"), "ab") as handle:
                handle.write(b'{"forged": true}\n')
            return result

        with mock.patch.object(bridge, "streams_match", check_then_append):
            outcome = self.run_op(self.invocation(op, emit(COMPLETE)), evidence)
        self.assertEqual(outcome["stream"], {"status": "changed", "sha256": None, "bytes": None})
        self.assertEqual((outcome["result"], outcome["result_error"]), (None, None))
        self.assertEqual((outcome["usage_recorded"], outcome["usage_skipped"]), ({}, list(DIMENSIONS)))
        self.assertEqual((self.usage_of(op, "tokens"), self.usage_of(op, "cost")), (None, None))
        # The run itself went to review; its usage stays unknown, which stops the request.
        self.assertEqual(outcome["state"], "REVIEW_PENDING")
        self.assertEqual(self.scheduler._requests["req-1"].stopped, "USAGE_UNKNOWN")

    def test_read_stream_compares_with_the_expected_facts(self):
        evidence = self.work / "facts"
        evidence.mkdir()
        path = evidence / "stdout.log"
        raw = b"".join((json.dumps(event) + "\n").encode() for event in COMPLETE)
        path.write_bytes(raw)
        good = facts_of(path)
        self.assertEqual(read_stream(str(evidence), expected=good)["status"], "ok", "legal baseline")
        self.assertEqual(read_stream(str(evidence), expected=None)["status"], "ok")

        def appended():
            with open(path, "ab") as handle:
                handle.write(b"\n")

        def rewritten():
            with open(path, "r+b") as handle:
                handle.write(b" ")

        def replaced():
            copy = evidence / "copy.tmp"
            copy.write_bytes(raw)
            os.replace(copy, path)

        for label, change, status in (("appended", appended, "changed"), ("rewritten in place", rewritten, "changed"),
                                      ("replaced by an identical copy", replaced, "changed")):
            with self.subTest(case=label):
                path.write_bytes(raw)
                facts = facts_of(path)
                self.assertEqual(read_stream(str(evidence), expected=facts)["status"], "ok")
                change()
                self.assertEqual(read_stream(str(evidence), expected=facts),
                                 {"status": status, "sha256": None, "bytes": None, "events": None})
        with self.subTest(case="a changed stream over the ceiling"):
            # The comparison with the facts comes first: a stream that is not
            # what the supervisor recorded is changed, whatever its size.
            path.write_bytes(raw)
            facts = facts_of(path)
            appended()
            self.assertEqual(read_stream(str(evidence), max_bytes=len(raw) - 1, expected=facts)["status"], "changed")
        with self.subTest(case="a matching stream over the ceiling"):
            path.write_bytes(raw)
            facts = facts_of(path)
            self.assertEqual(read_stream(str(evidence), max_bytes=len(raw) - 1, expected=facts)["status"],
                             "too_large")
        path.write_bytes(raw)
        good = facts_of(path)
        malformed = {"a list": list(good.values()), "without sha256": {k: v for k, v in good.items() if k != "sha256"},
                     "with an extra key": dict(good, mtime=0), "bytes as a bool": dict(good, bytes=True),
                     "sha256 in upper case": dict(good, sha256=good["sha256"].upper()),
                     "a negative inode": dict(good, inode=-1)}
        for label, expected in malformed.items():
            with self.subTest(case="expected " + label):
                with self.assertRaises(AdapterError) as raised:
                    read_stream(str(evidence), expected=expected)
                self.assertEqual(raised.exception.code, "E_ADAPTER_SCHEMA")


if __name__ == "__main__":
    unittest.main()
