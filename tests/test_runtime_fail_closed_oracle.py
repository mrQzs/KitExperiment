"""Fail-closed and exact-port cases for the canonical runtime layer.

Planner-provided acceptance oracle for pi-runtime-contract-port (v2); executors
must not modify this file.  Every assertion was run against the frozen Code-Kit
reference in .agent/design/runtime-reference/runtime.py.txt before dispatch.
The cases cover inputs the reference test suite does not exercise: missing,
empty and malformed observations, partial process identities, rejected
handoffs, the safe-integer bounds and the digest encoding.
"""
from __future__ import annotations

import hashlib
import unittest

from sentinelmesh.runtime import InvocationSpec, Lifecycle, ProcessIdentity, RuntimeErrorCode, classify_start, reconcile

SHA_A = "a" * 64
MAX_SAFE = 2**53 - 1
IDENTITY = {"pid": 10, "boot_id": "boot", "executable_sha256": "b" * 64, "worktree_id": "tree"}


def spec(**changes):
    values = {"request_id": "req", "run_id": "run", "command": ("python", "-c", "pass"), "cwd": "/repo",
              "environment_sha256": SHA_A, "timeout_seconds": 30}
    values.update(changes)
    return InvocationSpec(**values)


def observed(**fields):
    return {"run_id": "run", "environment_sha256": SHA_A, **fields}


def classify(observation, handoff=True):
    return classify_start(handoff_accepted=handoff, started_observation=observation, expected=spec())


class ReconcileFailsClosedTests(unittest.TestCase):
    def test_empty_observation_is_a_mismatch_and_never_replayable(self):
        record = reconcile(spec(), {})
        self.assertEqual(record["status"], "IDENTITY_MISMATCH")
        self.assertIs(record["replay_allowed"], False)

    def test_missing_observation_is_rejected(self):
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_OBSERVATION"):
            reconcile(spec(), None)

    def test_reconcile_assumes_an_accepted_handoff_and_ignores_record_flags(self):
        started = observed(state="STARTED", process_identity=IDENTITY)
        self.assertEqual(reconcile(spec(), started)["status"], "STARTED")
        self.assertEqual(reconcile(spec(), {**started, "handoff_accepted": False})["status"], "STARTED")
        record = reconcile(spec(), observed(state="NOT_STARTED", handoff_accepted="yes"))
        self.assertEqual((record["status"], record["replay_allowed"]), ("NOT_STARTED", True))

    def test_terminal_replay_requires_a_complete_identity(self):
        for identity in ({}, {"x": 1}, {"pid": 10}, {**IDENTITY, "run_id": "run"}, "10"):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_IDENTITY"):
                    reconcile(spec(), observed(state="FAILED", process_identity=identity, cleanup_confirmed=True))
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_PID"):
            reconcile(spec(), observed(state="FAILED", process_identity={**IDENTITY, "pid": 0},
                                       cleanup_confirmed=True))
        record = reconcile(spec(), observed(state="STOPPED", process_identity=IDENTITY, cleanup_confirmed=True))
        self.assertEqual(record["status"], "STOPPED")
        self.assertIs(record["replay_allowed"], True)

    def test_not_started_with_any_process_identity_key_is_not_replayable(self):
        for identity in (None, {}):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_IDENTITY"):
                    reconcile(spec(), observed(state="NOT_STARTED", process_identity=identity))
        record = reconcile(spec(), observed(state="NOT_STARTED", process_identity=IDENTITY))
        self.assertEqual(record["status"], "START_UNKNOWN")
        self.assertIs(record["replay_allowed"], False)

    def test_record_shape_passes_the_observed_environment_through(self):
        record = reconcile(spec(), {"run_id": "run", "environment_sha256": 12, "state": "NOT_STARTED"})
        self.assertEqual(set(record), {"status", "run_id", "expected_environment_sha256",
                                       "observed_environment_sha256", "replay_allowed"})
        self.assertEqual(record["status"], "IDENTITY_MISMATCH")
        self.assertEqual(record["run_id"], "run")
        self.assertEqual(record["expected_environment_sha256"], SHA_A)
        self.assertEqual(record["observed_environment_sha256"], 12)
        self.assertIs(record["replay_allowed"], False)


class ClassifyStartTests(unittest.TestCase):
    def test_empty_mapping_is_an_observation_not_a_missing_one(self):
        for handoff in (True, False):
            with self.subTest(handoff=handoff):
                self.assertEqual(classify({}, handoff), "IDENTITY_MISMATCH")

    def test_rejected_handoff_only_keeps_not_started(self):
        for state in ("STARTED", "FAILED", "STOPPED", "STOPPING", "BROKEN"):
            with self.subTest(state=state):
                observation = observed(state=state, process_identity=IDENTITY)
                self.assertEqual(classify(observation, handoff=False), "IDENTITY_MISMATCH")
        self.assertEqual(classify(observed(state="NOT_STARTED"), handoff=False), "NOT_STARTED")

    def test_started_requires_a_closed_four_field_identity(self):
        for identity in ({}, {"pid": 10}, {**IDENTITY, "run_id": "run", "environment_sha256": SHA_A}, "10", 10):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_IDENTITY"):
                    classify(observed(state="STARTED", process_identity=identity))
        self.assertEqual(classify(observed(state="STARTED", process_identity=IDENTITY)), "STARTED")
        self.assertEqual(classify(observed(state="STARTED", process_identity=ProcessIdentity(**IDENTITY))),
                         "STARTED")


class LifecycleIdentityAndBoundsTests(unittest.TestCase):
    def test_started_needs_an_identity_on_every_path(self):
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_IDENTITY"):
            Lifecycle().transition("STARTING").transition("STARTED")
        unknown = Lifecycle().transition("STARTING").transition("START_UNKNOWN")
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_IDENTITY"):
            unknown.transition("STARTED", expected_generation=unknown.generation)
        recovered = unknown.transition("STARTED", identity=ProcessIdentity(**IDENTITY),
                                       expected_generation=unknown.generation)
        self.assertEqual((recovered.state, recovered.generation), ("STARTED", unknown.generation + 1))

    def test_counters_stay_inside_the_safe_integer_range(self):
        self.assertEqual(spec(timeout_seconds=MAX_SAFE).timeout_seconds, MAX_SAFE)
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_TIMEOUT"):
            spec(timeout_seconds=MAX_SAFE + 1)
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_PID"):
            ProcessIdentity(**{**IDENTITY, "pid": MAX_SAFE + 1})
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_FENCE"):
            Lifecycle(generation=MAX_SAFE + 1)


class DigestTests(unittest.TestCase):
    def test_digest_uses_the_code_kit_encoding(self):
        value = spec(command=("python", "-c", "print(1)", ""), cwd="C:\\repo")
        payload = "\0".join(("req", "run", "python", "-c", "print(1)", "", "C:\\repo", SHA_A, "30"))
        self.assertEqual(value.digest(), hashlib.sha256(payload.encode("utf-8")).hexdigest())

    def test_every_accepted_spec_has_a_digest(self):
        large = spec(command=("python", "x" * 1_200_000), timeout_seconds=MAX_SAFE)
        self.assertRegex(large.digest(), r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
