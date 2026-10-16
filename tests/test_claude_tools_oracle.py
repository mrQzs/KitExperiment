"""Planner oracle for claude-tools (`.agent/design/claude-tools.md`, sections 3.2 and 5).

The second Claude Code recording (2026-10-16, Read only under ``--restricted``,
`tests/fixtures/claude_stream/windows-claude2_1_301-tools-a001.*`) carries tool
calls, six turns, a permission denial, a safety refusal with a model fallback that
retracts three frames, split assistant frames and per-model usage.  The
``claude-stream-json`` translator now:

* maps each ``tool_use`` block to ``tool_started`` (``tool_id``, ``tool``) after the
  frame's ``assistant`` event, and each ``tool_result`` block of a ``user`` frame to
  ``tool_finished`` (``tool_id``, ``tool``, ``is_error`` -- absent means false); a
  result for a tool that is not open, a repeated ``tool_use`` id and a non-boolean
  ``is_error`` make the whole frame invalid;
* counts and ignores ``system`` ``permission_denied`` (also ``permission_denials``),
  ``informational`` and ``thinking_tokens``, and a ``user`` text message marked
  ``isSynthetic``; accepts ``model_refusal_fallback`` (``fallbacks``, the fallback
  model joins ``models``); a frame with a ``parent_tool_use_id``, another
  ``system`` subtype and a plain ``user`` text message still fail closed;
* counts assistant messages by distinct ``message.id``;
* settles the sum of ``result.modelUsage`` over the models (the billed total) and
  no longer reads ``result.usage``;
* reports ``reported_usage``, ``message_usage`` (each message id once),
  ``illegal_message_usage``, ``tool_calls``, ``tool_errors``,
  ``permission_denials`` and ``fallbacks``.

``smctl ledger reference --dialect claude-stream-json`` reads the same translation:
the settled number when the ``result`` carried a legal usage, otherwise the
per-message sum with the reasons ``no_terminal``, ``illegal_usage``,
``partial_output`` and ``unknown_frames`` in that order.

Everything here replays fixtures through ``python -c`` children: no provider, no
credentials, no network.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from sentinelmesh import cli
from sentinelmesh.durable_ledger import open_ledger
from sentinelmesh.protocols import MAX_SAFE_INTEGER

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "claude_stream"
TOOLS = "windows-claude2_1_301-tools-a001"
SINGLE = "windows-claude2_1_301-live-a001"
PI_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "pi_native" / "windows-pi110-live-smctl-a001.jsonl"
DIMENSIONS = ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens"]
CAT = "import sys; sys.stdout.buffer.write(open(sys.argv[1], 'rb').read()); sys.stdout.buffer.flush()"
ODD = ("claude.unknown_frame", "claude.invalid_frame")

#: The billed total: ``modelUsage`` summed over claude-opus-5-5 and claude-opus-4-8.
BILLED = {"input_tokens": 10, "output_tokens": 670, "cache_read_tokens": 11602, "cache_write_tokens": 3401,
          "total_tokens": 15683}
#: Each of the four message ids once; output is only what the frames showed.
MESSAGES = {"input_tokens": 10, "output_tokens": 27, "cache_read_tokens": 11602, "cache_write_tokens": 3401,
            "total_tokens": 15040}
#: The first 13 frames: three messages, the third tool still open.
CUT_MESSAGES = {"input_tokens": 8, "output_tokens": 25, "cache_read_tokens": 7909, "cache_write_tokens": 2823,
                "total_tokens": 10765}
SINGLE_USAGE = {"input_tokens": 2, "output_tokens": 4, "cache_read_tokens": 649, "cache_write_tokens": 2330,
                "total_tokens": 2985}
TOOL_STREAM_TYPES = [
    "run_started", "assistant", "tool_started", "tool_finished",          # L1 L2 L2 L4
    "assistant", "assistant", "tool_started", "tool_finished",            # L8 L9 L9 L10
    "assistant", "assistant", "tool_started", "tool_finished",            # L12 L13 L13 L14
    "assistant", "tool_started", "tool_finished",                         # L15 L15 L16
    "assistant", "assistant", "usage", "run_finished",                    # L20 L21 L22 L22
]
TOOL_STREAM_IDS = ["L1", "L2", "L2.tool1", "L4.tool1", "L8", "L9", "L9.tool1", "L10.tool1", "L12", "L13",
                   "L13.tool1", "L14.tool1", "L15", "L15.tool1", "L16.tool1", "L20", "L21", "L22.usage", "L22"]
IGNORED = {"rate_limit_event": 1, "system/permission_denied": 1, "system/informational": 1,
           "system/thinking_tokens": 3, "user/synthetic": 1}
REFERENCE_KEYS = {"complete", "reasons", "terminal", "subtype", "is_error", "num_turns", "usage",
                  "assistant_messages", "tool_calls", "tool_errors", "permission_denials", "fallbacks",
                  "frames", "unknown", "models"}


def frames_of(raw: bytes):
    return [json.loads(line) for line in raw.split(b"\n") if line.strip()]


def fixture(name=TOOLS):
    return frames_of((FIXTURES / (name + ".jsonl")).read_bytes())


class ToolsCase(unittest.TestCase):
    def setUp(self):
        from sentinelmesh import claude_events
        self.ce = claude_events
        self.work = Path(tempfile.mkdtemp(prefix="claude-tools-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)

    def translate(self, frames):
        return self.ce.translate(frames)

    def types(self, translation):
        return [event["type"] for event in translation.events]

    def smctl(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main([str(a) for a in argv])
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def write_stream(self, frames, name="stream.jsonl"):
        path = self.work / name
        path.write_bytes("".join(json.dumps(frame) + NL for frame in frames).encode("utf-8"))
        return path

    def once(self, stream_path, adapter_id="claude"):
        """``smctl run once`` with a child that writes ``stream_path`` byte for byte.

        Returns the outcome, the ledger's usage of the operation, the ledger path
        and the operation id.
        """
        caps = {"dispatches": 9, **{dimension: 10 ** 6 for dimension in DIMENSIONS}}
        reservation = {"dispatches": 1, **{dimension: 10 ** 5 for dimension in DIMENSIONS}}
        ledger = str(self.work / ("ledger-%s.db" % len(list(self.work.glob("ledger-*.db")))))
        tree = self.work / "tree"
        tree.mkdir(exist_ok=True)
        evidence = Path(tempfile.mkdtemp(prefix="ev-", dir=str(self.work)))
        spec = {"request_id": "req-t", "repo_id": "KitExperiment", "caps": caps, "concurrency": 1,
                "tasks": [{"task_id": "task-t", "caps": caps, "max_attempts": 1}]}
        (self.work / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
        code, out, err = self.smctl("run", "init", ledger, "--spec", str(self.work / "spec.json"))
        self.assertEqual(code, 0, err)
        job = {"request_id": "req-t", "task_id": "task-t", "plan": 1, "idempotency_key": "idem-t", "guard_version": 1,
               "reservation": reservation, "resources": ["t"], "adapter_id": adapter_id, "role": "executor",
               "profile_id": "claude-code", "command": [PY, "-c", CAT, str(stream_path)], "capabilities": [],
               "cwd": str(tree), "environment": {"LANG": "C", "PATH": os.path.dirname(PY)},
               "allowed_secret_names": [], "timeout_seconds": 60, "usage_dimensions": DIMENSIONS}
        (self.work / "job.json").write_text(json.dumps(job), encoding="utf-8")
        code, out, err = self.smctl("run", "once", ledger, "--job", str(self.work / "job.json"), "--evidence",
                                    str(evidence), "--worktree-id", "wt-1", "--owner", "owner-1")
        self.assertEqual((code, err), (0, ""), err)
        result = json.loads(out)
        with open_ledger(ledger) as handle:
            usage = handle.operation_usage(result["operation_id"])
        return result["outcome"], usage, ledger, result["operation_id"]

    def reference(self, ledger, operation, dialect="claude-stream-json"):
        code, out, err = self.smctl("ledger", "reference", ledger, "--operation", operation, "--dialect", dialect)
        self.assertEqual((code, err), (0, ""), err)
        report = json.loads(out)
        self.assertEqual(report["dialect"], dialect)
        return report["reference"]


class ToolStreamOracle(ToolsCase):
    # -- the recorded tool stream ----------------------------------------------- #

    def test_the_tool_stream_translates_its_tools_and_counts_the_rest(self):
        translation = self.translate(fixture())
        self.assertEqual(self.types(translation), TOOL_STREAM_TYPES)
        self.assertEqual([event["event_id"] for event in translation.events], TOOL_STREAM_IDS)
        self.assertEqual([event["seq"] for event in translation.events], list(range(len(TOOL_STREAM_TYPES))))
        self.assertEqual((translation.frames, translation.unknown, translation.ignored), (22, 0, IGNORED))
        self.assertEqual((translation.assistant_messages, translation.tool_calls, translation.tool_errors,
                          translation.permission_denials, translation.fallbacks), (4, 4, 3, 1, 1))
        self.assertEqual(translation.models, ("claude-opus-4-8", "claude-opus-5-5"))
        self.assertEqual((translation.terminal, translation.subtype, translation.is_error, translation.num_turns),
                         ("result", "success", False, 6))
        self.assertEqual((translation.usage, translation.usage_complete), (BILLED, True))
        self.assertEqual(translation.reported_usage, BILLED)
        self.assertEqual((translation.message_usage, translation.illegal_message_usage), (MESSAGES, 0))
        started = [event["payload"] for event in translation.events if event["type"] == "tool_started"]
        finished = [event["payload"] for event in translation.events if event["type"] == "tool_finished"]
        self.assertEqual([set(payload) for payload in started], [{"tool_id", "tool"}] * 4)
        self.assertEqual([set(payload) for payload in finished], [{"tool_id", "tool", "is_error"}] * 4)
        self.assertEqual([payload["tool_id"] for payload in started], [payload["tool_id"] for payload in finished])
        self.assertEqual(len({payload["tool_id"] for payload in started}), 4)
        self.assertEqual({payload["tool"] for payload in started + finished}, {"Read"})
        # Denied, malformed input, read, missing file; the read's block has no is_error at all.
        self.assertEqual([payload["is_error"] for payload in finished], [True, True, False, True])
        assistants = [event["payload"] for event in translation.events if event["type"] == "assistant"]
        self.assertEqual([set(payload) for payload in assistants],
                         [{"model", "message_id", "text_blocks", "thinking_blocks", "tool_use_blocks"}] * 8)
        self.assertEqual([(p["text_blocks"], p["thinking_blocks"], p["tool_use_blocks"]) for p in assistants],
                         [(0, 0, 1), (0, 1, 0), (0, 0, 1), (0, 1, 0), (0, 0, 1), (0, 0, 1), (0, 1, 0), (1, 0, 0)])
        self.assertEqual(translation.events[-1]["payload"],
                         {"subtype": "success", "num_turns": 6, "assistant_messages": 4, "tool_calls": 4,
                          "tool_errors": 3, "permission_denials": 1, "fallbacks": 1})
        # The file's text and the tool inputs never enter an event.
        for event in translation.events:
            self.assertEqual(event["source"], "claude")
            self.assertNotIn("apple", json.dumps(event["payload"]))
            self.assertNotIn("file_path", json.dumps(event["payload"]))

    def test_the_meta_shows_model_usage_is_the_billed_total(self):
        meta = json.loads((FIXTURES / (TOOLS + ".meta.json")).read_text(encoding="utf-8"))
        report = meta["cli_report"]
        summed = {key: sum(entry[key] for entry in report["model_usage"].values())
                  for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")}
        self.assertEqual(summed, {"inputTokens": 10, "outputTokens": 670, "cacheReadInputTokens": 11602,
                                  "cacheCreationInputTokens": 3401})
        self.assertAlmostEqual(sum(entry["costUSD"] for entry in report["model_usage"].values()),
                               report["total_cost_usd"], places=9)
        # result.usage leaves the retracted turn out, which is why it is not the settlement.
        self.assertEqual(report["usage"], {"input_tokens": 6, "output_tokens": 627, "cache_read_input_tokens": 8253,
                                           "cache_creation_input_tokens": 3060})

    def test_the_tool_stream_replays_through_smctl_to_the_billed_usage(self):
        outcome, usage, _ledger, _op = self.once(FIXTURES / (TOOLS + ".jsonl"))
        self.assertEqual((outcome["state"], outcome["exit_code"], outcome["result_error"]), ("REVIEW_PENDING", 0, None))
        self.assertEqual((outcome["result"]["status"], outcome["result"]["raw_complete"]), ("SUCCEEDED", True))
        self.assertEqual(outcome["usage_recorded"], {d: "APPLIED" for d in DIMENSIONS})
        self.assertEqual({d: usage[d]["amount"] for d in DIMENSIONS}, BILLED)

    def test_the_single_turn_recording_settles_the_same_as_before(self):
        translation = self.translate(fixture(SINGLE))
        self.assertEqual((translation.usage, translation.reported_usage, translation.message_usage),
                         (SINGLE_USAGE, SINGLE_USAGE, SINGLE_USAGE))
        self.assertEqual((translation.tool_calls, translation.permission_denials, translation.fallbacks,
                          translation.assistant_messages), (0, 0, 0, 1))

    # -- cut streams and open tools --------------------------------------------- #

    def test_a_stream_cut_inside_a_tool_call_reviews_with_its_usage_unknown(self):
        outcome, _usage, _ledger, _op = self.once(self.write_stream(fixture()[:13]))
        self.assertEqual((outcome["state"], outcome["result"]["status"], outcome["result"]["raw_complete"]),
                         ("REVIEW_PENDING", "UNKNOWN", False))
        self.assertEqual((outcome["usage_recorded"], sorted(outcome["usage_skipped"])), ({}, sorted(DIMENSIONS)))

    def test_a_result_with_a_tool_still_open_is_refused_by_the_reducer(self):
        frames = fixture()
        del frames[15]  # the missing file's result
        translation = self.translate(frames)
        self.assertEqual(translation.unknown, 0)
        self.assertEqual(self.types(translation).count("tool_started") - self.types(translation).count("tool_finished"), 1)
        outcome, _usage, _ledger, _op = self.once(self.write_stream(frames))
        self.assertEqual((outcome["state"], outcome["result_error"]), ("REVIEW_PENDING", "E_ADAPTER_TOOL"))
        self.assertEqual(outcome["usage_recorded"], {})

    # -- failing closed ---------------------------------------------------------- #

    def test_tool_and_user_frames_outside_the_rules_fail_closed(self):
        base = fixture()

        def set_parent(index):
            return lambda f: f[index].__setitem__("parent_tool_use_id", "toolu_parent")

        cases = {
            "a result for a tool never opened": lambda f: f[13]["message"]["content"][0].update(tool_use_id="toolu_never"),
            "a second result for the same tool": lambda f: f.insert(14, copy.deepcopy(f[13])),
            "two results for one tool in one frame": lambda f: f[13]["message"]["content"].append(
                copy.deepcopy(f[13]["message"]["content"][0])),
            "a tool_use id used again": lambda f: f[14]["message"]["content"][0].update(id=f[12]["message"]["content"][0]["id"]),
            "a tool_use id used twice in one frame": lambda f: f[12]["message"]["content"].append(
                copy.deepcopy(f[12]["message"]["content"][0])),
            "a tool_use without an id": lambda f: f[12]["message"]["content"][0].pop("id"),
            "a tool_use with an empty name": lambda f: f[12]["message"]["content"][0].update(name=""),
            "an is_error that is text": lambda f: f[13]["message"]["content"][0].update(is_error="false"),
            "an is_error that is null": lambda f: f[15]["message"]["content"][0].update(is_error=None),
            "a user text message that is not synthetic": lambda f: f[6].pop("isSynthetic"),
            "a synthetic user message carrying a tool result": lambda f: f[6]["message"]["content"].append(
                copy.deepcopy(f[13]["message"]["content"][0])),
            "a user frame whose content is text": lambda f: f[13]["message"].update(content="plain"),
            "a user frame with no blocks": lambda f: f[13]["message"].update(content=[]),
            "a tool result next to a text block": lambda f: f[13]["message"]["content"].append({"type": "text", "text": "x"}),
            "a user frame whose message is an assistant's": lambda f: f[13]["message"].update(role="assistant"),
            "a subagent assistant frame": set_parent(12),
            "a subagent user frame": set_parent(13),
            "an image block": lambda f: f[20]["message"]["content"].append({"type": "image", "source": {}}),
            "a compaction boundary": lambda f: f.insert(5, {"type": "system", "subtype": "compact_boundary"}),
            "a hook frame": lambda f: f.insert(5, {"type": "system", "subtype": "hook_started"}),
            "a permission denial without its tool": lambda f: f[2].pop("tool_use_id"),
            "a fallback without its model": lambda f: f[10].pop("fallback_model"),
            "a fallback whose original model is a number": lambda f: f[10].update(original_model=7),
            "a stream event": lambda f: f.insert(5, {"type": "stream_event", "event": {}}),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                frames = copy.deepcopy(base)
                mutate(frames)
                translation = self.translate(frames)
                self.assertGreater(translation.unknown, 0)
                self.assertEqual((translation.usage, translation.usage_complete), ({}, False))
                odd_seen = False
                for event in translation.events:
                    if event["type"] in ODD:
                        odd_seen = True
                        self.assertEqual(set(event["payload"]), {"frame_type", "line", "reason", "sha256", "bytes"})
                    elif event["type"] == "usage" and odd_seen:
                        self.assertIs(event["payload"]["complete"], False)
                outcome, _usage, _ledger, _op = self.once(self.write_stream(frames, "case.jsonl"))
                status = None if outcome["result"] is None else outcome["result"]["status"]
                self.assertIn(status, (None, "UNKNOWN"))
                self.assertEqual(outcome["usage_recorded"], {})

    def test_an_odd_frame_emits_no_half_of_its_events(self):
        frames = fixture()
        # The third tool's frame names the first tool's id a second time: the
        # whole frame is one invalid record, with no assistant or tool event.
        frames[14]["message"]["content"][0]["id"] = frames[1]["message"]["content"][0]["id"]
        translation = self.translate(frames)
        line15 = [event for event in translation.events if event["event_id"].split(".")[0] == "L15"]
        self.assertEqual([event["type"] for event in line15], ["claude.invalid_frame"])
        self.assertEqual(translation.tool_calls, 3)

    # -- frames that are counted, not translated -------------------------------- #

    def test_notices_denials_estimates_and_synthetic_text_are_only_counted(self):
        frames = fixture(SINGLE)
        frames[2:2] = [
            {"type": "system", "subtype": "informational", "content": "note", "level": "notice"},
            {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 5},
            {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 9},
            {"type": "system", "subtype": "permission_denied", "tool_name": "Read", "tool_use_id": "toolu_x"},
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "notice"}]},
             "parent_tool_use_id": None, "isSynthetic": True},
        ]
        translation = self.translate(frames)
        self.assertEqual(self.types(translation), ["run_started", "assistant", "usage", "run_finished"])
        self.assertEqual(translation.ignored, {"rate_limit_event": 1, "system/informational": 1,
                                               "system/thinking_tokens": 2, "system/permission_denied": 1,
                                               "user/synthetic": 1})
        self.assertEqual((translation.unknown, translation.permission_denials, translation.usage),
                         (0, 1, SINGLE_USAGE))
        outcome, usage, _ledger, _op = self.once(self.write_stream(frames))
        self.assertEqual({d: usage[d]["amount"] for d in DIMENSIONS}, SINGLE_USAGE)

    def test_a_refusal_fallback_is_accepted_and_its_model_reported(self):
        frames = fixture(SINGLE)
        frames.insert(2, {"type": "system", "subtype": "model_refusal_fallback", "original_model": "claude-opus-5-5",
                          "fallback_model": "claude-fallback-x", "retracted_message_uuids": []})
        translation = self.translate(frames)
        self.assertEqual(self.types(translation)[-1], "run_finished")
        self.assertEqual((translation.fallbacks, translation.unknown), (1, 0))
        self.assertEqual(translation.models, ("claude-fallback-x", "claude-opus-5-5"))
        self.assertEqual(translation.events[-1]["payload"]["fallbacks"], 1)
        self.assertEqual(translation.usage, SINGLE_USAGE)

    # -- usage ---------------------------------------------------------------------- #

    def test_result_usage_is_no_longer_read(self):
        frames = fixture()
        frames[21]["usage"] = {"input_tokens": -1}
        translation = self.translate(frames)
        self.assertEqual((translation.usage, translation.usage_complete), (BILLED, True))
        del frames[21]["usage"]
        self.assertEqual(self.translate(frames).usage, BILLED)

    def test_an_illegal_model_usage_leaves_the_usage_unknown_and_the_run_finished(self):
        first = "claude-opus-5-5"

        def counter(key, value):
            return lambda f: f[21]["modelUsage"][first].__setitem__(key, value)

        cases = {
            "missing": lambda f: f[21].pop("modelUsage"),
            "empty": lambda f: f[21].update(modelUsage={}),
            "a list": lambda f: f[21].update(modelUsage=[1, 2]),
            "an entry that is not an object": lambda f: f[21]["modelUsage"].update({first: 5}),
            "a counter missing": lambda f: f[21]["modelUsage"][first].pop("cacheReadInputTokens"),
            "a negative counter": counter("inputTokens", -1),
            "a float counter": counter("outputTokens", 2.0),
            "a boolean counter": counter("cacheCreationInputTokens", True),
            "a text counter": counter("inputTokens", "6"),
            "a counter beyond the bound": counter("outputTokens", MAX_SAFE_INTEGER + 1),
            "a sum beyond the bound": lambda f: [entry.__setitem__("inputTokens", MAX_SAFE_INTEGER)
                                                 for entry in f[21]["modelUsage"].values()],
            # Output sums to exactly the bound (legal); only the five-way total is not.
            "a total beyond the bound": counter("outputTokens", MAX_SAFE_INTEGER - 547),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                frames = fixture()
                mutate(frames)
                translation = self.translate(frames)
                self.assertEqual((translation.usage, translation.usage_complete, translation.reported_usage),
                                 ({}, False, None))
                self.assertEqual((translation.unknown, self.types(translation)[-1]), (0, "run_finished"))
                outcome, _usage, _ledger, _op = self.once(self.write_stream(frames, "usage.jsonl"))
                self.assertEqual(outcome["usage_recorded"], {})

    def test_messages_count_once_per_id_and_the_last_frame_of_a_message_wins(self):
        frames = fixture(SINGLE)
        first = copy.deepcopy(frames[1])
        first["message"]["usage"] = dict(first["message"]["usage"], output_tokens=1)
        later = copy.deepcopy(frames[1])
        later["message"]["usage"] = dict(later["message"]["usage"], output_tokens=3)
        frames[1:1] = [first, later]  # three frames of one message: 1, 3, then the recorded 4
        translation = self.translate(frames)
        self.assertEqual(translation.assistant_messages, 1)
        self.assertEqual(translation.message_usage, SINGLE_USAGE)
        frames[3]["message"]["usage"]["output_tokens"] = -4  # the last frame's usage is illegal
        translation = self.translate(frames)
        self.assertEqual((translation.message_usage["total_tokens"], translation.illegal_message_usage), (0, 1))
        self.assertEqual(translation.usage, SINGLE_USAGE)  # the settlement does not depend on it

    # -- smctl ledger reference ---------------------------------------------------- #

    def test_the_reference_of_a_complete_tool_stream_is_the_settled_usage(self):
        outcome, usage, ledger, op = self.once(FIXTURES / (TOOLS + ".jsonl"))
        reference = self.reference(ledger, op)
        self.assertEqual(set(reference), REFERENCE_KEYS)
        self.assertEqual((reference["complete"], reference["reasons"], reference["usage"]), (True, [], BILLED))
        self.assertEqual((reference["terminal"], reference["subtype"], reference["is_error"], reference["num_turns"]),
                         ("result", "success", False, 6))
        self.assertEqual((reference["assistant_messages"], reference["tool_calls"], reference["tool_errors"],
                          reference["permission_denials"], reference["fallbacks"], reference["frames"],
                          reference["unknown"]), (4, 4, 3, 1, 1, 22, 0))
        self.assertEqual(reference["models"], ["claude-opus-4-8", "claude-opus-5-5"])

    def test_the_reference_of_a_cut_stream_sums_its_messages_and_marks_the_output_partial(self):
        _outcome, _usage, ledger, op = self.once(self.write_stream(fixture()[:13]))
        reference = self.reference(ledger, op)
        self.assertEqual((reference["complete"], reference["reasons"], reference["usage"]),
                         (False, ["no_terminal", "partial_output"], CUT_MESSAGES))
        self.assertEqual((reference["terminal"], reference["num_turns"], reference["tool_calls"]), (None, None, 3))

    def test_the_reference_reasons_come_in_one_order(self):
        cut = fixture()[:13]
        cut[1]["message"]["usage"]["input_tokens"] = "2"  # the first message's usage is illegal
        illegal_result = fixture()
        illegal_result[21]["modelUsage"] = {}
        odd_complete = fixture()
        odd_complete.insert(5, {"type": "stream_event", "event": {}})
        cut_odd = fixture()[:13] + [{"type": "stream_event", "event": {}}]
        expected = {
            "cut, one illegal message": (["no_terminal", "illegal_usage", "partial_output"],
                                         {"input_tokens": 6, "output_tokens": 9, "cache_read_tokens": 6239,
                                          "cache_write_tokens": 1144, "total_tokens": 7398}, cut),
            "an illegal result usage": (["illegal_usage", "partial_output"], MESSAGES, illegal_result),
            "an unknown frame, legal result": (["unknown_frames"], BILLED, odd_complete),
            "cut and an unknown frame": (["no_terminal", "partial_output", "unknown_frames"], CUT_MESSAGES, cut_odd),
        }
        for label, (reasons, usage, frames) in expected.items():
            with self.subTest(case=label):
                _outcome, _usage, ledger, op = self.once(self.write_stream(frames, "ref.jsonl"))
                reference = self.reference(ledger, op)
                self.assertEqual((reference["complete"], reference["reasons"], reference["usage"]),
                                 (False, reasons, usage))

    def test_the_pi_dialect_is_unchanged_and_a_pi_stream_read_as_claude_fails_closed(self):
        _outcome, _usage, ledger, op = self.once(PI_FIXTURE, adapter_id="pi")
        pi_reference = self.reference(ledger, op, dialect="pi-session-v3")
        self.assertIs(pi_reference["complete"], True)
        self.assertNotIn("partial_output", pi_reference["reasons"])
        claude_reference = self.reference(ledger, op)
        self.assertIs(claude_reference["complete"], False)
        self.assertIn("unknown_frames", claude_reference["reasons"])


if __name__ == "__main__":
    unittest.main()
