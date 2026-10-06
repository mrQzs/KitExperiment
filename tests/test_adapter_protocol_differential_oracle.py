"""Differential oracle: sentinelmesh.adapter_protocol must behave exactly like Code-Kit.

Planner-provided acceptance oracle for adapter-protocol-port; executors must not
modify this file.  It loads the frozen reference in .agent/design/adapter-reference/
(protocol.py.txt and offline.py.txt, checked against manifest.json), rewrites only
their package-relative imports to the KitExperiment modules that are byte-identical
in behavior (sentinelmesh.protocols, sentinelmesh.runtime), and compares
sentinelmesh.adapter_protocol with it over deterministic input grids.  For every
call both must return equal plain data, or raise an AdapterError with the same
code, seq and message (any other exception: the same type).
"""
from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import math
import sys
import unittest
from pathlib import Path

import sentinelmesh.adapter_protocol as candidate

REFERENCE_DIR = Path(__file__).resolve().parents[1] / ".agent" / "design" / "adapter-reference"
REWRITES = {
    "protocol.py.txt": [("from ..protocols import ", "from sentinelmesh.protocols import "),
                        ("from ..runtime import ", "from sentinelmesh.runtime import ")],
    "offline.py.txt": [("from .protocol import AdapterError, AdapterResult, Invocation, normalize_events\n", "")],
}


