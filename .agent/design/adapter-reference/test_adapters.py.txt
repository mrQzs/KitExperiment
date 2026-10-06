import sys
import unittest
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from codekit.adapters import AdapterDescriptor, AdapterError, AdapterRegistry, Invocation, OfflineAdapter, normalize_events


def invocation():
    return Invocation("req", "run", "executor", "offline-profile", ("python", "-c", "pass"), ("structured-events",), "a" * 64)


def event(seq, typ, payload=None, event_id=None):
    return {"seq": seq, "event_id": event_id or f"e{seq}", "type": typ, "source": "offline", "payload": payload or {}}


class AdapterTests(unittest.TestCase):
    def test_invocation_preserves_repeated_and_empty_arguments(self):
        argv = ("python", "-c", "pass\n", "--tag", "same", "--tag", "same", "")
        call = Invocation("req", "run", "executor", "offline-profile", argv, (), "a" * 64)
        self.assertEqual(call.command, argv)
        changed = Invocation("req", "run", "executor", "offline-profile", argv[:-1], (), "a" * 64)
        self.assertNotEqual(call.digest(), changed.digest())

    def test_invocation_rejects_string_vectors_and_non_string_environment(self):
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_SCHEMA"):
            Invocation("req", "run", "executor", "offline-profile", "python", (), "a" * 64)
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_SCHEMA"):
            Invocation("req", "run", "executor", "offline-profile", ("python",), "structured-events", "a" * 64)
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_INVOCATION"):
            Invocation("req", "run", "executor", "offline-profile", ("python",), (), 1)

    def test_descriptor_rejects_duplicate_or_non_string_contract_entries(self):
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_SCHEMA"):
            AdapterDescriptor("offline", "v1", ("events", "events"), (), True)
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_SCHEMA"):
            AdapterDescriptor("offline", "v1", (1,), (), True)

    def test_registry_rejects_unknown_and_duplicate(self):
        reg = AdapterRegistry([AdapterDescriptor("offline", "v1", ("structured-events",), ("executor",), True)])
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_UNKNOWN"):
            reg.resolve("missing")
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_DUPLICATE"):
            reg.register(AdapterDescriptor("offline", "v1", (), (), True))

    def test_probe_is_explicitly_offline(self):
        self.assertEqual(OfflineAdapter().probe()["process_started"], False)

    def test_invocation_can_bind_adapter_identity(self):
        reg = AdapterRegistry([AdapterDescriptor("offline", "v1", ("structured-events",), ("executor",), True)])
        bound = Invocation("req", "run", "executor", "offline-profile", ("python",), ("structured-events",), "a" * 64, "offline")
        self.assertEqual(reg.validate_invocation(bound).adapter_id, "offline")
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_IDENTITY"):
            OfflineAdapter().run(Invocation("req", "run", "executor", "offline-profile", ("python",), (), "a" * 64, "other"), [event(0, "run_started")])

    def test_events_normalize_in_order_and_usage_is_preserved(self):
        result = OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "usage", {"input": "3"}), event(2, "run_finished")])
        self.assertTrue(result.settled); self.assertEqual(result.status, "SUCCEEDED"); self.assertEqual(result.usage["input"], "3")

    def test_gap_duplicate_unknown_and_wrong_source_are_rejected(self):
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_EVENT_GAP"):
            normalize_events([event(1, "run_started")], expected_source="offline")
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_EVENT_DUPLICATE"):
            normalize_events([event(0, "run_started", event_id="x"), event(1, "assistant", event_id="x")], expected_source="offline")
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_EVENT_TYPE"):
            normalize_events([event(0, "wat")], expected_source="offline")
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_SOURCE"):
            normalize_events([{**event(0, "run_started"), "source": "pi"}], expected_source="offline")
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_EVENT"):
            normalize_events([{k: v for k, v in event(0, "run_started").items() if k != "payload"}], expected_source="offline")
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_EVENT_SEQ"):
            normalize_events([event(2**53, "run_started")], expected_source="offline")

    def test_offline_adapter_preserves_unknown_events_as_unsettled_evidence(self):
        result = OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "future_event")])
        self.assertFalse(result.settled)
        self.assertEqual(result.events[-1].event_type, "unknown")
        self.assertEqual(result.events[-1].payload["unknown_type"], "future_event")

    def test_missing_terminal_is_unknown_not_success(self):
        result = OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "assistant")])
        self.assertFalse(result.settled); self.assertEqual(result.status, "UNKNOWN"); self.assertFalse(result.raw_complete)

    def test_unknown_record_before_terminal_cannot_be_promoted_to_success(self):
        result = OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "future_event"), event(2, "run_finished")])
        self.assertFalse(result.settled)
        self.assertEqual(result.status, "UNKNOWN")
        self.assertFalse(result.raw_complete)

    def test_explicit_error_is_a_complete_terminal_observation(self):
        result = OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "error")])
        self.assertTrue(result.settled)
        self.assertEqual(result.status, "ERROR")
        self.assertTrue(result.raw_complete)

    def test_non_invocation_is_structured_error(self):
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_INVOCATION"):
            OfflineAdapter().run({}, [event(0, "run_started")])

    def test_terminal_is_unique_and_final(self):
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_SETTLEMENT"):
            OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "error"), event(2, "run_finished")])
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_SETTLEMENT"):
            OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "run_finished"), event(2, "assistant")])

    def test_run_started_is_unique_and_first(self):
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_SETTLEMENT"):
            OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "run_started"), event(2, "run_finished")])
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_SETTLEMENT"):
            OfflineAdapter().run(invocation(), [event(0, "assistant"), event(1, "run_started"), event(2, "run_finished")])

    def test_tool_events_must_form_closed_pairs_before_terminal(self):
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_TOOL"):
            OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "tool_started", {"tool_id": "t1"}), event(2, "run_finished")])
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_TOOL"):
            OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "tool_finished", {"tool_id": "t1"}), event(2, "run_finished")])
        result = OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "tool_started", {"tool_id": "t1"}), event(2, "tool_finished", {"tool_id": "t1"}), event(3, "run_finished")])
        self.assertTrue(result.settled)

    def test_text_and_payload_boundaries_are_strict(self):
        for bad in ("line\nfeed", "\x1f", "\x7f", "\ud800"):
            with self.assertRaisesRegex(AdapterError, "E_ADAPTER_SCHEMA"):
                AdapterDescriptor(bad, "v1", (), (), True)
        cyclic = {}
        cyclic["self"] = cyclic
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_EVENT"):
            normalize_events([event(0, "run_started", cyclic)], expected_source="offline")
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_EVENT"):
            normalize_events([event(0, "usage", {"value": math.nan})], expected_source="offline")
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_SCHEMA"):
            normalize_events([], expected_source="offline", preserve_unknown=1)
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_TOOL"):
            OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "tool_started", {"tool_id": "bad\x01"})])

    def test_unknown_event_is_explicit_and_terminal_stream_is_closed(self):
        result = OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "future_event")])
        self.assertEqual(result.events[-1].event_type, "unknown")
        with self.assertRaisesRegex(AdapterError, "E_ADAPTER_SETTLEMENT"):
            OfflineAdapter().run(invocation(), [event(0, "run_started"), event(1, "run_finished"), event(2, "future_event")])


if __name__ == "__main__":
    unittest.main()
