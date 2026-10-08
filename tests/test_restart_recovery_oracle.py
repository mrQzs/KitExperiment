"""Planner oracle for restart-recovery (`.agent/design/durable-ledger.md`, section 5 item 3).

Planner-provided; executors must not modify this file.

A controller that crashes leaves its operations in the durable ledger.  The
user decided (2026-10-08) that the ledger records each handoff's run
descriptor -- the specification's fields, the evidence directory and the
worktree -- so that a restarted controller can recover them without the
caller supplying them again:

* ``Scheduler.handoff(..., run=descriptor)`` validates and stores the
  descriptor; ``run_descriptor(op)`` and ``operation_ids()`` read it back;
* the bridge passes the descriptor of every supervised run it hands off;
* ``restart_recovery.recover_after_restart`` rolls the ledger over, cancels
  the claims of older generations that never reached a handoff, and settles
  every handoff the old generations never decided (``HANDOFF_ACCEPTED`` or
  ``RUNNING``) from its records with ``recover_supervised_operation``.  An
  operation the old controller already decided is left as it is.

The end-to-end cases start a real controller process, kill it with SIGKILL at
a chosen point, and recover in this process from the durable ledger it left.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh.durable_ledger import LedgerError, create_ledger, open_ledger
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.restart_recovery import recover_after_restart
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.scheduler import ContractError, Scheduler
from sentinelmesh.supervised_operation import run_supervised_operation
from sentinelmesh.supervised_record import record_supervised_run

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
CAPS = {"dispatches": 9, "tokens": 500}
RESERVATION = {"dispatches": 1, "tokens": 40}
USAGE = {"tokens": 5}
OLD, NEW = "c" * 64, "d" * 64
# Operation ids that sort in the opposite order to the one they are claimed in.
LATE_FIRST = ("op-" + "f" * 32, "op-" + "e" * 32)
DONE = "import os; os.write(1, b'done\\n')"
# The supervised child of the mid-run crash: reports its pid, then waits for
# the test to release it, so the controller dies while it is still running.
WAIT_FOR_RELEASE = ("import os, sys, time\n"
                    "started, release = sys.argv[1:3]\n"
                    "open(started + '.tmp', 'w').write(str(os.getpid()))\n"
                    "os.rename(started + '.tmp', started)\n"
                    "deadline = time.monotonic() + 30\n"
                    "while not os.path.exists(release) and time.monotonic() < deadline:\n"
                    "    time.sleep(0.01)\n")
CONTROLLER = r'''
import json, os, signal, sys
from sentinelmesh.durable_ledger import create_ledger
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec
from sentinelmesh.supervised_operation import run_supervised_operation

config = json.loads(sys.argv[1])
env = {"LANG": "C", "PATH": "/usr/bin:/bin"}
ledger = create_ledger(config["ledger"])
ledger.seed_request("req-1", "repo-1", {"dispatches": 9, "tokens": 500}, concurrency=4)
for task in ("task-1", "task-2", "task-3"):
    ledger.seed_task("req-1", task, {"dispatches": 9, "tokens": 500})


def claim(task):
    return ledger.claim("req-1", task, 1, "idem-" + task, "owner-1", 1, 1, {"dispatches": 1, "tokens": 40},
                        ["src/%s.py" % task])["operation_id"]


def run(op, evidence, code, *args, before_finish=None):
    spec = InvocationSpec("req-1", op, (config["python"], "-c", code) + tuple(args), config["cwd"],
                          environment_digest(env), 30)
    return run_supervised_operation(ledger, operation_id=op, owner="owner-1", generation=1, spec=spec,
                                    environment=env, evidence_dir=evidence, worktree_id="wt-1",
                                    candidate_hash=lambda directory: "c" * 64, before_finish=before_finish)


def die(*args, **kwargs):
    os.kill(os.getpid(), signal.SIGKILL)


ops = {"reviewed": claim("task-1")}
# The usage is settled while the run is still RUNNING, so the request is not
# stopped (USAGE_UNKNOWN) and the next tasks can still be claimed.
run(ops["reviewed"], config["evidence"]["reviewed"], "pass",
    before_finish=lambda run: ledger.settle(ops["reviewed"], {"tokens": 5}))
ops["claimed"] = claim("task-2")
ops["crashed"] = claim("task-3")
print(json.dumps(ops), flush=True)
if config["mode"] == "after-the-records":
    # The records are on disk when the bridge registers the start: die there.
    ledger.mark_running = die
    run(ops["crashed"], config["evidence"]["crashed"], config["done"])
elif config["mode"] == "before-the-review":
    # The start is registered (RUNNING) and the streams were checked: die there.
    run(ops["crashed"], config["evidence"]["crashed"], config["done"], before_finish=die)
else:
    run(ops["crashed"], config["evidence"]["crashed"], config["wait"], config["started"], config["release"])
'''


class Key(str):
    """A ``str`` subclass: equal to the text it spells, but not exact text."""


class Descriptor(dict):
    """A ``dict`` subclass: equal to the descriptor it holds, but not an exact dict."""


def pid_in(path: Path):
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text else None


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class RecoveryCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="restart-recovery-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.candidates = []

    def evidence(self, name):
        path = self.work / ("evidence-" + name)
        path.mkdir()
        return path

    def candidate_hash(self, evidence_dir):
        self.candidates.append(evidence_dir)
        return NEW

    def spec(self, op, code="pass", *args, timeout=30):
        return InvocationSpec("req-1", op, (PY, "-c", code, *args), str(self.cwd), environment_digest(BASE_ENV),
                              timeout)

    def descriptor(self, op, evidence, *, spec=None):
        spec = spec or self.spec(op)
        return {"spec": {"request_id": spec.request_id, "run_id": spec.run_id, "command": list(spec.command),
                         "cwd": spec.cwd, "environment_sha256": spec.environment_sha256,
                         "timeout_seconds": spec.timeout_seconds},
                "evidence_dir": str(evidence), "worktree_id": "wt-1"}

    def scheduler(self, ledger=None):
        scheduler = ledger or Scheduler()
        scheduler.seed_request("req-1", "repo-1", CAPS, concurrency=4)
        for task in ("task-1", "task-2", "task-3", "task-4"):
            scheduler.seed_task("req-1", task, CAPS)
        return scheduler

    def claim(self, scheduler, task):
        return scheduler.claim("req-1", task, 1, "idem-" + task, "owner-1", 1, 1, RESERVATION,
                               ["src/%s.py" % task])["operation_id"]

    def finished_run(self, scheduler, task, *, descriptor=True):
        """A handoff whose run ended and was recorded, but was never settled."""
        op = self.claim(scheduler, task)
        evidence = self.evidence(task)
        spec = self.spec(op, DONE)
        if descriptor:
            scheduler.handoff(op, "owner-1", 1, run=self.descriptor(op, evidence, spec=spec))
        else:
            scheduler.handoff(op, "owner-1", 1)
        record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")
        return op, str(evidence)


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class RunDescriptorOracle(RecoveryCase):
    def test_handoff_records_a_run_descriptor_and_reads_it_back(self):
        scheduler = self.scheduler()
        with mock.patch.object(Scheduler, "_new_operation_id", side_effect=LATE_FIRST):
            first = self.claim(scheduler, "task-1")
            second = self.claim(scheduler, "task-2")
        self.assertEqual(scheduler.operation_ids(), (first, second), "not in claim order")
        evidence = self.evidence("one")
        descriptor = self.descriptor(first, evidence)
        expected = json.loads(json.dumps(descriptor))
        scheduler.handoff(first, "owner-1", 1, run=descriptor)
        descriptor["spec"]["command"].append("--late")
        descriptor["worktree_id"] = "wt-9"
        self.assertEqual(scheduler.run_descriptor(first), expected, "the ledger kept the caller's objects")
        self.assertIsNone(scheduler.run_descriptor(second))
        copy = scheduler.run_descriptor(first)
        copy["spec"]["command"].append("--extra")
        copy["evidence_dir"] = "/elsewhere"
        self.assertEqual(scheduler.run_descriptor(first), expected, "the read shares state with the ledger")
        handed_off = [e for e in scheduler.events if e["type"] == "scheduler.handoff_accepted"]
        self.assertEqual(handed_off[0]["payload"], {"generation": 1}, "the handoff event changed")
        for value in ("op-missing", None, [first], Key(first)):
            with self.subTest(operation=repr(value)):
                with self.assertRaises(ContractError) as ctx:
                    scheduler.run_descriptor(value)
                self.assertEqual(ctx.exception.code, "E_OPERATION_UNKNOWN")

    def test_a_malformed_descriptor_refuses_the_handoff_and_changes_nothing(self):
        scheduler = self.scheduler()
        op = self.claim(scheduler, "task-1")
        evidence = self.evidence("bad")
        good = self.descriptor(op, evidence)

        def with_spec(**changes):
            return dict(good, spec=dict(good["spec"], **changes))

        bad = {
            "not a dict": [good],
            "a dict subclass": Descriptor(good),
            "a missing key": {k: v for k, v in good.items() if k != "worktree_id"},
            "an extra key": dict(good, note="x"),
            "a key that is not exact text": {Key("spec"): good["spec"], "evidence_dir": good["evidence_dir"],
                                             "worktree_id": "wt-1"},
            "a spec that is a dict subclass": dict(good, spec=Descriptor(good["spec"])),
            "a spec without cwd": dict(good, spec={k: v for k, v in good["spec"].items() if k != "cwd"}),
            "a spec with an extra key": with_spec(shell=False),
            "a command that is a tuple": with_spec(command=tuple(good["spec"]["command"])),
            "a command with a non-text part": with_spec(command=[PY, 7]),
            "an empty command": with_spec(command=[]),
            "a timeout that is a bool": with_spec(timeout_seconds=True),
            "a timeout the runtime refuses": with_spec(timeout_seconds=0),
            "a cwd the runtime refuses": with_spec(cwd=str(self.cwd) + "\n"),
            "another run id": with_spec(run_id="op-" + "0" * 32),
            "another request": with_spec(request_id="req-2"),
            "an empty evidence directory": dict(good, evidence_dir=""),
            "an evidence directory with a lone surrogate": dict(good, evidence_dir=str(evidence) + "\udcff"),
            "an evidence directory with a NUL": dict(good, evidence_dir=str(evidence) + "\x00"),
            "a worktree that is not text": dict(good, worktree_id=None),
            "a worktree with a control character": dict(good, worktree_id="wt\n1"),
        }
        before = scheduler.events
        for label, descriptor in bad.items():
            with self.subTest(case=label):
                with self.assertRaises(ContractError) as ctx:
                    scheduler.handoff(op, "owner-1", 1, run=descriptor)
                self.assertEqual(ctx.exception.code, "E_RUN_DESCRIPTOR")
                self.assertEqual(scheduler.events, before)
                self.assertEqual(scheduler.operation(op)["state"], "RESERVED")
                self.assertIsNone(scheduler.run_descriptor(op))
        with self.subTest(case="ownership is checked before the descriptor"):
            with self.assertRaises(ContractError) as ctx:
                scheduler.handoff(op, "owner-2", 1, run=bad["an extra key"])
            self.assertEqual(ctx.exception.code, "E_FENCED")
        scheduler.handoff(op, "owner-1", 1, run=good)
        self.assertEqual(scheduler.run_descriptor(op), good)

    def test_the_bridge_records_the_descriptor_of_every_supervised_run(self):
        scheduler = self.scheduler()
        op = self.claim(scheduler, "task-1")
        evidence = self.evidence("bridge")
        spec = self.spec(op)
        outcome = run_supervised_operation(scheduler, operation_id=op, owner="owner-1", generation=1, spec=spec,
                                           environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-7",
                                           candidate_hash=self.candidate_hash)
        self.assertEqual(outcome["state"], "REVIEW_PENDING")
        self.assertEqual(scheduler.run_descriptor(op), dict(self.descriptor(op, evidence, spec=spec),
                                                            worktree_id="wt-7"))

    def test_the_durable_ledger_keeps_the_descriptor_and_the_reads(self):
        path = str(self.work / "ledger.db")
        ledger = self.scheduler(create_ledger(path))
        first = self.claim(ledger, "task-1")
        second = self.claim(ledger, "task-2")
        descriptor = self.descriptor(first, self.evidence("durable"))
        # A descriptor the log could not record is refused before the
        # in-memory handoff, so the ledger is still usable afterwards.
        with self.assertRaises(ContractError) as ctx:
            ledger.handoff(first, "owner-1", 1, run=dict(descriptor, worktree_id="wt-\udcff"))
        self.assertEqual(ctx.exception.code, "E_RUN_DESCRIPTOR")
        ledger.handoff(first, "owner-1", 1, run=descriptor)
        self.assertEqual((ledger.operation_ids(), ledger.run_descriptor(first)), ((first, second), descriptor))
        ledger.close()
        for read in (ledger.operation_ids, lambda: ledger.run_descriptor(first)):
            with self.assertRaises(LedgerError) as ctx:
                read()
            self.assertEqual(ctx.exception.code, "E_LEDGER_UNAVAILABLE", "a closed ledger still answers")
        reopened = open_ledger(path)
        self.assertEqual((reopened.operation_ids(), reopened.run_descriptor(first), reopened.run_descriptor(second)),
                         ((first, second), descriptor, None))
        reopened.close()


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class RecoverAfterRestartOracle(RecoveryCase):
    def test_every_older_operation_is_settled_and_reported(self):
        scheduler = self.scheduler()
        reviewed = self.claim(scheduler, "task-1")
        run_supervised_operation(scheduler, operation_id=reviewed, owner="owner-1", generation=1,
                                 spec=self.spec(reviewed), environment=BASE_ENV,
                                 evidence_dir=str(self.evidence("reviewed")), worktree_id="wt-1",
                                 candidate_hash=lambda directory: OLD,
                                 before_finish=lambda run: scheduler.settle(reviewed, USAGE))
        claimed = self.claim(scheduler, "task-2")
        # The old controller died after the run's records were written.
        finished, evidence = self.finished_run(scheduler, "task-3")
        # A handoff without a descriptor cannot be recovered from evidence.
        bare, _ = self.finished_run(scheduler, "task-4", descriptor=False)
        report = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(report, {"generation": 2, "operations": [
            {"operation_id": claimed, "action": "cancelled", "state": "CANCELLED_NOT_STARTED"},
            {"operation_id": finished, "action": "recovered", "state": "REVIEW_PENDING"},
            {"operation_id": bare, "action": "skipped", "state": "HANDOFF_ACCEPTED", "reason": "no run descriptor"},
        ]})
        self.assertEqual(self.candidates, [evidence])
        self.assertEqual(scheduler.operation(reviewed)["state"], "REVIEW_PENDING")
        self.assertEqual((scheduler.operation(finished)["owner"], scheduler.operation(finished)["generation"]),
                         ("owner-2", 2))
        recovered = [e for e in scheduler.events if e["type"] == "scheduler.recovered"]
        self.assertEqual([(e["operation_id"], e["payload"]["outcome"], e["payload"].get("candidate"))
                          for e in recovered], [(finished, "FINISHED", NEW)])
        self.assertEqual(scheduler._requests["req-1"].stopped, "RECOVERY_REQUIRED")

    def test_an_operation_the_old_controller_decided_is_left_alone(self):
        scheduler = self.scheduler()
        # Its records describe a finished run, but the old controller, which
        # saw the supervisor's own observation, decided it could not be
        # reviewed: the records alone must not overturn that.
        decided, _ = self.finished_run(scheduler, "task-1")
        scheduler.unknown(decided, "owner-1", 1, reason="CLEANUP_UNCONFIRMED")
        before = scheduler.operation(decided)
        report = recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(report, {"generation": 2, "operations": []})
        self.assertEqual(scheduler.operation(decided), before)
        self.assertEqual(self.candidates, [])
        self.assertEqual([e for e in scheduler.events if e["type"] == "scheduler.recovered"], [])

    def test_a_failure_on_one_operation_does_not_stop_the_others(self):
        scheduler = self.scheduler()
        refused, evidence_refused = self.finished_run(scheduler, "task-1")
        coded, evidence_coded = self.finished_run(scheduler, "task-2")
        fine, _ = self.finished_run(scheduler, "task-3")

        def candidate_hash(evidence_dir):
            if evidence_dir == evidence_refused:
                raise RuntimeError("no candidate for this run")
            if evidence_dir == evidence_coded:
                raise RuntimeErrorCode("E_RUNTIME_EVIDENCE", "the candidate tree is gone")
            return NEW

        report = recover_after_restart(scheduler, owner="owner-2", candidate_hash=candidate_hash)
        self.assertEqual(report["operations"], [
            {"operation_id": refused, "action": "failed", "state": "HANDOFF_ACCEPTED", "error": "RuntimeError"},
            {"operation_id": coded, "action": "failed", "state": "HANDOFF_ACCEPTED", "error": "E_RUNTIME_EVIDENCE"},
            {"operation_id": fine, "action": "recovered", "state": "REVIEW_PENDING"},
        ])
        for op in (refused, coded):
            self.assertEqual((scheduler.operation(op)["state"], scheduler.operation(op)["generation"]),
                             ("HANDOFF_ACCEPTED", 1), "a failed recovery changed its operation")

    def test_a_ledger_error_or_an_interrupt_stops_the_recovery(self):
        with self.subTest(case="the ledger fails"):
            path = str(self.work / "ledger.db")
            ledger = self.scheduler(create_ledger(path))
            first, _ = self.finished_run(ledger, "task-1")
            last, last_evidence = self.finished_run(ledger, "task-2")
            calls = []

            def close_the_ledger_on_the_last(evidence_dir):
                # The failure comes on the last operation, so nothing after it
                # could reveal a swallowed ledger error.
                calls.append(evidence_dir)
                if evidence_dir == last_evidence:
                    ledger.close()
                return NEW

            with self.assertRaises(LedgerError) as ctx:
                recover_after_restart(ledger, owner="owner-2", candidate_hash=close_the_ledger_on_the_last)
            self.assertEqual(ctx.exception.code, "E_LEDGER_UNAVAILABLE")
            self.assertEqual(len(calls), 2)
            reopened = open_ledger(path)
            self.assertEqual((reopened.generation, reopened.operation(first)["state"],
                              reopened.operation(last)["state"]), (2, "REVIEW_PENDING", "HANDOFF_ACCEPTED"))
            reopened.close()
        with self.subTest(case="an interrupt"):
            scheduler = self.scheduler()
            first, _ = self.finished_run(scheduler, "task-3")
            second, _ = self.finished_run(scheduler, "task-4")

            def interrupt(evidence_dir):
                raise KeyboardInterrupt

            with self.assertRaises(KeyboardInterrupt):
                recover_after_restart(scheduler, owner="owner-2", candidate_hash=interrupt)
            self.assertEqual([scheduler.operation(op)["state"] for op in (first, second)],
                             ["HANDOFF_ACCEPTED", "HANDOFF_ACCEPTED"])

    def test_the_inputs_are_checked_before_the_rollover(self):
        scheduler = self.scheduler()
        op, _ = self.finished_run(scheduler, "task-1")
        before = scheduler.events
        for label, owner, candidate_hash in (("no owner", None, self.candidate_hash),
                                             ("an empty owner", "", self.candidate_hash),
                                             ("an owner that is not text", 7, self.candidate_hash),
                                             ("an owner that is not exact text", Key("owner-2"), self.candidate_hash),
                                             ("no candidate callback", "owner-2", None),
                                             ("a candidate that cannot be called", "owner-2", NEW)):
            with self.subTest(case=label):
                with self.assertRaises(ContractError) as ctx:
                    recover_after_restart(scheduler, owner=owner, candidate_hash=candidate_hash)
                self.assertEqual(ctx.exception.code, "E_RECOVERY_INPUT")
                self.assertEqual(scheduler.events, before)
        self.assertIsNone(scheduler._requests["req-1"].stopped)
        self.assertEqual(recover_after_restart(scheduler, owner="owner-2", candidate_hash=self.candidate_hash)
                         ["generation"], 2)
        self.assertEqual(scheduler.operation(op)["state"], "REVIEW_PENDING")

    def test_a_controller_killed_after_the_records_is_recovered_into_review(self):
        self.crash_and_recover("after-the-records", crashed="HANDOFF_ACCEPTED", expected_state="REVIEW_PENDING",
                               expected_outcome="FINISHED")

    def test_a_controller_killed_before_the_review_is_recovered_into_review(self):
        self.crash_and_recover("before-the-review", crashed="RUNNING", expected_state="REVIEW_PENDING",
                               expected_outcome="FINISHED")

    def test_a_controller_killed_while_its_run_is_still_going_is_start_unknown(self):
        self.crash_and_recover("mid-run", crashed="HANDOFF_ACCEPTED", expected_state="START_UNKNOWN",
                               expected_outcome="START_UNKNOWN")

    def crash_and_recover(self, mode, *, crashed, expected_state, expected_outcome):
        ledger_path = str(self.work / "ledger.db")
        started, release = self.work / "started", self.work / "release"
        self.addCleanup(lambda: release.touch())
        evidence = {"reviewed": str(self.evidence("reviewed")), "crashed": str(self.evidence("crashed"))}
        config = {"ledger": ledger_path, "python": PY, "cwd": str(self.cwd), "evidence": evidence, "mode": mode,
                  "done": DONE, "wait": WAIT_FOR_RELEASE, "started": str(started), "release": str(release)}
        controller = subprocess.Popen([PY, "-c", CONTROLLER, json.dumps(config)], cwd=str(REPO_ROOT),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: controller.poll() is None and controller.kill())
        if mode == "mid-run":
            deadline = time.monotonic() + 30
            while pid_in(started) is None and time.monotonic() < deadline and controller.poll() is None:
                time.sleep(0.01)
            child = pid_in(started)
            self.assertIsNotNone(child, "the supervised child never started")
            controller.kill()
        out, err = controller.communicate(timeout=60)
        self.assertEqual(controller.returncode, -signal.SIGKILL, err)
        ops = json.loads(out.strip().splitlines()[-1])
        if mode == "mid-run":
            # The child outlives its controller; let it finish before recovering.
            release.touch()
            deadline = time.monotonic() + 30
            while not gone(child) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(gone(child), "the supervised child never exited")
        ledger = open_ledger(ledger_path)
        self.assertEqual(ledger.operation(ops["crashed"])["state"], crashed)
        self.assertEqual(ledger.run_descriptor(ops["crashed"])["evidence_dir"], evidence["crashed"])
        report = recover_after_restart(ledger, owner="owner-2", candidate_hash=self.candidate_hash)
        self.assertEqual(report, {"generation": 2, "operations": [
            {"operation_id": ops["claimed"], "action": "cancelled", "state": "CANCELLED_NOT_STARTED"},
            {"operation_id": ops["crashed"], "action": "recovered", "state": expected_state},
        ]})
        self.assertEqual(ledger.operation(ops["reviewed"])["state"], "REVIEW_PENDING")
        expected = {op: ledger.operation(op) for op in ops.values()}
        events = ledger.events
        ledger.close()
        reopened = open_ledger(ledger_path)
        self.assertEqual({op: reopened.operation(op) for op in ops.values()}, expected,
                         "the recovery did not survive a reopen")
        self.assertEqual(reopened.events, events)
        recovered = [e for e in events if e["type"] == "scheduler.recovered"]
        self.assertEqual([(e["operation_id"], e["payload"]["outcome"]) for e in recovered],
                         [(ops["crashed"], expected_outcome)])
        reopened.close()


if __name__ == "__main__":
    unittest.main()
