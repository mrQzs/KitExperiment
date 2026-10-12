"""Executor tests for closing a task after an accepted run (task-done).

The planner oracle drives the product commands and the real processes; these
tests pin the places the oracle cannot see from the outside: the refusal itself
(a claim of an accepted task changes nothing, a rejected one still claims), the
rule's place ahead of the attempts rule, the two read-only views and their
durable proxies, and ``smctl ledger status`` listing the tasks per request.  No
provider CLI, no credentials, no network, no subprocesses.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import cli, ledger_cli
from sentinelmesh.durable_ledger import (
    LEDGER_FORMAT,
    MUTATING_METHODS,
    DurableScheduler,
    create_ledger,
    open_ledger,
)
from sentinelmesh.scheduler import ContractError, Scheduler

NL = chr(10)
CAPS = {"dispatches": 9, "tokens": 500, "cost": 200}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
CANDIDATE, EVIDENCE = "c" * 64, "e" * 64
TASK_KEYS = {"task_id", "max_attempts", "attempts", "done"}
STATUS_KEYS = {"request_id", "stopped", "stop_reasons", "snapshot", "attention", "reviews", "tasks"}


def view(task_id, max_attempts, attempts, done):
    return {"task_id": task_id, "max_attempts": max_attempts, "attempts": attempts, "done": done}


def without_self(method):
    """The signature of a method as its callers see it: ``self`` dropped."""
    return inspect.Signature(list(inspect.signature(method).parameters.values())[1:])


class TaskDoneCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="task-done-exec-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.path = str(self.work / "ledger.db")

    def seed(self, scheduler, request="req-1", *tasks, max_attempts=3):
        scheduler.seed_request(request, "repo-1", CAPS, concurrency=4)
        for task in tasks:
            scheduler.seed_task(request, task, CAPS, max_attempts)
        return scheduler

    def claim(self, scheduler, task, key, generation=1):
        return scheduler.claim("req-1", task, 1, key, "owner-1", generation, 1,
                               RESERVATION, ["src/%s.py" % task])["operation_id"]

    def accepted(self, scheduler, task, key, accepted=True):
        """One operation of ``task`` decided by the fixture hook."""
        op = self.claim(scheduler, task, key)
        scheduler.handoff(op, "owner-1", 1)
        scheduler.mark_running(op, "owner-1", 1)
        scheduler.settle(op, {"tokens": 5, "cost": 1})
        scheduler.finish(op, "owner-1", 1, CANDIDATE, True)
        scheduler.fixture_verdict(op, CANDIDATE, accepted, EVIDENCE)
        return op

    def refuse_code(self, scheduler, task, key, **kwargs):
        """The code of a refused claim, and proof it refused nothing."""
        before = (list(scheduler.events), scheduler.snapshot("req-1"))
        with self.assertRaises(ContractError) as ctx:
            self.claim(scheduler, task, key, **kwargs)
        self.assertEqual(ctx.exception.code, "E_TASK_DONE")
        self.assertEqual(before, (list(scheduler.events), scheduler.snapshot("req-1")),
                         "a refused claim changed the ledger")
        return ctx.exception.code

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


class AcceptedTaskTest(TaskDoneCase):
    def test_an_accepted_task_refuses_a_further_claim_without_touching_anything(self):
        scheduler = self.seed(Scheduler(), "req-1", "task-1")
        self.accepted(scheduler, "task-1", "idem-1")
        # The refusal records no event and moves no budget: the claim of an
        # accepted task is refused before anything is created.
        self.assertEqual(self.refuse_code(scheduler, "task-1", "idem-2"), "E_TASK_DONE")
        self.assertEqual(scheduler.task("req-1", "task-1"), view("task-1", 3, 1, True))

    def test_a_rejected_task_may_be_claimed_again(self):
        scheduler = self.seed(Scheduler(), "req-1", "task-1")
        self.accepted(scheduler, "task-1", "idem-1", accepted=False)
        self.assertEqual(scheduler.task("req-1", "task-1"), view("task-1", 3, 1, False))
        claimed = scheduler.claim("req-1", "task-1", 1, "idem-2", "owner-1", 1, 1,
                                  RESERVATION, ["src/task-1.py"])
        self.assertEqual(claimed["state"], "RESERVED")
        self.assertEqual(scheduler.task("req-1", "task-1"), view("task-1", 3, 2, False))

    def test_an_accepted_task_reports_its_own_code_when_its_attempts_run_out(self):
        # The baseline first: one attempt taken and no acceptance, and the
        # attempts rule refuses the next claim of that one-attempt task.
        baseline = self.seed(Scheduler(), "req-1", "task-1", max_attempts=1)
        cancelled = self.claim(baseline, "task-1", "idem-1")
        baseline.cancel_unstarted(cancelled, 1)
        with self.assertRaises(ContractError) as ctx:
            self.claim(baseline, "task-1", "idem-2")
        self.assertEqual(ctx.exception.code, "E_ATTEMPTS")
        # The same task, accepted instead: the task's own fact wins.
        accepted = self.seed(Scheduler(), "req-1", "task-1", max_attempts=1)
        self.accepted(accepted, "task-1", "idem-1")
        self.assertEqual(self.refuse_code(accepted, "task-1", "idem-2"), "E_TASK_DONE")
        self.assertEqual(accepted.task("req-1", "task-1"), view("task-1", 1, 1, True))


class TaskViewTest(TaskDoneCase):
    def test_each_request_lists_only_its_own_tasks_in_seeding_order(self):
        scheduler = Scheduler()
        self.seed(scheduler, "req-1", "task-2", "task-1")
        self.seed(scheduler, "req-2", "task-9", "task-3")
        self.assertEqual(scheduler.task_ids("req-1"), ("task-2", "task-1"))
        self.assertEqual(scheduler.task_ids("req-2"), ("task-9", "task-3"))
        with self.assertRaises(ContractError) as ctx:
            scheduler.task_ids("req-9")
        self.assertEqual(ctx.exception.code, "E_REQUEST_UNKNOWN")

    def test_attempts_count_every_claim_and_done_follows_the_verdict(self):
        scheduler = self.seed(Scheduler(), "req-1", "task-1")
        cancelled = self.claim(scheduler, "task-1", "idem-1")
        scheduler.cancel_unstarted(cancelled, 1)
        self.assertEqual(scheduler.task("req-1", "task-1"), view("task-1", 3, 1, False))
        self.accepted(scheduler, "task-1", "idem-2")
        self.assertEqual(scheduler.task("req-1", "task-1"), view("task-1", 3, 2, True))
        with self.assertRaises(ContractError) as ctx:
            scheduler.task("req-1", "task-9")
        self.assertEqual(ctx.exception.code, "E_SCOPE_UNKNOWN")

    def test_the_views_are_reads_the_durable_ledger_serves_the_same_way(self):
        self.assertEqual(MUTATING_METHODS[-2:], ("resolve_unknown", "resume"))
        self.assertNotIn("task", MUTATING_METHODS)
        self.assertNotIn("task_ids", MUTATING_METHODS)
        self.assertEqual(LEDGER_FORMAT, "sentinelmesh-ledger-v2")
        # The durable proxies carry the scheduler's own signatures, so a change
        # to either view shows up here.
        self.assertEqual(without_self(DurableScheduler.task_ids), without_self(Scheduler.task_ids))
        self.assertEqual(without_self(DurableScheduler.task), without_self(Scheduler.task))

    def test_the_rule_and_the_views_survive_a_close_and_a_reopen(self):
        ledger = create_ledger(self.path)
        try:
            self.seed(ledger, "req-1", "task-1", "task-2")
            op = self.claim(ledger, "task-1", "idem-1")
            ledger.handoff(op, "owner-1", 1)
            ledger.mark_running(op, "owner-1", 1)
            ledger.settle(op, {"tokens": 5, "cost": 1})
            ledger.finish(op, "owner-1", 1, CANDIDATE, True)
            ledger.fixture_verdict(op, CANDIDATE, True, EVIDENCE)
            ids, done, open_ = ledger.task_ids("req-1"), ledger.task("req-1", "task-1"), ledger.task("req-1", "task-2")
        finally:
            ledger.close()
        reopened = open_ledger(self.path)
        try:
            self.assertEqual(reopened.task_ids("req-1"), ids)
            self.assertEqual(reopened.task("req-1", "task-1"), done)
            self.assertEqual(reopened.task("req-1", "task-2"), open_)
            with self.assertRaises(ContractError) as ctx:
                self.claim(reopened, "task-1", "idem-2")
            self.assertEqual(ctx.exception.code, "E_TASK_DONE")
        finally:
            reopened.close()


class StatusTasksTest(TaskDoneCase):
    def test_status_lists_every_task_of_a_request_in_seeding_order(self):
        scheduler = self.seed(Scheduler(), "req-1", "task-2", "task-1")
        self.accepted(scheduler, "task-1", "idem-1")
        tasks = ledger_cli._tasks(scheduler, "req-1")
        self.assertEqual(tasks, [view("task-2", 3, 0, False), view("task-1", 3, 1, True)])
        self.assertTrue(all(set(item) == TASK_KEYS for item in tasks))

    def test_the_command_reports_what_the_helper_reads(self):
        ledger = create_ledger(self.path)
        try:
            self.seed(ledger, "req-1", "task-1", "task-2")
            op = self.claim(ledger, "task-1", "idem-1")
            ledger.handoff(op, "owner-1", 1)
            ledger.mark_running(op, "owner-1", 1)
            ledger.settle(op, {"tokens": 5, "cost": 1})
            ledger.finish(op, "owner-1", 1, CANDIDATE, True)
            ledger.fixture_verdict(op, CANDIDATE, True, EVIDENCE)
            expected = ledger_cli._tasks(ledger, "req-1")
        finally:
            ledger.close()
        entry = self.ok("ledger", "status", self.path)["requests"][0]
        self.assertEqual(set(entry), STATUS_KEYS)
        self.assertEqual(entry["tasks"], expected)
        self.assertEqual(entry["tasks"], [view("task-1", 3, 1, True), view("task-2", 3, 0, False)])


if __name__ == "__main__":
    unittest.main()
