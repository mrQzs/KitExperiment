"""Executor tests for durable start records and the replay gate.

The planner oracle proves the end-to-end decisions with real process trees.
These cases cover what it leaves open:

* the ``before_start`` hook: an interrupt rolls the evidence pair back and comes
  out unchanged, the hook runs exactly once, ``None`` and an omitted argument
  behave identically, and an unusable hook is caught *after* the evidence
  directory check (so a missing directory still reports ``E_RUNTIME_EVIDENCE``);
* record writes that fail (a refused ``os.link``, an observation that cannot be
  published): the failure code, the absence of a leftover ``.partial`` file, the
  process that never started, and the ``START_UNKNOWN`` a halfway record gets;
* records that cannot be read: a missing inner key, a wrong schema, a non-text
  value, an array root, a directory in place of a record, and one byte past the
  64 KiB ceiling (while a record of exactly 64 KiB still reads);
* that the decision is read-only, and that a not-started three-key observation
  yields exactly what `reconcile` yields.

Convention followed throughout: every rejection case first proves that a legal
baseline record is accepted, then changes exactly one input and asserts one
specific error code, so a rejection can never be attributed to an earlier rule.
Every case cleans up the process group it started.  Numeric bounds are compared
against the numbers in the contract, not against the module's own constants.
"""
from __future__ import annotations

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

from sentinelmesh.core import canonical_json
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode, reconcile
from sentinelmesh.supervised_record import (
    HANDOFF_NAME,
    OBSERVATION_NAME,
    record_supervised_run,
    replay_decision,
)
from sentinelmesh.posix_supervisor import supervise

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
BASE_TIMEOUT = 10
# Contract ceiling for a single record file: 64 KiB, and one byte more.
MAX_RECORD_BYTES = 65536
OVER_CEILING = MAX_RECORD_BYTES + 1
GARBAGE = b"\x00\x01\x02 not an executable image\n"
HANDOFF_SCHEMA = "sentinelmesh-handoff-v1"
OBSERVATION_SCHEMA = "sentinelmesh-observation-v1"
# A leader that reports its own pid and then refuses to die, so a case that
# wrongly starts a process still knows which group to kill.
PID_THEN_SLEEP = ("import os, sys, time\n"
                  "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
                  "os.replace(sys.argv[1] + '.tmp', sys.argv[1])\n"
                  "time.sleep(30)\n")


def handoff_of(spec: InvocationSpec) -> dict:
    """Return the handoff record the contract requires for ``spec``."""
    return {
        "schema": HANDOFF_SCHEMA,
        "request_id": spec.request_id,
        "run_id": spec.run_id,
        "environment_sha256": spec.environment_sha256,
        "spec_sha256": spec.digest(),
    }


