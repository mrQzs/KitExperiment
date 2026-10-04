"""Direct-call tests for the canonical runtime identity/lifecycle contract.

Every test calls the canonical public shapes (`LIFECYCLE`, `ALLOWED_TRANSITIONS`,
`validate_argv`, `InvocationSpec`, `ProcessIdentity`, `Lifecycle`,
`classify_start`, `reconcile`) the way a Code-Kit runtime caller would.
"""
from __future__ import annotations

import unittest

from sentinelmesh.runtime import (
    ALLOWED_TRANSITIONS,
    LIFECYCLE,
    InvocationSpec,
    Lifecycle,
    ProcessIdentity,
    RuntimeErrorCode,
    RuntimeErrorSM,
    classify_start,
    reconcile,
    validate_argv,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def spec(**overrides):
    """Return a legal baseline InvocationSpec with optional field overrides."""
    values = {
        "request_id": "req-1",
        "run_id": "run-1",
        "command": ["python", "-c", "print(1)"],
        "cwd": "/work/tree",
        "environment_sha256": SHA_A,
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return InvocationSpec(**values)


def identity(pid=4242, boot_id="boot-1", executable_sha256=SHA_B, worktree_id="tree-1"):
    return ProcessIdentity(pid, boot_id, executable_sha256, worktree_id)


def observation(identity_value=None, state="NOT_STARTED", cleanup_confirmed=None, **extra):
    """Return an observation mapping bound to the baseline spec."""
    values = {"run_id": "run-1", "environment_sha256": SHA_A, "state": state}
    if identity_value is not None:
        values["process_identity"] = identity_value
    if cleanup_confirmed is not None:
        values["cleanup_confirmed"] = cleanup_confirmed
    values.update(extra)
    return values


def classify(observed, *, handoff=True, value=None):
    """Call the canonical classify_start against the baseline expected spec."""
    return classify_start(
        handoff_accepted=handoff,
        started_observation=observed,
        expected=spec() if value is None else value,
    )


class CodeMixin(unittest.TestCase):
    def code_of(self, callable_obj, *args, **kwargs):
        with self.assertRaises(RuntimeErrorCode) as ctx:
            callable_obj(*args, **kwargs)
        return ctx.exception.code, ctx.exception.message


class LifecycleConstantTests(CodeMixin):
    def test_lifecycle_tuple_and_transition_table_are_canonical(self):
        self.assertEqual(
            LIFECYCLE,
            ("NEW", "STARTING", "STARTED", "STOPPING", "STOPPED", "FAILED", "START_UNKNOWN"),
        )
        self.assertEqual(
            ALLOWED_TRANSITIONS,
            {
                "NEW": {"STARTING", "FAILED"},
                "STARTING": {"STARTED", "START_UNKNOWN", "FAILED"},
                "STARTED": {"STOPPING", "FAILED"},
                "STOPPING": {"STOPPED", "FAILED", "START_UNKNOWN"},
                "STOPPED": set(),
                "FAILED": set(),
                "START_UNKNOWN": {"STARTED", "STOPPING", "FAILED"},
            },
        )

    def test_start_unknown_is_recoverable_and_never_terminal(self):
        self.assertEqual(ALLOWED_TRANSITIONS["START_UNKNOWN"], {"STARTED", "STOPPING", "FAILED"})
        self.assertEqual(ALLOWED_TRANSITIONS["STOPPED"], set())
        self.assertEqual(ALLOWED_TRANSITIONS["FAILED"], set())

    def test_legal_transition_is_accepted_before_each_rejection(self):
        self.assertEqual(Lifecycle().transition("STARTING").state, "STARTING")
        self.assertEqual(Lifecycle().transition("STARTING").generation, 1)


class RuntimeErrorTests(CodeMixin):
    def test_code_error_renders_code_and_message(self):
        error = RuntimeErrorCode("E_ARGV_NUL", "argv[0] must not contain NUL")
        self.assertIsInstance(error, ValueError)
        self.assertEqual(error.code, "E_ARGV_NUL")
        self.assertEqual(error.message, "argv[0] must not contain NUL")
        self.assertEqual(str(error), "E_ARGV_NUL: argv[0] must not contain NUL")

    def test_legacy_error_remains_catchable_with_same_fields(self):
        with self.assertRaises(RuntimeErrorCode) as ctx:
            raise RuntimeErrorSM("E_INVOCATION_ARGV")
        self.assertEqual(ctx.exception.code, "E_INVOCATION_ARGV")
        self.assertIsInstance(ctx.exception, RuntimeErrorSM)
        self.assertIsInstance(ctx.exception, RuntimeErrorCode)
        self.assertEqual(str(ctx.exception), "E_INVOCATION_ARGV")
        two_part = RuntimeErrorSM("E_ARGV_TYPE", "argv must be strings")
        self.assertEqual(two_part.code, "E_ARGV_TYPE")
        self.assertEqual(str(two_part), "E_ARGV_TYPE: argv must be strings")


class ValidateArgvTests(CodeMixin):
    def test_legal_argv_shapes_are_preserved(self):
        self.assertEqual(validate_argv(["python", "-c", "print(1)"]), ("python", "-c", "print(1)"))
        self.assertEqual(validate_argv(("python",)), ("python",))
        # Ordinary arguments may be empty strings or contain newlines.
        self.assertEqual(validate_argv(["python", ""]), ("python", ""))
        self.assertEqual(validate_argv(["python", "\n", "  "]), ("python", "\n", "  "))
        self.assertEqual(validate_argv(["Python", "--FLAG"]), ("Python", "--FLAG"))

    def test_shell_carrier_rejected(self):
        self.assertEqual(validate_argv(["python"]), ("python",))
        self.assertEqual(self.code_of(validate_argv, ["python"], shell=True)[0], "E_RUNTIME_SHELL_ARGV")
        self.assertEqual(self.code_of(validate_argv, ["python"], shell=1)[0], "E_RUNTIME_SHELL_ARGV")

    def test_non_sequence_argv_rejected(self):
        self.assertEqual(validate_argv(["python"]), ("python",))
        for bad in ("python", {"python": 1}, None, b"python", 7, iter(["python"])):
            with self.subTest(bad=bad):
                self.assertEqual(self.code_of(validate_argv, bad)[0], "E_RUNTIME_ARGV")

    def test_empty_argv_rejected(self):
        self.assertEqual(validate_argv(["python"]), ("python",))
        self.assertEqual(self.code_of(validate_argv, [])[0], "E_RUNTIME_ARGV")
        self.assertEqual(self.code_of(validate_argv, ())[0], "E_RUNTIME_ARGV")

    def test_non_string_item_rejected(self):
        self.assertEqual(validate_argv(["python", "arg"]), ("python", "arg"))
        for bad in (["python", 1], ["python", None], ["python", b"x"], ["python", ["x"]]):
            with self.subTest(bad=bad):
                self.assertEqual(self.code_of(validate_argv, bad)[0], "E_RUNTIME_ARGV")

    def test_nul_rejected_in_every_slot(self):
        self.assertEqual(validate_argv(["python", "arg"]), ("python", "arg"))
        self.assertEqual(self.code_of(validate_argv, ["pyt\x00hon", "arg"])[0], "E_RUNTIME_ARGV")
        self.assertEqual(self.code_of(validate_argv, ["python", "ar\x00g"])[0], "E_RUNTIME_ARGV")

    def test_surrogate_rejected_in_every_slot(self):
        self.assertEqual(validate_argv(["python", "arg"]), ("python", "arg"))
        for bad in (["pyt\ud800hon", "arg"], ["python", "ar\udfffg"]):
            with self.subTest(bad=bad):
                self.assertEqual(self.code_of(validate_argv, bad)[0], "E_RUNTIME_ARGV")

    def test_empty_executable_rejected(self):
        self.assertEqual(validate_argv(["python", "arg"]), ("python", "arg"))
        self.assertEqual(self.code_of(validate_argv, ["", "arg"])[0], "E_RUNTIME_ARGV")
        self.assertEqual(self.code_of(validate_argv, [""])[0], "E_RUNTIME_ARGV")

    def test_carriage_return_or_line_feed_in_executable_rejected(self):
        self.assertEqual(validate_argv(["python", "arg"]), ("python", "arg"))
        self.assertEqual(self.code_of(validate_argv, ["py\rthon", "arg"])[0], "E_RUNTIME_ARGV")
        self.assertEqual(self.code_of(validate_argv, ["py\nthon", "arg"])[0], "E_RUNTIME_ARGV")
        self.assertEqual(self.code_of(validate_argv, ["\r", "arg"])[0], "E_RUNTIME_ARGV")

    def test_control_and_unicode_text_in_arguments_stays_legal(self):
        self.assertEqual(validate_argv(["python", "\t--flag"])[1], "\t--flag")
        self.assertEqual(validate_argv(["python", "ключ"])[1], "ключ")


class InvocationSpecTests(CodeMixin):
    def test_construction_validates_immediately_and_normalises_command(self):
        value = spec()
        self.assertEqual(value.command, ("python", "-c", "print(1)"))
        self.assertEqual(value.shell, False)
        self.assertEqual(value.validate(), value)

    def test_digest_is_canonical_and_covers_every_field(self):
        base = spec().digest()
        self.assertEqual(len(base), 64)
        self.assertEqual(base, spec().digest())
        fields = {
            "request_id": "req-2",
            "run_id": "run-2",
            "command": ["python", "-c", "print(2)"],
            "cwd": "/work/other",
            "environment_sha256": SHA_B,
            "timeout_seconds": 31,
        }
        for field_name, changed in fields.items():
            with self.subTest(field=field_name):
                self.assertNotEqual(spec(**{field_name: changed}).digest(), base)

    def test_spec_is_frozen(self):
        value = spec()
        with self.assertRaises(Exception):
            value.cwd = "/other"  # type: ignore[misc]

    def test_bad_request_id_rejected(self):
        self.assertEqual(spec(request_id="req-2").request_id, "req-2")
        for bad in ("", "  \x00", "req\x01", "req\ud800", 7, None):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.code_of(InvocationSpec, bad, "run-1", ["python"], "/w", SHA_A, 30)[0],
                    "E_RUNTIME_INVOCATION",
                )
        # Uppercase and punctuation opaque ids stay legal.
        self.assertEqual(spec(request_id="REQ-1:x_2").request_id, "REQ-1:x_2")

    def test_bad_run_id_rejected(self):
        self.assertEqual(spec(run_id="run-2").run_id, "run-2")
        for bad in ("", "run\x00", "run\ud800", (), None):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.code_of(InvocationSpec, "req-1", bad, ["python"], "/w", SHA_A, 30)[0],
                    "E_RUNTIME_INVOCATION",
                )

    def test_bad_cwd_rejected(self):
        self.assertEqual(spec(cwd="/other/tree").cwd, "/other/tree")
        for bad in ("", "/tmp\x00", "/tmp\n", "л\x7ftr", "/tmp\ud800", 5, None):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.code_of(InvocationSpec, "req-1", "run-1", ["python"], bad, SHA_A, 30)[0],
                    "E_RUNTIME_INVOCATION",
                )

    def test_bad_environment_sha256_rejected(self):
        self.assertEqual(spec(environment_sha256=SHA_B).environment_sha256, SHA_B)
        for bad in (SHA_B.upper(), SHA_B[:-1], SHA_B + "c", "z" * 64, 7, None, b"a" * 64):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.code_of(InvocationSpec, "req-1", "run-1", ["python"], "/w", bad, 30)[0],
                    "E_RUNTIME_INVOCATION",
                )

    def test_bad_timeout_rejected(self):
        self.assertEqual(spec(timeout_seconds=1).timeout_seconds, 1)
        for bad in (0, -1, True, 1.0, "30", None):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.code_of(InvocationSpec, "req-1", "run-1", ["python"], "/w", SHA_A, bad)[0],
                    "E_RUNTIME_TIMEOUT",
                )

    def test_shell_carrier_and_command_errors_rejected(self):
        self.assertEqual(spec().shell, False)
        # A shell carrier is rejected by the argv guard once the text fields pass.
        self.assertEqual(self.code_of(spec, shell=True)[0], "E_RUNTIME_SHELL_ARGV")
        # A non-boolean shell flag is a spec error, not an argv error.
        self.assertEqual(self.code_of(spec, shell=1)[0], "E_RUNTIME_INVOCATION")
        self.assertEqual(self.code_of(spec, command="python")[0], "E_RUNTIME_ARGV")
        self.assertEqual(self.code_of(spec, command=[])[0], "E_RUNTIME_ARGV")
        self.assertEqual(self.code_of(spec, command=["python", "a\x00"])[0], "E_RUNTIME_ARGV")


