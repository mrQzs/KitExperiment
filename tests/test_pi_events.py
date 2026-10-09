"""Executor tests for the Pi native event translator (``sentinelmesh/pi_events.py``).

These complement the planner oracle (``tests/test_pi_events_oracle.py``): every
rejection case here first proves a legal baseline, then changes exactly one field
and asserts a single ``pi.invalid_frame`` event with the expected ``line``,
``frame_type`` and ``reason``.  Check-order cases deliberately break two fields at
once and assert the earlier rule wins.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest
import unittest.mock

from sentinelmesh.adapter_protocol import AdapterError, Invocation, OfflineAdapter
from sentinelmesh.pi_events import (
    DIALECT,
    INVALID_FRAME,
    UNKNOWN_FRAME,
    USAGE_KEYS,
    PiNativeAdapter,
    PiTranslation,
    translate,
)

TOKEN_KEYS = tuple(dimension for _vendor, dimension in USAGE_KEYS)
ODD_TYPES = (UNKNOWN_FRAME, INVALID_FRAME)
SETTLED = {"type": "agent_settled"}


# --- synthetic frame builders ----------------------------------------------

def session(version=3, **overrides):
    frame = {"type": "session", "version": version, "id": "01a1-sess",
             "timestamp": "2026-10-09T00:00:00.000Z", "cwd": "/w"}
    frame.update(overrides)
    return frame


def usage(input_tokens=10, output_tokens=5, cache_read=0, cache_write=0, total=None):
    if total is None:
        total = input_tokens + output_tokens + cache_read + cache_write
    return {"input": input_tokens, "output": output_tokens, "cacheRead": cache_read,
            "cacheWrite": cache_write, "totalTokens": total, "cost": {"total": 0}}


def asst(**changes):
    message = {"role": "assistant",
               "content": [{"type": "thinking", "thinking": "t"}, {"type": "text", "text": "ok"}],
               "provider": "step", "model": "step-5-preview", "usage": usage(),
               "stopReason": "stop", "responseId": "resp-1", "thinkingLevel": "high"}
    message.update(changes)
    return {"type": "message_end", "message": message}


def asst_without(*keys):
    frame = asst()
    for key in keys:
        frame["message"].pop(key, None)
    return frame


def tool_start(tool_id="t-1", name="bash", **overrides):
    frame = {"type": "tool_execution_start", "toolCallId": tool_id, "toolName": name}
    frame.update(overrides)
    return frame


def tool_start_without(*keys):
    frame = tool_start()
    for key in keys:
        frame.pop(key, None)
    return frame


def tool_end(tool_id="t-1", name="bash", is_error=False, **overrides):
    frame = {"type": "tool_execution_end", "toolCallId": tool_id, "toolName": name, "isError": is_error}
    frame.update(overrides)
    return frame


def tool_end_without(*keys):
    frame = tool_end()
    for key in keys:
        frame.pop(key, None)
    return frame


def agent_end(will_retry=False, **overrides):
    frame = {"type": "agent_end", "willRetry": will_retry}
    frame.update(overrides)
    return frame


def agent_end_without(*keys):
    frame = agent_end()
    for key in keys:
        frame.pop(key, None)
    return frame


def system_end():
    return {"type": "message_end", "message": {"role": "system", "content": ""}}


def user_end():
    return {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}}


def tool_result_end():
    return {"type": "message_end", "message": {"role": "toolResult", "toolCallId": "t-1", "isError": False}}


def canonical_bytes(frame):
    text = json.dumps(frame, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=repr)
    return text.encode("utf-8", "surrogatepass")


def odd_events(translation):
    return [event for event in translation.events if event["type"] in ODD_TYPES]


# --- session header --------------------------------------------------------

class SessionHeader(unittest.TestCase):
    def test_session_id_not_text(self):
        self.assertEqual(odd_events(translate([session()])), [])
        odd = [e for e in translate([session(id=3)]).events if e["type"] == INVALID_FRAME]
        self._assert_one(odd, "session", 1, "session id")

    def test_session_timestamp_not_text(self):
        odd = [e for e in translate([session(timestamp=5)]).events if e["type"] == INVALID_FRAME]
        self._assert_one(odd, "session", 1, "session id")

    def test_session_version_as_text(self):
        odd = [e for e in translate([session(version="3")]).events if e["type"] == INVALID_FRAME]
        self._assert_one(odd, "session", 1, "session version")

    def _assert_one(self, odd, frame_type, line, reason_prefix):
        self.assertEqual(len(odd), 1)
        payload = odd[0]["payload"]
        self.assertEqual((payload["frame_type"], payload["line"]), (frame_type, line))
        self.assertTrue(payload["reason"].startswith(reason_prefix), payload["reason"])


# --- malformed known frames ------------------------------------------------

class MalformedFrames(unittest.TestCase):
    def _one_invalid(self, good, bad, reason_prefix, frame_type, line=2):
        self.assertEqual(odd_events(translate([session(), good])), [],
                         "the legal baseline must produce no unknown/invalid event")
        result = translate([session(), bad])
        odd = [e for e in result.events if e["type"] == INVALID_FRAME]
        self.assertEqual(len(odd), 1, "exactly one invalid frame: %r" % [e["type"] for e in result.events])
        payload = odd[0]["payload"]
        self.assertEqual(odd[0]["event_id"], "L%d" % line)
        self.assertEqual((payload["frame_type"], payload["line"]), (frame_type, line))
        self.assertTrue(payload["reason"].startswith(reason_prefix), payload["reason"])
        self.assertEqual(set(payload), {"frame_type", "line", "reason", "sha256", "bytes"})
        self.assertEqual(result.unknown, 1)
        self.assertFalse(result.usage_complete)

    def test_message_end_shapes(self):
        cases = [
            ("message is not an object", {"type": "message_end", "message": "x"}, "message is not"),
            ("role is not text", asst(role=5), "message role"),
            ("role outside the dialect", asst(role="tool"), "message role"),
            ("content is not a list", asst(content="ok"), "assistant content"),
            ("block is not an object", asst(content=["x"]), "assistant content block"),
            ("block type is not text", asst(content=[{"text": "x"}]), "assistant content block"),
            ("toolCall block without an id", asst(content=[{"type": "toolCall", "name": "bash"}]), "toolCall block"),
            ("toolCall id is empty", asst(content=[{"type": "toolCall", "id": ""}]), "toolCall block"),
            ("toolCall id is not text", asst(content=[{"type": "toolCall", "id": 7}]), "toolCall block"),
            ("stopReason is missing", asst_without("stopReason"), "stopReason"),
            ("stopReason is not text", asst(stopReason=5), "stopReason"),
            ("provider is missing", asst_without("provider"), "provider"),
            ("provider is not text", asst(provider=5), "provider"),
            ("model is missing", asst_without("model"), "model"),
            ("model is not text", asst(model=["m"]), "model"),
            ("responseId present but not text", asst(responseId=7), "responseId"),
            ("thinkingLevel present but not text", asst(thinkingLevel=True), "thinkingLevel"),
        ]
        for label, bad, prefix in cases:
            with self.subTest(case=label):
                self._one_invalid(asst(), bad, prefix, "message_end")

    def test_tool_execution_start_shapes(self):
        cases = [
            ("toolCallId is missing", tool_start_without("toolCallId"), "toolCallId"),
            ("toolCallId is empty", tool_start(tool_id=""), "toolCallId"),
            ("toolCallId is not text", tool_start(tool_id=7), "toolCallId"),
            ("toolName is missing", tool_start_without("toolName"), "toolCallId"),
            ("toolName is empty", tool_start(name=""), "toolCallId"),
            ("toolName is not text", tool_start(name=5), "toolCallId"),
        ]
        for label, bad, prefix in cases:
            with self.subTest(case=label):
                self._one_invalid(tool_start(), bad, prefix, "tool_execution_start")

    def test_tool_execution_end_shapes(self):
        cases = [
            ("isError is missing", tool_end_without("isError"), "isError"),
            ("isError is not a boolean", tool_end(is_error="no"), "isError"),
        ]
        for label, bad, prefix in cases:
            with self.subTest(case=label):
                self._one_invalid(tool_end(), bad, prefix, "tool_execution_end")

    def test_agent_end_shapes(self):
        cases = [
            ("willRetry is missing", agent_end_without("willRetry"), "willRetry"),
            ("willRetry is not a boolean", agent_end(will_retry="later"), "willRetry"),
        ]
        for label, bad, prefix in cases:
            with self.subTest(case=label):
                self._one_invalid(agent_end(), bad, prefix, "agent_end")


# --- check order -----------------------------------------------------------

class CheckOrder(unittest.TestCase):
    def _first_invalid_reason(self, frames):
        odd = [e for e in translate(frames).events if e["type"] == INVALID_FRAME]
        self.assertEqual(len(odd), 1)
        return odd[0]["payload"]

    def test_second_session_beats_a_wrong_version(self):
        self.assertEqual(odd_events(translate([session()])), [])
        payload = self._first_invalid_reason([session(), session(version=2)])
        self.assertEqual((payload["frame_type"], payload["line"]), ("session", 2))
        self.assertTrue(payload["reason"].startswith("second session"), payload["reason"])

    def test_role_beats_content(self):
        self.assertEqual(odd_events(translate([session(), asst()])), [])
        payload = self._first_invalid_reason(
            [session(), {"type": "message_end", "message": {"role": "tool", "content": "notlist"}}])
        self.assertTrue(payload["reason"].startswith("message role"), payload["reason"])

    def test_stop_reason_beats_provider(self):
        self.assertEqual(odd_events(translate([session(), asst()])), [])
        payload = self._first_invalid_reason([session(), asst(stopReason=5, provider=5)])
        self.assertTrue(payload["reason"].startswith("stopReason"), payload["reason"])


# --- ignored counts --------------------------------------------------------

class IgnoredCounts(unittest.TestCase):
    def test_each_ignored_type_is_counted_and_message_end_sums_roles(self):
        frames = [session(),
                  {"type": "agent_start"}, {"type": "agent_start"},
                  {"type": "turn_start"}, {"type": "turn_start"},
                  {"type": "message_start"}, {"type": "message_start"},
                  {"type": "message_update"}, {"type": "message_update"},
                  {"type": "tool_execution_update"}, {"type": "tool_execution_update"},
                  system_end(), user_end(), tool_result_end(),
                  system_end(), user_end(), tool_result_end(),
                  {"type": "turn_end"}, {"type": "turn_end"},
                  agent_end(), agent_end(),
                  asst(), SETTLED]
        result = translate(frames)
        self.assertEqual(result.unknown, 0)
        self.assertEqual(result.ignored, {
            "agent_start": 2, "turn_start": 2, "message_start": 2,
            "message_update": 2, "tool_execution_update": 2,
            "message_end": 6, "turn_end": 2, "agent_end": 2,
        })
        # message_end counts system + user + toolResult (six), not the assistant one.
        self.assertEqual(result.ignored["message_end"], 6)

    def test_unknown_and_invalid_frames_are_never_ignored(self):
        frames = [session(),
                  {"type": "agent_start"},
                  {"type": "compaction_end", "result": {"usage": usage(5, 5)}},
                  {"type": "message_end", "message": "x"},
                  {"type": "turn_start"},
                  asst(), SETTLED]
        result = translate(frames)
        self.assertEqual(result.unknown, 2)
        self.assertEqual(result.ignored, {"agent_start": 1, "turn_start": 1})
        self.assertNotIn("compaction_end", result.ignored)
        self.assertNotIn("message_end", result.ignored)


# --- event identity --------------------------------------------------------

class EventIdentity(unittest.TestCase):
    def test_ids_and_seq_across_a_multi_frame_stream(self):
        frames = [session(),                        # L1 -> run_started
                  {"type": "agent_start"},           # L2 -> ignored
                  asst(),                            # L3 -> assistant + usage
                  tool_start("t-1"),                 # L4 -> tool_started
                  tool_end("t-1"),                   # L5 -> tool_finished
                  SETTLED]                           # L6 -> run_finished
        result = translate(frames)
        self.assertEqual([e["event_id"] for e in result.events],
                         ["L1", "L3.assistant", "L3.usage", "L4", "L5", "L6"])
        self.assertEqual([e["seq"] for e in result.events], [0, 1, 2, 3, 4, 5])
        for event in result.events:
            self.assertEqual(set(event), {"seq", "event_id", "type", "source", "payload"})
            self.assertEqual(event["source"], "pi")


# --- unknown-frame evidence ------------------------------------------------

class UnknownFrameEvidence(unittest.TestCase):
    def test_payload_carries_the_canonical_digest(self):
        frames = [
            {"type": "future_frame", "note": "café ☕ 日本語", "n": 3, "flag": True},
            {"type": "weird", "nested": {"a": [1, 2, {"b": "deep"}], "c": {"d": None}}},
        ]
        for frame in frames:
            with self.subTest(frame_type=frame["type"]):
                result = translate([session(), frame])
                odd = [e for e in result.events if e["type"] == UNKNOWN_FRAME]
                self.assertEqual(len(odd), 1)
                payload = odd[0]["payload"]
                self.assertEqual(set(payload), {"frame_type", "line", "reason", "sha256", "bytes"})
                self.assertEqual(payload["frame_type"], frame["type"])
                self.assertEqual(payload["line"], 2)
                self.assertEqual(payload["reason"], "frame type outside the dialect")
                data = canonical_bytes(frame)
                self.assertEqual(payload["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(payload["bytes"], len(data))
                self.assertNotIn(frame["type"], payload)  # the original frame is not embedded


# --- usage -----------------------------------------------------------------

class Usage(unittest.TestCase):
    def test_complete_turns_false_and_stays_false(self):
        bad_usage = {"input": 5, "output": 5, "cacheRead": 0, "cacheWrite": 0}  # totalTokens missing
        frames = [session(),
                  asst(usage=usage(10, 5)),   # L2 legal
                  asst(usage=bad_usage),       # L3 illegal
                  asst(usage=usage(100, 1)),   # L4 legal
                  SETTLED]
        result = translate(frames)
        self.assertEqual(result.unknown, 0, "an illegal usage is not an invalid frame")
        self.assertFalse(result.usage_complete)
        # The reference sum still adds the two legal messages.
        self.assertEqual(result.usage, {"input_tokens": 110, "output_tokens": 6,
                                        "cache_read_tokens": 0, "cache_write_tokens": 0,
                                        "total_tokens": 116})
        usage_events = [e["payload"] for e in result.events if e["type"] == "usage"]
        self.assertEqual(len(usage_events), 3)
        self.assertTrue(usage_events[0]["complete"])
        for key in TOKEN_KEYS:
            self.assertIn(key, usage_events[0])
        for payload in usage_events[1:]:
            self.assertFalse(payload["complete"])
            for key in TOKEN_KEYS:
                self.assertNotIn(key, payload)
        assistants = [e["payload"] for e in result.events if e["type"] == "assistant"]
        self.assertEqual(len(assistants), 3)
        self.assertIsNone(assistants[1]["usage"])
        self.assertEqual(assistants[0]["usage"]["input_tokens"], 10)
        self.assertEqual(assistants[2]["usage"]["input_tokens"], 100)


# --- input contract --------------------------------------------------------

class InputContract(unittest.TestCase):
    def test_a_generator_is_consumed_once(self):
        generator = (frame for frame in [session(), asst(), SETTLED])
        result = translate(generator)
        self.assertEqual(result.frames, 3)
        self.assertEqual(list(generator), [], "the generator must be exhausted")

    def test_frames_must_be_an_iterable_of_frames(self):
        for bad in ("[]", b"[]", bytearray(b"[]"), 7, None):
            with self.subTest(frames=repr(bad)):
                with self.assertRaises(AdapterError) as raised:
                    translate(bad)
                self.assertEqual(raised.exception.code, "E_ADAPTER_EVENT")

    def test_pi_translation_is_a_frozen_dataclass_with_a_tuple_of_events(self):
        result = translate([session(), asst(), SETTLED])
        self.assertIsInstance(result, PiTranslation)
        self.assertIsInstance(result.events, tuple)
        self.assertTrue(all(isinstance(event, dict) for event in result.events))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.frames = 999


# --- collected facts -------------------------------------------------------

class CollectedFacts(unittest.TestCase):
    def test_models_cycles_errors_and_last_stop_reason(self):
        frames = [session(),
                  {"type": "agent_start"},
                  asst(model="b", provider="step"),
                  asst(model="a", provider="zeta"),
                  asst(model="b", provider="step"),   # duplicate (step, b)
                  tool_end("t-1", is_error=True),
                  tool_end("t-2", is_error=False),
                  tool_end("t-3", is_error=True),
                  {"type": "agent_start"},
                  asst(stopReason="length", model="c", provider="step"),
                  SETTLED]
        result = translate(frames)
        self.assertEqual(result.models, (("step", "b"), ("step", "c"), ("zeta", "a")))
        self.assertEqual(result.agent_cycles, 2)
        self.assertEqual(result.tool_errors, 2)
        self.assertEqual(result.last_stop_reason, "length")


# --- adapter surface -------------------------------------------------------

def invocation(adapter_id="pi"):
    return Invocation("req-1", "run-1", "executor", "profile-1",
                      ("pi", "--mode", "json"), ("streaming_events",), "a" * 64, adapter_id=adapter_id)


class AdapterSurface(unittest.TestCase):
    def test_probe_reports_the_dialect(self):
        adapter = PiNativeAdapter()
        self.assertEqual(adapter.adapter_id, "pi")
        self.assertEqual(adapter.protocol_version, DIALECT)
        self.assertEqual(adapter.probe(), {"adapter_id": "pi", "protocol_version": DIALECT,
                                           "offline": True, "process_started": False,
                                           "network_used": False, "dialect": DIALECT})

    def test_run_accepts_unbound_and_rejects_foreign_adapters(self):
        adapter = PiNativeAdapter()
        frames = [session(), asst(), SETTLED]
        self.assertEqual(adapter.run(invocation(None), frames).status, "SUCCEEDED")
        for foreign in ("offline", "claude"):
            with self.subTest(adapter_id=foreign):
                with self.assertRaises(AdapterError) as raised:
                    adapter.run(invocation(foreign), frames)
                self.assertEqual(raised.exception.code, "E_ADAPTER_IDENTITY")

    def test_run_hands_the_reducer_copies_of_the_translation(self):
        adapter = PiNativeAdapter()
        frames = [session(), asst(), SETTLED]
        captured = {}
        real_run = OfflineAdapter.run

        def spy(inner_self, inv, events):
            captured["events"] = events
            return real_run(inner_self, inv, events)

        with unittest.mock.patch.object(OfflineAdapter, "run", spy):
            result = adapter.run(invocation("pi"), frames)
        self.assertEqual(result.status, "SUCCEEDED")
        fresh = translate(frames)
        self.assertEqual(len(captured["events"]), len(fresh.events))
        for got, own in zip(captured["events"], fresh.events):
            self.assertEqual(got, own, "the reducer must see equal events")
            self.assertIsNot(got, own, "the reducer must see copies, not the translator's objects")
            self.assertIsNot(got["payload"], own["payload"])


if __name__ == "__main__":
    unittest.main()
