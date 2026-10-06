"""Executor-provided tests for the Code-Kit adapter boundary port.

The port must be import-safe (no ``ctypes``, no ``codekit`` import as a side effect of
importing ``sentinelmesh.adapter_protocol``) and must expose exactly the merged surface of
``adapters/protocol.py`` and ``adapters/offline.py``.  Behavioral conformance lives in the
planner oracles (tests/test_adapter_protocol_reference_oracle.py and
tests/test_adapter_protocol_differential_oracle.py); this file only checks the contract
points those oracles do not observe.
"""
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

from sentinelmesh import adapter_protocol

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

IMPORT_CHECK = (
    "import sys\n"
    "import sentinelmesh.adapter_protocol\n"
    "assert not any(name == 'ctypes' or name.startswith('ctypes.') for name in sys.modules), "
    "sorted(name for name in sys.modules if name.startswith('ctypes'))\n"
    "leaked = sorted(name for name in sys.modules if name == 'codekit' or name.startswith('codekit.'))\n"
    "assert not leaked, leaked\n"
    "print('IMPORT_SAFE_OK')\n"
)


class ImportSafetyTests(unittest.TestCase):
    def test_fresh_interpreter_imports_neither_ctypes_nor_codekit(self):
        """Importing the port in a new process must not pull in ctypes or codekit."""
        completed = subprocess.run([sys.executable, "-c", IMPORT_CHECK],
                                   cwd=str(REPOSITORY_ROOT), capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertIn("IMPORT_SAFE_OK", completed.stdout)

    def test_no_codekit_module_is_reachable_through_the_port(self):
        leaked = [name for name in sys.modules if name == "codekit" or name.startswith("codekit.")]
        self.assertEqual(leaked, [])
        self.assertNotIn("codekit", adapter_protocol.__dict__)


class SurfaceTests(unittest.TestCase):
    def test_all_is_exactly_the_merged_nine_names(self):
        self.assertEqual(list(adapter_protocol.__all__),
                         ["AdapterDescriptor", "AdapterError", "AdapterRegistry", "AdapterResult",
                          "EVENT_TYPES", "Invocation", "NormalizedEvent", "OfflineAdapter",
                          "normalize_events"])
        for name in adapter_protocol.__all__:
            self.assertTrue(hasattr(adapter_protocol, name), name)

    def test_offline_adapter_probe_declares_no_process_and_no_network(self):
        probe = adapter_protocol.OfflineAdapter().probe()
        self.assertEqual(probe, {"adapter_id": "offline", "protocol_version": "offline-adapter-v1",
                                 "offline": True, "process_started": False, "network_used": False})


class PortedBehaviorTests(unittest.TestCase):
    """A legal baseline plus the single documented rejections of the ported boundary."""

    def invocation(self):
        return adapter_protocol.Invocation("req", "run", "executor", "offline-profile",
                                           ("python", "-c", "pass"), ("structured-events",), "a" * 64)

    def event(self, seq, typ, payload=None, event_id=None):
        return {"seq": seq, "event_id": event_id or "e%d" % seq, "type": typ, "source": "offline",
                "payload": {} if payload is None else payload}

    def test_legal_stream_is_settled_and_its_digest_matches_the_invocation(self):
        invocation = self.invocation()
        result = adapter_protocol.OfflineAdapter().run(
            invocation, [self.event(0, "run_started"), self.event(1, "usage", {"input": "3"}),
                         self.event(2, "run_finished")])
        self.assertTrue(result.settled)
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertTrue(result.raw_complete)
        self.assertEqual(result.usage, {"input": "3"})
        self.assertEqual(result.invocation_sha256, invocation.digest())

    def test_control_characters_are_rejected_in_adapter_text(self):
        for bad in ("line\nfeed", "\x1f", "\x7f", "\x85"):
            with self.assertRaisesRegex(adapter_protocol.AdapterError, "E_ADAPTER_SCHEMA"):
                adapter_protocol.AdapterDescriptor(bad, "v1", (), (), True)

    def test_normalize_events_keeps_a_known_type_without_adding_an_unknown_marker(self):
        events = adapter_protocol.normalize_events(
            [self.event(0, "run_started"), self.event(1, "usage", {"input": "3"})],
            expected_source="offline")
        self.assertEqual([event.event_type for event in events], ["run_started", "usage"])
        self.assertEqual(events[1].payload, {"input": "3"})

    def test_normalize_events_wraps_an_unknown_type_only_when_preserving_it(self):
        events = adapter_protocol.normalize_events([self.event(0, "run_started"),
                                                    self.event(1, "future_event")],
                                                   expected_source="offline", preserve_unknown=True)
        self.assertEqual(events[1].event_type, "unknown")
        self.assertEqual(events[1].payload["unknown_type"], "future_event")
        with self.assertRaisesRegex(adapter_protocol.AdapterError, "E_ADAPTER_EVENT_TYPE"):
            adapter_protocol.normalize_events([self.event(0, "future_event")], expected_source="offline")

    def test_normalized_payloads_are_copies(self):
        payload = {"nested": {"a": 1}}
        events = adapter_protocol.normalize_events([self.event(0, "run_started", payload)],
                                                   expected_source="offline")
        payload["nested"]["a"] = 2
        self.assertEqual(events[0].payload, {"nested": {"a": 1}})

    def test_invocation_digest_is_canonical_json_over_sorted_keys(self):
        invocation = self.invocation()
        payload = {"request_id": "req", "run_id": "run", "role": "executor",
                   "profile_id": "offline-profile", "command": ["python", "-c", "pass"],
                   "capabilities": ["structured-events"], "environment_sha256": "a" * 64}
        expected = hashlib.sha256((json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()
        self.assertEqual(invocation.digest(), expected)


if __name__ == "__main__":
    unittest.main()