class ProcessIdentityTests(CodeMixin):
    def test_four_field_identity_is_constructed_and_validated(self):
        value = identity()
        self.assertEqual((value.pid, value.boot_id, value.executable_sha256, value.worktree_id),
                         (4242, "boot-1", SHA_B, "tree-1"))
        self.assertEqual(value.to_dict()["pid"], 4242)
        with self.assertRaises(TypeError):
            ProcessIdentity(4242, "boot-1", SHA_B)  # missing worktree_id
        with self.assertRaises(TypeError):
            ProcessIdentity(4242, "boot-1", SHA_B, "tree-1", "extra")  # no extra field

    def test_identity_is_frozen(self):
        value = identity()
        with self.assertRaises(Exception):
            value.pid = 1  # type: ignore[misc]

    def test_bad_pid_rejected(self):
        self.assertEqual(identity(pid=1).pid, 1)
        for bad in (0, -1, True, 4242.0, "4242", None):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.code_of(ProcessIdentity, bad, "boot-1", SHA_B, "tree-1")[0],
                    "E_RUNTIME_PID",
                )

    def test_bad_opaque_fields_rejected(self):
        self.assertEqual(identity(boot_id="BOOT:2", worktree_id="tree.2").boot_id, "BOOT:2")
        for bad in ("", "boot\x00", "boot\ud800", "boot\n", 1, None):
            with self.subTest(bad=bad, field="boot_id"):
                self.assertEqual(
                    self.code_of(ProcessIdentity, 4242, bad, SHA_B, "tree-1")[0], "E_RUNTIME_IDENTITY"
                )
            with self.subTest(bad=bad, field="worktree_id"):
                self.assertEqual(
                    self.code_of(ProcessIdentity, 4242, "boot-1", SHA_B, bad)[0], "E_RUNTIME_IDENTITY"
                )

    def test_bad_executable_digest_rejected(self):
        self.assertEqual(identity(executable_sha256=SHA_A).executable_sha256, SHA_A)
        for bad in (SHA_B.upper(), SHA_B[:-1], "z" * 64, 1, None, b"b" * 64):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.code_of(ProcessIdentity, 4242, "boot-1", bad, "tree-1")[0],
                    "E_RUNTIME_IDENTITY",
                )


