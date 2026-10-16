"""Executor tests for the Claude Code stream-json translator (``sentinelmesh/claude_events.py``).

These complement the planner oracle (``tests/test_claude_adapter_oracle.py``):
the oracle pins the recorded fixture, the terminal, the failing-closed matrix, the
registry and the cross-adapter behaviour, so this file only covers what it does
not -- a hand-written multi-message success stream, the exact shape of an odd
frame's evidence payload, the summed-usage bound, the two entry-point refusals
and the frozen translation result.  Every rejection case starts from the legal
stream and changes one frame of it.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest

from sentinelmesh import claude_events
from sentinelmesh.adapter_protocol import AdapterError, Invocation
from sentinelmesh.claude_events import (
    DIALECT,
    INVALID_FRAME,
    SOURCE,
    TERMINAL_FRAME,
    TOTAL_DIMENSION,
    UNKNOWN_FRAME,
    USAGE_KEYS,
    ClaudeTranslation,
    ClaudeStreamAdapter,
    translate,
)
from sentinelmesh.protocols import MAX_SAFE_INTEGER

ODD_TYPES = (UNKNOWN_FRAME, INVALID_FRAME)
DIMENSIONS = tuple(dimension for _vendor, dimension in USAGE_KEYS) + (TOTAL_DIMENSION,)


# --- synthetic frame builders ---------------------------------------------- #

def init(**overrides):
    frame = {"type": "system", "subtype": "init", "session_id": "01a1-sess-claude",
             "model": "claude-opus-5-5", "claude_code_version": "2.1.301"}
    frame.update(overrides)
    return frame


def assistant(text="first answer", thinking="first thought", model="claude-opus-5-5", **overrides):
    content = [{"type": "thinking", "thinking": thinking}, {"type": "text", "text": text}]
    frame = {"type": "assistant", "session_id": "01a1-sess-claude",
             "message": {"id": "msg_01", "type": "message", "role": "assistant",
                         "model": model, "content": content, "usage": {"input_tokens": 2}}}
    frame.update(overrides)
    return frame


def result(**overrides):
    frame = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
             "session_id": "01a1-sess-claude", "total_cost_usd": 0.0188578,
             "usage": {"input_tokens": 2, "output_tokens": 4, "cache_read_input_tokens": 649,
                       "cache_creation_input_tokens": 2330}}
    frame.update(overrides)
    return frame


def stream():
    """A legal two-turn stream: init, two assistant messages, rate limit, result."""
    return [init(), assistant(text="first answer", thinking="first thought"),
            assistant(text="second answer", thinking="second thought", model="claude-sonnet-5"),
            {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}, result()]


def digest_of(frame):
    text = json.dumps(frame, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=repr)
    data = text.encode("utf-8", "surrogatepass")
    return hashlib.sha256(data).hexdigest(), len(data)


class ClaudeCase(unittest.TestCase):
    def translate(self, frames):
        return claude_events.translate(frames)

    def types(self, translation):
        return [event["type"] for event in translation.events]

    def odd_events(self, translation):
        return [event for event in translation.events if event["type"] in ODD_TYPES]


class ClaudeAdapterTests(ClaudeCase):
    # -- a hand-written multi-message success stream -------------------------- #

    def test_two_assistant_messages_count_their_blocks_and_finish(self):
        translation = self.translate(stream())
        self.assertEqual(self.types(translation),
                         ["run_started", "assistant", "assistant", "usage", "run_finished"])
        self.assertEqual(translation.dialect, DIALECT)
        self.assertEqual((translation.frames, translation.assistant_messages), (5, 2))
        self.assertEqual((translation.terminal, translation.subtype, translation.is_error,
                          translation.num_turns), (TERMINAL_FRAME, "success", False, 1))
        self.assertEqual(translation.ignored, {"rate_limit_event": 1})
        self.assertEqual(translation.unknown, 0)
        self.assertEqual(translation.usage, {"input_tokens": 2, "output_tokens": 4,
                                             "cache_read_tokens": 649, "cache_write_tokens": 2330,
                                             TOTAL_DIMENSION: 2985})
        self.assertIs(translation.usage_complete, True)
        # Both models are reported, sorted, and every event is this adapter's own.
        self.assertEqual(translation.models, ("claude-opus-5-5", "claude-sonnet-5"))
        for event in translation.events:
            self.assertEqual(event["source"], SOURCE)
        for event_id, payload in ((event["event_id"], event["payload"]) for event in translation.events
                                  if event["type"] == "assistant"):
            self.assertEqual(set(payload), {"model", "message_id", "text_blocks", "thinking_blocks"})
            self.assertEqual((payload["model"], payload["message_id"], payload["text_blocks"],
                              payload["thinking_blocks"]),
                             ("claude-opus-5-5" if event_id == "L2" else "claude-sonnet-5", "msg_01", 1, 1))
            # Neither the answer nor the thinking is copied into the event.
            self.assertNotIn("answer", json.dumps(payload))
            self.assertNotIn("thought", json.dumps(payload))
        # Sequence numbers are continuous from zero and event ids stay unique.
        self.assertEqual([event["seq"] for event in translation.events], list(range(5)))
        self.assertEqual(len({event["event_id"] for event in translation.events}), 5)

    def test_the_translation_result_fields_are_exactly_the_contracts(self):
        fields = tuple(field.name for field in dataclasses.fields(ClaudeTranslation))
        self.assertEqual(fields, ("dialect", "events", "frames", "ignored", "unknown", "usage",
                                  "usage_complete", "assistant_messages", "terminal", "subtype",
                                  "is_error", "num_turns", "models"))
        translation = self.translate(stream())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            translation.usage_complete = False
        with self.assertRaises(dataclasses.FrozenInstanceError):
            translation.usage = {}

    # -- the evidence payload of an odd frame --------------------------------- #

    def test_an_odd_frame_event_carries_its_own_digest(self):
        legal = stream()
        self.assertEqual(self.odd_events(self.translate(legal)), [])
        odd = dict(legal[3], type="user", message={"role": "user", "content": []})
        frames = legal[:3] + [odd] + [legal[4]]
        translation = self.translate(frames)
        self.assertEqual(self.types(translation), ["run_started", "assistant", "assistant",
                                                   UNKNOWN_FRAME, "usage", "run_finished"])
        event = self.odd_events(translation)[0]
        self.assertEqual(event["event_id"], "L4")
        self.assertEqual(set(event["payload"]), {"frame_type", "line", "reason", "sha256", "bytes"})
        digest, size = digest_of(odd)
        self.assertEqual((event["payload"]["frame_type"], event["payload"]["line"],
                          event["payload"]["reason"], event["payload"]["sha256"],
                          event["payload"]["bytes"]), ("user", 4, "frame type outside the dialect",
                                                       digest, size))
        self.assertEqual(translation.unknown, 1)
        # An odd frame anywhere in the stream leaves the run's usage unknown.
        self.assertEqual((translation.usage, translation.usage_complete), ({}, False))
        usage_events = [event for event in translation.events if event["type"] == "usage"]
        self.assertEqual(len(usage_events), 1)
        self.assertIs(usage_events[0]["payload"]["complete"], False)
        self.assertFalse(set(DIMENSIONS) & set(usage_events[0]["payload"]))

    def test_block_counts_follow_the_block_types(self):
        # Review F6: two text blocks and one thinking block, so a swap of the
        # two counters shows.
        frames = stream()
        frames[1]["message"]["content"] = [{"type": "text", "text": "a"},
                                         {"type": "thinking", "thinking": "b"},
                                         {"type": "text", "text": "c"}]
        payload = self.translate(frames).events[1]["payload"]
        self.assertEqual((payload["text_blocks"], payload["thinking_blocks"]), (2, 1))

    def test_the_digest_of_a_non_ascii_frame_is_taken_over_its_utf8_text(self):
        # Review F6: the canonical JSON keeps non-ASCII characters as they are
        # (``ensure_ascii=False``), so the digest and the byte length are the
        # UTF-8 ones, not those of the escaped form.
        legal = stream()
        odd = {"type": "user", "message": {"role": "user", "content": "\u5bc6\u94a5 \u00e9"}}
        translation = self.translate(legal[:3] + [odd] + [legal[4]])
        payload = self.odd_events(translation)[0]["payload"]
        self.assertEqual((payload["sha256"], payload["bytes"]), digest_of(odd))
        escaped = json.dumps(odd, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        self.assertNotEqual(payload["bytes"], len(escaped))

    def test_num_turns_beyond_max_safe_integer_is_an_invalid_result(self):
        # Review F4: the reducer refuses such an integer, so the translator does
        # not report a complete usage for it.
        frames = stream()
        frames[4] = result(num_turns=MAX_SAFE_INTEGER)
        self.assertEqual(self.types(self.translate(frames))[-1], "run_finished")
        frames[4] = result(num_turns=MAX_SAFE_INTEGER + 1)
        translation = self.translate(frames)
        self.assertEqual(self.types(translation)[-1], INVALID_FRAME)
        self.assertEqual((translation.terminal, translation.num_turns, translation.usage,
                          translation.usage_complete), (None, None, {}, False))

    # -- the summed-usage bound ------------------------------------------------ #

    def test_a_sum_beyond_max_safe_integer_leaves_the_usage_unknown(self):
        # Every counter is legal on its own; only their sum is not.
        frames = stream()
        frames[4] = result(usage={"input_tokens": MAX_SAFE_INTEGER, "output_tokens": 1,
                                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
        translation = self.translate(frames)
        self.assertEqual((translation.usage, translation.usage_complete), ({}, False))
        self.assertEqual(translation.unknown, 0)
        usage_event = [event for event in translation.events if event["type"] == "usage"][0]
        self.assertEqual(set(usage_event["payload"]), {"assistant_messages", "complete"})
        self.assertIs(usage_event["payload"]["complete"], False)
        # The run itself is still a complete success: only its usage is unknown.
        self.assertEqual(self.types(translation)[-1], "run_finished")
        self.assertEqual(translation.terminal, TERMINAL_FRAME)

    # -- the entry-point refusals --------------------------------------------- #

    def test_translate_refuses_a_non_iterable_and_the_adapter_refuses_an_invocation(self):
        # Text, bytes and anything that is not iterable at all: the same refusal
        # pi's translator gives, so no caller can mistake a payload for frames.
        for bad in (42, None, "[]", b"{}", bytearray(b"{}")):
            with self.subTest(frames=bad):
                with self.assertRaises(AdapterError) as ctx:
                    translate(bad)
                self.assertEqual(ctx.exception.code, "E_ADAPTER_EVENT")
        adapter = ClaudeStreamAdapter()
        self.assertEqual((adapter.adapter_id, adapter.protocol_version), ("claude", DIALECT))
        self.assertEqual(adapter.probe()["dialect"], DIALECT)
        with self.assertRaises(AdapterError) as ctx:
            adapter.run("x", stream())
        self.assertEqual(ctx.exception.code, "E_ADAPTER_INVOCATION")

    def test_a_matching_invocation_settles_the_stream(self):
        adapter = ClaudeStreamAdapter()
        invocation = Invocation("req", "op-1", "executor", "claude-code", ("claude",), (),
                                "e" * 64, "claude")
        settled = adapter.run(invocation, stream())
        self.assertEqual((settled.settled, settled.status, settled.raw_complete), (True, "SUCCEEDED", True))
        self.assertEqual({key: settled.usage[key] for key in DIMENSIONS},
                         {"input_tokens": 2, "output_tokens": 4, "cache_read_tokens": 649,
                          "cache_write_tokens": 2330, TOTAL_DIMENSION: 2985})
        self.assertEqual([event.event_type for event in settled.events],
                         ["run_started", "assistant", "assistant", "usage", "run_finished"])


if __name__ == "__main__":
    unittest.main()
