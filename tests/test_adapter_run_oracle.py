"""Acceptance oracle for running adapter invocations under the native supervisor.

Planner-provided; executors must not modify this file.  Cases start real local
``python`` processes on macOS that print a normalized adapter event stream (the
synthetic protocol of `.agent/design/adapter-supervisor-wiring.md`, not any
vendor's native output; no provider CLI, no credentials, no network) and check:

* usage is recorded in the Scheduler only for a run that is being finished for
  review with a settled and complete result: one all-or-nothing ``settle``
  whose evidence is the SHA-256 of the whole stdout stream, made while the
  operation is still RUNNING, so ``finish`` sees final usage and the request
  can claim again;
* any other stream (no terminal, an unknown event, invalid JSON, too large,
  replaced by a link) or any other run state records nothing: ``finish`` then
  stops the request with USAGE_UNKNOWN (an unconfirmed cleanup stops it with
  START_UNKNOWN), and later claims are refused;
* checks on the invocation, the adapter, the usage dimensions, the stream limit
  and the timeout all run before anything is handed off or started.

Each case kills whatever it started, even when an assertion fails.
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

from sentinelmesh.adapter_protocol import AdapterError, AdapterResult, Invocation, OfflineAdapter
from sentinelmesh.adapter_run import read_stream, run_adapter_operation, supervised_spec
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.scheduler import ContractError, Scheduler

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
CANDIDATE = "c" * 64
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
# The group leader exits while an escaped descendant lives on: cleanup is unconfirmed.
LEAVE_ESCAPED_PARENT = ("import os, subprocess, time\n"
                        "subprocess.Popen([sys.executable, '-c', %r, sys.argv[1]])\n"
                        "while not os.path.exists(sys.argv[1]):\n"
                        "    time.sleep(0.02)\n" % ESCAPED_PARENT)
# The child swaps its own stdout record for a link to a forged, complete stream.
REPLACE_STREAM_WITH_LINK = ("import os\n"
                            "path = sys.argv[1]\n"
                            "os.symlink(sys.argv[2], path + '.lnk')\n"
                            "os.replace(path + '.lnk', path)\n")


def pid_in(path: Path):
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text else None


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StubAdapter(OfflineAdapter):
    adapter_id = "stub"


class ForcedResultAdapter(OfflineAdapter):
    """Reduces the stream as usual, then reports a chosen settled/raw_complete pair with usage."""

    def __init__(self, settled, raw_complete):
        self.forced = (settled, raw_complete)

    def run(self, invocation, events):
        result = OfflineAdapter.run(self, invocation, events)
        return AdapterResult(result.invocation_sha256, result.events, self.forced[0],
                             "SUCCEEDED" if self.forced[0] else "UNKNOWN", {"tokens": 12, "cost": 3}, self.forced[1])


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class AdapterRunOracle(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="adapter-run-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.scheduler = Scheduler()
        self.scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        for task in ("task-1", "task-2"):
            self.scheduler.seed_task("req-1", task, CAPS, max_attempts=3)
        self.count = 0

    def candidate_hash(self, evidence_dir):
        return CANDIDATE

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
        return run_adapter_operation(self.scheduler, adapter or OfflineAdapter(), invocation, cwd=str(self.cwd),
                                     environment=environment or BASE_ENV, owner="owner-1", generation=1,
                                     evidence_dir=str(evidence), worktree_id="wt-1",
                                     candidate_hash=self.candidate_hash, **kwargs)

    def kill_pid_in(self, path: Path):
        pid = pid_in(path)
        if pid is not None:
            for kill in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
                try:
                    kill()
                except (ProcessLookupError, PermissionError):
                    pass

    def usage_of(self, op, dimension):
        usage = self.scheduler._operations[op].usage.get(dimension)
        return None if usage is None else (usage.call_key, usage.revision, usage.amount, usage.final,
                                           usage.evidence_hash)

    def ledger(self):
        return (self.scheduler.events, {op: (o.state, sorted(o.usage)) for op, o in self.scheduler._operations.items()},
                dict(self.scheduler._resource_holds), {r: q.stopped for r, q in self.scheduler._requests.items()})

    def assert_claim_blocked(self, stopped="USAGE_UNKNOWN"):
        self.assertEqual(self.scheduler._requests["req-1"].stopped, stopped)
        with self.assertRaises(ContractError) as raised:
            self.operation("task-2")
        self.assertEqual(raised.exception.code, "E_STOPPED")

    def types_of(self, op):
        return [(event["type"], event["payload"].get("dimension")) for event in self.scheduler.events
                if event["operation_id"] == op]

    def assert_nothing_recorded(self, op, outcome):
        self.assertEqual(outcome["usage_recorded"], {})
        self.assertEqual(outcome["usage_skipped"], list(DIMENSIONS))
        self.assertEqual((self.usage_of(op, "tokens"), self.usage_of(op, "cost")), (None, None))

    # a complete stream from a run that went to review ---------------------

    def test_a_complete_stream_records_its_usage_and_frees_the_request(self):
        op, evidence = self.operation()
        outcome = self.run_op(self.invocation(op, emit(COMPLETE)), evidence)
        stream_sha = sha256_of(evidence / "stdout.log")
        self.assertEqual((outcome["state"], outcome["stop_reason"], outcome["exit_code"], outcome["error"]),
                         ("REVIEW_PENDING", "completed", 0, None))
        self.assertEqual(outcome["stream"], {"status": "ok", "sha256": stream_sha,
                                             "bytes": len((evidence / "stdout.log").read_bytes())})
        self.assertEqual(outcome["result"], {"settled": True, "status": "SUCCEEDED", "raw_complete": True,
                                             "usage": {"tokens": 12, "cost": 3},
                                             "invocation_sha256": self.invocation(op, emit(COMPLETE)).digest()})
        self.assertEqual((outcome["result_error"], outcome["usage_recorded"], outcome["usage_skipped"]),
                         (None, {"tokens": "APPLIED", "cost": "APPLIED"}, []))
        self.assertEqual(self.usage_of(op, "tokens"), ("local:%s:tokens" % op, 1, 12, True, stream_sha))
        self.assertEqual(self.usage_of(op, "cost"), ("local:%s:cost" % op, 1, 3, True, stream_sha))
        # The usage is final before the run is finished for review.
        self.assertEqual(self.types_of(op), [("scheduler.claimed", None), ("scheduler.handoff_accepted", None),
                                             ("run.started", None), ("usage.observed", "tokens"),
                                             ("usage.observed", "cost"), ("run.finished_for_review", None)])
        snapshot = self.scheduler.snapshot("req-1")
        self.assertEqual((snapshot["tokens"]["known"], snapshot["tokens"]["unknown_operations"],
                          snapshot["cost"]["unknown_operations"]), (12, 0, 0))
        self.assertIsNone(self.scheduler._requests["req-1"].stopped)
        self.operation("task-2")  # the request can claim again

    def test_a_failed_process_with_a_complete_stream_still_records_usage(self):
        op, evidence = self.operation()
        outcome = self.run_op(self.invocation(op, emit(COMPLETE, "sys.exit(3)\n")), evidence)
        self.assertEqual((outcome["state"], outcome["exit_code"]), ("REVIEW_PENDING", 3))
        self.assertEqual(outcome["result"]["status"], "SUCCEEDED")
        self.assertEqual(outcome["usage_recorded"], {"tokens": "APPLIED", "cost": "APPLIED"})

    def test_an_error_terminal_is_a_complete_observation(self):
        events = [ev(0, "run_started"), ev(1, "usage", {"tokens": 7, "cost": 1}), ev(2, "error", {"reason": "x"})]
        op, evidence = self.operation()
        outcome = self.run_op(self.invocation(op, emit(events)), evidence)
        self.assertEqual((outcome["result"]["settled"], outcome["result"]["status"], outcome["result"]["raw_complete"]),
                         (True, "ERROR", True))
        self.assertEqual(outcome["usage_recorded"], {"tokens": "APPLIED", "cost": "APPLIED"})

    def test_only_valid_reserved_dimensions_are_recorded(self):
        cases = [({"tokens": 12}, {"tokens": "APPLIED"}, ["cost"]),
                 ({"tokens": "12", "cost": 3}, {"cost": "APPLIED"}, ["tokens"]),
                 ({"tokens": -1, "cost": True}, {}, ["tokens", "cost"]),
                 ({"tokens": 12, "cost": 3, "gpu": 9, "dispatches": 4}, {"tokens": "APPLIED", "cost": "APPLIED"}, [])]
        for usage, recorded, skipped in cases:
            with self.subTest(usage=usage):
                self.setUp()
                events = [ev(0, "run_started"), ev(1, "usage", usage), ev(2, "run_finished")]
                op, evidence = self.operation()
                outcome = self.run_op(self.invocation(op, emit(events)), evidence)
                self.assertEqual((outcome["usage_recorded"], outcome["usage_skipped"]), (recorded, skipped))
                self.assertEqual(self.usage_of(op, "dispatches")[2], 1)  # the handoff's own dispatch only
                if skipped:
                    self.assert_claim_blocked()
                else:
                    self.operation("task-2")

    # streams that are not settled and complete ---------------------------

    def test_an_unsettled_stream_records_nothing_and_keeps_the_request_blocked(self):
        cases = {"no terminal": [ev(0, "run_started"), ev(1, "usage", {"tokens": 12, "cost": 3})],
                 "unknown event": [ev(0, "run_started"), ev(1, "future_event"), ev(2, "usage", {"tokens": 12, "cost": 3}),
                                   ev(3, "run_finished")]}
        for label, events in cases.items():
            with self.subTest(label):
                self.setUp()
                op, evidence = self.operation()
                outcome = self.run_op(self.invocation(op, emit(events)), evidence)
                self.assertEqual(outcome["state"], "REVIEW_PENDING")
                self.assertEqual((outcome["result"]["settled"], outcome["result"]["status"],
                                  outcome["result"]["raw_complete"]), (False, "UNKNOWN", False))
                self.assert_nothing_recorded(op, outcome)
                self.assertEqual(self.scheduler.snapshot("req-1")["tokens"]["unknown_operations"], 1)
                self.assertNotIn(("usage.observed", "tokens"), self.types_of(op))
                self.assert_claim_blocked()

    def test_usage_needs_a_result_that_is_both_settled_and_complete(self):
        for settled, raw_complete in ((False, True), (True, False), (False, False)):
            with self.subTest(settled=settled, raw_complete=raw_complete):
                self.setUp()
                op, evidence = self.operation()
                outcome = self.run_op(self.invocation(op, emit(COMPLETE)), evidence,
                                      adapter=ForcedResultAdapter(settled, raw_complete))
                self.assertEqual((outcome["result"]["settled"], outcome["result"]["raw_complete"]),
                                 (settled, raw_complete))
                self.assert_nothing_recorded(op, outcome)
                self.assert_claim_blocked()

    def test_a_stream_the_adapter_rejects_records_nothing(self):
        events = [ev(0, "run_started"), ev(1, "run_finished"), ev(2, "assistant")]  # terminal not last
        op, evidence = self.operation()
        outcome = self.run_op(self.invocation(op, emit(events)), evidence)
        self.assertEqual((outcome["stream"]["status"], outcome["result"], outcome["result_error"]),
                         ("ok", None, "E_ADAPTER_SETTLEMENT"))
        self.assert_nothing_recorded(op, outcome)

    def test_invalid_lines_name_the_line_and_record_nothing(self):
        cases = [("print('not json')\n", 1, "E_JSON_SYNTAX"),
                 (emit([ev(0, "run_started")], "print('[1]')\n"), 2, "E_ADAPTER_STREAM"),
                 ("print(); print('{}')\n", 1, "E_JSON_LIMIT")]
        for code, line, error in cases:
            with self.subTest(error=error):
                self.setUp()
                op, evidence = self.operation()
                outcome = self.run_op(self.invocation(op, code), evidence)
                raw = (evidence / "stdout.log").read_bytes()
                self.assertEqual(outcome["stream"], {"status": "invalid", "sha256": hashlib.sha256(raw).hexdigest(),
                                                     "bytes": len(raw), "line": line, "code": error})
                self.assertEqual((outcome["result"], outcome["result_error"]), (None, None))
                self.assert_nothing_recorded(op, outcome)

    def test_a_stream_over_the_limit_records_nothing(self):
        op, evidence = self.operation()
        outcome = self.run_op(self.invocation(op, emit(COMPLETE)), evidence, max_stream_bytes=64)
        self.assertEqual(outcome["stream"], {"status": "too_large", "sha256": None, "bytes": None})
        self.assertEqual(outcome["result"], None)
        self.assert_nothing_recorded(op, outcome)

    def test_a_stream_replaced_by_a_link_is_unreadable(self):
        forged = self.work / "forged.jsonl"
        forged.write_text("".join(json.dumps(event) + "\n" for event in
                                  [ev(0, "run_started"), ev(1, "usage", {"tokens": 1, "cost": 1}), ev(2, "run_finished")]))
        op, evidence = self.operation()
        code = emit(COMPLETE, REPLACE_STREAM_WITH_LINK)
        outcome = self.run_op(self.invocation(op, code, str(evidence / "stdout.log"), str(forged)), evidence)
        self.assertTrue((evidence / "stdout.log").is_symlink())
        self.assertEqual(outcome["stream"], {"status": "unreadable", "sha256": None, "bytes": None})
        self.assert_nothing_recorded(op, outcome)

    # other run states ------------------------------------------------------

    def test_an_unconfirmed_cleanup_records_nothing_even_for_a_complete_stream(self):
        escaped = self.work / "escaped.pid"
        self.addCleanup(self.kill_pid_in, escaped)
        op, evidence = self.operation()
        outcome = self.run_op(self.invocation(op, emit(COMPLETE, LEAVE_ESCAPED_PARENT), str(escaped)), evidence)
        self.assertIsNotNone(pid_in(escaped), "the escaped parent never reported its pid")
        self.assertEqual(outcome["state"], "START_UNKNOWN")
        self.assertEqual((outcome["result"]["settled"], outcome["result"]["raw_complete"]), (True, True))
        self.assert_nothing_recorded(op, outcome)
        self.assert_claim_blocked("START_UNKNOWN")

    def test_a_rejected_input_is_not_started_and_has_no_stream(self):
        environment = dict(BASE_ENV, API_TOKEN="not-a-real-secret")
        op, evidence = self.operation()
        outcome = self.run_op(self.invocation(op, emit(COMPLETE), environment=environment), evidence,
                              environment=environment)
        self.assertEqual((outcome["state"], outcome["error"]), ("NOT_STARTED", "E_RUNTIME_ENVIRONMENT"))
        self.assertEqual(outcome["stream"], {"status": "missing", "sha256": None, "bytes": None})
        self.assertEqual((outcome["result"], outcome["result_error"]), (None, None))
        self.assert_nothing_recorded(op, outcome)
        self.assertIsNone(self.scheduler._requests["req-1"].stopped)
        self.operation("task-2")  # a run proven not started does not block the request

    def test_another_adapter_id_binds_its_own_event_source(self):
        events = [dict(event, source="stub") for event in COMPLETE]
        op, evidence = self.operation()
        outcome = self.run_op(self.invocation(op, emit(events), adapter_id="stub"), evidence, adapter=StubAdapter())
        self.assertEqual(outcome["usage_recorded"], {"tokens": "APPLIED", "cost": "APPLIED"})

    # checks that run before anything is handed off -------------------------

    def assert_refused_before_handoff(self, call, error, code):
        op, evidence = self.operation()
        before = self.ledger()
        with self.assertRaises(error) as raised:
            call(op, evidence)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(self.ledger(), before, "a refused call changed the Scheduler")
        self.assertEqual(self.scheduler._operations[op].state, "RESERVED")
        self.assertEqual(sorted(os.listdir(evidence)), [], "a refused call wrote evidence")

    def test_refusals_happen_before_the_handoff(self):
        bad_dimensions = [["tokens"], ("tokens", "tokens"), ("dispatches",), ("",), (type("S", (str,), {})("tokens"),), (1,)]
        cases = [("an InvocationSpec instead of an Invocation", AdapterError, "E_ADAPTER_INVOCATION",
                  lambda op, ev_: self.run_op(InvocationSpec("req-1", op, (PY, "-c", "pass"), str(self.cwd),
                                                             environment_digest(BASE_ENV), 10), ev_)),
                 ("not an adapter", AdapterError, "E_ADAPTER_INVOCATION",
                  lambda op, ev_: self.run_op(self.invocation(op, "pass"), ev_, adapter=object())),
                 ("another adapter id", AdapterError, "E_ADAPTER_IDENTITY",
                  lambda op, ev_: self.run_op(self.invocation(op, "pass", adapter_id="other"), ev_)),
                 ("a float timeout", RuntimeErrorCode, "E_RUNTIME_TIMEOUT",
                  lambda op, ev_: self.run_op(self.invocation(op, "pass"), ev_, timeout_seconds=1.5))]
        for value in bad_dimensions:
            cases.append(("usage dimensions %r" % (value,), AdapterError, "E_ADAPTER_SCHEMA",
                          lambda op, ev_, v=value: self.run_op(self.invocation(op, "pass"), ev_, usage_dimensions=v)))
        for limit in (0, True, 1.5, 16 * 1_048_576 + 1):
            cases.append(("stream limit %r" % (limit,), AdapterError, "E_ADAPTER_SCHEMA",
                          lambda op, ev_, v=limit: self.run_op(self.invocation(op, "pass"), ev_, max_stream_bytes=v)))
        for label, error, code, call in cases:
            with self.subTest(label):
                self.setUp()
                self.assert_refused_before_handoff(call, error, code)

    # the two helpers -------------------------------------------------------

    def test_supervised_spec_locks_the_invocation(self):
        invocation = self.invocation("op-x", "pass")
        spec = supervised_spec(invocation, cwd=str(self.cwd), timeout_seconds=30)
        self.assertEqual(spec, InvocationSpec("req-1", "op-x", (PY, "-c", "pass"), str(self.cwd),
                                              environment_digest(BASE_ENV), 30))
        with self.assertRaises(AdapterError) as raised:
            supervised_spec(spec, cwd=str(self.cwd), timeout_seconds=30)
        self.assertEqual(raised.exception.code, "E_ADAPTER_INVOCATION")

    def test_read_stream_reports_each_state(self):
        evidence = self.work / "plain"
        evidence.mkdir()
        self.assertEqual(read_stream(str(evidence)), {"status": "missing", "sha256": None, "bytes": None, "events": None})
        (evidence / "stdout.log").write_bytes(b"")
        self.assertEqual(read_stream(str(evidence)), {"status": "ok", "sha256": hashlib.sha256(b"").hexdigest(),
                                                      "bytes": 0, "events": []})
        raw = (json.dumps(COMPLETE[0]) + "\n").encode()
        (evidence / "stdout.log").write_bytes(raw)
        self.assertEqual(read_stream(str(evidence), max_bytes=len(raw))["events"], [COMPLETE[0]])
        self.assertEqual(read_stream(str(evidence), max_bytes=len(raw) - 1)["status"], "too_large")
        (evidence / "stdout.log").unlink()
        (evidence / "stdout.log").mkdir()
        self.assertEqual(read_stream(str(evidence))["status"], "unreadable")


if __name__ == "__main__":
    unittest.main()