class LifecycleTests(CodeMixin):
    def test_defaults_and_happy_path_increment_generation(self):
        initial = Lifecycle()
        self.assertEqual((initial.state, initial.generation, initial.identity), ("NEW", 0, None))
        starting = initial.transition("STARTING")
        started = starting.transition("STARTED", identity=identity())
        stopping = started.transition("STOPPING")
        stopped = stopping.transition("STOPPED")
        self.assertEqual((starting.state, starting.generation), ("STARTING", 1))
        self.assertEqual((started.state, started.generation), ("STARTED", 2))
        self.assertEqual((stopping.state, stopping.generation), ("STOPPING", 3))
        self.assertEqual((stopped.state, stopped.generation), ("STOPPED", 4))
        self.assertEqual(initial.state, "NEW")  # transition returns a new record

    def test_terminal_states_have_no_transition(self):
        for terminal in ("STOPPED", "FAILED"):
            for target in LIFECYCLE:
                with self.subTest(terminal=terminal, target=target):
                    self.assertEqual(
                        self.code_of(Lifecycle(terminal, 4).transition, target)[0],
                        "E_RUNTIME_TRANSITION",
                    )

    def test_disallowed_transition_rejected(self):
        legal = Lifecycle().transition("STARTING")
        started = legal.transition("STARTED", identity=identity())
        stages = {
            "NEW": Lifecycle(),
            "STARTING": legal,
            "STARTED": started,
            "STOPPING": started.transition("STOPPING"),
        }
        for source, target in (
            ("NEW", "STARTED"),
            ("NEW", "STOPPING"),
            ("NEW", "STOPPED"),
            ("NEW", "NEW"),
            ("STARTING", "NEW"),
            ("STARTING", "STARTING"),
            ("STARTED", "STARTED"),
            ("STARTED", "NEW"),
            ("STOPPING", "STARTED"),
            ("STOPPING", "NEW"),
        ):
            with self.subTest(source=source, target=target):
                self.assertEqual(
                    self.code_of(stages[source].transition, target)[0], "E_RUNTIME_TRANSITION"
                )

    def test_start_unknown_recovery_requires_explicit_generation_fence(self):
        unknown = Lifecycle().transition("STARTING").transition("START_UNKNOWN")
        self.assertEqual((unknown.state, unknown.generation), ("START_UNKNOWN", 2))
        self.assertEqual(
            self.code_of(unknown.transition, "STARTED", identity=identity())[0],
            "E_RUNTIME_FENCE",
        )
        recovered = unknown.transition("STARTED", identity=identity(), expected_generation=2)
        self.assertEqual((recovered.state, recovered.generation), ("STARTED", 3))
        self.assertEqual(recovered.identity.pid, 4242)
        self.assertEqual(unknown.transition("STOPPING", expected_generation=2).state, "STOPPING")
        self.assertEqual(unknown.transition("FAILED", expected_generation=2).state, "FAILED")

    def test_start_unknown_entry_does_not_require_fence(self):
        self.assertEqual(
            Lifecycle().transition("STARTING").transition("START_UNKNOWN").state, "START_UNKNOWN"
        )
        self.assertEqual(
            Lifecycle("STARTED", 2, identity()).transition("STOPPING").transition("START_UNKNOWN").state,
            "START_UNKNOWN",
        )

    def test_stale_generation_fence_rejected(self):
        stage = Lifecycle().transition("STARTING")
        self.assertEqual(
            stage.transition("STARTED", identity=identity(), expected_generation=1).generation, 2
        )
        # The same fenced move without a process identity is rejected: STARTED is
        # only reachable with direct start evidence.
        self.assertEqual(
            self.code_of(stage.transition, "STARTED", expected_generation=1)[0], "E_RUNTIME_IDENTITY"
        )
        for bad in (0, 2, 5, -1):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.code_of(
                        stage.transition, "STARTED", identity=identity(), expected_generation=bad
                    )[0],
                    "E_RUNTIME_FENCE",
                )
        self.assertEqual(
            self.code_of(
                stage.transition, "STARTED", identity=identity(), expected_generation="1"
            )[0],
            "E_RUNTIME_FENCE",
        )

    def test_identity_binding_is_immutable(self):
        bound = identity(pid=1)
        other = identity(pid=2)
        started = Lifecycle().transition("STARTING").transition("STARTED", identity=bound)
        stopping = started.transition("STOPPING")
        self.assertEqual((stopping.identity.pid, stopping.generation), (1, 3))
        for stage, target in ((started, "STOPPING"), (started, "FAILED"), (stopping, "FAILED")):
            with self.subTest(stage=stage.state, target=target):
                self.assertEqual(
                    self.code_of(stage.transition, target, identity=other)[0],
                    "E_RUNTIME_IDENTITY",
                )
        self.assertEqual(started.transition("STOPPING", identity=bound).identity.pid, 1)
        self.assertEqual(started.transition("FAILED").state, "FAILED")

    def test_identity_cannot_be_bound_after_start_unknown_without_fence(self):
        unknown = Lifecycle().transition("STARTING").transition("START_UNKNOWN")
        self.assertEqual(
            self.code_of(unknown.transition, "STARTED", identity=identity(pid=1))[0],
            "E_RUNTIME_FENCE",
        )
        recovered = unknown.transition(
            "STARTED", identity=identity(pid=1), expected_generation=2
        )
        self.assertEqual((recovered.state, recovered.generation, recovered.identity.pid), ("STARTED", 3, 1))

    def test_record_construction_is_validated(self):
        self.assertEqual(Lifecycle("STARTED", 3, identity()).state, "STARTED")
        self.assertEqual(self.code_of(Lifecycle, "STARTED", 3)[0], "E_RUNTIME_IDENTITY")
        self.assertEqual(self.code_of(Lifecycle, "RUNNING", 0)[0], "E_RUNTIME_TRANSITION")
        self.assertEqual(self.code_of(Lifecycle, "NEW", -1)[0], "E_RUNTIME_FENCE")
        self.assertEqual(self.code_of(Lifecycle, "NEW", True)[0], "E_RUNTIME_FENCE")
        self.assertEqual(self.code_of(Lifecycle, "NEW", 0, "identity")[0], "E_RUNTIME_IDENTITY")
        self.assertEqual(self.code_of(Lifecycle().transition, "RUNNING")[0], "E_RUNTIME_TRANSITION")