def _source(name):
    manifest = json.loads((REFERENCE_DIR / "manifest.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["path"] == name)
    data = (REFERENCE_DIR / name).read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise AssertionError("frozen reference %s does not match manifest.json" % name)
    text = data.decode("utf-8")
    for old, new in REWRITES[name]:
        if text.count(old) != 1:
            raise AssertionError("expected exactly one %r in %s" % (old, name))
        text = text.replace(old, new)
    return text


def _module(module_name, file_name, namespace=None):
    spec = importlib.util.spec_from_loader(module_name, loader=None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    if namespace:
        module.__dict__.update(namespace)
    exec(compile(_source(file_name), str(REFERENCE_DIR / file_name), "exec"), module.__dict__)
    return module


protocol_ref = _module("sentinelmesh_adapter_reference_protocol", "protocol.py.txt")
offline_ref = _module("sentinelmesh_adapter_reference_offline", "offline.py.txt",
                      {name: getattr(protocol_ref, name)
                       for name in ("AdapterError", "AdapterResult", "Invocation", "normalize_events")})


class Ref:
    """The reference surface under the candidate module's names."""
    AdapterDescriptor = protocol_ref.AdapterDescriptor
    AdapterError = protocol_ref.AdapterError
    AdapterRegistry = protocol_ref.AdapterRegistry
    AdapterResult = protocol_ref.AdapterResult
    Invocation = protocol_ref.Invocation
    NormalizedEvent = protocol_ref.NormalizedEvent
    normalize_events = staticmethod(protocol_ref.normalize_events)
    OfflineAdapter = offline_ref.OfflineAdapter
    EVENT_TYPES = protocol_ref.EVENT_TYPES


MODULES = (Ref, candidate)
SHA_A = "a" * 64


class S(str):
    pass


class D(dict):
    pass


def plain(value):
    """Module-specific values as comparable plain data."""
    name = type(value).__name__
    if name == "AdapterDescriptor":
        return (name, value.adapter_id, value.protocol_version, value.capabilities, value.roles, value.offline)
    if name == "Invocation":
        return (name, value.request_id, value.run_id, value.role, value.profile_id, value.command,
                value.capabilities, value.environment_sha256, value.adapter_id, value.digest())
    if name == "NormalizedEvent":
        return (name, value.seq, value.event_id, value.event_type, value.source, plain(value.payload))
    if name == "AdapterResult":
        return (name, value.invocation_sha256, plain(value.events), value.settled, value.status,
                plain(value.usage), value.raw_complete)
    if isinstance(value, dict):
        return {"dict": sorted((repr(k), plain(v)) for k, v in value.items())}
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, [plain(item) for item in value])
    return (type(value).__name__, repr(value))


def outcome(module, call):
    try:
        return ("ok", plain(call(module)))
    except Exception as exc:  # noqa: BLE001 - the exception is the observable outcome
        if isinstance(exc, module.AdapterError):
            return ("AdapterError", exc.code, exc.seq, str(exc))
        return ("raise", type(exc).__name__)


TEXTS = ["x", "offline", "", "a\nb", "a\x1fb", "a\x7fb", "a\x85b", "a\x9fb", "a\ud800b", "é", S("x"),
         1, None, b"x", ("x",)]
TUPLES = [(), ("a",), ("a", "b"), ("a", "a"), ["a"], "a", ("a", 1), ("a", ""), ("a\n",), (S("a"),), None]
COMMANDS = [("python",), ("python", "-c", "pass\n"), ("python", ""), ("python", "--tag", "same", "--tag", "same"),
            (), ["python"], "python", ("",), ("py\nthon",), ("py\x00",), ("python", "a\x00"),
            ("python", "\ud800"), (S("python"),), ("python", 1)]
DIGESTS = [SHA_A, "A" * 64, "a" * 63, "g" * 64, "a" * 65, 1, None, S(SHA_A)]


def cyclic():
    value = {}
    value["self"] = value
    return value


PAYLOADS = [{}, {"a": 1}, {"tokens": 5, "cost": "0.1"}, [], None, "x", {"x": math.nan}, {"x": math.inf},
            {1: "x"}, {"x": 2**60}, {"x": (1, 2)}, {"x": b"y"}, {"x": {"y": [1, {"z": None}]}}, "cyclic"]


def ev(seq, typ, payload=None, event_id=None, source="offline"):
    return {"seq": seq, "event_id": event_id or "e%d" % seq if isinstance(seq, int) else event_id or "ex",
            "type": typ, "source": source, "payload": {} if payload is None else payload}


class AdapterProtocolDifferentialOracle(unittest.TestCase):
    def assert_same(self, call, label):
        ref, cand = (outcome(module, call) for module in MODULES)
        self.assertEqual(cand, ref, label)

    def test_module_surface(self):
        for name in ("AdapterDescriptor", "AdapterError", "AdapterRegistry", "AdapterResult", "Invocation",
                     "NormalizedEvent", "normalize_events", "OfflineAdapter", "EVENT_TYPES"):
            self.assertTrue(hasattr(candidate, name), name)
        self.assertEqual(candidate.EVENT_TYPES, Ref.EVENT_TYPES)
        self.assertEqual(outcome(candidate, lambda m: m.OfflineAdapter().probe()),
                         outcome(Ref, lambda m: m.OfflineAdapter().probe()))

    def test_descriptor_grid(self):
        base = {"adapter_id": "offline", "protocol_version": "v1", "capabilities": ("structured-events",),
                "roles": ("executor",), "offline": True}
        variants = {"adapter_id": TEXTS, "protocol_version": TEXTS, "capabilities": TUPLES, "roles": TUPLES,
                    "offline": [True, False, 1, 0, None, "yes"]}
        for field, values in variants.items():
            for value in values:
                args = dict(base, **{field: value})
                self.assert_same(lambda m, a=args: m.AdapterDescriptor(**a), "descriptor %s=%r" % (field, value))

    def test_invocation_grid(self):
        base = {"request_id": "req", "run_id": "run", "role": "executor", "profile_id": "offline-profile",
                "command": ("python", "-c", "pass"), "capabilities": ("structured-events",),
                "environment_sha256": SHA_A}
        variants = {"request_id": TEXTS, "run_id": TEXTS, "role": TEXTS, "profile_id": TEXTS,
                    "command": COMMANDS, "capabilities": TUPLES, "environment_sha256": DIGESTS,
                    "adapter_id": [None] + TEXTS}
        for field, values in variants.items():
            for value in values:
                args = dict(base, **{field: value})
                self.assert_same(lambda m, a=args: m.Invocation(**a), "invocation %s=%r" % (field, value))

    def test_normalize_single_field_variations(self):
        legal = [ev(0, "run_started"), ev(1, "usage", {"input": "3"}), ev(2, "run_finished")]
        field_values = {
            "seq": [0, 1, 2, 3, -1, 2**53, 2**53 - 1, True, "0", None, 1.0],
            "event_id": TEXTS + ["e0", "e1"],
            "type": sorted(Ref.EVENT_TYPES) + ["future_event", "", "Usage", "unknown", 1, None],
            "source": ["offline", "pi", "", "a\nb", 1, None],
            "payload": PAYLOADS,
        }
        for position in range(3):
            for field, values in field_values.items():
                for value in values:
                    def call(m, position=position, field=field, value=value):
                        events = [dict(item) for item in legal]
                        events[position][field] = cyclic() if value == "cyclic" else value
                        return m.normalize_events(events, expected_source="offline", preserve_unknown=True)
                    self.assert_same(call, "position %d %s=%r" % (position, field, value))
                    def strict(m, position=position, field=field, value=value):
                        events = [dict(item) for item in legal]
                        events[position][field] = cyclic() if value == "cyclic" else value
                        return m.normalize_events(events, expected_source="offline")
                    self.assert_same(strict, "strict position %d %s=%r" % (position, field, value))

    def test_normalize_shape_errors(self):
        base = ev(0, "run_started")
        shapes = [[base, dict(base, seq=1, event_id="e1", extra=1)], [{1: "x", **base}], [[base]], [None], ["x"], [],
                  [D(base)], [dict(base, payload=D({"a": 1}))], [dict(base, payload={"x": "a" * 1_048_576})]]
        shapes += [[{k: v for k, v in base.items() if k != name}] for name in base]
        for events in shapes:
            self.assert_same(lambda m, e=events: m.normalize_events(e, expected_source="offline"), "shape %r" % (events,))
        # Each module gets its own fresh iterable: an iterator is consumed by the first call.
        iterables = {"int": lambda: 5, "None": lambda: None, "tuple": lambda: (base,),
                     "iterator": lambda: iter([base, dict(base, seq=1, event_id="e1")]),
                     "generator": lambda: (item for item in [base, dict(base, seq=1, event_id="e1")])}
        for label, make in iterables.items():
            self.assert_same(lambda m, make=make: m.normalize_events(make(), expected_source="offline"),
                             "iterable %s" % label)
        for source in ("offline", "", 1, "pi", S("offline")):
            for preserve in (False, True, 1, None):
                self.assert_same(lambda m, s=source, p=preserve: m.normalize_events(
                    [base, ev(1, "future_event")], expected_source=s, preserve_unknown=p),
                    "source=%r preserve=%r" % (source, preserve))

    def test_normalize_copies_payloads(self):
        for module in MODULES:
            payload = {"nested": {"a": 1}}
            events = [ev(0, "run_started", payload)]
            result = module.normalize_events(events, expected_source="offline")
            payload["nested"]["a"] = 2
            self.assertEqual(result[0].payload, {"nested": {"a": 1}}, module.__name__)

    def test_offline_run_exhaustive_sequences(self):
        alphabet = [("run_started", {}), ("assistant", {}), ("tool_started", {"tool_id": "t1"}),
                    ("tool_finished", {"tool_id": "t1"}), ("tool_started", {"tool_id": "t2"}),
                    ("usage", {"tokens": 5}), ("run_finished", {}), ("error", {}), ("future_event", {})]
        for length in range(5):
            for combo in itertools.product(range(len(alphabet)), repeat=length):
                def call(m, combo=combo):
                    events = [ev(seq, alphabet[i][0], dict(alphabet[i][1])) for seq, i in enumerate(combo)]
                    invocation = m.Invocation("req", "run", "executor", "offline-profile", ("python",),
                                              ("structured-events",), SHA_A)
                    return m.OfflineAdapter().run(invocation, events)
                self.assert_same(call, "sequence %r" % ([alphabet[i][0] for i in combo],))

    def test_offline_run_payload_and_identity_variations(self):
        tool_payloads = [{"tool_id": "t1"}, {"tool_id": ""}, {"tool_id": 1}, {"tool_id": "bad\x01"},
                         {"tool_id": "bad\x85"}, {"tool_id": "\ud800"}, {}, {"tool_id": S("t1")}]
        for start in tool_payloads:
            for finish in tool_payloads:
                def call(m, start=start, finish=finish):
                    events = [ev(0, "run_started"), ev(1, "tool_started", dict(start)),
                              ev(2, "tool_finished", dict(finish)), ev(3, "run_finished")]
                    invocation = m.Invocation("req", "run", "executor", "p", ("python",), (), SHA_A)
                    return m.OfflineAdapter().run(invocation, events)
                self.assert_same(call, "tools %r -> %r" % (start, finish))
        usages = [[{"tokens": 1}], [{"tokens": 1}, {"tokens": 2}], [{"tokens": 1}, {}]]
        for payloads in usages:
            def usage_call(m, payloads=payloads):
                events = [ev(0, "run_started")] + [ev(i + 1, "usage", dict(p)) for i, p in enumerate(payloads)]
                events.append(ev(len(events), "run_finished"))
                invocation = m.Invocation("req", "run", "executor", "p", ("python",), (), SHA_A)
                return m.OfflineAdapter().run(invocation, events)
            self.assert_same(usage_call, "usages %r" % (payloads,))
        for adapter_id in (None, "offline", "other"):
            def identity_call(m, adapter_id=adapter_id):
                invocation = m.Invocation("req", "run", "executor", "p", ("python",), (), SHA_A, adapter_id)
                return m.OfflineAdapter().run(invocation, [ev(0, "run_started"), ev(1, "run_finished")])
            self.assert_same(identity_call, "adapter_id %r" % (adapter_id,))
        for wrong in ({}, None, "run"):
            self.assert_same(lambda m, w=wrong: m.OfflineAdapter().run(w, [ev(0, "run_started")]), "invocation %r" % (wrong,))

    def test_registry_grid(self):
        def descriptor(m, adapter_id="offline", capabilities=("structured-events",), roles=("executor",)):
            return m.AdapterDescriptor(adapter_id, "v1", capabilities, roles, True)

        def invocation(m, adapter_id="offline", role="executor", capabilities=("structured-events",)):
            return m.Invocation("req", "run", role, "p", ("python",), capabilities, SHA_A, adapter_id)

        cases = {
            "resolve": lambda m: m.AdapterRegistry([descriptor(m)]).resolve("offline"),
            "resolve role": lambda m: m.AdapterRegistry([descriptor(m)]).resolve("offline", role="executor"),
            "resolve wrong role": lambda m: m.AdapterRegistry([descriptor(m)]).resolve("offline", role="planner"),
            "resolve unknown": lambda m: m.AdapterRegistry([descriptor(m)]).resolve("missing"),
            "duplicate": lambda m: m.AdapterRegistry([descriptor(m), descriptor(m)]),
            "register duplicate": lambda m: m.AdapterRegistry([descriptor(m)]).register(descriptor(m)),
            "validate": lambda m: m.AdapterRegistry([descriptor(m)]).validate_invocation(invocation(m)),
            "validate unbound": lambda m: m.AdapterRegistry([descriptor(m)]).validate_invocation(invocation(m, None)),
            "validate unknown": lambda m: m.AdapterRegistry([descriptor(m)]).validate_invocation(invocation(m, "x")),
            "validate role": lambda m: m.AdapterRegistry([descriptor(m)]).validate_invocation(invocation(m, role="planner")),
            "validate missing capability": lambda m: m.AdapterRegistry([descriptor(m)]).validate_invocation(
                invocation(m, capabilities=())),
            "validate extra capability": lambda m: m.AdapterRegistry([descriptor(m)]).validate_invocation(
                invocation(m, capabilities=("structured-events", "extra"))),
            "empty registry": lambda m: m.AdapterRegistry().resolve("offline"),
        }
        for label, call in cases.items():
            self.assert_same(call, label)


if __name__ == "__main__":
    unittest.main()
