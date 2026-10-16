"""Executor tests for the claude-tools translation (``sentinelmesh/claude_events.py``
and the ``claude-stream-json`` reference in ``sentinelmesh/ledger_cli.py``).

These complement the planner oracle (``tests/test_claude_tools_oracle.py``), which
replays the recorded six-turn fixture through a real ``smctl run once``.  This file
only covers what the oracle does not, on hand-written streams: a single assistant
frame opening two tools that one user frame closes, the per-message usage sum across
split and distinct messages, the four ordered ``reference`` reasons on a truncated
stream, and a fallback model reported once.  Every stream is offline: no provider,
no credentials, no network.
"""
from __future__ import annotations

import unittest

from sentinelmesh import claude_events, ledger_cli
from sentinelmesh.claude_events import translate


# --- synthetic frame builders ---------------------------------------------- #

LEGAL_USAGE = {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3,
               "cache_creation_input_tokens": 4}
LEGAL_MODEL_USAGE = {"claude-opus-5-5": {"inputTokens": 1, "outputTokens": 2,
                                         "cacheReadInputTokens": 3, "cacheCreationInputTokens": 4}}


def init(model="claude-opus-5-5", **overrides):
    frame = {"type": "system", "subtype": "init", "session_id": "01-sess-claude",
             "model": model, "claude_code_version": "2.1.301"}
    frame.update(overrides)
    return frame


def assistant(message_id, usage, blocks, model="claude-opus-5-5", **overrides):
    frame = {"type": "assistant", "parent_tool_use_id": None, "session_id": "01-sess-claude",
             "message": {"id": message_id, "type": "message", "role": "assistant",
                         "model": model, "content": blocks, "usage": usage}}
    frame.update(overrides)
    return frame


def tool_use(tool_id, name="Read"):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": {}}


def tool_result(tool_use_id, is_error="absent"):
    block = {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}
    if is_error != "absent":
        block["is_error"] = is_error
    return block


def user(blocks, **overrides):
    frame = {"type": "user", "parent_tool_use_id": None, "session_id": "01-sess-claude",
             "message": {"role": "user", "content": blocks}}
    frame.update(overrides)
    return frame


def result(model_usage=None, **overrides):
    frame = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
             "session_id": "01-sess-claude"}
    if model_usage is not None:
        frame["modelUsage"] = model_usage
    frame.update(overrides)
    return frame


def fallback(fallback_model, original_model="claude-opus-5-5", **overrides):
    frame = {"type": "system", "subtype": "model_refusal_fallback", "original_model": original_model,
             "fallback_model": fallback_model, "retracted_message_uuids": []}
    frame.update(overrides)
    return frame