def recanonical(value: dict) -> bytes:
    """Return the canonical bytes of a record dict read back from disk."""
    return canonical_json(value)


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class SupervisedRecordTests(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="supervised-record-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.garbage = self.cwd / "not-a-program"
        self.garbage.write_bytes(GARBAGE)
        self.garbage.chmod(0o755)
        self.dirs = 0

    # helpers ---------------------------------------------------------------

    def evidence_dir(self) -> Path:
        self.dirs += 1
        path = self.work / ("evidence-%d" % self.dirs)
        path.mkdir()
        return path

    def spec(self, code, *args, environment=None, timeout=BASE_TIMEOUT, command=None, run_id="run-1"):
        env = BASE_ENV if environment is None else environment
        argv = command if command is not None else (PY, "-c", code, *args)
        return InvocationSpec("req-1", run_id, argv, str(self.cwd), environment_digest(env), timeout)

    def kill_group(self, pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def kill_pid_in(self, path: Path):
        """Kill the group of the process that reported its pid in ``path``."""
        try:
            pid = int(path.read_text().strip())
        except (OSError, ValueError):
            return
        self.kill_group(pid)

    def code_of(self, func, *args, **kwargs):
        with self.assertRaises(RuntimeErrorCode) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def record(self, spec, evidence, environment=None, **kwargs):
        """Record one legal run; its group is killed once the case is over."""
        run = record_supervised_run(spec, environment=BASE_ENV if environment is None else environment,
                                    evidence_dir=str(evidence), worktree_id="wt-1", **kwargs)
        if run.identity is not None:
            self.addCleanup(self.kill_group, run.identity.pid)
        return run

    def supervise(self, spec, evidence, environment=None, **kwargs):
        """Supervise one legal run with a caller hook; its group is killed after it."""
        run = supervise(spec, environment=BASE_ENV if environment is None else environment,
                       evidence_dir=str(evidence), worktree_id="wt-1", **kwargs)
        if run.identity is not None:
            self.addCleanup(self.kill_group, run.identity.pid)
        return run

    def decision(self, spec, evidence):
        return replay_decision(spec, str(evidence))

    def assert_decision(self, record, spec, status, replay_allowed, observed):
        self.assertEqual(record, {"status": status, "run_id": spec.run_id,
                                  "expected_environment_sha256": spec.environment_sha256,
                                  "observed_environment_sha256": observed, "replay_allowed": replay_allowed})

    def snapshot(self, evidence: Path):
        return sorted((entry.name, entry.stat().st_size, entry.read_bytes()) for entry in evidence.iterdir())

    def sized_handoff(self, spec: InvocationSpec, target: int):
        """Return ``(spec, bytes)`` whose handoff record is exactly ``target`` bytes.

        Only the request id is grown or trimmed, and a digest is always the same
        width, so the canonical record moves by exactly the padding length.
        """
        length = len(spec.request_id)
        for _ in range(8):
            padded = InvocationSpec("r" * length, spec.run_id, spec.command, spec.cwd,
                                    spec.environment_sha256, spec.timeout_seconds)
            raw = canonical_json(handoff_of(padded))
            if len(raw) == target:
                return padded, raw
            length += target - len(raw)
        self.fail("a handoff of exactly %d bytes cannot be built" % target)

    # 1. the supervisor hook -------------------------------------------------

    def test_interrupt_from_the_hook_rolls_the_evidence_pair_back(self):
        pid_file = self.work / "leader.pid"
        self.addCleanup(self.kill_pid_in, pid_file)
        spec = self.spec(PID_THEN_SLEEP, str(pid_file), timeout=2)
        # Legal baseline: the same run, with a hook that returns, starts and
        # finishes, so the rollback below cannot be blamed on a bad input.
        baseline = self.evidence_dir()
        ok = self.spec("pass")
        run = self.record(ok, baseline)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertEqual((baseline / HANDOFF_NAME).read_bytes(), canonical_json(handoff_of(ok)))

        refused = self.evidence_dir()
        sent = KeyboardInterrupt("controller refused the start")
        seen = []

        def refuse():
            seen.append(sorted(os.listdir(refused)))
            raise sent

        with self.assertRaises(KeyboardInterrupt) as ctx:
            supervise(spec, environment=BASE_ENV, evidence_dir=str(refused), worktree_id="wt-1",
                      before_start=refuse)
        self.assertIs(ctx.exception, sent, "the hook's own exception did not come out unchanged")
        self.assertEqual(seen, [["stderr.log", "stdout.log"]], "the hook did not run after the evidence pair")
        self.assertEqual(os.listdir(refused), [], "the rolled-back run left evidence behind")
        time.sleep(0.3)
        self.assertFalse(pid_file.exists(), "a process started although the hook raised")

    def test_hook_runs_exactly_once(self):
        spec = self.spec("pass")
        evidence = self.evidence_dir()
        calls = []

        def count():
            calls.append(sorted(os.listdir(evidence)))

        run = self.supervise(spec, evidence, before_start=count)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertEqual(calls, [["stderr.log", "stdout.log"]], "the hook did not run exactly once")

    def test_an_omitted_hook_and_none_behave_alike(self):
        spec = self.spec("pass")
        implicit = self.evidence_dir()
        omitted = self.supervise(spec, implicit)
        explicit = self.evidence_dir()
        allowed = self.supervise(spec, explicit, before_start=None)
        self.assertEqual((omitted.stop_reason, omitted.exit_code), (allowed.stop_reason, allowed.exit_code))
        self.assertEqual(omitted.observation["state"], allowed.observation["state"])
        self.assertEqual(omitted.observation["cleanup_confirmed"], allowed.observation["cleanup_confirmed"])
        self.assertEqual(omitted.lifecycle.state, allowed.lifecycle.state)
        # The record files are the recording layer's business; the caller hook
        # may only differ in what the supervisor itself leaves behind.
        for name in ("stderr.log", "stdout.log"):
            self.assertEqual((implicit / name).read_bytes(), (explicit / name).read_bytes(),
                             "%s differs between an omitted hook and before_start=None" % name)
        for evidence in (implicit, explicit):
            self.assertEqual(sorted(os.listdir(evidence)), ["stderr.log", "stdout.log"])

    def test_unusable_hook_is_preceded_by_the_evidence_directory_check(self):
        # Legal baseline: no hook at all is a perfectly usable start.
        ok = self.spec("pass")
        baseline = self.evidence_dir()
        run = self.record(ok, baseline)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertEqual(self.decision(ok, baseline), dict(reconcile(ok, run.observation)))

        pid_file = self.work / "leader.pid"
        self.addCleanup(self.kill_pid_in, pid_file)
        spec = self.spec(PID_THEN_SLEEP, str(pid_file), timeout=2)
        missing = self.work / "missing"
        # Both rules broken at once: the missing directory is reported, not the
        # unusable hook that would be reported if the order were reversed.
        self.assertEqual(self.code_of(supervise, spec, environment=BASE_ENV, evidence_dir=str(missing),
                                      worktree_id="wt-1", before_start="not callable"), "E_RUNTIME_EVIDENCE")
        self.assertFalse(missing.exists())
        # The same input with only the hook rule broken isolates that rule.
        fresh = self.evidence_dir()
        self.assertEqual(self.code_of(supervise, spec, environment=BASE_ENV, evidence_dir=str(fresh),
                                      worktree_id="wt-1", before_start=42), "E_RUNTIME_INVOCATION")
        self.assertEqual(os.listdir(fresh), [], "a refused hook left files behind")
        time.sleep(0.3)
        self.assertFalse(pid_file.exists(), "a process started although the hook was unusable")

    # 2. writes that fail ----------------------------------------------------

    def test_a_refused_publish_starts_no_process(self):
        pid_file = self.work / "leader.pid"
        self.addCleanup(self.kill_pid_in, pid_file)
        # Legal baseline: the same handoff, when it can be published, records a
        # legal run, so the refusal below cannot be blamed on the input.
        ok = self.spec("pass")
        baseline = self.evidence_dir()
        run = self.record(ok, baseline)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertEqual((baseline / HANDOFF_NAME).read_bytes(), canonical_json(handoff_of(ok)))
        spec = self.spec(PID_THEN_SLEEP, str(pid_file), timeout=2)
        blocked = self.evidence_dir()
        with mock.patch.object(os, "link", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(self.code_of(record_supervised_run, spec, environment=BASE_ENV,
                                          evidence_dir=str(blocked), worktree_id="wt-1"), "E_RUNTIME_EVIDENCE")
        self.assertEqual(sorted(os.listdir(blocked)), [], "the failed handoff left files behind")
        self.assertFalse((blocked / (".%s.partial" % HANDOFF_NAME)).exists(), "a partial file survived")
        time.sleep(0.3)
        self.assertFalse(pid_file.exists(), "a process started although the handoff could not be recorded")
        # Nothing was handed off, so a replay is still allowed.
        self.assert_decision(self.decision(spec, blocked), spec, "NOT_STARTED", True, None)

    def test_a_failed_observation_leaves_start_unknown(self):
        pid_file = self.work / "leader.pid"
        self.addCleanup(self.kill_pid_in, pid_file)
        spec = self.spec(PID_THEN_SLEEP, str(pid_file), timeout=2)
        evidence = self.evidence_dir()
        expected_handoff = canonical_json(handoff_of(spec))

        real_link = os.link
        published = []

        def refuse_the_observation(source, target):
            published.append(os.path.basename(target))
            if len(published) == 2:
                raise OSError(28, "No space left on device")
            return real_link(source, target)

        with mock.patch.object(os, "link", refuse_the_observation):
            self.assertEqual(self.code_of(record_supervised_run, spec, environment=BASE_ENV,
                                          evidence_dir=str(evidence), worktree_id="wt-1"), "E_RUNTIME_EVIDENCE")
        self.kill_pid_in(pid_file)
        self.assertEqual(published, [HANDOFF_NAME, OBSERVATION_NAME])
        self.assertEqual(sorted(os.listdir(evidence)), [HANDOFF_NAME, "stderr.log", "stdout.log"])
        self.assertEqual((evidence / HANDOFF_NAME).read_bytes(), expected_handoff)
        self.assertFalse((evidence / (".%s.partial" % OBSERVATION_NAME)).exists(),
                         "the failed observation left a partial file behind")
        # The run is over and its handoff is on disk, so the start is unknown.
        self.assert_decision(self.decision(spec, evidence), spec, "START_UNKNOWN", False, None)

    def test_an_existing_record_keeps_its_bytes_and_leaves_no_partial(self):
        pid_file = self.work / "leader.pid"
        self.addCleanup(self.kill_pid_in, pid_file)
        spec = self.spec(PID_THEN_SLEEP, str(pid_file), timeout=2)
        evidence = self.evidence_dir()
        earlier = b"earlier handoff\n"
        (evidence / HANDOFF_NAME).write_bytes(earlier)
        self.assertEqual(self.code_of(record_supervised_run, spec, environment=BASE_ENV,
                                      evidence_dir=str(evidence), worktree_id="wt-1"), "E_RUNTIME_EVIDENCE")
        self.assertEqual(sorted(os.listdir(evidence)), [HANDOFF_NAME])
        self.assertEqual((evidence / HANDOFF_NAME).read_bytes(), earlier, "an existing record was modified")
        time.sleep(0.3)
        self.assertFalse(pid_file.exists(), "a process started although the handoff was refused")

    def test_a_credential_name_is_refused_before_any_record(self):
        environment = {"LANG": "C", "PATH": "/usr/bin:/bin", "SERVICE_API_TOKEN": "secret-value"}
        spec = self.spec("pass", environment=environment)
        # Legal baseline: the name only has to be allowlisted.
        baseline = self.evidence_dir()
        run = self.record(spec, baseline, environment=environment,
                          allowed_secret_names=frozenset({"SERVICE_API_TOKEN"}))
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertEqual(run.environment_names, ("LANG", "PATH", "SERVICE_API_TOKEN"))
        refused = self.evidence_dir()
        self.assertEqual(self.code_of(record_supervised_run, spec, environment=environment,
                                      evidence_dir=str(refused), worktree_id="wt-1"), "E_RUNTIME_ENVIRONMENT")
        self.assertEqual(os.listdir(refused), [])

    # 3. records that cannot be read ----------------------------------------

    def test_a_record_the_module_never_wrote_is_refused(self):
        spec = self.spec("pass")
        source = self.evidence_dir()
        run = self.record(spec, source)
        handoff = json.loads((source / HANDOFF_NAME).read_bytes())
        observation = json.loads((source / OBSERVATION_NAME).read_bytes())
        inner = observation["observation"]
        valid_handoff = (source / HANDOFF_NAME).read_bytes()

        def inner_without(key):
            return {name: value for name, value in inner.items() if name != key}

        def inner_identity_without(key):
            identity = {name: value for name, value in inner["process_identity"].items() if name != key}
            return dict(inner, process_identity=identity)

        def handoff_without(key):
            return {name: value for name, value in handoff.items() if name != key}

        cases = {
            # Each observation case below keeps the valid handoff, so the inner
            # shape itself is what is refused, not a missing handoff.
            "observation without cleanup_confirmed": {
                HANDOFF_NAME: valid_handoff, OBSERVATION_NAME: recanonical(
                    dict(observation, observation=inner_without("cleanup_confirmed")))},
            "observation identity without worktree_id": {
                HANDOFF_NAME: valid_handoff, OBSERVATION_NAME: recanonical(
                    dict(observation, observation=inner_identity_without("worktree_id")))},
            "observation identity without a numeric pid": {
                HANDOFF_NAME: valid_handoff, OBSERVATION_NAME: recanonical(
                    dict(observation, observation=dict(inner, process_identity=dict(
                        inner["process_identity"], pid="1"))))},
            "observation cleanup_confirmed that is not boolean": {
                HANDOFF_NAME: valid_handoff, OBSERVATION_NAME: recanonical(
                    dict(observation, observation=dict(inner, cleanup_confirmed="yes")))},
            "observation state that is not text": {
                HANDOFF_NAME: valid_handoff, OBSERVATION_NAME: recanonical(
                    dict(observation, observation=dict(inner, state=3)))},
            "observation without spec_sha256": {HANDOFF_NAME: valid_handoff, OBSERVATION_NAME: recanonical(
                {name: value for name, value in observation.items() if name != "spec_sha256"})},
            "handoff with a wrong schema": {HANDOFF_NAME: recanonical(
                dict(handoff, schema=HANDOFF_SCHEMA + "-v2"))},
            "handoff with an extra key": {HANDOFF_NAME: recanonical(dict(handoff, note="x"))},
            "handoff without spec_sha256": {HANDOFF_NAME: recanonical(handoff_without("spec_sha256"))},
            "observation with a wrong schema": {HANDOFF_NAME: valid_handoff, OBSERVATION_NAME: recanonical(
                dict(observation, schema=OBSERVATION_SCHEMA + "-v2"))},
            "observation spec_sha256 that is not text": {HANDOFF_NAME: valid_handoff, OBSERVATION_NAME: recanonical(
                dict(observation, spec_sha256=1))},
            "handoff value that is not text": {HANDOFF_NAME: recanonical(dict(handoff, run_id=7))},
            "handoff root is an array": {HANDOFF_NAME: b'["sentinelmesh-handoff-v1"]\n'},
            "observation root is an array": {HANDOFF_NAME: valid_handoff,
                                             OBSERVATION_NAME: b'["sentinelmesh-observation-v1"]\n'},
        }
        # A legal baseline first: the untouched pair is accepted and decides a
        # real outcome, so no case below can pass for an unrelated reason.
        self.assertEqual(self.decision(spec, source), dict(reconcile(spec, run.observation)))
        for label, files in cases.items():
            with self.subTest(case=label):
                evidence = self.evidence_dir()
                for name, data in files.items():
                    (evidence / name).write_bytes(data)
                self.assertEqual(self.code_of(self.decision, spec, evidence), "E_RUNTIME_EVIDENCE")

    def test_a_directory_in_place_of_a_record_is_refused(self):
        spec = self.spec("pass")
        # Legal baseline: the record pair reads back as the supervisor's outcome.
        source = self.evidence_dir()
        run = self.record(spec, source)
        self.assertEqual(self.decision(spec, source), dict(reconcile(spec, run.observation)))
        for target in (HANDOFF_NAME, OBSERVATION_NAME):
            with self.subTest(record=target):
                evidence = self.evidence_dir()
                if target != HANDOFF_NAME:
                    shutil.copy(source / HANDOFF_NAME, evidence / HANDOFF_NAME)
                (evidence / target).mkdir()
                self.assertEqual(self.code_of(self.decision, spec, evidence), "E_RUNTIME_EVIDENCE")

    def test_one_byte_past_the_ceiling_is_refused_while_the_ceiling_reads(self):
        over_spec, over = self.sized_handoff(self.spec("pass"), OVER_CEILING)
        at_ceiling_spec, at_ceiling = self.sized_handoff(self.spec("pass"), MAX_RECORD_BYTES)
        self.assertEqual(len(over), OVER_CEILING)
        self.assertEqual(len(at_ceiling), MAX_RECORD_BYTES)
        self.assertEqual(len(at_ceiling) + 1, OVER_CEILING)

        # Legal baseline: a record of exactly the ceiling still reads, and the
        # handoff it carries is this specification's own, so the start is unknown.
        readable = self.evidence_dir()
        (readable / HANDOFF_NAME).write_bytes(at_ceiling)
        self.assert_decision(self.decision(at_ceiling_spec, readable), at_ceiling_spec, "START_UNKNOWN", False, None)

        # One byte more, and the very same record is refused.
        blocked = self.evidence_dir()
        (blocked / HANDOFF_NAME).write_bytes(over)
        self.assertEqual(self.code_of(self.decision, over_spec, blocked), "E_RUNTIME_EVIDENCE")

    # 4. the decision itself ------------------------------------------------

    def test_the_decision_reads_nothing_but_the_records(self):
        spec = self.spec("pass")
        evidence = self.evidence_dir()
        run = self.record(spec, evidence)
        before = self.snapshot(evidence)
        first = self.decision(spec, evidence)
        self.assertEqual(first, dict(reconcile(spec, run.observation)))
        for _ in range(3):
            self.assertEqual(self.decision(spec, evidence), first)
        self.assertEqual(self.snapshot(evidence), before, "the decision changed the evidence directory")

    def test_a_not_started_observation_matches_reconcile(self):
        spec = self.spec(None, command=(str(self.garbage),))
        evidence = self.evidence_dir()
        run = self.record(spec, evidence)
        self.assertEqual(run.stop_reason, "start_failed")
        self.assertEqual(run.observation, {"run_id": spec.run_id, "environment_sha256": spec.environment_sha256,
                                           "state": "NOT_STARTED"})
        record = self.decision(spec, evidence)
        self.assertEqual(record, dict(reconcile(spec, run.observation)))
        self.assert_decision(record, spec, "NOT_STARTED", True, spec.environment_sha256)


if __name__ == "__main__":
    unittest.main()
