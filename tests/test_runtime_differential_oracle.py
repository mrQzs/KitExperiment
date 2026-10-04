"""Differential oracle: the canonical runtime layer must behave exactly like Code-Kit.

Planner-provided acceptance oracle for pi-runtime-contract-port (v2); executors
must not modify this file.  It loads the frozen reference
.agent/design/runtime-reference/runtime.py.txt (checked against manifest.json)
and compares sentinelmesh.runtime with it over a deterministic grid of inputs.
For every call both modules must return the same value, or raise a
RuntimeErrorCode with the same ``code`` (other exceptions: the same type).
"""
from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import sys
import unittest
from pathlib import Path

import sentinelmesh.runtime as candidate

REFERENCE_DIR = Path(__file__).resolve().parents[1] / ".agent" / "design" / "runtime-reference"


def load_reference():
    manifest = json.loads((REFERENCE_DIR / "manifest.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["path"] == "runtime.py.txt")
    source = (REFERENCE_DIR / "runtime.py.txt").read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(source).hexdigest() != entry["sha256"]:
        raise AssertionError("frozen runtime reference does not match manifest.json")
    spec = importlib.util.spec_from_loader("sentinelmesh_runtime_reference", loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    exec(compile(source.decode("utf-8"), str(REFERENCE_DIR / "runtime.py.txt"), "exec"), module.__dict__)
    return module


reference = load_reference()
SHA_A, SHA_B = "a" * 64, "b" * 64
MAX_SAFE = 2**53 - 1


def normalize(value):
    """Map module-specific values to comparable plain data."""
    if isinstance(value, (reference.ProcessIdentity, candidate.ProcessIdentity)):
        return ("ProcessIdentity", value.pid, value.boot_id, value.executable_sha256, value.worktree_id)
    if isinstance(value, (reference.InvocationSpec, candidate.InvocationSpec)):
        return ("InvocationSpec", value.request_id, value.run_id, value.command, value.cwd,
                value.environment_sha256, value.timeout_seconds, value.shell, value.digest())
    if isinstance(value, (reference.Lifecycle, candidate.Lifecycle)):
        return ("Lifecycle", value.state, value.generation, normalize(value.identity))
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(normalize(item) for item in value)
    return value


def outcome(module, call):
    try:
        return ("ok", normalize(call(module)))
    except Exception as exc:  # noqa: BLE001 - the exception is the observable outcome
        if isinstance(exc, module.RuntimeErrorCode):
            return ("RuntimeErrorCode", exc.code)
        return ("raise", type(exc).__name__)


def identity_of(module, pid=10):
    return module.ProcessIdentity(pid, "boot", SHA_B, "tree")


def spec_of(module, **changes):
    values = {"request_id": "req", "run_id": "run", "command": ("python", "-c", "pass"), "cwd": "/repo",
              "environment_sha256": SHA_A, "timeout_seconds": 30}
    values.update(changes)
    return module.InvocationSpec(**values)


IDENTITY_FIELDS = {"pid": 10, "boot_id": "boot", "executable_sha256": SHA_B, "worktree_id": "tree"}
TEXT_VARIANTS = ["x", "", "a\x00b", "a\nb", "a\x7fb", "a\x85b", "a\ud800b", 1, b"x", None]
IDENTITY_VARIANTS = {
    "absent": None, "none": "<None>", "empty": {}, "valid": dict(IDENTITY_FIELDS), "object": "<object>",
    "partial": {"pid": 10}, "extra": {**IDENTITY_FIELDS, "run_id": "run"}, "bad_pid": {**IDENTITY_FIELDS, "pid": 0},
    "bad_sha": {**IDENTITY_FIELDS, "executable_sha256": "B" * 64}, "text": "10", "number": 10,
}
STATES = ["NOT_STARTED", "STARTED", "FAILED", "STOPPED", "STOPPING", "START_UNKNOWN", "BROKEN", [], None, 3]


def observation_variants():
    yield "none", None
    yield "empty", {}
    yield "text", "started"
    for (run_id, env), state, (ident_name, ident), cleanup in itertools.product(
            [("run", SHA_A), ("other", SHA_A), ("run", SHA_B), (None, None)], STATES,
            IDENTITY_VARIANTS.items(), ["absent", True, False, 1]):
        def build(module, run_id=run_id, env=env, state=state, ident=ident, cleanup=cleanup):
            observed = {}
            if run_id is not None:
                observed.update(run_id=run_id, environment_sha256=env)
            if state is not None:
                observed["state"] = state
            if ident == "<None>":
                observed["process_identity"] = None
            elif ident == "<object>":
                observed["process_identity"] = identity_of(module)
            elif ident is not None:
                observed["process_identity"] = dict(ident) if isinstance(ident, dict) else ident
            if cleanup != "absent":
                observed["cleanup_confirmed"] = cleanup
            return observed
        yield "run=%s env=%s state=%r identity=%s cleanup=%s" % (run_id, (env or "-")[:1], state, ident_name,
                                                                 cleanup), build


def materialize(module, value):
    return value(module) if callable(value) else value


class DifferentialOracleTests(unittest.TestCase):
    def assertSameOutcomes(self, cases):
        differences = []
        for label, call in cases:
            expected, actual = outcome(reference, call), outcome(candidate, call)
            if expected != actual:
                differences.append("%s: reference=%r candidate=%r" % (label, expected, actual))
        self.assertEqual(differences[:15], [], "%d differing inputs" % len(differences))

    def test_lifecycle_constants(self):
        self.assertEqual(candidate.LIFECYCLE, reference.LIFECYCLE)
        self.assertEqual(candidate.ALLOWED_TRANSITIONS, reference.ALLOWED_TRANSITIONS)

    def test_validate_argv(self):
        commands = [("python",), ["python", ""], ("python", "a\nb"), ("",), ("py\nthon",), ("py\rthon",),
                    ("python", "\ud800"), ("python", "\x00"), ("cmd\x00",), (), [], "python", None, ("python", 1),
                    (1,), ("python", b"x"), ("中文", "\t")]
        shells = [False, True, 0, 1, None]
        self.assertSameOutcomes(
            ("validate_argv(%r, shell=%r)" % (command, shell),
             lambda m, command=command, shell=shell: m.validate_argv(command, shell=shell))
            for command, shell in itertools.product(commands, shells))

    def test_invocation_spec_fields_and_digest(self):
        changes = [{}]
        for field in ("request_id", "run_id", "cwd"):
            changes += [{field: value} for value in TEXT_VARIANTS]
        changes += [{"command": value} for value in (["python", "x"], ("",), ("py\nthon",), ("python", "\x00"), ())]
        changes += [{"environment_sha256": value} for value in (SHA_B, "A" * 64, "a" * 63, None, 1)]
        changes += [{"timeout_seconds": value} for value in (1, 0, -1, MAX_SAFE, MAX_SAFE + 1, True, 1.0, "30")]
        changes += [{"shell": value} for value in (True, 1, None)]
        changes += [{"request_id": "", "timeout_seconds": 0}, {"shell": True, "command": ()},
                    {"cwd": None, "environment_sha256": None}, {"command": ("python", "x" * 1_200_000)}]
        self.assertSameOutcomes(("InvocationSpec(%r)" % (change,), lambda m, change=change: spec_of(m, **change))
                                for change in changes)

    def test_process_identity_fields(self):
        changes = [{}] + [{"pid": value} for value in (1, 0, -1, MAX_SAFE, MAX_SAFE + 1, True, 1.0, "10")]
        for field in ("boot_id", "worktree_id"):
            changes += [{field: value} for value in TEXT_VARIANTS]
        changes += [{"executable_sha256": value} for value in (SHA_A, "B" * 64, "b" * 63, None)]
        self.assertSameOutcomes(
            ("ProcessIdentity(%r)" % (change,),
             lambda m, change=change: m.ProcessIdentity(**{**IDENTITY_FIELDS, **change}))
            for change in changes)

    def test_lifecycle_construction(self):
        cases = []
        for state, generation, ident in itertools.product(
                list(reference.LIFECYCLE) + ["BROKEN", None, 1], [0, 5, -1, MAX_SAFE, MAX_SAFE + 1, True, 1.0],
                ["none", "object", "text"]):
            def call(m, state=state, generation=generation, ident=ident):
                identity = {"none": None, "object": identity_of(m), "text": "pid-only"}[ident]
                return m.Lifecycle(state, generation, identity)
            cases.append(("Lifecycle(%r, %r, %s)" % (state, generation, ident), call))
        self.assertSameOutcomes(cases)

    def test_lifecycle_transitions(self):
        cases = []
        for start, target, ident, fence, bound in itertools.product(
                reference.LIFECYCLE, list(reference.LIFECYCLE) + ["BROKEN"], ["none", "same", "other", "text"],
                ["none", "match", "stale", "bool", "negative"], [False, True]):
            if start == "STARTED" and not bound:
                continue  # a STARTED record always carries an identity
            def call(m, start=start, target=target, ident=ident, fence=fence, bound=bound):
                record = m.Lifecycle(start, 4, identity_of(m) if bound else None)
                identity = {"none": None, "same": identity_of(m), "other": identity_of(m, 11), "text": "pid"}[ident]
                expected = {"none": None, "match": 4, "stale": 3, "bool": True, "negative": -1}[fence]
                return record.transition(target, identity=identity, expected_generation=expected)
            cases.append(("%s(bound=%s)->%s identity=%s fence=%s" % (start, bound, target, ident, fence), call))
        cases.append(("generation exhausted", lambda m: m.Lifecycle("NEW", MAX_SAFE).transition(
            "STARTING", expected_generation=MAX_SAFE)))
        self.assertSameOutcomes(cases)

    def test_classify_start(self):
        cases = []
        for (label, observed), handoff in itertools.product(observation_variants(), [True, False, 1, None]):
            cases.append(("classify(handoff=%r, %s)" % (handoff, label),
                          lambda m, observed=observed, handoff=handoff: m.classify_start(
                              handoff_accepted=handoff, started_observation=materialize(m, observed),
                              expected=spec_of(m))))
        cases.append(("classify(expected=not a spec)", lambda m: m.classify_start(
            handoff_accepted=True, started_observation=None, expected="spec")))
        self.assertSameOutcomes(cases)

    def test_reconcile(self):
        cases = [("reconcile(%s)" % label,
                  lambda m, observed=observed: m.reconcile(spec_of(m), materialize(m, observed)))
                 for label, observed in observation_variants()]
        cases.append(("reconcile(expected=not a spec)", lambda m: m.reconcile(
            "spec", {"run_id": "run", "environment_sha256": SHA_A, "state": "NOT_STARTED"})))
        self.assertSameOutcomes(cases)


if __name__ == "__main__":
    unittest.main()