class ClaudeToolsTests(unittest.TestCase):
    def translate(self, frames):
        return translate(frames)

    def types(self, translation):
        return [event["type"] for event in translation.events]

    def ids(self, translation):
        return [event["event_id"] for event in translation.events]

    def test_two_tools_in_one_frame_are_paired_by_one_user_frame(self):
        frames = [
            init(),
            assistant("msg_1", LEGAL_USAGE, [tool_use("toolu_a"), tool_use("toolu_b")]),
            user([tool_result("toolu_a", is_error=True), tool_result("toolu_b")]),
            result(LEGAL_MODEL_USAGE),
        ]
        translation = self.translate(frames)
        # The assistant frame opens both tools after its own event, in block order;
        # the user frame closes both, in block order, with the right ids.
        self.assertEqual(self.types(translation),
                         ["run_started", "assistant", "tool_started", "tool_started",
                          "tool_finished", "tool_finished", "usage", "run_finished"])
        self.assertEqual(self.ids(translation),
                         ["L1", "L2", "L2.tool1", "L2.tool2", "L3.tool1", "L3.tool2", "L4.usage", "L4"])
        started = [event["payload"] for event in translation.events if event["type"] == "tool_started"]
        finished = [event["payload"] for event in translation.events if event["type"] == "tool_finished"]
        self.assertEqual([payload["tool_id"] for payload in started], ["toolu_a", "toolu_b"])
        self.assertEqual([payload["tool_id"] for payload in finished], ["toolu_a", "toolu_b"])
        self.assertEqual([payload["tool"] for payload in started + finished], ["Read"] * 4)
        # The first tool errored, the second did not (its block carries no is_error).
        self.assertEqual([payload["is_error"] for payload in finished], [True, False])
        self.assertEqual((translation.tool_calls, translation.tool_errors), (2, 1))
        self.assertEqual(translation.events[-1]["payload"],
                         {"subtype": "success", "num_turns": 1, "assistant_messages": 1,
                          "tool_calls": 2, "tool_errors": 1, "permission_denials": 0, "fallbacks": 0})

    def test_message_usage_sums_each_id_once_across_split_frames(self):
        usage_a = {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3,
                   "cache_creation_input_tokens": 4}
        usage_b = {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30,
                   "cache_creation_input_tokens": 40}
        usage_c = {"input_tokens": 100, "output_tokens": 200, "cache_read_input_tokens": 300,
                   "cache_creation_input_tokens": 400}
        frames = [
            init(),
            assistant("msg_a", usage_a, [{"type": "text", "text": "a"}]),
            # msg_b is split across two frames: it counts once, with its last
            # frame's usage (review F5: the first frame shows a lower output).
            assistant("msg_b", dict(usage_b, output_tokens=5), [{"type": "thinking", "thinking": "t"}]),
            assistant("msg_b", usage_b, [{"type": "text", "text": "b"}]),
            assistant("msg_c", usage_c, [{"type": "text", "text": "c"}]),
            # The billed total differs from the per-message sum (review F5), so the
            # two numbers cannot stand in for each other.
            result({"claude-opus-5-5": {"inputTokens": 111, "outputTokens": 900,
                                        "cacheReadInputTokens": 333, "cacheCreationInputTokens": 444}}),
        ]
        translation = self.translate(frames)
        self.assertEqual(translation.assistant_messages, 3)
        self.assertEqual(translation.message_usage,
                         {"input_tokens": 111, "output_tokens": 222, "cache_read_tokens": 333,
                          "cache_write_tokens": 444, "total_tokens": 1110})
        self.assertEqual(translation.reported_usage,
                         {"input_tokens": 111, "output_tokens": 900, "cache_read_tokens": 333,
                          "cache_write_tokens": 444, "total_tokens": 1788})
        self.assertEqual(translation.illegal_message_usage, 0)

    def test_a_cut_stream_with_an_illegal_message_lists_all_four_reasons_in_order(self):
        illegal = dict(LEGAL_USAGE, output_tokens="boom")
        frames = [
            init(),
            assistant("msg_bad", illegal, [{"type": "text", "text": "x"}]),
            assistant("msg_ok", LEGAL_USAGE, [{"type": "text", "text": "y"}]),
            {"type": "stream_event", "event": {}},
            # No result: the run was cut before it settled.
        ]
        reference = ledger_cli._REFERENCES["claude-stream-json"](frames)
        self.assertEqual(reference["reasons"],
                         ["no_terminal", "illegal_usage", "partial_output", "unknown_frames"])
        self.assertIs(reference["complete"], False)
        self.assertIsNone(reference["terminal"])
        # Only the legal message is summed; the one with an illegal counter is left out.
        self.assertEqual(reference["usage"],
                         {"input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 3,
                          "cache_write_tokens": 4, "total_tokens": 10})
        self.assertEqual(reference["unknown"], 1)

    def test_a_repeated_fallback_model_is_counted_twice_but_reported_once(self):
        frames = [
            init(),
            assistant("msg_1", LEGAL_USAGE, [{"type": "text", "text": "x"}]),
            fallback("claude-fb-x"),
            fallback("claude-fb-x"),
            result(LEGAL_MODEL_USAGE),
        ]
        translation = self.translate(frames)
        self.assertEqual(translation.fallbacks, 2)
        # Review F6: the exact tuple, since ``models`` comes from a set and a
        # count over it could never exceed one.
        self.assertEqual(translation.models, ("claude-fb-x", "claude-opus-5-5"))
        self.assertEqual(translation.events[-1]["payload"]["fallbacks"], 2)


if __name__ == "__main__":
    unittest.main()