class ClassifyStartTests(CodeMixin):
    def test_no_observation_depends_only_on_handoff(self):
        self.assertEqual(
            classify_start(handoff_accepted=False, started_observation=None, expected=spec()),
            "NOT_STARTED",
        )
        self.assertEqual(
            classify_start(handoff_accepted=True, started_observation=None, expected=spec()),
            "START_UNKNOWN",
        )
        # An empty observation is not a missing one: its run/environment keys are
        # absent, so the record mismatches instead of following the handoff flag.
        self.assertEqual(
            classify_start(handoff_accepted=True, started_observation={}, expected=spec()),
            "IDENTITY_MISMATCH",
        )

    def test_mismatched_observation_run_or_environment_is_identity_mismatch(self):
        legal = observation(state="STARTED", identity_value=identity())
        self.assertEqual(
            classify_start(handoff_accepted=True, started_observation=legal, expected=spec()),
            "STARTED",
        )
        self.assertEqual(
            classify_start(
                handoff_accepted=True,
                started_observation=observation(state="STARTED", run_id="run-2"),
                expected=spec(),
            ),
            "IDENTITY_MISMATCH",
        )
        self.assertEqual(
            classify_start(
                handoff_accepted=True,
                started_observation=observation(state="STARTED", environment_sha256=SHA_B),
                expected=spec(),
            ),
            "IDENTITY_MISMATCH",
        )
        self.assertEqual(
            classify_start(
                handoff_accepted=True, started_observation={"state": "STARTED"}, expected=spec()
            ),
            "IDENTITY_MISMATCH",
        )

    def test_not_started_observation_stays_not_started(self):
        self.assertEqual(
            classify_start(handoff_accepted=True, started_observation=observation(), expected=spec()),
            "NOT_STARTED",
        )
        self.assertEqual(
            classify_start(
                handoff_accepted=False,
                started_observation=observation(identity_value=identity()),
                expected=spec(),
            ),
            "START_UNKNOWN",
        )

    def test_started_observation_requires_bound_process_identity(self):
        self.assertEqual(classify(observation(state="STARTED", identity_value=identity())), "STARTED")
        self.assertEqual(
            classify(observation(state="STARTED", identity_value=identity().to_dict())), "STARTED"
        )
        self.assertEqual(classify(observation(state="STARTED")), "START_UNKNOWN")
        # The observation identity is a closed four-field object; extra keys are
        # rejected outright instead of being silently ignored or re-derived.
        for extra in (
            {"run_id": "run-1", "environment_sha256": SHA_A},
            {"run_id": "run-9"},
            {"environment_sha256": SHA_B},
            {"note": "x"},
        ):
            with self.subTest(extra=extra):
                self.assertEqual(
                    self.code_of(
                        classify,
                        observation(state="STARTED", identity_value=dict(identity().to_dict(), **extra)),
                    )[0],
                    "E_RUNTIME_IDENTITY",
                )
        # A different but complete identity is still direct start evidence.
        self.assertEqual(
            classify(observation(state="STARTED", identity_value=dict(identity().to_dict(), pid=4243))),
            "STARTED",
        )

    def test_failed_and_stopped_pass_through(self):
        for state in ("FAILED", "STOPPED"):
            with self.subTest(state=state):
                self.assertEqual(classify(observation(state=state)), state)
                self.assertEqual(classify(observation(state=state, identity_value=identity())), state)

    def test_unknown_or_partial_state_fails_closed(self):
        for state in ("STARTING", "STOPPING", "RUNNING", "UNKNOWN", "", None):
            with self.subTest(state=state):
                self.assertEqual(classify(observation(state=state)), "START_UNKNOWN")

    def test_model_text_and_exit_codes_are_never_consulted(self):
        prose = {
            "summary": "the CLI said it started",
            "stdout": "ready",
            "stderr": "",
            "exit_code": 0,
            "started": True,
        }
        self.assertEqual(classify(observation(state="NOT_STARTED", **prose)), "NOT_STARTED")
        self.assertEqual(classify(observation(state="STARTED", **prose)), "START_UNKNOWN")
        self.assertEqual(classify(observation(state="FAILED", exit_code=3)), "FAILED")
        self.assertEqual(classify(observation(state="STOPPED", exit_code=137)), "STOPPED")
        # A rejected handoff can never adopt a terminal observation, whatever the
        # recorded exit code or prose says.
        self.assertEqual(
            classify(observation(state="FAILED", exit_code=3), handoff=False),
            "IDENTITY_MISMATCH",
        )

    def test_signature_and_input_types(self):
        with self.assertRaises(TypeError):
            classify_start(True, None, spec())  # type: ignore[misc]
        self.assertEqual(
            self.code_of(
                classify_start, handoff_accepted=1, started_observation=None, expected=spec()
            )[0],
            "E_RUNTIME_OBSERVATION",
        )
        self.assertEqual(
            self.code_of(
                classify_start,
                handoff_accepted=True,
                started_observation=["state"],
                expected=spec(),
            )[0],
            "E_RUNTIME_OBSERVATION",
        )
        self.assertEqual(
            self.code_of(
                classify_start,
                handoff_accepted=True,
                started_observation=None,
                expected={"run_id": "run-1"},
            )[0],
            "E_RUNTIME_INVOCATION",
        )


