from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sentinelmesh.adapter import AdapterError, AdapterRegistry, AdapterRequest
from sentinelmesh.observer import DeterministicObserver, ObserverError, ObserverRequest
from sentinelmesh.runtime import Invocation, RuntimeErrorSM, StaticAdapter
from sentinelmesh.config import ConfigResolver


class ObserverBoundaryTests(unittest.TestCase):
    def test_request_rejects_unbounded_or_empty_input(self):
        with self.assertRaises(ObserverError):
            ObserverRequest("req-1", "cursor-1", ("src-1",), max_items=0)
        with self.assertRaises(ObserverError):
            ObserverRequest("req-1", "cursor-1", ("src-1",), max_items=True)
        with self.assertRaises(ObserverError):
            ObserverRequest("req-1", "cursor-1", ())

    def test_summary_rejects_missing_source_closure_and_bad_events(self):
        request = ObserverRequest("req-1", "cursor-1", ("src-1",), max_items=2)
        observer = DeterministicObserver()
        with self.assertRaises(ObserverError):
            observer.summarize(request, [{"type": "a"}, {"type": "b"}])
        with self.assertRaises(ObserverError):
            observer.summarize(request, [{"type": "a"}, "bad"])

    def test_summary_preserves_one_source_per_fact(self):
        request = ObserverRequest("req-1", "cursor-1", ("src-1", "src-2"), max_items=2)
        result = DeterministicObserver().summarize(
            request, [{"type": "a"}, {"type": "b"}, {"type": "c"}]
        )
        self.assertEqual([item["source_ref"] for item in result.facts], ["src-1", "src-2"])
        self.assertEqual(result.omitted, ("event:2",))


class AdapterBoundaryTests(unittest.TestCase):
    def test_normalizer_requires_object_payload(self):
        adapter = AdapterRegistry().adapter("pi")
        with self.assertRaises(AdapterError):
            adapter.normalize(b'{"type":"tool.completed"}\n')
        with self.assertRaises(AdapterError):
            adapter.normalize(b'{"type":"tool.completed","payload":[]}\n')
        with self.assertRaises(AdapterError):
            adapter.normalize('{"type":"tool.completed","payload":{}}\n')  # type: ignore[arg-type]

    def test_request_rejects_invalid_process_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AdapterError):
                AdapterRequest("req-1", "p", "executor", "linux", directory, args=("bad\x00",))
            request = AdapterRequest("req-1", "p", "executor", "linux", directory)
            self.assertEqual(request.env, {})


class RuntimeBoundaryTests(unittest.TestCase):
    def test_invocation_rejects_nonfinite_timeout_and_invalid_env(self):
        cwd = str(Path.cwd())
        with self.assertRaises(RuntimeErrorSM):
            Invocation(("echo",), cwd, {}, float("inf")).validate()
        with self.assertRaises(RuntimeErrorSM):
            Invocation(("echo",), cwd, {"A": "bad\x00"}, 1).validate()
        with self.assertRaises(RuntimeErrorSM):
            Invocation((), cwd, {}, 1, shell=True, script="echo\x00 x").validate()

    def test_static_adapter_rejects_non_mapping_arguments(self):
        profile = ConfigResolver({"p": {"adapter": "pi", "model": "m", "role": "executor"}}).resolve("p").profile
        with self.assertRaises(RuntimeErrorSM):
            StaticAdapter().build_invocation(profile, {"cwd": str(Path.cwd()), "args": "-c print(1)"})


if __name__ == "__main__":
    unittest.main()
