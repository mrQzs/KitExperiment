"""Translate Claude Code's ``--output-format stream-json`` stream into the
normalized adapter protocol, and settle it with the reference reducer.

Claude Code's print/streaming mode writes one JSON object per line (dialect
``claude-stream-json``).  The supervised adapter path
(:mod:`sentinelmesh.adapter_run`) only accepts normalized event JSONL, so a real
Claude Code run never reached the dispatcher's usage settlement.  This module is
the missing translation layer, structurally identical to
:mod:`sentinelmesh.pi_events` for pi: it maps each frame onto a vocabulary event
(or an ``unknown``/``invalid`` record) and hands the result to the frozen
reference reducer in :mod:`sentinelmesh.adapter_protocol` unchanged.

Design (``.agent/design/claude-adapter.md`` sections 3.2, 5 and 6):

* the translator is thin -- it maps frames faithfully and lets the reference
  reducer keep pairing, ordering and termination rules (no re-implementation);
* ``system`` with ``subtype: init`` is the single ``run_started`` and must be the
  first frame; it carries the session id, the model and the CLI version;
* ``assistant`` (``message.role == "assistant"``, content blocks ``text`` and
  ``thinking`` only) is an ``assistant`` event carrying block counts and the
  message id -- never any text or thinking;
* ``rate_limit_event`` is counted and ignored;
* ``result`` is the only terminal.  ``subtype == "success"`` with
  ``is_error: false`` and at least one assistant message is ``run_finished``;
  every other shape is a complete ``error`` terminal with a ``reason``;
* usage is taken from the terminal ``result`` alone -- its
  ``input_tokens``, ``output_tokens``, ``cache_read_input_tokens`` and
  ``cache_creation_input_tokens`` plus their sum as ``total_tokens`` -- because
  those four numbers are the session's own totals; summing the per-message
  observations would double count, and the dollar cost
  (``total_cost_usd``) is deliberately not a dimension;
* unknown frame types, unknown ``system`` subtypes, tool-use content blocks,
  frames after ``result`` and malformed known frames all fail closed: they
  become an ``unknown`` record, so the stream never settles and no usage is
  recorded;
* payloads carry structural facts only -- text, thinking and tool input never
  enter an event, so every payload is bounded by construction.

One recording (2026-10-16, ``tests/fixtures/claude_stream/``) covers a single
turn without tools.  Tool calls, multi-turn streams, retries and compaction have
no sample here and are therefore refused as unknown frames, exactly as pi's
translator refuses what it has never seen.

Importing this module has no side effect: no file, environment, process or
network access, no ``ctypes``.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from .adapter_protocol import AdapterError, Invocation, OfflineAdapter
from .protocols import MAX_SAFE_INTEGER

#: Dialect identifier for Claude Code's ``--output-format stream-json`` stream.
DIALECT = "claude-stream-json"
#: Event ``source`` for every translated event; equals the adapter id.
SOURCE = "claude"
#: The only frame that terminates a run.
TERMINAL_FRAME = "result"
#: Event type for a frame whose type is outside the dialect (or after terminal).
UNKNOWN_FRAME = "claude.unknown_frame"
#: Event type for a frame that is not an object or a known type with a bad shape.
INVALID_FRAME = "claude.invalid_frame"
#: (vendor usage key, normalized dimension) pairs; cost is deliberately not a dimension.
USAGE_KEYS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
    ("cache_creation_input_tokens", "cache_write_tokens"),
)
#: The dimension summed from the four vendor counters of the terminal frame.
TOTAL_DIMENSION = "total_tokens"
#: The only ``system`` subtype this dialect understands.
_INIT_SUBTYPE = "init"
#: Known frame types that never produce an event and are simply counted.
_IGNORED_FRAMES = frozenset({"rate_limit_event"})
#: Assistant content blocks this dialect understands; anything else fails closed.
_CONTENT_BLOCKS = frozenset({"text", "thinking"})
#: The only ``result`` subtype that can end a run successfully.
_SUCCESS_SUBTYPE = "success"


@dataclass(frozen=True)
class ClaudeTranslation:
    """Immutable result of translating a Claude Code frame stream.

    ``events`` are fresh objects.  ``usage`` holds the five dimensions of the
    terminal ``result`` and is reported only when the stream had no odd frame at
    all and the CLI's own counters were legal; ``usage_complete`` says the same.
    """

    dialect: str
    events: Tuple[Dict[str, Any], ...]
    frames: int
    ignored: Dict[str, int]
    unknown: int
    usage: Dict[str, int]
    usage_complete: bool
    assistant_messages: int
    terminal: Optional[str]
    subtype: Optional[str]
    is_error: Optional[bool]
    num_turns: Optional[int]
    models: Tuple[str, ...]


def _frame_digest(frame: Any) -> Tuple[str, int]:
    """SHA-256 and byte length of a frame's canonical JSON, for bounded evidence.

    The canonical form is ``json.dumps(frame, sort_keys=True, ensure_ascii=False,
    separators=(",", ":"), default=repr)`` UTF-8 encoded with ``surrogatepass``.
    Any failure to produce it (unsortable keys, cycles, ...) falls back to
    ``repr(frame)``; the translator never raises on frame content.
    """
    try:
        text = json.dumps(frame, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), default=repr)
        data = text.encode("utf-8", "surrogatepass")
    except Exception:
        try:
            data = repr(frame).encode("utf-8", "surrogatepass")
        except Exception:
            data = b""
    return hashlib.sha256(data).hexdigest(), len(data)


class _Accumulator:
    """Single-pass frame translator holding the running translation state."""

    def __init__(self) -> None:
        self.events: list = []
        self.frames = 0
        self.ignored: Dict[str, int] = {}
        self.unknown = 0
        self.usage: Dict[str, int] = {}
        self.usage_complete = False
        self.assistant_messages = 0
        self.init_seen = False
        self.terminal_seen = False
        self.terminal: Optional[str] = None
        self.subtype: Optional[str] = None
        self.is_error: Optional[bool] = None
        self.num_turns: Optional[int] = None
        self.models: set = set()

    # -- emit helpers -------------------------------------------------------

    def _emit(self, event_type: str, event_id: str, payload: Dict[str, Any]) -> None:
        self.events.append({"seq": len(self.events), "event_id": event_id,
                            "type": event_type, "source": SOURCE, "payload": payload})

    def _ignore(self, frame_type: str) -> None:
        self.ignored[frame_type] = self.ignored.get(frame_type, 0) + 1

    def _odd(self, line: int, frame: Any, frame_type: Optional[str], reason: str,
             event_type: str) -> None:
        digest, size = _frame_digest(frame)
        self._emit(event_type, "L%d" % line,
                   {"frame_type": frame_type, "line": line, "reason": reason,
                    "sha256": digest, "bytes": size})
        self.unknown += 1
        self.usage = {}
        self.usage_complete = False

    # -- per-frame dispatch -------------------------------------------------

    def feed(self, frame: Any, line: int) -> None:
        self.frames += 1
        if type(frame) is not dict:
            self._odd(line, frame, None, "frame is not an object", INVALID_FRAME)
            return
        frame_type = frame.get("type")
        if type(frame_type) is not str:
            self._odd(line, frame, None, "type is not text", INVALID_FRAME)
            return
        if self.terminal_seen:
            self._odd(line, frame, frame_type, "frame after result", UNKNOWN_FRAME)
            return
        if frame_type == "system":
            self._system(frame, line, frame_type)
        elif frame_type == "assistant":
            self._assistant(frame, line, frame_type)
        elif frame_type == TERMINAL_FRAME:
            self._result(frame, line, frame_type)
        elif frame_type in _IGNORED_FRAMES:
            self._ignore(frame_type)
        else:
            self._odd(line, frame, frame_type, "frame type outside the dialect", UNKNOWN_FRAME)

    def _system(self, frame: Dict[str, Any], line: int, frame_type: str) -> None:
        subtype = frame.get("subtype")
        if type(subtype) is not str or subtype != _INIT_SUBTYPE:
            self._odd(line, frame, frame_type, "system subtype is not init", UNKNOWN_FRAME)
            return
        if self.init_seen:
            self._odd(line, frame, frame_type, "second init header", INVALID_FRAME)
            return
        self.init_seen = True
        if line != 1:
            self._odd(line, frame, frame_type, "init header is not the first frame", INVALID_FRAME)
            return
        session_id = frame.get("session_id")
        model = frame.get("model")
        version = frame.get("claude_code_version")
        if type(session_id) is not str or type(model) is not str:
            self._odd(line, frame, frame_type, "session id or model is not text", INVALID_FRAME)
            return
        if version is not None and type(version) is not str:
            self._odd(line, frame, frame_type, "claude_code_version is not text", INVALID_FRAME)
            return
        self.models.add(model)
        self._emit("run_started", "L%d" % line,
                   {"dialect": DIALECT, "session_id": session_id, "model": model,
                    "claude_code_version": version})

    def _assistant(self, frame: Dict[str, Any], line: int, frame_type: str) -> None:
        message = frame.get("message")
        if type(message) is not dict or message.get("role") != "assistant":
            self._odd(line, frame, frame_type, "message is not an assistant message", INVALID_FRAME)
            return
        content = message.get("content")
        if type(content) is not list:
            self._odd(line, frame, frame_type, "assistant content is not a list", INVALID_FRAME)
            return
        text_blocks = 0
        thinking_blocks = 0
        for block in content:
            if type(block) is not dict or type(block.get("type")) is not str:
                self._odd(line, frame, frame_type, "assistant content block is malformed", INVALID_FRAME)
                return
            block_type = block["type"]
            if block_type == "text":
                text_blocks += 1
            elif block_type == "thinking":
                thinking_blocks += 1
            else:
                # Tool use, images, redacted blocks: no sample, so fail closed.
                self._odd(line, frame, frame_type, "assistant content block is outside the dialect",
                          UNKNOWN_FRAME)
                return
        model = message.get("model")
        message_id = message.get("id")
        if type(model) is not str or type(message_id) is not str:
            self._odd(line, frame, frame_type, "model or message id is not text", INVALID_FRAME)
            return
        self.assistant_messages += 1
        self.models.add(model)
        # The block text itself never enters the event: only the counts do.
        self._emit("assistant", "L%d" % line,
                   {"model": model, "message_id": message_id,
                    "text_blocks": text_blocks, "thinking_blocks": thinking_blocks})

    def _result(self, frame: Dict[str, Any], line: int, frame_type: str) -> None:
        # The frame is the terminal one whatever its shape: nothing after it
        # belongs to this run.
        self.terminal_seen = True
        subtype = frame.get("subtype")
        is_error = frame.get("is_error")
        num_turns = frame.get("num_turns")
        if type(subtype) is not str or type(is_error) is not bool or type(num_turns) is not int \
                or num_turns < 0:
            self._odd(line, frame, frame_type, "result subtype, is_error or num_turns is malformed",
                      INVALID_FRAME)
            return
        self.terminal = TERMINAL_FRAME
        self.subtype = subtype
        self.is_error = is_error
        self.num_turns = num_turns
        dimensions = self._dimensions(frame.get("usage"))
        if dimensions is not None and self.unknown == 0:
            self.usage = dimensions
            self.usage_complete = True
        else:
            # An illegal counter, or an odd frame earlier in the stream, leaves
            # the whole run's usage unknown: no padding, no partial sum.
            self.usage = {}
            self.usage_complete = False
        usage_payload: Dict[str, Any] = {"assistant_messages": self.assistant_messages,
                                         "complete": self.usage_complete}
        if self.usage_complete:
            usage_payload.update(self.usage)
        self._emit("usage", "L%d.usage" % line, usage_payload)
        payload: Dict[str, Any] = {"subtype": subtype, "num_turns": num_turns,
                                   "assistant_messages": self.assistant_messages}
        if subtype == _SUCCESS_SUBTYPE and not is_error and self.assistant_messages > 0:
            self._emit("run_finished", "L%d" % line, payload)
        else:
            if subtype == _SUCCESS_SUBTYPE and not is_error and self.assistant_messages == 0:
                payload["reason"] = "no_assistant_message"
            else:
                payload["reason"] = "subtype:%s is_error:%s" % (subtype, "true" if is_error else "false")
            self._emit("error", "L%d" % line, payload)

    @staticmethod
    def _dimensions(usage_obj: Any) -> Optional[Dict[str, int]]:
        """The terminal frame's five dimensions, or ``None`` if any is illegal.

        The CLI reports whole-session counters; each must be an exact integer
        (a boolean is not one) within ``0..MAX_SAFE_INTEGER``, and their sum must
        stay inside the same bound so the normalized ``total_tokens`` is legal.
        """
        if type(usage_obj) is not dict:
            return None
        values: Dict[str, int] = {}
        total = 0
        for vendor, dimension in USAGE_KEYS:
            if vendor not in usage_obj:
                return None
            value = usage_obj[vendor]
            if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
                return None
            values[dimension] = value
            total += value
        if total > MAX_SAFE_INTEGER:
            return None
        values[TOTAL_DIMENSION] = total
        return values

    def result(self) -> ClaudeTranslation:
        return ClaudeTranslation(
            dialect=DIALECT,
            events=tuple(self.events),
            frames=self.frames,
            ignored=dict(self.ignored),
            unknown=self.unknown,
            usage=dict(self.usage),
            usage_complete=self.usage_complete,
            assistant_messages=self.assistant_messages,
            terminal=self.terminal,
            subtype=self.subtype,
            is_error=self.is_error,
            num_turns=self.num_turns,
            models=tuple(sorted(self.models)),
        )


def translate(frames: Any) -> ClaudeTranslation:
    """Translate an iterable of Claude Code frames into a :class:`ClaudeTranslation`.

    ``frames`` may be any iterable of frame objects (list, tuple, generator); it is
    consumed once and its first element is line 1.  Text or bytes input, or a
    non-iterable, raises :class:`AdapterError` ``E_ADAPTER_EVENT``.  Frame content
    never raises: every frame becomes an event, is counted, or becomes an
    unknown/invalid record.
    """
    if isinstance(frames, (str, bytes, bytearray)):
        raise AdapterError("E_ADAPTER_EVENT",
                           "frames must be an iterable of frame objects, not text or bytes")
    try:
        iterator = iter(frames)
    except TypeError:
        raise AdapterError("E_ADAPTER_EVENT",
                           "frames must be an iterable of frame objects") from None
    accumulator = _Accumulator()
    for line, frame in enumerate(iterator, start=1):
        accumulator.feed(frame, line)
    return accumulator.result()


class ClaudeStreamAdapter(OfflineAdapter):
    """Offline adapter that translates Claude Code's stream-json and settles it offline."""

    adapter_id = "claude"
    protocol_version = DIALECT

    def probe(self) -> Dict[str, Any]:
        report = dict(super().probe())
        report["dialect"] = DIALECT
        return report

    def translate(self, frames: Any) -> ClaudeTranslation:
        return translate(frames)

    def run(self, invocation: Any, frames: Any) -> Any:
        if not isinstance(invocation, Invocation):
            raise AdapterError("E_ADAPTER_INVOCATION", "invocation must be an Invocation")
        if invocation.adapter_id is not None and invocation.adapter_id != self.adapter_id:
            raise AdapterError("E_ADAPTER_IDENTITY", "invocation adapter does not match the claude adapter")
        translation = translate(frames)
        # Hand the reference reducer copies so the translation result is never mutated.
        events = [copy.deepcopy(event) for event in translation.events]
        return super().run(invocation, events)


__all__ = [
    "DIALECT",
    "SOURCE",
    "TERMINAL_FRAME",
    "UNKNOWN_FRAME",
    "INVALID_FRAME",
    "USAGE_KEYS",
    "TOTAL_DIMENSION",
    "ClaudeTranslation",
    "ClaudeStreamAdapter",
    "translate",
]
