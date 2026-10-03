from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from sentinelmesh.approvals import ApprovalError, ApprovalStore
from sentinelmesh.config import ConfigError, ConfigResolver, load_profiles
from sentinelmesh.core import EventEnvelope, EventStore, ProtocolError, ResourceRef, new_request_id, new_run_id, strict_loads, stable_snapshot_bytes
from sentinelmesh.evidence import EvidenceSegmentStore
from sentinelmesh.observability import DebugBundleBuilder, Metrics, StructuredLogger, redact
from sentinelmesh.plugins import PluginError, PluginManifest, resolve_plugins
from sentinelmesh.runtime import Invocation, StaticAdapter, SubprocessRunner
from sentinelmesh.adapter import AdapterError, AdapterRegistry, AdapterRequest
from sentinelmesh.migration import MigrationController, MigrationError, MigrationStage
from sentinelmesh.verification import CheckCase, CheckObservation, ResultStatus, VerificationCatalog, VerificationError
from sentinelmesh.scheduler import ContractError, Scheduler, readiness, topological
from sentinelmesh.state import EventIndex, Projector, StateError


class ProtocolTests(unittest.TestCase):
    def test_duplicate_json_key_rejected(self):
        with self.assertRaises(ProtocolError) as ctx:
            strict_loads(b'{"a":1,"a":2}')
        self.assertEqual(ctx.exception.code, "E_JSON_DUPLICATE_KEY")

    def test_bom_nan_and_limits_rejected(self):
        for raw, code in [(b'\xef\xbb\xbf{}', "E_JSON_BOM"), (b'{"x":NaN}', "E_JSON_NON_FINITE")]:
            with self.subTest(code=code), self.assertRaises(ProtocolError) as ctx:
                strict_loads(raw)
            self.assertEqual(ctx.exception.code, code)

    def test_resource_hash_and_event_seq(self):
        data = b"hello"
        ref = ResourceRef.from_bytes("fixture", "f1", data)
        self.assertEqual(ref.sha256, "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
        req, run = new_request_id(), new_run_id()
        store = EventStore()
        event = EventEnvelope(req, run, 1, "run.prepared", {})
        store.append(event)
        self.assertIs(store.append(event), event)
        with self.assertRaises(ProtocolError) as ctx:
            store.append(EventEnvelope(req, run, 3, "run.done", {}))
        self.assertEqual(ctx.exception.code, "E_EVENT_GAP")

    def test_stable_snapshot_has_one_lf(self):
        self.assertTrue(stable_snapshot_bytes({"b": 1, "a": "x"}).endswith(b"\n"))


class EvidenceTests(unittest.TestCase):
    def test_segment_is_immutable_and_spans_are_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            store = EvidenceSegmentStore(d)
            seg = store.append(b"abcdef", segment_id="seg-1")
            self.assertEqual(store.read(seg.ref), b"abcdef")
            self.assertEqual(seg.span(1, 3)["byte_start"], 1)
            with self.assertRaises(ProtocolError):
                seg.span(0, 7)
            with self.assertRaises(ProtocolError):
                store.append(b"changed", segment_id="seg-1")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.profiles = {"planner": {"adapter": "pi", "model": "m1", "role": "planner", "fixed_args": ["--json"], "allowed_tools": ["git"]}}

    def test_effective_config_is_deterministic(self):
        resolver = ConfigResolver(self.profiles)
        a = resolver.resolve("planner")
        b = resolver.resolve("planner")
        self.assertEqual(a.semantic_hash, b.semantic_hash)

    def test_local_override_cannot_change_model_or_permissions(self):
        with self.assertRaises(ConfigError):
            ConfigResolver(self.profiles).resolve("planner", local_override={"model": "unsafe"})

    def test_unregistered_adapter_rejected(self):
        with self.assertRaises(ConfigError):
            ConfigResolver({"x": {"adapter": "unknown", "model": "m", "role": "planner"}}).resolve("x")


class ApprovalTests(unittest.TestCase):
    def test_cas_and_input_drift(self):
        store = ApprovalStore()
        refs = [{"kind": "contract", "id": "c1", "sha256": "a" * 64}]
        record = store.approve("subject", refs, actor="alice", expected_generation=0)
        self.assertEqual(store.validate(record.approval_id, refs).approval_id, record.approval_id)
        with self.assertRaises(ApprovalError):
            store.validate(record.approval_id, [{"kind": "contract", "id": "c1", "sha256": "b" * 64}])
        with self.assertRaises(ApprovalError):
            store.approve("other", refs, actor="bob", expected_generation=0)


class PluginTests(unittest.TestCase):
    def test_dependency_order_and_lock_digest(self):
        a = PluginManifest.from_dict({"id": "a", "version": "1", "api_version": 1, "dependencies": [], "resources": {"a.txt": "a" * 64}, "required_capabilities": []})
        b = PluginManifest.from_dict({"id": "b", "version": "1", "api_version": 1, "dependencies": ["a"], "resources": {"b.txt": "b" * 64}, "required_capabilities": []})
        lock = resolve_plugins({"a": a, "b": b}, ["b"])
        self.assertEqual(lock.ordered_plugins, ("a", "b"))
        self.assertEqual(len(lock.digest), 64)

    def test_cycle_and_entrypoint_rejected(self):
        with self.assertRaises(PluginError):
            PluginManifest.from_dict({"id": "a", "version": "1", "api_version": 1, "dependencies": [], "resources": {}, "required_capabilities": [], "entrypoint": "x"})
        a = PluginManifest("a", "1", 1, ("b",), {}, frozenset())
        b = PluginManifest("b", "1", 1, ("a",), {}, frozenset())
        with self.assertRaises(PluginError):
            resolve_plugins({"a": a, "b": b}, ["a"])


class RuntimeTests(unittest.TestCase):
    def test_subprocess_capture_and_timeout(self):
        profile = ConfigResolver({"p": {"adapter": "pi", "model": "m", "role": "executor"}}).resolve("p").profile
        invocation = StaticAdapter(executable="python").build_invocation(profile, {"cwd": str(Path.cwd()), "args": ["-c", "print('ok')"]})
        result = SubprocessRunner().run(invocation)
        self.assertTrue(result.started)
        self.assertEqual(result.exit_code, 0)
        self.assertIn(b"ok", result.stdout)

    def test_shell_and_argv_carriers_are_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            Invocation(("echo", "x"), str(Path.cwd()), {}, 1, shell=True, script="echo x").validate()


class ObservabilityTests(unittest.TestCase):
    def test_redaction_logger_metrics_and_bundle(self):
        self.assertEqual(redact({"token": "secret", "nested": "Authorization: abc"})["token"], "[REDACTED]")
        with tempfile.TemporaryDirectory() as d:
            log = StructuredLogger(Path(d) / "events.jsonl")
            log.write("diagnostic-log", code="E_TEST", token="secret", message="ok")
            self.assertIn("[REDACTED]", (Path(d) / "events.jsonl").read_text(encoding="utf-8"))
            metrics = Metrics(); metrics.inc("test_total", phase="unit")
            self.assertEqual(metrics.snapshot()[0]["value"], 1)
            bundle = DebugBundleBuilder().build(incident_id="i1", scope={"run_id": "r"}, evidence={"token": "secret"})
            self.assertEqual(len(bundle.sha256), 64)


class SchedulingTests(unittest.TestCase):
    def test_dag_and_readiness(self):
        self.assertEqual(topological([{"id": "b", "depends_on": ["a"]}, {"id": "a", "depends_on": []}]), ["a", "b"])
        with self.assertRaises(ContractError):
            topological([{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]}])
        facts = {name: True for name in ("inputs_accepted", "baseline_present", "baseline_authorized", "contract_current", "approval_current", "locks_match", "capabilities_ok", "authorization_ok", "budget_ok", "conflicts_clear", "request_open")}
        self.assertTrue(readiness(facts)["can_execute"])

    def test_claim_handoff_usage_and_verdict(self):
        scheduler = Scheduler()
        scheduler.seed_request("req-1", "repo-1", {"dispatches": 2, "tokens": 10})
        scheduler.seed_task("req-1", "task-1", {"dispatches": 2, "tokens": 10})
        claim = scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 5}, ["src/a.py"])
        op = claim["operation_id"]
        scheduler.handoff(op, "owner-1", 1)
        scheduler.mark_running(op, "owner-1", 1)
        scheduler.observe(op, "tokens", "call-1", 1, 3, True, "a" * 64)
        scheduler.finish(op, "owner-1", 1, "b" * 64, True)
        scheduler.fixture_verdict(op, "b" * 64, True, "c" * 64)
        self.assertEqual(scheduler.events[-1]["type"], "run.verdict")

    def test_claim_is_idempotent_and_conflict_is_rejected(self):
        scheduler = Scheduler()
        scheduler.seed_request("req-1", "repo-1", {"dispatches": 2, "tokens": 10})
        scheduler.seed_task("req-1", "task-1", {"dispatches": 2, "tokens": 10})
        first = scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 5}, ["src/a.py"])
        again = scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 5}, ["src/a.py"])
        self.assertTrue(again["replayed"])
        with self.assertRaises(ContractError):
            scheduler.claim("req-1", "task-1", 1, "idem-1", "owner-1", 1, 1, {"dispatches": 1, "tokens": 4}, ["src/a.py"])