class ReconcileTests(CodeMixin):
    def test_record_has_exactly_the_five_canonical_keys(self):
        record = reconcile(spec(), observation())
        self.assertEqual(
            set(record),
            {"status", "run_id", "expected_environment_sha256", "observed_environment_sha256", "replay_allowed"},
        )
        self.assertEqual(record["status"], "NOT_STARTED")
        self.assertEqual(record["run_id"], "run-1")
        self.assertEqual(record["expected_environment_sha256"], SHA_A)
        self.assertEqual(record["observed_environment_sha256"], SHA_A)
        self.assertIs(record["replay_allowed"], True)

    def test_replay_allowed_only_for_clear_not_started(self):
        for record in (
            reconcile(spec(), observation()),
            reconcile(spec(), observation(handoff_accepted=False)),
        ):
            with self.subTest(record=record):
                self.assertIs(record["replay_allowed"], True)
        for blocked in (
            {"run_id": "run-1", "environment_sha256": SHA_A, "handoff_accepted": True},
            {"run_id": "run-1", "environment_sha256": SHA_A, "state": "STARTED", "process_identity": identity()},
            {"run_id": "run-9", "environment_sha256": SHA_A, "state": "NOT_STARTED"},
            {"run_id": "run-1", "environment_sha256": SHA_B, "state": "NOT_STARTED"},
        ):
            with self.subTest(blocked=blocked):
                self.assertIs(reconcile(spec(), blocked)["replay_allowed"], False)
        # An empty observation carries contradictory run/environment evidence and
        # a missing observation is rejected outright; neither is replayable.
        self.assertEqual(
            (reconcile(spec(), {})["status"], reconcile(spec(), {})["replay_allowed"]),
            ("IDENTITY_MISMATCH", False),
        )
        self.assertEqual(self.code_of(reconcile, spec(), None)[0], "E_RUNTIME_OBSERVATION")
        # A present-but-null identity is not "no identity": it is rejected, never replayable.
        self.assertEqual(
            self.code_of(reconcile, spec(), {**observation(), "process_identity": None})[0],
            "E_RUNTIME_IDENTITY",
        )

    def test_terminal_replay_requires_bound_identity_and_confirmed_cleanup(self):
        terminal = observation(state="FAILED", identity_value=identity(), cleanup_confirmed=True)
        self.assertEqual(reconcile(spec(), terminal)["status"], "FAILED")
        self.assertIs(reconcile(spec(), terminal)["replay_allowed"], True)
        # Cleanup that is not explicitly confirmed never re-opens a replay.
        for change in (
            {"cleanup_confirmed": False},
            {"cleanup_confirmed": 1},
            {"cleanup_confirmed": "yes"},
            {"cleanup_confirmed": None},
        ):
            with self.subTest(change=change):
                self.assertIs(reconcile(spec(), dict(terminal, **change))["replay_allowed"], False)
        # A present identity is validated against the closed four-field shape.
        for malformed in (None, {"run_id": "run-9"}, {"pid": 4242}):
            with self.subTest(malformed=malformed):
                self.assertEqual(
                    self.code_of(
                        reconcile,
                        spec(),
                        dict(terminal, process_identity=malformed),
                    )[0],
                    "E_RUNTIME_IDENTITY",
                )
        stopped = observation(state="STOPPED", identity_value=identity(pid=7), cleanup_confirmed=True)
        self.assertEqual(reconcile(spec(), stopped)["status"], "STOPPED")
        self.assertIs(reconcile(spec(), stopped)["replay_allowed"], True)

    def test_partial_cleanup_or_missing_identity_blocks_replay(self):
        for state in ("FAILED", "STOPPED"):
            with self.subTest(state=state):
                no_identity = observation(state=state, cleanup_confirmed=True)
                self.assertIs(reconcile(spec(), no_identity)["replay_allowed"], False)
                unconfirmed = observation(state=state, identity_value=identity())
                self.assertIs(reconcile(spec(), unconfirmed)["replay_allowed"], False)

    def test_status_is_taken_from_classify_start(self):
        cases = (
            ({}, "IDENTITY_MISMATCH"),
            ({"handoff_accepted": True}, "IDENTITY_MISMATCH"),
            (observation(), "NOT_STARTED"),
            (observation(handoff_accepted=False), "NOT_STARTED"),
            (observation(state="NOT_STARTED", identity_value=identity()), "START_UNKNOWN"),
            (observation(state="STOPPING"), "START_UNKNOWN"),
            (observation(state="STARTED", identity_value=identity()), "STARTED"),
            (observation(state="FAILED"), "FAILED"),
            (observation(state="STOPPED"), "STOPPED"),
        )
        for observed, expected_status in cases:
            with self.subTest(observed=observed):
                self.assertEqual(reconcile(spec(), observed)["status"], expected_status)
        # A missing observation is not a record; reconcile rejects it outright.
        self.assertEqual(self.code_of(reconcile, spec(), None)[0], "E_RUNTIME_OBSERVATION")

    def test_empty_observation_record_has_no_start_evidence(self):
        # An empty observation is not a missing one: its run/environment keys are
        # absent, so the record is an identity mismatch that never allows replay.
        empty = reconcile(spec(), {})
        self.assertEqual(empty["status"], "IDENTITY_MISMATCH")
        self.assertEqual(empty["observed_environment_sha256"], None)
        self.assertIs(empty["replay_allowed"], False)

    def test_observed_environment_and_run_fields(self):
        self.assertEqual(reconcile(spec(), {})["observed_environment_sha256"], None)
        self.assertEqual(
            reconcile(spec(), {"run_id": "run-1", "environment_sha256": SHA_B})["observed_environment_sha256"],
            SHA_B,
        )
        self.assertEqual(reconcile(spec(), {"run_id": "run-1", "environment_sha256": 12})["observed_environment_sha256"], 12)
        mismatch = reconcile(spec(), {"run_id": "run-9", "environment_sha256": SHA_B})
        self.assertEqual(mismatch["status"], "IDENTITY_MISMATCH")
        self.assertEqual(mismatch["run_id"], "run-1")
        self.assertEqual(mismatch["expected_environment_sha256"], SHA_A)
        self.assertEqual(mismatch["observed_environment_sha256"], SHA_B)
        self.assertIs(mismatch["replay_allowed"], False)

    def test_input_types(self):
        record = reconcile(spec(), {})
        self.assertEqual(
            set(record),
            {"status", "run_id", "expected_environment_sha256", "observed_environment_sha256", "replay_allowed"},
        )
        # A missing observation is rejected, and so is a non-mapping one.
        self.assertEqual(self.code_of(reconcile, spec(), None)[0], "E_RUNTIME_OBSERVATION")
        self.assertEqual(self.code_of(reconcile, spec(), ["state"])[0], "E_RUNTIME_OBSERVATION")
        # The expected value must already be an InvocationSpec; reconcile does not
        # coerce or accept a mapping-shaped stand-in.
        self.assertEqual(
            self.code_of(reconcile, {"run_id": "run-1"}, observation())[0], "E_RUNTIME_INVOCATION"
        )
        # A recorded ``handoff_accepted`` flag is data about the handoff, never the
        # handoff itself: reconcile never reads it, coerced text included.
        for value in ("true", "yes", None, 0, 1, False):
            with self.subTest(value=value):
                observed = observation(state="STARTED", identity_value=identity(), handoff_accepted=value)
                self.assertEqual(reconcile(spec(), observed)["status"], "STARTED")


class LegacyApiRegressionTests(unittest.TestCase):
    def test_legacy_runtime_api_is_preserved(self):
        from pathlib import Path

        from sentinelmesh.config import Profile
        from sentinelmesh.runtime import Invocation, ProcessResult, StaticAdapter, SubprocessRunner

        cwd = str(Path.cwd())
        profile = Profile("p", "pi", "m", "executor", timeout_seconds=1)
        invocation = StaticAdapter(executable="python").build_invocation(
            profile, {"cwd": cwd, "args": ["-c", "print(1)"]}
        )
        invocation.validate()
        result = SubprocessRunner().run(invocation)
        self.assertIsInstance(result, ProcessResult)
        self.assertTrue(result.started)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), b"1")
        with self.assertRaises(RuntimeErrorSM):
            Invocation(("python",), cwd, {}, float("inf")).validate()
        with self.assertRaises(RuntimeErrorCode):
            Invocation((), cwd, {}, 1).validate()


if __name__ == "__main__":
    unittest.main()
