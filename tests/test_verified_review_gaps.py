"""Planner follow-up for verified-review (review of attempt-001).

Two rules the oracle and the executor tests left unguarded:

* the stream check on the review path runs after ``mark_running``, so it sits
  under the bridge's rule that an operation never stays RUNNING: an interrupt
  while both streams are read again (up to the digest limit each) moves the
  operation to ``START_UNKNOWN`` and is re-raised, like a refusal of the hook,
  of ``candidate_hash`` or of ``finish``;
* the checks before the handoff keep the contract's order -- specification,
  evidence directory, hook, the operation's request -- which a single bad input
  cannot show: only two bad inputs at once tell which check came first.

Every case starts real local ``python`` processes or none at all.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sentinelmesh.supervised_operation as bridge
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.scheduler import Scheduler
from sentinelmesh.supervised_operation import recover_supervised_operation, run_supervised_operation

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
CANDIDATE = "c" * 64
CAPS = {"dispatches": 5, "tokens": 100}
LEADER = "import os\nos.write(1, b'leader\\n')\n"


class VerifiedReviewGaps(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="verified-review-gaps-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.scheduler = Scheduler()
        self.scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        self.scheduler.seed_request("req-2", "repo-2", CAPS, concurrency=4)
        self.scheduler.seed_task("req-1", "task-1", CAPS, max_attempts=5)
        self.calls = []

    def operation(self):
        op = self.scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1,
                                  {"dispatches": 1, "tokens": 40}, ["src/a.py"])["operation_id"]
        evidence = self.work / "evidence"
        evidence.mkdir()
        return op, evidence

    def spec(self, op, request_id="req-1"):
        return InvocationSpec(request_id, op, (PY, "-c", LEADER), str(self.cwd), environment_digest(BASE_ENV), 10)

    def candidate_hash(self, evidence_dir):
        self.calls.append("candidate_hash")
        return CANDIDATE

    def before_finish(self, run):
        self.calls.append("before_finish")

    def run_op(self, op, spec, evidence_dir, **kwargs):
        kwargs.setdefault("before_finish", self.before_finish)
        return run_supervised_operation(self.scheduler, operation_id=op, owner="owner-1", generation=1, spec=spec,
                                        environment=BASE_ENV, evidence_dir=evidence_dir, worktree_id="wt-1",
                                        candidate_hash=self.candidate_hash, **kwargs)

    def test_an_interrupt_while_the_streams_are_checked_is_start_unknown(self):
        op, evidence = self.operation()

        def interrupted(directory, streams):
            raise KeyboardInterrupt

        with mock.patch.object(bridge, "streams_match", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_op(op, self.spec(op), str(evidence))
        self.assertEqual(self.scheduler.operation(op)["state"], "START_UNKNOWN", "the operation was left RUNNING")
        unknown = [e for e in self.scheduler.events if e["type"] == "scheduler.start_unknown"]
        self.assertEqual([e["payload"]["reason"] for e in unknown], ["START_UNKNOWN"])
        self.assertEqual(self.calls, [], "the review went on after the interrupted check")
        self.assertIn(("repo-1", "src/a.py"), self.scheduler._resource_holds)
        self.assertEqual(self.scheduler._requests["req-1"].stopped, "START_UNKNOWN")

    def test_the_checks_before_the_handoff_keep_their_order(self):
        op, evidence = self.operation()
        missing = "op-" + "0" * 32
        before = self.scheduler.events
        cases = {
            # The hook is checked before the operation is looked up.
            "a hook that cannot be called, for an operation the scheduler does not know":
                (dict(op=missing, spec=self.spec(missing), evidence_dir=str(evidence), before_finish=5),
                 "E_RUNTIME_INVOCATION"),
            # The evidence directory is checked before the hook.
            "an evidence directory that is not a string, with a hook that cannot be called":
                (dict(op=op, spec=self.spec(op), evidence_dir=5, before_finish=5), "E_RUNTIME_EVIDENCE"),
            # The evidence directory is checked before the operation's request.
            "an evidence directory that is not a string, for another request":
                (dict(op=op, spec=self.spec(op, request_id="req-2"), evidence_dir=5), "E_RUNTIME_EVIDENCE"),
        }
        for label, (arguments, code) in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(RuntimeErrorCode) as ctx:
                    self.run_op(arguments.pop("op"), **arguments)
                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(self.scheduler.events, before)
                self.assertEqual(self.scheduler.operation(op)["state"], "RESERVED")
                self.assertEqual(os.listdir(evidence), [])
        with self.subTest(case="recovery: an evidence directory that is not a string, for another request"):
            with self.assertRaises(RuntimeErrorCode) as ctx:
                recover_supervised_operation(self.scheduler, operation_id=op, owner="owner-2", generation=1,
                                             spec=self.spec(op, request_id="req-2"), evidence_dir=5,
                                             candidate_hash=self.candidate_hash)
            self.assertEqual(ctx.exception.code, "E_RUNTIME_EVIDENCE")
            self.assertEqual(self.scheduler.events, before)
        # The legal call still works on the same ledger.
        self.assertEqual(self.run_op(op, self.spec(op), str(evidence))["state"], "REVIEW_PENDING")
        self.assertEqual(self.calls, ["before_finish", "candidate_hash"])


if __name__ == "__main__":
    unittest.main()
