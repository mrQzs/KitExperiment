"""Planner-only evidence: every captured Pi stream, in full, against the dispatcher.

The dispatcher keeps Pi's stdout byte for byte in ``.agent/runs/<task>/attempt-NNN/events.jsonl``
and its own summary in ``events-summary.json``.  Those directories are ignored by git and
never reach an executor's worktree, so this module skips itself wherever they are absent
and runs only on a machine that holds the runs (the planner's); ``KITEXPERIMENT_RUNS_DIR``
points it at another runs directory.

It checks two things the committed fixtures cannot:

* on every full stream the translation agrees with the dispatcher's independent sums,
  every line parses under the protocol's strict parser, and the adapter settles exactly
  the runs the dispatcher saw complete;
* each reduced fixture translates exactly like the full stream it was reduced from, so the
  reduction in ``tests/fixtures/pi_native/reduce.py`` changed no structural fact.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from sentinelmesh.adapter_protocol import Invocation
from sentinelmesh.pi_events import TERMINAL_FRAME, USAGE_KEYS, PiNativeAdapter, translate
from sentinelmesh.protocols import strict_json_loads

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pi_native"
RUNS_ENV = "KITEXPERIMENT_RUNS_DIR"
INVOCATION = Invocation("req-1", "run-1", "executor", "profile-1", ("pi", "--mode", "json"),
                        ("streaming_events",), "a" * 64, adapter_id="pi")


def runs_dir() -> Path:
    override = os.environ.get(RUNS_ENV)
    return Path(override) if override else REPO_ROOT / ".agent" / "runs"


def attempts():
    found = []
    for events in sorted(runs_dir().glob("*/attempt-*/events.jsonl")):
        if (events.parent / "events-summary.json").exists():
            found.append(events.parent)
    return found


def frames_of(path: Path):
    raw = path.read_bytes()
    segments = raw.split(b"\n")
    if segments and segments[-1] == b"":
        segments.pop()
    return [strict_json_loads(segment) for segment in segments]


def summary_of(attempt: Path):
    return json.loads((attempt / "events-summary.json").read_text(encoding="utf-8"))


@unittest.skipUnless(attempts(), "captured Pi streams are planner evidence; none under %s" % runs_dir())
class CapturedStreams(unittest.TestCase):
    def test_every_captured_stream_agrees_with_the_dispatcher(self):
        seen = 0
        for attempt in attempts():
            with self.subTest(attempt=attempt.parent.name + "/" + attempt.name):
                summary = summary_of(attempt)
                frames = frames_of(attempt / "events.jsonl")
                self.assertEqual(len(frames), summary["lines"])
                translation = translate(frames)
                self.assertEqual(translation.unknown, 0)
                self.assertTrue(translation.usage_complete)
                for vendor, dimension in USAGE_KEYS:
                    self.assertEqual(translation.usage[dimension], summary["usage"][vendor], dimension)
                self.assertEqual(translation.assistant_messages, summary["assistant_messages"])
                self.assertEqual(translation.tool_calls, sum(summary["tool_calls"].values()))
                self.assertEqual(translation.tool_errors, summary["tool_errors"])
                self.assertEqual(translation.last_stop_reason, summary["final_stop_reason"])
                self.assertEqual(translation.agent_cycles, summary["event_counts"]["agent_start"])
                self.assertEqual(translation.terminal, TERMINAL_FRAME if summary["complete"] else None)
                self.assertEqual(translation.models,
                                 tuple(sorted((m["provider"], m["model"]) for m in summary["actual_models"])))
                result = PiNativeAdapter().run(INVOCATION, frames)
                self.assertEqual(result.settled, summary["complete"])
                if summary["complete"]:
                    self.assertEqual(result.status, "SUCCEEDED" if summary["final_stop_reason"] == "stop" else "ERROR")
                    self.assertEqual(result.usage["total_tokens"], summary["usage"]["totalTokens"])
                else:
                    self.assertEqual(dict(result.usage), {"status": "unknown"})
                seen += 1
        self.assertGreaterEqual(seen, 1)

    def test_each_reduced_fixture_translates_like_its_source(self):
        metas = sorted(FIXTURES.glob("*.meta.json"))
        self.assertTrue(metas, "no fixtures to compare")
        for meta_path in metas:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            with self.subTest(fixture=meta["fixture"]):
                attempt_id = meta["source"]["attempt_id"]
                attempt = runs_dir() / meta["source"]["task_id"] / ("attempt-" + attempt_id.rsplit(".a", 1)[1])
                if not (attempt / "events.jsonl").exists():
                    self.skipTest("source attempt %s is not on this machine" % attempt_id)
                full = translate(frames_of(attempt / "events.jsonl"))
                reduced = translate(frames_of(FIXTURES / (meta["fixture"] + ".jsonl")))
                # Event ids carry the line number of their frame, which the elision moves;
                # everything else about every event has to be identical.
                self.assertEqual([(e["seq"], e["type"], e["source"], e["payload"]) for e in reduced.events],
                                 [(e["seq"], e["type"], e["source"], e["payload"]) for e in full.events])
                self.assertEqual(reduced.usage, full.usage)
                self.assertEqual((reduced.assistant_messages, reduced.tool_calls, reduced.tool_errors,
                                  reduced.agent_cycles, reduced.terminal, reduced.last_stop_reason,
                                  reduced.models, reduced.unknown, reduced.usage_complete),
                                 (full.assistant_messages, full.tool_calls, full.tool_errors, full.agent_cycles,
                                  full.terminal, full.last_stop_reason, full.models, full.unknown,
                                  full.usage_complete))
                self.assertEqual(full.frames - reduced.frames,
                                 meta["reduction"]["elided_message_updates"] + meta["reduction"]["elided_tool_updates"])


if __name__ == "__main__":
    unittest.main()
