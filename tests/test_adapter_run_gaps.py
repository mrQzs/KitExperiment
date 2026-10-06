"""Planner follow-up for adapter-supervised-run (review of attempt-001).

The contract orders the checks that run before the handoff, and the
invocation's exact type comes first.  The candidate checks it at the entry and
again in ``supervised_spec``, so a single bad input cannot tell the two apart:
only an ``Invocation`` subclass combined with another bad input shows whether
the entry check is exact.  Every refusal names its code, leaves the Scheduler
unchanged and writes no evidence.  An evidence directory that is not a string
(review F2) is refused by the bridge before the handoff and reaches the caller.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from sentinelmesh.adapter_protocol import AdapterError, Invocation, OfflineAdapter
from sentinelmesh.adapter_run import run_adapter_operation
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import RuntimeErrorCode
from sentinelmesh.scheduler import Scheduler

BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}


class InvocationSubclass(Invocation):
    pass


class AdapterRunGaps(unittest.TestCase):
    def test_the_invocation_type_is_checked_before_the_other_inputs(self):
        work = Path(tempfile.mkdtemp(prefix="adapter-run-gaps-")).resolve()
        self.addCleanup(shutil.rmtree, work, True)
        evidence = work / "evidence"
        evidence.mkdir()
        scheduler = Scheduler()
        scheduler.seed_request("req-1", "repo-1", {"dispatches": 5, "tokens": 100}, concurrency=4)
        scheduler.seed_task("req-1", "task-1", {"dispatches": 5, "tokens": 100}, max_attempts=3)
        op = scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 40},
                             ["src/a.py"])["operation_id"]
        invocation = InvocationSubclass("req-1", op, "executor", "stub", ("python3", "-c", "pass"), (),
                                        environment_digest(BASE_ENV))
        before = scheduler.events
        for extra in ({"usage_dimensions": ("dispatches",)}, {"usage_dimensions": ["tokens"]},
                      {"max_stream_bytes": 0}, {"timeout_seconds": 1.5}):
            with self.subTest(**{key: repr(value) for key, value in extra.items()}):
                arguments = dict(cwd=str(work), timeout_seconds=10, environment=BASE_ENV, owner="owner-1",
                                 generation=1, evidence_dir=str(evidence), worktree_id="wt-1",
                                 candidate_hash=lambda directory: "c" * 64, usage_dimensions=("tokens",))
                arguments.update(extra)
                with self.assertRaises(AdapterError) as raised:
                    run_adapter_operation(scheduler, OfflineAdapter(), invocation, **arguments)
                self.assertEqual(raised.exception.code, "E_ADAPTER_INVOCATION")
                self.assertEqual(scheduler.events, before, "a refused call changed the Scheduler")
                self.assertEqual(scheduler._operations[op].state, "RESERVED")
                self.assertEqual(os.listdir(evidence), [], "a refused call wrote evidence")


    def test_an_evidence_directory_that_is_not_a_string_is_refused_before_the_handoff(self):
        work = Path(tempfile.mkdtemp(prefix="adapter-run-gaps-")).resolve()
        self.addCleanup(shutil.rmtree, work, True)
        scheduler = Scheduler()
        scheduler.seed_request("req-1", "repo-1", {"dispatches": 5, "tokens": 100}, concurrency=4)
        scheduler.seed_task("req-1", "task-1", {"dispatches": 5, "tokens": 100}, max_attempts=3)
        op = scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 40},
                             ["src/a.py"])["operation_id"]
        invocation = Invocation("req-1", op, "executor", "stub", ("python3", "-c", "pass"), (),
                                environment_digest(BASE_ENV))
        before = scheduler.events
        for value in (None, 123, work):
            with self.subTest(evidence_dir=repr(value)):
                with self.assertRaises(RuntimeErrorCode) as raised:
                    run_adapter_operation(scheduler, OfflineAdapter(), invocation, cwd=str(work), timeout_seconds=10,
                                          environment=BASE_ENV, owner="owner-1", generation=1, evidence_dir=value,
                                          worktree_id="wt-1", candidate_hash=lambda directory: "c" * 64,
                                          usage_dimensions=("tokens",))
                self.assertEqual(raised.exception.code, "E_RUNTIME_EVIDENCE")
                self.assertEqual(scheduler.events, before, "a refused call changed the Scheduler")
                self.assertEqual(scheduler._operations[op].state, "RESERVED")


if __name__ == "__main__":
    unittest.main()
