"""Gap tests for ``sentinelmesh.adapter_run``.

The planner oracle (``tests/test_adapter_run_oracle.py``) covers the main
contract: usage recorded only for a settled and complete result on the review
path, nothing recorded anywhere else, and the pre-handoff refusals.  What it does
not cover is here:

* ``read_stream`` on the shapes a child's stdout can take that the oracle's
  streams do not: CRLF line endings, a final line without a newline, a blank
  line, a named pipe, a symlink, and the exact byte ceiling with its rejection
  of an out-of-range ``max_bytes``;
* a caller whose ``candidate_hash`` cannot produce a candidate: the usage must
  already be recorded while the operation is still RUNNING, the operation must
  end ``START_UNKNOWN`` and the original exception (including a
  ``KeyboardInterrupt``) must reach the caller unchanged;
* a run killed on timeout with a confirmed cleanup, which is a review like any
  other, so its usage is recorded like any other;
* an empty set of usage dimensions, which records nothing and leaves the request
  unclaimable rather than pretending the run was free;
* a relative ``cwd``, refused by the supervisor after the handoff.

Every case starts a real local ``python`` process that prints the synthetic
normalized event stream of `.agent/design/adapter-supervisor-wiring.md` -- not
any vendor's native output, and no provider CLI, credential or network -- and
kills whatever it started.  The refusal cases first assert a legal baseline and
then change exactly one thing.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from sentinelmesh.adapter_protocol import AdapterError, Invocation, OfflineAdapter
from sentinelmesh.adapter_run import (
    DEFAULT_MAX_STREAM_BYTES,
    MAX_STREAM_BYTES,
    STREAM_NAME,
    read_stream,
    run_adapter_operation,
    supervised_spec,
)
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.scheduler import ContractError, Scheduler

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
CAPS = {"dispatches": 5, "tokens": 100, "cost": 50}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
DIMENSIONS = ("tokens", "cost")


def ev(seq, typ, payload=None):
    return {"seq": seq, "event_id": "e%d" % seq, "type": typ, "source": "offline",
            "payload": {} if payload is None else payload}


COMPLETE = [ev(0, "run_started"), ev(1, "assistant"), ev(2, "usage", {"tokens": 12, "cost": 3}), ev(3, "run_finished")]


def emit(events, after=""):
    """Python code that prints each event as one JSON line, then runs ``after``."""
    return ("import json, sys\n"
            "for event in json.loads(%r):\n"
            "    sys.stdout.write(json.dumps(event) + '\\n')\n"
            "sys.stdout.flush()\n" % json.dumps(events)) + after


# Prints the complete stream, reports its own pid, then sleeps long enough that
# the timeout -- not the program -- ends the run.
REPORT_PID_AND_SLEEP = ("import os, sys, time\n"
                        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                        "time.sleep(30)\n")


class RefusingAdapter(OfflineAdapter):
    """Refuses the stream with a code of its own, the way a provider adapter would."""

    def run(self, invocation, events):
        raise AdapterError("E_ADAPTER_SOURCE", "event source does not match adapter", seq=1)


class InvocationSubclass(Invocation):
    """A subclass is not the frozen ``Invocation``; only the exact type is."""


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class AdapterRunGaps(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="adapter-run-gaps-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.scheduler = Scheduler()
        self.scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        for task in ("task-1", "task-2"):
            self.scheduler.seed_task("req-1", task, CAPS, max_attempts=3)
        self.count = 0

    def candidate_hash(self, evidence_dir):
        return "c" * 64

    def operation(self, task="task-1"):
        self.count += 1
        claim = self.scheduler.claim("req-1", task, 1, "idem-%d" % self.count, "owner-1", 1, 1, RESERVATION,
                                     ["src/%s.py" % task])
        evidence = self.work / ("evidence-%d" % self.count)
        evidence.mkdir()
        return claim["operation_id"], evidence

    def invocation(self, op, code, *args, environment=None, adapter_id=None):
        return Invocation("req-1", op, "executor", "stub-profile", (PY, "-c", code, *args), (),
                          environment_digest(environment or BASE_ENV), adapter_id)

    def run_op(self, invocation, evidence, adapter=None, environment=None, **kwargs):
        kwargs.setdefault("usage_dimensions", DIMENSIONS)
        kwargs.setdefault("timeout_seconds", 10)
        kwargs.setdefault("candidate_hash", self.candidate_hash)
        return run_adapter_operation(self.scheduler, adapter or OfflineAdapter(), invocation, cwd=str(self.cwd),
                                     environment=environment or BASE_ENV, owner="owner-1", generation=1,
                                     evidence_dir=str(evidence), worktree_id="wt-1", **kwargs)

    def usage_of(self, op, dimension):
        usage = self.scheduler._operations[op].usage.get(dimension)
        return None if usage is None else (usage.call_key, usage.revision, usage.amount, usage.final,
                                           usage.evidence_hash)

    def kill_pid_in(self, path: Path):
        """Kill the pid recorded in ``path``, so a failing case leaves nothing running."""
        try:
            pid = int(path.read_text().strip())
        except (OSError, ValueError):
            return
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def assert_nothing_recorded(self, op, outcome):
        self.assertEqual(outcome["usage_recorded"], {})
        self.assertEqual(outcome["usage_skipped"], list(DIMENSIONS))
        self.assertEqual((self.usage_of(op, "tokens"), self.usage_of(op, "cost")), (None, None))

    def assert_stopped(self, stopped="USAGE_UNKNOWN"):
        self.assertEqual(self.scheduler._requests["req-1"].stopped, stopped)
        with self.assertRaises(ContractError) as raised:
            self.operation("task-2")
        self.assertEqual(raised.exception.code, "E_STOPPED")

    # read_stream: the shapes a child's stdout can take --------------------

    def write_stream(self, raw):
        evidence = self.work / ("stream-%d" % self.count)
        evidence.mkdir()
        self.count += 1
        (evidence / STREAM_NAME).write_bytes(raw)
        return evidence

    def test_a_stream_with_crlf_endings_and_no_final_newline_is_parsed(self):
        cases = [("CRLF endings", b"\r\n".join(json.dumps(event).encode() for event in COMPLETE) + b"\r\n", COMPLETE),
                 ("no final newline", b"\n".join(json.dumps(event).encode() for event in COMPLETE), COMPLETE),
                 ("one line", json.dumps(COMPLETE[0]).encode() + b"\r\n", [COMPLETE[0]])]
        for label, raw, expected in cases:
            with self.subTest(label):
                self.setUp()
                evidence = self.write_stream(raw)
                outcome = read_stream(str(evidence))
                self.assertEqual(outcome, {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(),
                                           "bytes": len(raw), "events": expected})

    def test_a_blank_line_names_its_line_and_is_not_an_event(self):
        self.setUp()
        raw = b"".join(json.dumps(event).encode() + b"\n" for event in COMPLETE)
        baseline = self.write_stream(raw)
        self.assertEqual(read_stream(str(baseline))["status"], "ok")
        for label, broken in [("spaces", raw + b"   \n"), ("tabs", raw + b"\t\n"), ("empty", raw + b"\n")]:
            with self.subTest(label):
                evidence = self.write_stream(broken)
                outcome = read_stream(str(evidence))
                self.assertEqual(outcome["status"], "invalid")
                self.assertEqual(outcome["line"], len(COMPLETE) + 1)
                self.assertEqual((outcome["sha256"], outcome["bytes"], outcome["events"]),
                                 (hashlib.sha256(broken).hexdigest(), len(broken), None))
        # A line that is valid JSON but not an event object is reported the
        # same way, with this module's own code rather than the parser's.
        self.assertEqual(read_stream(str(self.write_stream(b"{}\n")))["events"], [{}])
        for label, broken in [("number", b"12\n"), ("string", b'"text"\n'), ("array", b"[]\n")]:
            with self.subTest(label):
                self.setUp()
                evidence = self.write_stream(broken)
                outcome = read_stream(str(evidence))
                self.assertEqual(outcome, {"status": "invalid", "sha256": hashlib.sha256(broken).hexdigest(),
                                           "bytes": len(broken), "events": None, "line": 1,
                                           "code": "E_ADAPTER_STREAM"})

    def test_a_fifo_is_unreadable_and_does_not_block(self):
        self.setUp()
        evidence = self.write_stream(json.dumps(COMPLETE[0]).encode() + b"\n")
        self.assertEqual(read_stream(str(evidence))["status"], "ok")  # legal baseline: a regular file
        os.unlink(evidence / STREAM_NAME)
        os.mkfifo(evidence / STREAM_NAME)  # no writer will ever appear
        # The read runs in a thread so a reader that blocks makes this case fail
        # instead of hanging the session (planner fix after review F6).
        outcomes = []
        reader = threading.Thread(target=lambda: outcomes.append(read_stream(str(evidence))), daemon=True)
        reader.start()
        reader.join(5.0)
        if reader.is_alive():
            os.close(os.open(evidence / STREAM_NAME, os.O_WRONLY))  # unblock the reader, then fail
            reader.join(5.0)
            self.fail("opening a fifo must not wait for a writer")
        self.assertEqual(outcomes, [{"status": "unreadable", "sha256": None, "bytes": None, "events": None}])

    def test_a_symlink_to_a_stream_is_unreadable(self):
        self.setUp()
        raw = json.dumps(COMPLETE[0]).encode() + b"\n"
        evidence = self.write_stream(raw)
        self.assertEqual(read_stream(str(evidence))["status"], "ok")  # legal baseline
        target = self.work / "target.jsonl"
        target.write_bytes(raw)
        os.unlink(evidence / STREAM_NAME)
        os.symlink(target, evidence / STREAM_NAME)
        self.assertEqual(read_stream(str(evidence)),
                         {"status": "unreadable", "sha256": None, "bytes": None, "events": None})

    def test_the_byte_ceiling_accepts_the_limit_and_refuses_one_more(self):
        self.setUp()
        raw = b"".join(json.dumps(event).encode() + b"\n" for event in COMPLETE)
        self.assertEqual(read_stream(str(self.write_stream(raw)), max_bytes=len(raw))["status"], "ok")
        self.assertEqual(read_stream(str(self.write_stream(raw)), max_bytes=len(raw) - 1)["status"], "too_large")
        # The ceiling is asked for per call: one byte past it is refused without
        # the stream being pulled in whole, and a raised ceiling reads it as
        # bytes that are simply not an event object.
        grew = self.write_stream(b"x" * (DEFAULT_MAX_STREAM_BYTES + 1))
        self.assertEqual(read_stream(str(grew))["status"], "too_large")
        larger = read_stream(str(grew), max_bytes=MAX_STREAM_BYTES)
        # With the ceiling raised the same bytes are read: they are not an event
        # object, and one line this long is over the parser's own per-document
        # limit, so the answer is now the parser's code.
        self.assertEqual((larger["status"], larger["bytes"], larger["line"], larger["code"]),
                         ("invalid", DEFAULT_MAX_STREAM_BYTES + 1, 1, "E_JSON_LIMIT"))

    def test_an_out_of_range_stream_limit_is_refused(self):
        self.setUp()
        raw = json.dumps(COMPLETE[0]).encode() + b"\n"
        evidence = self.write_stream(raw)
        self.assertEqual(read_stream(str(evidence)), {"status": "ok", "sha256": hashlib.sha256(raw).hexdigest(),
                                                      "bytes": len(raw), "events": [COMPLETE[0]]})
        for limit in (0, -1, True, 1.5, "64", MAX_STREAM_BYTES + 1, 16 * 1_048_576 * 2):
            with self.subTest(limit=limit):
                with self.assertRaises(AdapterError) as raised:
                    read_stream(str(evidence), max_bytes=limit)
                self.assertEqual(raised.exception.code, "E_ADAPTER_SCHEMA")

    def test_a_missing_stream_is_an_answer_not_an_error(self):
        self.setUp()
        evidence = self.work / "empty-dir"
        evidence.mkdir()
        self.assertEqual(read_stream(str(evidence)),
                         {"status": "missing", "sha256": None, "bytes": None, "events": None})
        self.assertEqual(os.listdir(evidence), [], "reading a stream must not create one")

    def test_a_subclass_of_invocation_is_not_the_frozen_invocation(self):
        self.setUp()
        op, evidence = self.operation()
        command, digest, timeout = (PY, "-c", "pass"), environment_digest(BASE_ENV), 30
        # Legal baseline: the exact ``Invocation`` type.
        self.assertEqual(supervised_spec(Invocation("req-1", op, "executor", "stub-profile", command, (), digest),
                                         cwd=str(self.cwd), timeout_seconds=timeout),
                         InvocationSpec("req-1", op, command, str(self.cwd), digest, timeout))
        # Only the type changed: a subclass could resolve the run id or the
        # digest differently from the record the replay is checked against.
        subclass = InvocationSubclass("req-1", op, "executor", "stub-profile", command, (), digest)
        with self.assertRaises(AdapterError) as raised:
            supervised_spec(subclass, cwd=str(self.cwd), timeout_seconds=timeout)
        self.assertEqual(raised.exception.code, "E_ADAPTER_INVOCATION")

    # a caller that cannot produce a candidate ------------------------------

    def test_a_candidate_the_caller_cannot_hash_leaves_the_usage_recorded(self):
        for error in (RuntimeError("no candidate"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                self.setUp()
                op, evidence = self.operation()
                seen = []

                def candidate(directory):
                    # The usage must be final while the operation is still
                    # RUNNING: that is the only moment the bridge offers, and
                    # ``finish`` is what stops a request whose usage is not.
                    seen.append((self.scheduler._operations[op].state, self.usage_of(op, "tokens"),
                                 [event["type"] for event in self.scheduler.events
                                  if event["operation_id"] == op]))
                    raise error

                with self.assertRaises(type(error)) as raised:
                    self.run_op(self.invocation(op, emit(COMPLETE)), evidence, candidate_hash=candidate)
                self.assertIs(raised.exception, error)
                self.assertEqual(len(seen), 1)
                state, usage, events = seen[0]
                self.assertEqual(state, "RUNNING")
                self.assertEqual(usage, ("local:%s:tokens" % op, 1, 12, True, sha256_of(evidence / "stdout.log")))
                self.assertNotIn("run.finished_for_review", events)
                # The bridge turns a refused candidate into an unknown start and
                # keeps the usage it already recorded.
                self.assertEqual(self.scheduler._operations[op].state, "START_UNKNOWN")
                self.assertEqual(self.usage_of(op, "cost"),
                                 ("local:%s:cost" % op, 1, 3, True, sha256_of(evidence / "stdout.log")))
                self.assert_stopped("START_UNKNOWN")

    def test_an_adapter_that_refuses_the_stream_carries_its_own_code(self):
        self.setUp()
        op, evidence = self.operation()
        outcome = self.run_op(self.invocation(op, emit(COMPLETE)), evidence, adapter=RefusingAdapter())
        self.assertEqual(outcome["stream"]["status"], "ok")
        self.assertEqual((outcome["result"], outcome["result_error"]), (None, "E_ADAPTER_SOURCE"))
        self.assert_nothing_recorded(op, outcome)
        self.assert_stopped()

    def test_an_empty_stream_is_a_complete_run_with_no_result(self):
        self.setUp()
        op, evidence = self.operation()
        outcome = self.run_op(self.invocation(op, "pass"), evidence)
        self.assertEqual((outcome["state"], outcome["exit_code"]), ("REVIEW_PENDING", 0))
        self.assertEqual(outcome["stream"], {"status": "ok", "sha256": hashlib.sha256(b"").hexdigest(),
                                             "bytes": 0})
        self.assertEqual((outcome["result"], outcome["result_error"]), (None, "E_ADAPTER_SETTLEMENT"))
        self.assert_nothing_recorded(op, outcome)
        self.assert_stopped()

    def test_a_timed_out_run_with_a_confirmed_cleanup_records_its_usage(self):
        self.setUp()
        pid_path = self.work / "sleeper.pid"
        # Registered before the run, so a failed assertion below never leaves the
        # sleeper running (planner fix after review F6).
        self.addCleanup(self.kill_pid_in, pid_path)
        op, evidence = self.operation()
        code = emit(COMPLETE, REPORT_PID_AND_SLEEP)
        started = time.monotonic()
        outcome = self.run_op(self.invocation(op, code, str(pid_path)), evidence, timeout_seconds=1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 30.0, "the timeout must end the run, not the test")
        self.assertEqual(outcome["state"], "REVIEW_PENDING")
        self.assertEqual(outcome["stop_reason"], "timeout")
        self.assertEqual((outcome["result"]["settled"], outcome["result"]["raw_complete"]), (True, True))
        self.assertEqual(outcome["usage_recorded"], {"tokens": "APPLIED", "cost": "APPLIED"})
        self.assertEqual((outcome["usage_skipped"], outcome["result_error"]), ([], None))
        self.assertEqual(self.usage_of(op, "tokens"),
                         ("local:%s:tokens" % op, 1, 12, True, sha256_of(evidence / "stdout.log")))
        self.operation("task-2")  # the request can claim again
        self.assertIsNone(self.scheduler._requests["req-1"].stopped)
        # The timeout killed the process group: nothing the run started is left
        # running, which is what "cleanup confirmed" means here.
        self.assertTrue(pid_path.exists(), "the sleeper never reported its pid")
        pid = int(pid_path.read_text().strip())
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                break
            time.sleep(0.05)
        else:
            self.fail("pid %d is still alive after a confirmed cleanup" % pid)

    # the dimensions and the cwd -------------------------------------------

    def test_no_usage_dimensions_records_nothing(self):
        self.setUp()
        op, evidence = self.operation()
        outcome = self.run_op(self.invocation(op, emit(COMPLETE)), evidence, usage_dimensions=())
        self.assertEqual((outcome["usage_recorded"], outcome["usage_skipped"]), ({}, []))
        self.assertEqual((outcome["result"]["settled"], outcome["result"]["raw_complete"]), (True, True))
        self.assertEqual(outcome["result"]["usage"], {"tokens": 12, "cost": 3})
        self.assertEqual(outcome["result_error"], None)
        self.assertEqual((self.usage_of(op, "tokens"), self.usage_of(op, "cost")), (None, None))
        # Nothing recorded means nothing is final at the review, so the request
        # is stopped rather than allowed to claim again on an unproven amount.
        self.assert_stopped()

    def test_a_relative_cwd_is_refused_after_the_handoff(self):
        self.setUp()
        op, evidence = self.operation()
        outcome = run_adapter_operation(self.scheduler, OfflineAdapter(), self.invocation(op, emit(COMPLETE)),
                                        cwd="tree", environment=BASE_ENV, owner="owner-1", generation=1,
                                        evidence_dir=str(evidence), worktree_id="wt-1",
                                        candidate_hash=self.candidate_hash, usage_dimensions=DIMENSIONS,
                                        timeout_seconds=10)
        self.assertEqual((outcome["state"], outcome["error"], outcome["stop_reason"], outcome["exit_code"]),
                         ("NOT_STARTED", "E_RUNTIME_INVOCATION", None, None))
        self.assertEqual(outcome["stream"], {"status": "missing", "sha256": None, "bytes": None})
        self.assertEqual((outcome["result"], outcome["result_error"]), (None, None))
        self.assertEqual((outcome["usage_recorded"], outcome["usage_skipped"]), ({}, list(DIMENSIONS)))
        self.assertEqual(os.listdir(evidence), [], "a run that never started wrote no stream")
        self.assertEqual(self.scheduler._operations[op].state, "NOT_STARTED")
        self.assertIsNone(self.scheduler._requests["req-1"].stopped)
        self.operation("task-2")  # a run proven not to have started does not block the request

    def test_a_runtime_refusal_reaches_the_caller_unchanged(self):
        """A float timeout is the runtime's own refusal, not this module's."""
        self.setUp()
        op, evidence = self.operation()
        before = tuple(self.scheduler.events)
        with self.assertRaises(RuntimeErrorCode) as raised:
            self.run_op(self.invocation(op, emit(COMPLETE)), evidence, timeout_seconds=1.5)
        self.assertEqual(raised.exception.code, "E_RUNTIME_TIMEOUT")
        self.assertEqual(self.scheduler.events, before, "a refused call changed the Scheduler")
        self.assertEqual(self.scheduler._operations[op].state, "RESERVED")
        self.assertEqual(os.listdir(evidence), [], "a refused call wrote evidence")

    def test_a_cwd_the_runtime_refuses_is_refused_before_the_handoff(self):
        """The specification is built before the handoff, so its refusals land there too."""
        self.setUp()
        op, evidence = self.operation()
        before = tuple(self.scheduler.events)
        with self.assertRaises(RuntimeErrorCode) as raised:
            run_adapter_operation(self.scheduler, OfflineAdapter(), self.invocation(op, emit(COMPLETE)),
                                  cwd="", environment=BASE_ENV, owner="owner-1", generation=1,
                                  evidence_dir=str(evidence), worktree_id="wt-1",
                                  candidate_hash=self.candidate_hash, usage_dimensions=DIMENSIONS,
                                  timeout_seconds=10)
        self.assertEqual(raised.exception.code, "E_RUNTIME_INVOCATION")
        self.assertEqual(self.scheduler.events, before, "a refused call changed the Scheduler")
        self.assertEqual(self.scheduler._operations[op].state, "RESERVED")
        self.assertEqual(os.listdir(evidence), [], "a refused call wrote evidence")


if __name__ == "__main__":
    unittest.main()