class StateTests(unittest.TestCase):
    def test_projection_rebuild_and_generation_conflict(self):
        store = EventIndex(repository_namespace="repo", project_id="p1")
        base = {"repository_namespace": "repo", "project_id": "p1", "producer": "test", "source_record_id": "src-1", "request_id": "req-1", "stream": "task", "subject_id": "task-1", "revision": 1, "value": {"attempt_status": "TERMINAL", "verdict": "PASS", "candidate_id": "cand-1", "checks_candidate_id": "cand-1"}}
        decision = {**base, "source_record_id": "src-2", "stream": "decision", "subject_id": "decision-1", "value": {"decision": "use-pi"}}
        store.append(base)
        store.append(decision)
        state = Projector(store).project()
        self.assertEqual(len(state["task_views"]), 1)
        self.assertEqual(state["task_views"][0]["display_state"], "needs-acceptance")
        self.assertIn("task-1", Projector(store).brief(state)[0].decode("utf-8"))
        old = store.read()[0][-1]
        with self.assertRaises(StateError):
            store.append({**decision, "value": {"decision": "changed"}})
        store.rollover("index-2")
        self.assertEqual(store.cursor["generation"], "index-2")
        self.assertEqual(store.append(old)[0]["cursor"]["seq"], 1)


class AdapterVerificationMigrationTests(unittest.TestCase):
    def test_adapter_capability_and_event_normalization(self):
        registry = AdapterRegistry()
        self.assertEqual(registry.probe("pi").adapter_id, "pi")
        records = registry.adapter("pi").normalize(b'{"type":"tool.completed","payload":{"id":"1"}}\n')
        self.assertEqual(records[0].type, "tool.completed")
        with self.assertRaises(AdapterError):
            registry.adapter("pi").normalize(b'{"payload":{}}\n')

    def test_verification_fails_closed_until_required_evidence_exists(self):
        catalog = VerificationCatalog([CheckCase("C1", ("01-A01",), "baseline")])
        self.assertFalse(catalog.summary()["can_dispatch"])
        with self.assertRaises(VerificationError):
            catalog.observe(CheckObservation("C1", ResultStatus.PASS, "oracle", "ok"))
        catalog.observe(CheckObservation("C1", ResultStatus.PASS, "oracle", "ok", ("artifact-1",), reviewer="reviewer"))
        self.assertTrue(catalog.summary()["can_dispatch"])

    def test_migration_cas_holds_on_user_edit(self):
        controller = MigrationController()
        before = {"config.json": b"old"}
        after = {"config.json": b"new"}
        plan = controller.preview(before, after)
        plan = controller.verify_backup(plan, before)
        with self.assertRaises(MigrationError):
            controller.apply(plan, {"config.json": b"user-edit"}, after)
        self.assertEqual(controller.history[-1].stage, MigrationStage.HOLD)
        result = controller.apply(plan, before, after)
        self.assertEqual(result["config.json"], b"new")


if __name__ == "__main__":
    unittest.main()
