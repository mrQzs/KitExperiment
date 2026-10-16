"""Executor tests for ledger-catch-up (`.agent/design/serve-watch.md`, section 3.1).

The planner oracle covers the mechanics of the catch-up -- two instances taking
turns, two processes writing at once, a rollover, a checkpoint past an instance,
another history, a damaged tail, a failure inside the replay and the real
``smctl`` processes.  What this file adds is the surface around it, one rule per
case, each on a legal baseline first and then with only the one condition under
test changed:

* ``refresh()`` with nothing to catch up: the two keys it returns, and the fact
  that it leaves the log alone;
* a created instance catching up another instance's claim: the operation is
  readable afterwards, and the ids are the ones a reopen shows;
* a refusal by the Scheduler after a catch-up: the instance stays usable, the
  commands table does not grow and the caught-up state is the one the next
  command judges;
* a rollover that is not the first command to catch up, which the head check
  alone would not see: still ``E_LEDGER_STALE``, and nothing appended;
* ``refresh()`` on a closed instance, which is ``E_LEDGER_UNAVAILABLE`` like
  every other read.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import durable_ledger
from sentinelmesh.durable_ledger import DurableScheduler, LedgerError, create_ledger, open_ledger
from sentinelmesh.scheduler import ContractError

CAPS = {"dispatches": 9, "tokens": 500, "cost": 200}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
FULL_RESERVATION = {"dispatches": 1, "tokens": 500, "cost": 200}


def rows_of(path):
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT seq, command, sha256, previous FROM commands ORDER BY seq").fetchall()
    finally:
        db.close()


class CatchUpCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="catch-up-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.path = str(self.work / "ledger.db")

    def code_of(self, error_type, func, *args, **kwargs):
        with self.assertRaises(error_type) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def opened(self):
        ledger = open_ledger(self.path)
        self.addCleanup(ledger.close)
        return ledger

    def unavailable(self, ledger):
        self.assertEqual(self.code_of(LedgerError, lambda: ledger.seq), "E_LEDGER_UNAVAILABLE")

    def seeded(self):
        """A created instance with one request and one task: two commands."""
        first = create_ledger(self.path)
        self.addCleanup(first.close)
        first.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        first.seed_task("req-1", "task-1", CAPS, max_attempts=3)
        return first


class RefreshWithoutAnythingToCatchUp(CatchUpCase):
    def test_refresh_reports_zero_and_touches_no_row(self):
        first = self.seeded()
        rows = rows_of(self.path)
        self.assertEqual(first.refresh(), {"seq": 2, "applied": 0})
        self.assertEqual(first.refresh(), {"seq": 2, "applied": 0})
        self.assertEqual(rows_of(self.path), rows)

    def test_a_read_alone_still_does_not_catch_up(self):
        # The read views are served from memory: only a write or ``refresh()``
        # replays what another instance appended.
        first = self.seeded()
        second = self.opened()
        second.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        self.assertEqual(first.task_ids("req-1"), ("task-1",))
        self.assertEqual(first.refresh(), {"seq": 3, "applied": 1})
        self.assertEqual(first.task_ids("req-1"), ("task-1", "task-2"))


class CreatedInstanceCatchesUpAnotherClaim(CatchUpCase):
    def test_the_claim_is_replayed_with_the_id_the_log_recorded(self):
        first = self.seeded()
        first.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        other = self.opened()
        claim = other.claim("req-1", "task-1", 1, "idem-a", "owner-1", 1, 1, dict(RESERVATION), ["src/a.py"])
        operation_id = claim["operation_id"]
        rows = rows_of(self.path)
        # The legal baseline: the same key on another task is a conflict whatever
        # the instance that judges it, so nothing but the catch-up refuses it.
        self.assertEqual(self.code_of(ContractError, other.claim, "req-1", "task-2", 1, "idem-a", "owner-1", 1, 1,
                                      dict(RESERVATION), ["src/b.py"]), "E_IDEMPOTENCY_CONFLICT")
        self.assertEqual(rows_of(self.path), rows)
        # One condition changed: the instance has to catch the claim up first.
        self.assertEqual(self.code_of(ContractError, first.claim, "req-1", "task-2", 1, "idem-a", "owner-1", 1, 1,
                                      dict(RESERVATION), ["src/b.py"]), "E_IDEMPOTENCY_CONFLICT")
        self.assertEqual(rows_of(self.path), rows)
        self.assertEqual(first.operation_ids(), (operation_id,))
        self.assertEqual(first.operation(operation_id)["task_id"], "task-1")
        with open_ledger(self.path) as reopened:
            self.assertEqual((reopened.operation_ids(), reopened.operation(operation_id)["task_id"]),
                             ((operation_id,), "task-1"))

    def test_the_claim_keeps_the_recorded_id_when_a_write_catches_it_up(self):
        first = self.seeded()
        first.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        other = self.opened()
        claim = other.claim("req-1", "task-1", 1, "idem-a", "owner-1", 1, 1, dict(RESERVATION), ["src/a.py"])
        operation_id = claim["operation_id"]
        first.seed_task("req-1", "task-3", CAPS, max_attempts=3)  # catches up the claim, then appends
        self.assertEqual(first.operation_ids(), (operation_id,))
        self.assertEqual(first.snapshot("req-1")["tokens"]["held"], RESERVATION["tokens"])


class RefusalAfterCatchingUp(CatchUpCase):
    def test_a_refusal_keeps_the_instance_and_the_caught_up_state(self):
        first = self.seeded()
        second = self.opened()
        second.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        rows = rows_of(self.path)
        # The legal baseline: a claim of a task the request does not hold is
        # refused, and only that, on the state the catch-up rebuilds.
        self.assertEqual(self.code_of(ContractError, second.claim, "req-1", "task-9", 1, "idem-x", "owner-1", 1, 1,
                                      dict(RESERVATION), ["src/x.py"]), "E_SCOPE_UNKNOWN")
        self.assertEqual(rows_of(self.path), rows)
        # One condition changed: the instance that has to catch up first.
        self.assertEqual(self.code_of(ContractError, first.claim, "req-1", "task-9", 1, "idem-x", "owner-1", 1, 1,
                                      dict(RESERVATION), ["src/x.py"]), "E_SCOPE_UNKNOWN")
        self.assertEqual(rows_of(self.path), rows)
        self.assertEqual(first.task_ids("req-1"), ("task-1", "task-2"))
        first.stop("req-1", "OPERATOR_STOP")  # still usable, and it appends after the catch-up
        self.assertEqual((first.seq, len(rows_of(self.path))), (4, 4))

    def test_the_budget_guard_of_a_caught_up_command_is_the_one_that_refuses(self):
        # The claim the other instance recorded holds part of the budget: the
        # catch-up has to see it, or this claim would be allowed.
        first = self.seeded()
        first.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        other = self.opened()
        other.claim("req-1", "task-1", 1, "idem-a", "owner-1", 1, 1, dict(RESERVATION), ["src/a.py"])
        rows = rows_of(self.path)
        self.assertEqual(self.code_of(ContractError, other.claim, "req-1", "task-2", 2, "idem-b", "owner-1", 1, 1,
                                      dict(FULL_RESERVATION), ["src/b.py"]), "E_BUDGET_INSUFFICIENT")
        self.assertEqual(rows_of(self.path), rows)
        self.assertEqual(self.code_of(ContractError, first.claim, "req-1", "task-2", 2, "idem-b", "owner-1", 1, 1,
                                      dict(FULL_RESERVATION), ["src/b.py"]), "E_BUDGET_INSUFFICIENT")
        self.assertEqual(rows_of(self.path), rows)


class RolloverThatIsNotTheFirstCommand(CatchUpCase):
    def test_a_rollover_after_another_command_is_still_stale(self):
        first = self.seeded()
        second = self.opened()
        second.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        # The rollover is the third command: the first of the commands to catch
        # up is a ``seed_task`` that links to this instance's head, so only the
        # rollover check -- and not the head check -- refuses this write.
        takeover = ("import sys" + chr(10)
                    + "from sentinelmesh.durable_ledger import open_ledger" + chr(10)
                    + "ledger = open_ledger(sys.argv[1])" + chr(10)
                    + "ledger.rollover()" + chr(10)
                    + "ledger.close()" + chr(10))
        done = subprocess.run([sys.executable, "-c", takeover, self.path], capture_output=True, text=True, timeout=120,
                              cwd=str(Path(__file__).resolve().parents[1]))
        self.assertEqual(done.returncode, 0, done.stderr)
        rows_after = rows_of(self.path)
        self.assertEqual([row[0] for row in rows_after], [1, 2, 3, 4])
        self.assertEqual(self.code_of(LedgerError, first.stop, "req-1", "OPERATOR_STOP"), "E_LEDGER_STALE")
        self.unavailable(first)
        self.assertEqual(self.code_of(LedgerError, second.refresh), "E_LEDGER_STALE")
        self.unavailable(second)
        self.assertEqual(rows_of(self.path), rows_after)
        with open_ledger(self.path) as reopened:
            self.assertEqual((reopened.generation, reopened.seq, reopened.task_ids("req-1")),
                             (2, 4, ("task-1", "task-2")))


class RefreshOnAClosedInstance(CatchUpCase):
    def test_refresh_after_close_is_unavailable(self):
        first = self.seeded()
        second = self.opened()
        second.seed_task("req-1", "task-2", CAPS, max_attempts=3)
        first.close()
        self.assertEqual(self.code_of(LedgerError, first.refresh), "E_LEDGER_UNAVAILABLE")
        self.assertEqual(self.code_of(LedgerError, first.stop, "req-1", "OPERATOR_STOP"), "E_LEDGER_UNAVAILABLE")
        self.unavailable(first)

    def test_refresh_is_not_a_mutating_method(self):
        self.assertNotIn("refresh", durable_ledger.MUTATING_METHODS)
        self.assertTrue(callable(getattr(DurableScheduler, "refresh", None)))
        ledger = self.seeded()
        self.assertEqual(set(ledger.refresh()), {"seq", "applied"})


if __name__ == "__main__":
    unittest.main()
