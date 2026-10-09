"""Planner oracle for the Pi native event translation (``.agent/design/pi-native-events.md``).

Two kinds of evidence, kept apart:

* **captured** -- the reduced real streams in ``tests/fixtures/pi_native/``: Pi's
  own ``--mode json`` output as the dispatcher recorded it (pi 1.0.1 on Windows,
  1.0.2 and 1.0.4 on macOS), reduced by ``reduce.py`` without touching one
  structural field, each with the dispatcher's independent summary in its
  ``.meta.json``.  The translation has to agree with that summary on every number
  it reports, and the adapter has to settle the complete ones and refuse to settle
  the truncated ones;
* **synthetic** -- hand-built frames for the paths no captured run exercises
  (unknown and malformed frames, illegal usage values, non-``stop`` terminals,
  retry cycles, pairing violations).  They prove the rules, not Pi's behaviour.
"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

from sentinelmesh.adapter_protocol import AdapterError, Invocation, OfflineAdapter, normalize_events
from sentinelmesh.pi_events import (
    DIALECT,
    INVALID_FRAME,
    SOURCE,
    TERMINAL_FRAME,
    UNKNOWN_FRAME,
    USAGE_KEYS,
    PiNativeAdapter,
    PiTranslation,
    translate,
)
from sentinelmesh.protocols import MAX_SAFE_INTEGER, strict_json_loads

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pi_native"
TOKEN_KEYS = tuple(dimension for _vendor, dimension in USAGE_KEYS)
EVENT_KEYS = {"seq", "event_id", "type", "source", "payload"}

INVOCATION = Invocation("req-1", "run-1", "executor", "profile-1", ("pi", "--mode", "json"),
                        ("streaming_events",), "a" * 64, adapter_id="pi")
UNBOUND = Invocation("req-1", "run-1", "executor", "profile-1", ("pi", "--mode", "json"),
                     ("streaming_events",), "a" * 64)


# --- synthetic frames -------------------------------------------------------

def session(version=3, **overrides):
    frame = {"type": "session", "version": version, "id": "01a1-session", "timestamp": "2026-10-09T00:00:00.000Z",
             "cwd": "/w"}
    frame.update(overrides)
    return frame


def usage(input_tokens=10, output_tokens=5, cache_read=0, cache_write=0, total=None):
    if total is None:
        # The total of the legal counters; an illegal one is deliberately left out of it.
        total = sum(value for value in (input_tokens, output_tokens, cache_read, cache_write)
                    if type(value) is int)
    return {"input": input_tokens, "output": output_tokens, "cacheRead": cache_read, "cacheWrite": cache_write,
            "totalTokens": total, "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}


def assistant(stop="stop", tokens=None, tool_calls=(), **overrides):
    content = [{"type": "thinking", "thinking": "…", "thinkingSignature": "sig"}, {"type": "text", "text": "ok"}]
    content += [{"type": "toolCall", "id": tool_id, "name": "bash", "arguments": {"command": "true"}}
                for tool_id in tool_calls]
    message = {"role": "assistant", "content": content, "api": "anthropic-messages", "provider": "step",
               "model": "step-5-preview", "usage": usage() if tokens is None else tokens, "stopReason": stop,
               "timestamp": 1, "responseId": "resp-1", "rawStopReason": "end_turn", "thinkingLevel": "high"}
    message.update(overrides)
    return {"type": "message_end", "message": message}


def tool_start(tool_id="t-1", name="bash"):
    return {"type": "tool_execution_start", "toolCallId": tool_id, "toolName": name, "args": {"command": "true"}}


def tool_end(tool_id="t-1", name="bash", error=False):
    return {"type": "tool_execution_end", "toolCallId": tool_id, "toolName": name,
            "result": {"content": [{"type": "text", "text": ""}]}, "isError": error}


def tool_result(tool_id="t-1"):
    return {"type": "message_end", "message": {"role": "toolResult", "toolCallId": tool_id, "toolName": "bash",
                                               "content": [{"type": "text", "text": ""}], "isError": False,
                                               "timestamp": 1}}


def agent_end(will_retry=False):
    return {"type": "agent_end", "messages": [], "willRetry": will_retry}


def update():
    return {"type": "message_update", "usage": usage(999, 999), "assistantMessageEvent": {"type": "text_delta"}}


SETTLED = {"type": TERMINAL_FRAME}
SYSTEM = {"type": "message_end", "message": {"role": "system", "content": "", "sections": {}}}
USER = {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": "do"}]}}


def run(*middle, settle=True):
    """A complete minimal run around ``middle``: header, one cycle, one turn."""
    frames = [session(), {"type": "agent_start"}, {"type": "turn_start"},
              {"type": "message_start", "message": {"role": "system"}}, SYSTEM, USER, *middle,
              {"type": "turn_end", "message": {}, "toolResults": []}, agent_end()]
    if settle:
        frames.append(SETTLED)
    return frames


def kinds(translation):
    return [event["type"] for event in translation.events]


def last_usage(translation):
    return [event["payload"] for event in translation.events if event["type"] == "usage"][-1]


# --- captured fixtures ------------------------------------------------------

def fixtures():
    found = []
    for meta_path in sorted(FIXTURES.glob("*.meta.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        stream = FIXTURES / (meta["fixture"] + ".jsonl")
        found.append((meta, stream))
    return found


def frames_of(path: Path):
    raw = path.read_bytes()
    segments = raw.split(b"\n")
    if segments and segments[-1] == b"":
        segments.pop()
    # Every line of a real stream parses under the protocol's own strict parser.
    return [strict_json_loads(segment) for segment in segments]


class CapturedFixtures(unittest.TestCase):
    def test_five_fixtures_three_complete_two_truncated(self):
        found = fixtures()
        self.assertEqual(len(found), 5)
        complete = [meta for meta, _stream in found if meta["dispatcher_summary"]["complete"] is True]
        self.assertEqual(len(complete), 3)
        self.assertEqual(sorted({meta["source"]["pi_version"] for meta in complete}), ["1.0.1", "1.0.2", "1.0.4"])
        self.assertEqual(sorted({meta["source"]["platform"][:5] for meta in complete}), ["Windo", "macOS"])
        for meta, stream in found:
            with self.subTest(fixture=meta["fixture"]):
                self.assertLessEqual(stream.stat().st_size, 400 * 1024, "a fixture is larger than 400 KB")
                self.assertEqual(meta["dialect"], DIALECT)
                self.assertEqual(meta["reduction"]["kept_lines"], len(frames_of(stream)))

    def test_translation_agrees_with_the_dispatcher(self):
        for meta, stream in fixtures():
            with self.subTest(fixture=meta["fixture"]):
                summary = meta["dispatcher_summary"]
                translation = translate(frames_of(stream))
                self.assertEqual(translation.dialect, DIALECT)
                self.assertEqual(translation.unknown, 0)
                self.assertTrue(translation.usage_complete)
                self.assertEqual(translation.frames, meta["reduction"]["kept_lines"])
                for vendor, dimension in USAGE_KEYS:
                    self.assertEqual(translation.usage[dimension], summary["usage"][vendor], dimension)
                self.assertEqual(translation.assistant_messages, summary["assistant_messages"])
                self.assertEqual(translation.tool_calls, sum(summary["tool_calls"].values()))
                self.assertEqual(translation.tool_errors, summary["tool_errors"])
                self.assertEqual(translation.last_stop_reason, summary["final_stop_reason"])
                self.assertEqual(translation.models,
                                 tuple(sorted((m["provider"], m["model"]) for m in summary["actual_models"])))
                self.assertEqual(translation.agent_cycles, summary["event_counts"]["agent_start"])
                self.assertEqual(translation.terminal, TERMINAL_FRAME if summary["complete"] else None)
                # Every frame either made an event or was counted as ignored.
                producing = (1 + summary["event_counts"].get("tool_execution_start", 0)
                             + summary["event_counts"].get("tool_execution_end", 0)
                             + summary["assistant_messages"] + (1 if summary["complete"] else 0))
                self.assertEqual(sum(translation.ignored.values()) + producing, translation.frames)
                self.assertEqual(translation.ignored["message_end"],
                                 summary["event_counts"]["message_end"] - summary["assistant_messages"])
                # The event stream itself is a valid normalized stream without unknowns.
                normalized = normalize_events(list(translation.events), expected_source=SOURCE)
                self.assertEqual([e.seq for e in normalized], list(range(len(normalized))))
                self.assertEqual(len({e.event_id for e in normalized}), len(normalized))

    def test_the_adapter_settles_complete_streams_and_not_truncated_ones(self):
        adapter = PiNativeAdapter()
        for meta, stream in fixtures():
            with self.subTest(fixture=meta["fixture"]):
                summary = meta["dispatcher_summary"]
                result = adapter.run(INVOCATION, frames_of(stream))
                self.assertEqual(result.invocation_sha256, INVOCATION.digest())
                if summary["complete"]:
                    self.assertEqual((result.settled, result.status, result.raw_complete), (True, "SUCCEEDED", True))
                    expected = {dimension: summary["usage"][vendor] for vendor, dimension in USAGE_KEYS}
                    expected.update({"assistant_messages": summary["assistant_messages"],
                                     "tool_calls": sum(summary["tool_calls"].values()), "complete": True})
                    self.assertEqual(dict(result.usage), expected)
                    self.assertEqual(result.events[-1].event_type, "run_finished")
                else:
                    self.assertEqual((result.settled, result.status, result.raw_complete), (False, "UNKNOWN", False))
                    self.assertEqual(dict(result.usage), {"status": "unknown"})
                    self.assertNotIn(result.events[-1].event_type, {"run_finished", "error"})


# --- mapping rules ----------------------------------------------------------

class Mapping(unittest.TestCase):
    def test_a_minimal_run_maps_to_the_four_events(self):
        translation = translate(run(assistant()))
        self.assertEqual(kinds(translation), ["run_started", "assistant", "usage", "run_finished"])
        for event in translation.events:
            self.assertEqual(set(event), EVENT_KEYS)
            self.assertEqual(event["source"], SOURCE)
        self.assertEqual([e["seq"] for e in translation.events], [0, 1, 2, 3])
        self.assertEqual([e["event_id"] for e in translation.events], ["L1", "L7.assistant", "L7.usage", "L10"])
        started, said, used, finished = (e["payload"] for e in translation.events)
        self.assertEqual(started, {"dialect": DIALECT, "session_id": "01a1-session", "session_version": 3,
                                   "timestamp": "2026-10-09T00:00:00.000Z"})
        self.assertEqual(said, {"stop_reason": "stop", "provider": "step", "model": "step-5-preview",
                                "response_id": "resp-1", "thinking_level": "high", "text_blocks": 1,
                                "thinking_blocks": 1, "tool_calls": [],
                                "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
                                          "cache_write_tokens": 0, "total_tokens": 15}})
        self.assertEqual(used, {"assistant_messages": 1, "tool_calls": 0, "complete": True, "input_tokens": 10,
                                "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 0,
                                "total_tokens": 15})
        self.assertEqual(finished, {"stop_reason": "stop", "assistant_messages": 1, "tool_calls": 0,
                                    "agent_cycles": 1})
        self.assertEqual(translation.ignored, {"agent_start": 1, "turn_start": 1, "message_start": 1,
                                               "message_end": 2, "turn_end": 1, "agent_end": 1})
        self.assertEqual((translation.frames, translation.unknown, translation.usage_complete),
                         (10, 0, True))
        self.assertEqual((translation.assistant_messages, translation.tool_calls, translation.tool_errors,
                          translation.agent_cycles, translation.terminal, translation.last_stop_reason,
                          translation.models),
                         (1, 0, 0, 1, TERMINAL_FRAME, "stop", (("step", "step-5-preview"),)))

    def test_tools_map_by_call_id_and_usage_accumulates(self):
        frames = run(assistant("toolUse", usage(100, 20, 1000, 0), tool_calls=("t-1", "t-2")),
                     tool_start("t-1"), tool_start("t-2", "read"), tool_end("t-1"), tool_end("t-2", "read", True),
                     tool_result("t-1"), tool_result("t-2"), update(), update(),
                     assistant("stop", usage(200, 30, 2000, 5)))
        translation = translate(frames)
        self.assertEqual(kinds(translation), ["run_started", "assistant", "usage", "tool_started", "tool_started",
                                              "tool_finished", "tool_finished", "assistant", "usage", "run_finished"])
        tools = [e["payload"] for e in translation.events if e["type"].startswith("tool_")]
        self.assertEqual(tools, [{"tool_id": "t-1", "tool": "bash"}, {"tool_id": "t-2", "tool": "read"},
                                 {"tool_id": "t-1", "tool": "bash", "is_error": False},
                                 {"tool_id": "t-2", "tool": "read", "is_error": True}])
        first, second = [e["payload"] for e in translation.events if e["type"] == "usage"]
        self.assertEqual((first["input_tokens"], first["tool_calls"], first["assistant_messages"]), (100, 0, 1))
        self.assertEqual(second, {"assistant_messages": 2, "tool_calls": 2, "complete": True, "input_tokens": 300,
                                  "output_tokens": 50, "cache_read_tokens": 3000, "cache_write_tokens": 5,
                                  "total_tokens": 1120 + 2235})
        said = [e["payload"] for e in translation.events if e["type"] == "assistant"]
        self.assertEqual(said[0]["tool_calls"], ["t-1", "t-2"])
        self.assertEqual((translation.tool_calls, translation.tool_errors), (2, 1))
        # Snapshots and mirrors never count: the update frames carried 999/999.
        self.assertEqual(translation.usage, {key: second[key] for key in TOKEN_KEYS})
        self.assertEqual(translation.ignored["message_update"], 2)

    def test_optional_assistant_fields_may_be_absent(self):
        frame = assistant()
        del frame["message"]["responseId"]
        del frame["message"]["thinkingLevel"]
        translation = translate(run(frame))
        said = translation.events[1]["payload"]
        self.assertEqual((said["response_id"], said["thinking_level"]), (None, None))
        self.assertEqual(translation.unknown, 0)

    def test_models_are_collected_sorted_and_unique(self):
        frames = run(assistant("toolUse", model="b-model"), assistant("toolUse", provider="alpha"),
                     assistant("stop", model="b-model"))
        self.assertEqual(translate(frames).models, (("alpha", "step-5-preview"), ("step", "b-model")))

    def test_translation_is_deterministic_and_fresh(self):
        frames = run(assistant("toolUse", tool_calls=("t-1",)), tool_start(), tool_end(), tool_result(), assistant())
        first, second = translate(frames), translate(frames)
        self.assertEqual(first, second)
        first.events[1]["payload"]["stop_reason"] = "tampered"
        self.assertEqual(translate(frames), second)
        self.assertIsInstance(first, PiTranslation)


# --- fail closed ------------------------------------------------------------

class FailClosed(unittest.TestCase):
    def odd(self, frames, kind, reason, line, frame_type):
        translation = translate(frames)
        odd = [e for e in translation.events if e["type"] in (UNKNOWN_FRAME, INVALID_FRAME)]
        self.assertEqual(len(odd), 1, kinds(translation))
        event = odd[0]
        self.assertEqual(event["type"], kind)
        self.assertEqual(event["event_id"], "L%d" % line)
        payload = event["payload"]
        self.assertEqual(set(payload), {"frame_type", "line", "reason", "sha256", "bytes"})
        self.assertEqual((payload["frame_type"], payload["line"], payload["reason"]), (frame_type, line, reason))
        self.assertRegex(payload["sha256"], "^[0-9a-f]{64}$")
        self.assertIsInstance(payload["bytes"], int)
        self.assertEqual(translation.unknown, 1)
        self.assertFalse(translation.usage_complete)
        return translation

    def test_a_frame_type_outside_the_dialect_is_unknown_and_the_run_does_not_settle(self):
        frames = run(assistant("toolUse"), {"type": "compaction_end", "result": {"usage": usage(5, 5)}}, assistant())
        translation = self.odd(frames, UNKNOWN_FRAME, "frame type outside the dialect", 8, "compaction_end")
        self.assertEqual(translation.usage["input_tokens"], 20, "legal messages are still summed as a reference")
        self.assertNotIn("input_tokens", last_usage(translation))
        result = PiNativeAdapter().run(INVOCATION, frames)
        self.assertEqual((result.settled, result.status, result.raw_complete), (False, "UNKNOWN", False))
        self.assertEqual(dict(result.usage), {"status": "unknown"})
        self.assertEqual([e.event_type for e in result.events].count("unknown"), 1)

    def test_malformed_known_frames_are_invalid(self):
        bad_usage = assistant()
        bad_usage["message"]["usage"] = "lots"
        cases = {
            "message without an object": ([{"type": "message_end", "message": "x"}], "message is not an object"),
            "role outside the dialect": ([{"type": "message_end", "message": {"role": "tool"}}],
                                         "message role outside the dialect"),
            "content not a list": ([assistant(content="ok")], "assistant content is not a list"),
            "block without a type": ([assistant(content=[{"text": "x"}])], "assistant content block is malformed"),
            "block type not text": ([assistant(content=[{"type": 5}])], "assistant content block is malformed"),
            "toolCall without an id": ([assistant(content=[{"type": "toolCall", "id": ""}])],
                                       "toolCall block without an id"),
            "stopReason not text": ([assistant(stopReason=None)], "stopReason is not text"),
            "provider not text": ([assistant(provider=5)], "provider is not text"),
            "model not text": ([assistant(model=["m"])], "model is not text"),
            "responseId not text": ([assistant(responseId=7)], "responseId is not text"),
            "thinkingLevel not text": ([assistant(thinkingLevel=True)], "thinkingLevel is not text"),
            "tool start without an id": ([tool_start("")], "toolCallId or toolName is not text"),
            "tool start with an empty name": ([tool_start("t-1", "")], "toolCallId or toolName is not text"),
            "tool end with a numeric id": ([tool_end(7)], "toolCallId or toolName is not text"),
            "tool end with a numeric name": ([tool_end("t-1", 5)], "toolCallId or toolName is not text"),
            "tool end without a name": ([{"type": "tool_execution_end", "toolCallId": "t", "isError": False}],
                                        "toolCallId or toolName is not text"),
            "isError not a boolean": ([tool_end("t-1", error="no")], "isError is not a boolean"),
            "willRetry not a boolean": ([agent_end("later")], "willRetry is not a boolean"),
        }
        for label, (frames, reason) in cases.items():
            with self.subTest(case=label):
                translation = translate([session()] + frames)
                odd = [e for e in translation.events if e["type"] == INVALID_FRAME]
                self.assertEqual(len(odd), 1, kinds(translation))
                # Reasons are compared whole (planner completion after the review of
                # attempt-001, F4): a prefix matched more than one reason.
                self.assertEqual(odd[0]["payload"]["reason"], reason)
                self.assertEqual(odd[0]["payload"]["frame_type"], frames[0]["type"])
        # The usage shape is checked per value, not per key presence only.
        self.assertEqual(kinds(translate([session(), bad_usage])), ["run_started", "assistant", "usage"])

    def test_session_rules(self):
        self.odd([session(version=2), assistant()], INVALID_FRAME, "session version is not 3", 1, "session")
        self.odd([session(version=True), assistant()], INVALID_FRAME, "session version is not 3", 1, "session")
        self.odd([session(id=3)], INVALID_FRAME, "session id or timestamp is not text", 1, "session")
        self.odd([session(), session()], INVALID_FRAME, "second session header", 2, "session")
        # The header has to be the first frame, not merely the first frame that makes an event.
        self.odd([{"type": "agent_start"}, session()], INVALID_FRAME, "session header is not the first frame", 2,
                 "session")

    def test_a_malformed_first_header_still_counts_as_the_header(self):
        # Planner decision N10 after the review of attempt-001 (F3): a header that
        # failed its shape check was still a header, so the next one is the second.
        translation = translate([session(version=2), session()])
        self.assertEqual([(e["type"], e["payload"]["reason"]) for e in translation.events],
                         [(INVALID_FRAME, "session version is not 3"), (INVALID_FRAME, "second session header")])
        self.assertEqual(translation.unknown, 2)

    def test_a_frame_after_the_terminal_is_unknown_and_the_run_is_refused(self):
        frames = run(assistant()) + [{"type": "agent_start"}]
        self.odd(frames, UNKNOWN_FRAME, "frame after agent_settled", 11, "agent_start")
        with self.assertRaises(AdapterError) as raised:
            PiNativeAdapter().run(INVOCATION, frames)
        self.assertEqual(raised.exception.code, "E_ADAPTER_SETTLEMENT")

    def test_the_object_and_type_checks_come_before_the_terminal_gate(self):
        # Planner decision N15 after the review of attempt-001 (F2): a frame that is
        # not an object, or whose type is not text, is invalid wherever it appears,
        # including after agent_settled; only a well-formed frame there is "after".
        frames = run(assistant()) + ["x", {"type": 7}, {"type": "agent_start"}]
        translation = translate(frames)
        tail = [(e["type"], e["payload"]["reason"]) for e in translation.events[-3:]]
        self.assertEqual(tail, [(INVALID_FRAME, "frame is not an object"), (INVALID_FRAME, "type is not text"),
                                (UNKNOWN_FRAME, "frame after agent_settled")])
        with self.assertRaises(AdapterError) as raised:
            PiNativeAdapter().run(INVOCATION, frames)
        self.assertEqual(raised.exception.code, "E_ADAPTER_SETTLEMENT")

    def test_frames_that_are_not_objects_are_invalid(self):
        self.odd([session(), "agent_start"], INVALID_FRAME, "frame is not an object", 2, None)
        self.odd([session(), ["agent_start"]], INVALID_FRAME, "frame is not an object", 2, None)
        self.odd([session(), None], INVALID_FRAME, "frame is not an object", 2, None)
        self.odd([session(), {"type": 7}], INVALID_FRAME, "type is not text", 2, None)
        self.odd([session(), {"kind": "agent_start"}], INVALID_FRAME, "type is not text", 2, None)

    def test_strings_copied_into_a_payload_are_bounded_by_the_reducer(self):
        # Planner decision N4 after the review of attempt-001 (F5): the translator
        # copies identifiers (type, toolCallId, toolName, ...) as they are and never
        # raises; a string longer than the protocol's payload bound is refused by the
        # reducer's own payload check, which is a refusal of the stream, not a
        # settlement.  Real streams never get here: read_stream parses one line
        # within 1 MiB, and Pi's identifiers are short.
        for label, frame in {"a huge frame type": {"type": "x" * 1_100_000},
                             "a huge tool id": tool_start("t" * 1_100_000)}.items():
            with self.subTest(case=label):
                frames = [session(), frame]
                translation = translate(frames)
                self.assertEqual(len(translation.events), 2)
                with self.assertRaises(AdapterError) as raised:
                    PiNativeAdapter().run(INVOCATION, frames)
                self.assertEqual(raised.exception.code, "E_ADAPTER_EVENT")

    def test_a_frame_that_cannot_be_encoded_at_all_still_becomes_an_event(self):
        # Planner decision N16 after the review of attempt-001 (F6): when neither the
        # canonical JSON nor repr() can be produced, the digest is that of no bytes.
        class Unprintable:
            def __repr__(self):
                raise RuntimeError("no repr")

        translation = translate([session(), {"type": "future_frame", "value": Unprintable()}])
        payload = translation.events[1]["payload"]
        self.assertEqual((translation.events[1]["type"], payload["frame_type"], payload["bytes"]),
                         (UNKNOWN_FRAME, "future_frame", 0))
        self.assertEqual(payload["sha256"], hashlib.sha256(b"").hexdigest())

    def test_an_odd_frame_payload_is_bounded_whatever_the_frame_holds(self):
        huge = {"type": "future_frame", "blob": "x" * 2_000_000, "nested": {"a": [1, 2, {"b": "y" * 100_000}]}}
        translation = self.odd([session(), huge], UNKNOWN_FRAME, "frame type outside the dialect", 2, "future_frame")
        payload = translation.events[1]["payload"]
        self.assertLess(len(json.dumps(payload)), 400)
        self.assertEqual(payload["bytes"], len(json.dumps(huge, sort_keys=True, ensure_ascii=False,
                                                          separators=(",", ":")).encode("utf-8")))
        normalize_events(list(translation.events), expected_source=SOURCE, preserve_unknown=True)
        unsortable = {"type": "future_frame", 1: "a", "b": object()}
        translation = translate([session(), unsortable])
        self.assertEqual(translation.events[1]["type"], UNKNOWN_FRAME)

    def test_illegal_usage_values_leave_the_whole_stream_unrecorded(self):
        cases = {
            "a boolean": usage(True, 5),
            "a float": usage(10.0, 5),
            "a negative count": usage(-1, 5),
            "an unsafe integer": usage(MAX_SAFE_INTEGER + 1, 5),
            "a missing counter": {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0},
            "a text count": usage("10", 5),
            "no usage object": None,
        }
        for label, tokens in cases.items():
            with self.subTest(usage=label):
                bad = assistant("toolUse", tokens=tokens)
                if tokens is None:
                    del bad["message"]["usage"]
                frames = run(assistant("toolUse", usage(7, 3)), bad, assistant("stop", usage(100, 1)))
                translation = translate(frames)
                self.assertEqual(translation.unknown, 0, "an illegal usage is not an invalid frame")
                self.assertFalse(translation.usage_complete)
                self.assertEqual(translation.assistant_messages, 3)
                self.assertEqual(translation.usage["input_tokens"], 107, "legal messages are summed as a reference")
                payloads = [e["payload"] for e in translation.events if e["type"] == "usage"]
                self.assertIn("input_tokens", payloads[0])
                self.assertEqual(payloads[1], {"assistant_messages": 2, "tool_calls": 0, "complete": False})
                self.assertEqual(payloads[2], {"assistant_messages": 3, "tool_calls": 0, "complete": False})
                said = [e["payload"] for e in translation.events if e["type"] == "assistant"][1]
                self.assertIsNone(said["usage"])
                result = PiNativeAdapter().run(INVOCATION, frames)
                self.assertEqual((result.settled, result.status, result.raw_complete), (True, "SUCCEEDED", True))
                self.assertEqual(dict(result.usage), {"assistant_messages": 3, "tool_calls": 0, "complete": False})
                for dimension in TOKEN_KEYS:
                    self.assertNotIn(dimension, result.usage)

    def test_boolean_and_safe_integer_boundaries(self):
        ok = assistant("stop", usage(0, 0, 0, 0, MAX_SAFE_INTEGER))
        translation = translate(run(ok))
        self.assertTrue(translation.usage_complete)
        self.assertEqual(translation.usage["total_tokens"], MAX_SAFE_INTEGER)


# --- terminals --------------------------------------------------------------

class Terminals(unittest.TestCase):
    def test_only_agent_settled_ends_a_run(self):
        for label, frames in {"no terminal at all": run(assistant(), settle=False),
                              "cut inside a message": run(assistant("toolUse"), settle=False)
                              + [{"type": "message_start", "message": {"role": "assistant"}}, update()],
                              "only agent_end": run(assistant("stop"), settle=False)}.items():
            with self.subTest(case=label):
                translation = translate(frames)
                self.assertIsNone(translation.terminal)
                self.assertNotIn("run_finished", kinds(translation))
                self.assertNotIn("error", kinds(translation))
                self.assertTrue(translation.usage_complete)
                self.assertEqual(translation.usage["input_tokens"], 10, "the reference sum survives the cut")
                result = PiNativeAdapter().run(INVOCATION, frames)
                self.assertEqual((result.settled, result.status, result.raw_complete), (False, "UNKNOWN", False))
                self.assertEqual(dict(result.usage), {"status": "unknown"})

    def test_a_non_stop_final_message_ends_in_error(self):
        for stop in ("error", "aborted", "length", "toolUse"):
            with self.subTest(stop_reason=stop):
                frames = run(assistant("stop", usage(1, 1)), assistant(stop, usage(2, 2)))
                translation = translate(frames)
                self.assertEqual(kinds(translation)[-1], "error")
                self.assertEqual(translation.events[-1]["payload"],
                                 {"stop_reason": stop, "assistant_messages": 2, "tool_calls": 0, "agent_cycles": 1,
                                  "reason": "stop_reason:" + stop})
                result = PiNativeAdapter().run(INVOCATION, frames)
                self.assertEqual((result.settled, result.status, result.raw_complete), (True, "ERROR", True))
                self.assertEqual(result.usage["input_tokens"], 3)
                self.assertTrue(result.usage["complete"])

    def test_a_run_without_an_assistant_message_ends_in_error(self):
        frames = run()
        translation = translate(frames)
        self.assertEqual(kinds(translation), ["run_started", "error"])
        self.assertEqual(translation.events[-1]["payload"],
                         {"stop_reason": None, "assistant_messages": 0, "tool_calls": 0, "agent_cycles": 1,
                          "reason": "no_assistant_message"})
        result = PiNativeAdapter().run(INVOCATION, frames)
        self.assertEqual((result.settled, result.status), (True, "ERROR"))
        self.assertEqual(dict(result.usage), {"status": "unknown"}, "no usage event was ever emitted")

    def test_a_retry_cycle_is_one_run_with_both_usages(self):
        frames = [session(), {"type": "agent_start"}, {"type": "turn_start"},
                  assistant("error", usage(5, 0), errorMessage="overloaded"),
                  {"type": "turn_end", "message": {}, "toolResults": []}, agent_end(True),
                  {"type": "agent_start"}, {"type": "turn_start"}, assistant("stop", usage(50, 7)),
                  {"type": "turn_end", "message": {}, "toolResults": []}, agent_end(False), SETTLED]
        translation = translate(frames)
        self.assertEqual(kinds(translation), ["run_started", "assistant", "usage", "assistant", "usage", "run_finished"])
        self.assertEqual(translation.agent_cycles, 2)
        self.assertEqual(translation.events[-1]["payload"]["agent_cycles"], 2)
        self.assertEqual(translation.usage["input_tokens"], 55)
        result = PiNativeAdapter().run(INVOCATION, frames)
        self.assertEqual((result.settled, result.status), (True, "SUCCEEDED"))
        self.assertEqual(result.usage["input_tokens"], 55)


# --- the reference reducer keeps its rules ---------------------------------

class Reducer(unittest.TestCase):
    def refused(self, frames, code):
        with self.assertRaises(AdapterError) as raised:
            PiNativeAdapter().run(INVOCATION, frames)
        self.assertEqual(raised.exception.code, code)

    def test_tool_pairing_violations_are_refused_by_the_reducer(self):
        self.refused(run(assistant("toolUse"), tool_end("t-1"), assistant()), "E_ADAPTER_TOOL")
        self.refused(run(assistant("toolUse"), tool_start("t-1"), tool_start("t-1"), tool_end("t-1"), assistant()),
                     "E_ADAPTER_TOOL")
        self.refused(run(assistant("toolUse"), tool_start("t-1"), assistant()), "E_ADAPTER_TOOL")
        # And the translator itself passes the frames on faithfully.
        self.assertEqual(kinds(translate(run(assistant("toolUse"), tool_end("t-1"), assistant()))),
                         ["run_started", "assistant", "usage", "tool_finished", "assistant", "usage", "run_finished"])

    def test_a_stream_without_a_header_is_refused(self):
        self.refused([], "E_ADAPTER_SETTLEMENT")
        self.refused([assistant(), SETTLED], "E_ADAPTER_SETTLEMENT")
        self.refused([{"type": "agent_start"}, session(), assistant(), SETTLED], "E_ADAPTER_SETTLEMENT")
        self.assertEqual(translate([]).events, ())
        self.assertEqual(kinds(translate([assistant(), SETTLED])), ["assistant", "usage", "run_finished"])


# --- adapter surface --------------------------------------------------------

class Surface(unittest.TestCase):
    def test_probe_and_identity(self):
        adapter = PiNativeAdapter()
        self.assertIsInstance(adapter, OfflineAdapter)
        self.assertEqual(adapter.probe(), {"adapter_id": "pi", "protocol_version": DIALECT, "offline": True,
                                           "process_started": False, "network_used": False, "dialect": DIALECT})
        self.assertEqual(adapter.run(UNBOUND, run(assistant())).status, "SUCCEEDED")
        for bad in (Invocation("req-1", "run-1", "executor", "profile-1", ("pi",), (), "a" * 64,
                               adapter_id="offline"),):
            with self.assertRaises(AdapterError) as raised:
                adapter.run(bad, run(assistant()))
            self.assertEqual(raised.exception.code, "E_ADAPTER_IDENTITY")
        with self.assertRaises(AdapterError) as raised:
            adapter.run({"request_id": "req-1"}, run(assistant()))
        self.assertEqual(raised.exception.code, "E_ADAPTER_INVOCATION")
        self.assertEqual(adapter.translate(run(assistant())), translate(run(assistant())))

    def test_frames_must_be_an_iterable_of_frames(self):
        for bad in (7, None, "[]", b"[]", bytearray(b"[]")):
            with self.subTest(frames=repr(bad)):
                with self.assertRaises(AdapterError) as raised:
                    translate(bad)
                self.assertEqual(raised.exception.code, "E_ADAPTER_EVENT")
        generator = (frame for frame in run(assistant()))
        self.assertEqual(translate(generator).frames, 10)
        self.assertEqual(translate(iter(())).frames, 0)

    def test_the_offline_adapter_is_untouched(self):
        # The reference reducer is reused, not re-implemented: a Pi stream handed to
        # the offline adapter is nothing but unknown events to it.
        result = OfflineAdapter().run(UNBOUND, [{"seq": 0, "event_id": "a", "type": "run_started", "source": "offline",
                                                 "payload": {}}])
        self.assertEqual(result.status, "UNKNOWN")

    def test_importing_has_no_side_effect(self):
        probe = ("import sys, builtins\n"
                 "opened = []\n"
                 "real_open = builtins.open\n"
                 "builtins.open = lambda *a, **k: opened.append(a) or real_open(*a, **k)\n"
                 "import sentinelmesh.pi_events as module\n"
                 "builtins.open = real_open\n"
                 "assert 'ctypes' not in sys.modules, 'ctypes imported'\n"
                 "assert not opened, opened\n"
                 "print('IMPORT_OK')\n")
        done = subprocess.run([sys.executable, "-B", "-c", probe], cwd=str(REPO_ROOT), capture_output=True, text=True,
                              timeout=120)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "IMPORT_OK"), done.stderr)


if __name__ == "__main__":
    unittest.main()
