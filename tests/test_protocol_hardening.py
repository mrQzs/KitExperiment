from __future__ import annotations

import unittest

from sentinelmesh.approvals import ApprovalError, ApprovalStore
from sentinelmesh.config import ConfigError, ConfigResolver, load_profiles
from sentinelmesh.migration import MigrationController, MigrationError
from sentinelmesh.plugins import PluginError, PluginManifest, resolve_plugins
from sentinelmesh.verification import CheckCase, CheckObservation, ResultStatus, VerificationError, VerificationCatalog
from sentinelmesh.state import EventIndex, StateError
from sentinelmesh.scheduler import ContractError, canonical, path_conflict, safe_path
from sentinelmesh.observability import Metrics, StructuredLogger


REF = {"kind": "contract", "id": "c1", "sha256": "a" * 64}


class ConfigHardeningTests(unittest.TestCase):
    def test_string_input_is_supported_and_invalid_shell_is_rejected(self):
        profiles = load_profiles('{"profiles":{"p":{"adapter":"pi","model":"m","role":"executor"}}}')
        self.assertEqual(ConfigResolver(profiles).resolve("p").profile.profile_id, "p")
        with self.assertRaises(ConfigError):
            ConfigResolver({"p": {"adapter": "pi", "model": "m", "role": "executor", "shell": "bad\nvalue"}}).resolve("p")


class ApprovalHardeningTests(unittest.TestCase):
    def test_refs_are_closed_and_copied(self):
        refs = [dict(REF)]
        record = ApprovalStore().approve("subject", refs, actor="alice")
        refs[0]["sha256"] = "b" * 64
        self.assertEqual(record.lock.refs[0]["sha256"], "a" * 64)
        with self.assertRaises(ApprovalError):
            ApprovalStore().approve("subject", [{"kind": "bad"}], actor="alice")


class PluginHardeningTests(unittest.TestCase):
    def test_duplicate_requested_plugin_is_rejected(self):
        manifest = PluginManifest.from_dict({"id": "a", "version": "1", "api_version": 1, "dependencies": [], "resources": {}, "required_capabilities": []})
        with self.assertRaises(PluginError):
            resolve_plugins({"a": manifest}, ["a", "a"])


class VerificationHardeningTests(unittest.TestCase):
    def test_string_status_is_normalized_and_invalid_status_rejected(self):
        catalog = VerificationCatalog([CheckCase("C1", ("R1",), "check")])
        catalog.observe(CheckObservation("C1", "FAIL", "oracle", "failed"))  # type: ignore[arg-type]
        self.assertEqual(catalog.summary()["failed_required"], ["C1"])
        with self.assertRaises(VerificationError):
            CheckObservation("C1", "BROKEN", "oracle", "bad")  # type: ignore[arg-type]


class MigrationHardeningTests(unittest.TestCase):
    def test_after_bytes_are_verified_before_apply(self):
        controller = MigrationController()
        plan = controller.verify_backup(controller.preview({"a.txt": b"old"}, {"a.txt": b"new"}), {"a.txt": b"old"})
        with self.assertRaises(MigrationError):
            controller.apply(plan, {"a.txt": b"old"}, {"a.txt": b"tampered"})


class StateHardeningTests(unittest.TestCase):
    def test_cycles_and_invalid_source_identity_fail_closed(self):
        value = []
        value.append(value)
        with self.assertRaises(StateError):
            EventIndex().append({"repository_namespace": "repo", "project_id": "project", "producer": "p", "source_record_id": "s", "request_id": "r", "stream": "task", "subject_id": "t", "revision": 1, "value": value})
        with self.assertRaises(StateError):
            EventIndex().append({"repository_namespace": "repo", "project_id": "project", "producer": "p", "source_record_id": "s", "request_id": "r", "stream": "task", "subject_id": [], "revision": 1, "value": {}})


class SchedulerHardeningTests(unittest.TestCase):
    def test_cycles_reserved_paths_and_missing_retry_shape_are_rejected(self):
        value = []
        value.append(value)
        with self.assertRaises(ContractError):
            canonical(value)
        with self.assertRaises(ContractError):
            safe_path("CON/config.json")
        with self.assertRaises(ContractError):
            path_conflict({"kind": "file"}, {"kind": "file", "path": "a"})


class ObservabilityHardeningTests(unittest.TestCase):
    def test_metrics_reject_nonfinite_values(self):
        with self.assertRaises(ValueError):
            Metrics().inc("x", float("nan"))
        with self.assertRaises(ValueError):
            StructuredLogger("events.jsonl").write("bad\nrecord")


if __name__ == "__main__":
    unittest.main()
