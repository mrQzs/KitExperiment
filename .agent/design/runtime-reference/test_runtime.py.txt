import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from codekit.runtime import InvocationSpec, Lifecycle, ProcessIdentity, RuntimeErrorCode, classify_start, reconcile, validate_argv


def invocation():
    return InvocationSpec("req", "run", ("python", "-c", "pass"), "C:\\repo", "a" * 64, 30)


def process_identity():
    return ProcessIdentity(10, "boot", "b" * 64, "tree")


class RuntimeTests(unittest.TestCase):
    def test_argv_preserves_empty_repeated_and_multiline_arguments(self):
        argv = ("python", "-c", "pass\n", "--tag", "same", "--tag", "same", "")
        self.assertEqual(validate_argv(argv), argv)
        for command in (("",), ("py\nthon",), ("python", "\ud800"), ("python", "\x00")):
            with self.subTest(command=command), self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_ARGV"):
                validate_argv(command)

    def test_argv_and_shell_are_mutually_exclusive(self):
        self.assertEqual(validate_argv(["cmd", "中文"]), ("cmd", "中文"))
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_SHELL_ARGV"):
            validate_argv(["cmd"], shell=True)
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_ARGV"):
            validate_argv(["cmd\x00bad"])

    def test_invocation_canonicalizes_mutable_argv(self):
        argv = ["python", "-c", "pass"]
        spec = InvocationSpec("req", "run", argv, "C:/repo", "a" * 64, 30)
        digest = spec.digest()
        argv.append("mutated-after-construction")
        self.assertIsInstance(spec.command, tuple)
        self.assertEqual(spec.command, ("python", "-c", "pass"))
        self.assertEqual(spec.digest(), digest)

    def test_invocation_and_process_identity_fields_are_strict_text(self):
        cases = [
            {"request_id": 1},
            {"run_id": b"run"},
            {"cwd": Path("C:/repo")},
            {"cwd": "C:\x00repo"},
            {"request_id": "req\nline"},
            {"run_id": "run\ud800"},
            {"environment_sha256": None},
            {"shell": 1},
            {"timeout_seconds": 2**53},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                values = {
                    "request_id": "req", "run_id": "run", "command": ("python",),
                    "cwd": "C:/repo", "environment_sha256": "a" * 64,
                    "timeout_seconds": 30,
                }
                values.update(changes)
                with self.assertRaises(RuntimeErrorCode):
                    InvocationSpec(**values)
        identity_cases = [
            ({"boot_id": 1}, "E_RUNTIME_IDENTITY"),
            ({"worktree_id": b"tree"}, "E_RUNTIME_IDENTITY"),
            ({"worktree_id": "tree\x7f"}, "E_RUNTIME_IDENTITY"),
            ({"executable_sha256": None}, "E_RUNTIME_IDENTITY"),
            ({"pid": 2**53}, "E_RUNTIME_PID"),
        ]
        for changes, code in identity_cases:
            with self.subTest(changes=changes):
                values = {"pid": 10, "boot_id": "boot", "executable_sha256": "b" * 64,
                          "worktree_id": "tree"}
                values.update(changes)
                with self.assertRaisesRegex(RuntimeErrorCode, code):
                    ProcessIdentity(**values)

    def test_observation_inputs_are_rejected_before_attribute_errors(self):
        spec = invocation()
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_OBSERVATION"):
            classify_start(handoff_accepted=True, started_observation="started", expected=spec)
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_OBSERVATION"):
            classify_start(handoff_accepted=1, started_observation=None, expected=spec)
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_OBSERVATION"):
            reconcile(spec, "started")

    def test_malformed_observation_state_is_conservative(self):
        spec = invocation()
        self.assertEqual(classify_start(
            handoff_accepted=True,
            started_observation={"state": [], "run_id": spec.run_id,
                                 "environment_sha256": spec.environment_sha256},
            expected=spec), "START_UNKNOWN")

    def test_lifecycle_rejects_invalid_state_generation_and_identity(self):
        for kwargs, code in (
            ({"state": "BROKEN"}, "E_RUNTIME_TRANSITION"),
            ({"generation": True}, "E_RUNTIME_FENCE"),
            ({"identity": "pid-only"}, "E_RUNTIME_IDENTITY"),
            ({"state": "STARTED"}, "E_RUNTIME_IDENTITY"),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(RuntimeErrorCode, code):
                    Lifecycle(**kwargs)
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_FENCE"):
            Lifecycle().transition("STARTING", expected_generation=True)

    def test_lifecycle_requires_identity_and_fences_transitions(self):
        identity = ProcessIdentity(10, "boot", "b" * 64, "tree")
        state = Lifecycle().transition("STARTING")
        state = state.transition("STARTED", identity=identity)
        state = state.transition("STOPPING")
        self.assertEqual(state.transition("STOPPED").state, "STOPPED")
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_TRANSITION"):
            state.transition("STARTED", identity=identity)

    def test_lifecycle_identity_cannot_be_replaced_after_start(self):
        first = ProcessIdentity(10, "boot", "b" * 64, "tree")
        reused_pid = ProcessIdentity(10, "new-boot", "c" * 64, "tree")
        state = Lifecycle().transition("STARTING").transition("STARTED", identity=first)
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_IDENTITY"):
            state.transition("STOPPING", identity=reused_pid)

    def test_unknown_handoff_never_becomes_started(self):
        spec = invocation()
        self.assertEqual(classify_start(handoff_accepted=True, started_observation=None, expected=spec), "START_UNKNOWN")
        self.assertEqual(classify_start(handoff_accepted=False, started_observation=None, expected=spec), "NOT_STARTED")
        self.assertEqual(reconcile(spec, {"state": "STARTED", "run_id": "other", "environment_sha256": "a" * 64})["status"], "IDENTITY_MISMATCH")

    def test_started_observation_requires_process_identity(self):
        spec = invocation()
        incomplete = {"state": "STARTED", "run_id": spec.run_id,
                      "environment_sha256": spec.environment_sha256}
        self.assertEqual(classify_start(handoff_accepted=True, started_observation=incomplete, expected=spec),
                         "START_UNKNOWN")
        malformed = {**incomplete, "process_identity": {"pid": 10}}
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_IDENTITY"):
            classify_start(handoff_accepted=True, started_observation=malformed, expected=spec)

    def test_rejected_handoff_cannot_adopt_started_observation(self):
        spec = invocation()
        observation = {"state": "STARTED", "run_id": spec.run_id,
                       "environment_sha256": spec.environment_sha256,
                       "process_identity": process_identity()}
        self.assertEqual(classify_start(handoff_accepted=False, started_observation=observation, expected=spec),
                         "IDENTITY_MISMATCH")

    def test_explicit_not_started_observation_allows_replay(self):
        spec = invocation()
        observed = {"state": "NOT_STARTED", "run_id": spec.run_id,
                    "environment_sha256": spec.environment_sha256}
        self.assertEqual(classify_start(handoff_accepted=True, started_observation=observed, expected=spec),
                         "NOT_STARTED")
        self.assertTrue(reconcile(spec, observed)["replay_allowed"])

    def test_not_started_with_process_identity_is_unknown_and_not_replayable(self):
        spec = invocation()
        observed = {"state": "NOT_STARTED", "run_id": spec.run_id,
                    "environment_sha256": spec.environment_sha256,
                    "process_identity": process_identity()}
        self.assertEqual(classify_start(handoff_accepted=True, started_observation=observed, expected=spec),
                         "START_UNKNOWN")
        self.assertFalse(reconcile(spec, observed)["replay_allowed"])

    def test_matching_observation_is_started_and_replay_policy_is_conservative(self):
        spec = invocation()
        observation = {"state": "STARTED", "run_id": spec.run_id, "environment_sha256": spec.environment_sha256,
                       "process_identity": process_identity()}
        self.assertEqual(classify_start(handoff_accepted=True, started_observation=observation, expected=spec), "STARTED")
        self.assertFalse(reconcile(spec, observation)["replay_allowed"])
        self.assertFalse(reconcile(spec, {"state": "FAILED", "run_id": spec.run_id, "environment_sha256": spec.environment_sha256})["replay_allowed"])
        self.assertFalse(reconcile(spec, {"state": "FAILED", "run_id": spec.run_id,
                                          "environment_sha256": spec.environment_sha256,
                                          "cleanup_confirmed": True})["replay_allowed"])
        self.assertTrue(reconcile(spec, {"state": "FAILED", "run_id": spec.run_id,
                                         "environment_sha256": spec.environment_sha256,
                                         "process_identity": process_identity(),
                                         "cleanup_confirmed": True})["replay_allowed"])

    def test_unknown_state_requires_generation_fence(self):
        identity = ProcessIdentity(10, "boot", "b" * 64, "tree")
        state = Lifecycle().transition("STARTING").transition("START_UNKNOWN")
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_FENCE"):
            state.transition("FAILED")
        self.assertEqual(state.transition("FAILED", expected_generation=state.generation).state, "FAILED")

    def test_lifecycle_generation_does_not_overflow_safe_integer(self):
        state = Lifecycle(state="NEW", generation=2**53 - 1)
        with self.assertRaisesRegex(RuntimeErrorCode, "E_RUNTIME_FENCE"):
            state.transition("STARTING", expected_generation=2**53 - 1)


if __name__ == "__main__":
    unittest.main()
