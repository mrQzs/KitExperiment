"""Reduce a captured Pi event stream into a committed fixture.

A dispatcher attempt directory (``.agent/runs/<task>/attempt-NNN``) holds Pi's
stdout byte for byte in ``events.jsonl`` and the dispatcher's own summary in
``events-summary.json``.  The full stream is megabytes of prompts, file
contents and streaming deltas; this tool keeps every structural fact and drops
the bulk, so a real stream can live in ``tests/fixtures/pi_native/`` and be
checked against the dispatcher's independent numbers.

Rules (deterministic; the translator must give the same result on the reduced
stream as on the full one, which ``tests/test_pi_events_captured.py`` asserts
on the planner's machine):

* every frame is kept, except the streaming update frames: inside each
  assistant message only the first and the last ``message_update`` are kept,
  and for each tool call only the first and the last ``tool_execution_update``;
  the elided frames are counted.  They carry text deltas, partial results and
  cumulative usage snapshots, none of which the translator reads;
* every string value longer than 64 characters is replaced by its first 48
  characters followed by ``…[reduced len=N sha256=<12 hex>]``; keys, numbers,
  booleans, null and the nesting are untouched;
* ``<name>.meta.json`` records the provenance (attempt, pi version, platform,
  dispatcher version, sha256 and size of the original) and the dispatcher's
  summary (usage, assistant messages, tool calls, tool errors, final stop
  reason, terminal event, completeness, event counts, models).

Usage::

    python tests/fixtures/pi_native/reduce.py <attempt-dir> <fixture-name> [--out-dir tests/fixtures/pi_native]

Planner tool: it reads outside the test tree and is never run by a contract check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

LIMIT = 64
KEEP = 48
SUMMARY_FIELDS = ("usage", "assistant_messages", "tool_calls", "tool_errors", "final_stop_reason",
                  "terminal_event", "complete", "event_counts", "actual_models", "lines")


def shorten(value):
    """Return ``(reduced value, number of strings shortened)``."""
    if type(value) is str:
        if len(value) <= LIMIT:
            return value, 0
        digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]
        return value[:KEEP] + "…[reduced len=%d sha256=%s]" % (len(value), digest), 1
    if type(value) is list:
        items, count = [], 0
        for item in value:
            reduced, n = shorten(item)
            items.append(reduced)
            count += n
        return items, count
    if type(value) is dict:
        mapping, count = {}, 0
        for key, item in value.items():
            reduced, n = shorten(item)
            mapping[key] = reduced
            count += n
        return mapping, count
    return value, 0


def _first_and_last(frames):
    kept = []
    if frames:
        kept.append(frames[0])
    if len(frames) > 1:
        kept.append(frames[-1])
    return kept, max(0, len(frames) - 2)


def reduce_frames(frames):
    """Elide the inner streaming updates of each assistant message and tool call.

    Returns ``(kept frames, elided message updates, elided tool updates)``.
    """
    kept, in_assistant, pending = [], False, []
    tool_updates = {}
    elided_messages = elided_tools = 0

    def flush_tool(tool_id):
        nonlocal elided_tools
        updates = tool_updates.pop(tool_id, [])
        first_last, elided = _first_and_last(updates)
        kept.extend(first_last)
        elided_tools += elided

    for frame in frames:
        kind = frame.get("type") if isinstance(frame, dict) else None
        if kind == "message_start":
            message = frame.get("message") if isinstance(frame.get("message"), dict) else {}
            in_assistant = message.get("role") == "assistant"
            kept.append(frame)
            continue
        if kind == "message_update" and in_assistant:
            pending.append(frame)
            continue
        if kind == "message_end" and in_assistant:
            first_last, elided = _first_and_last(pending)
            kept.extend(first_last)
            elided_messages += elided
            pending, in_assistant = [], False
        if kind == "tool_execution_update":
            tool_updates.setdefault(frame.get("toolCallId"), []).append(frame)
            continue
        if kind == "tool_execution_end":
            flush_tool(frame.get("toolCallId"))
        kept.append(frame)
    # A stream cut inside a message or a tool call: the pending updates are
    # reduced the same way, first and last kept.
    first_last, elided = _first_and_last(pending)
    kept.extend(first_last)
    elided_messages += elided
    for tool_id in list(tool_updates):
        flush_tool(tool_id)
    return kept, elided_messages, elided_tools


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("attempt_dir")
    parser.add_argument("fixture_name")
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent))
    args = parser.parse_args(argv)
    attempt = Path(args.attempt_dir)
    out_dir = Path(args.out_dir)
    raw = (attempt / "events.jsonl").read_bytes()
    segments = raw.split(b"\n")
    if segments and segments[-1] == b"":
        segments.pop()
    frames = [json.loads(segment.decode("utf-8")) for segment in segments]
    kept, elided_messages, elided_tools = reduce_frames(frames)
    reduced, truncated = [], 0
    for frame in kept:
        value, n = shorten(frame)
        reduced.append(value)
        truncated += n
    lines = [json.dumps(frame, ensure_ascii=False, separators=(",", ":")) for frame in reduced]
    body = ("\n".join(lines) + "\n").encode("utf-8")
    meta = load_json(attempt / "meta.json")
    summary = load_json(attempt / "events-summary.json")
    preflight = attempt / "preflight" / "preflight.json"
    pi_version = load_json(preflight).get("pi_version") if preflight.exists() else None
    record = {
        "fixture": args.fixture_name,
        "dialect": "pi-session-v3",
        "source": {
            "attempt_id": meta.get("attempt_id"), "task_id": meta.get("task_id"),
            "platform": meta.get("platform"), "tool_version": meta.get("tool_version"),
            "pi_version": pi_version, "created_at": meta.get("created_at"),
            "events_sha256": hashlib.sha256(raw).hexdigest(), "events_lines": len(segments),
            "events_bytes": len(raw),
        },
        "reduction": {
            "rule": "every frame kept except the inner message_update frames of each assistant message and the "
                    "inner tool_execution_update frames of each tool call; strings over %d characters cut to %d "
                    "plus a length and sha256 marker" % (LIMIT, KEEP),
            "kept_lines": len(lines), "bytes": len(body), "elided_message_updates": elided_messages,
            "elided_tool_updates": elided_tools, "truncated_strings": truncated,
        },
        "dispatcher_summary": {field: summary.get(field) for field in SUMMARY_FIELDS},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / (args.fixture_name + ".jsonl")).write_bytes(body)
    with open(out_dir / (args.fixture_name + ".meta.json"), "w", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print("%s: %d -> %d lines, %d bytes, %d message updates and %d tool updates elided, %d strings cut"
          % (args.fixture_name, len(segments), len(lines), len(body), elided_messages, elided_tools, truncated))
    return 0


if __name__ == "__main__":
    sys.exit(main())
