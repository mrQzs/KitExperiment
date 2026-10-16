"""Reduce a recorded Claude Code ``--output-format stream-json`` stream into a committed fixture.

The planner recorded one real stream on 2026-10-16 with the user's approval
(`.agent/design/claude-adapter.md`, section 3.1): ``smctl run once`` ran the
native ``claude.exe`` under the Windows supervisor with a fixed one-turn prompt,
no tools, no settings and no MCP.  The raw stdout carries local paths (``cwd``,
``memory_paths`` and the like) and the account's rate-limit utilisation; this
tool keeps every structural fact and drops those values, so the stream can live
in ``tests/fixtures/claude_stream/`` without any release-check exception.

Rules (deterministic):

* every frame and every key is kept, in order; numbers, booleans and null are
  untouched, except the numbers inside ``rate_limit_info`` (any depth), which
  are set to 0 and listed in the meta file;
* a string value is kept when it is at most 32 characters long and does not
  look like a path (no ``:\\``, no backslash, does not start with ``/``);
  otherwise it becomes ``[reduced len=N sha256=<12 hex>]`` -- no prefix is kept,
  so no local path survives;
* ``<name>.meta.json`` records the provenance (the recording, Claude Code's
  version, the platform, sha256 and size of the original stream, the
  executable's sha256), the CLI's own report from the ``result`` frame (token
  usage, ``total_cost_usd``, ``is_error``, ``subtype``, ``num_turns``) and the
  ``smctl run once`` outcome of the recording.

Usage::

    python tests/fixtures/claude_stream/reduce.py <stdout.log> <once.json> <fixture-name> --version V --exe-sha256 H [--out-dir DIR]

Planner tool: it reads outside the test tree and is never run by a contract check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path

MAX_KEPT = 32
TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _looks_like_path(text: str) -> bool:
    return ":\\" in text or "\\" in text or text.startswith("/")


def reduce_string(text: str) -> str:
    if len(text) <= MAX_KEPT and not _looks_like_path(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return "[reduced len=%d sha256=%s]" % (len(text), digest)


def reduce_value(value, zeroed, path=(), in_rate_limit=False):
    if isinstance(value, dict):
        return {key: reduce_value(item, zeroed, path + (key,), in_rate_limit or key == "rate_limit_info")
                for key, item in value.items()}
    if isinstance(value, list):
        return [reduce_value(item, zeroed, path + (str(index),), in_rate_limit) for index, item in enumerate(value)]
    if isinstance(value, str):
        return reduce_string(value)
    if in_rate_limit and isinstance(value, (int, float)) and not isinstance(value, bool):
        zeroed.append(".".join(path))
        return 0
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stdout")
    parser.add_argument("once")
    parser.add_argument("name")
    parser.add_argument("--version", required=True)
    parser.add_argument("--exe-sha256", required=True)
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent))
    args = parser.parse_args(argv)
    raw = Path(args.stdout).read_bytes()
    frames = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    zeroed = []
    reduced = [reduce_value(frame, zeroed) for frame in frames]
    result = next((frame for frame in frames if frame.get("type") == "result"), {})
    usage = result.get("usage") or {}
    outcome = json.loads(Path(args.once).read_text(encoding="utf-8"))["outcome"]
    meta = {
        "source": "planner recording, approved by the user on 2026-10-16 (.agent/design/claude-adapter.md section 3.1)",
        "claude_code_version": args.version,
        "platform": "%s %s" % (platform.system(), platform.release()),
        "executable_sha256": args.exe_sha256,
        "original": {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "frames": len(frames)},
        "frame_types": [frame.get("type") for frame in frames],
        "cli_report": {"usage": {key: usage.get(key) for key in TOKEN_KEYS},
                       "total_cost_usd": result.get("total_cost_usd"), "is_error": result.get("is_error"),
                       "subtype": result.get("subtype"), "num_turns": result.get("num_turns")},
        "smctl_once": {key: outcome.get(key) for key in ("state", "exit_code", "stop_reason")}
                      | {"stream_status": (outcome.get("stream") or {}).get("status"), "adapter_id": "offline"},
        "reduction": {"max_kept_chars": MAX_KEPT, "zeroed_numbers": zeroed},
    }
    out = Path(args.out_dir)
    (out / (args.name + ".jsonl")).write_bytes(
        "".join(json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n" for frame in reduced).encode("utf-8"))
    (out / (args.name + ".meta.json")).write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote %s (%d frames, %d numbers zeroed)" % (args.name, len(reduced), len(zeroed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
