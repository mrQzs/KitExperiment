"""Translate Pi's ``--mode json`` native event stream into the normalized adapter
protocol, and settle it with the reference reducer.

Pi's streaming CLI prints one JSON object per line (a *session v3* frame).  The
supervised adapter path (:mod:`sentinelmesh.adapter_run`) only accepts normalized
event JSONL, so real Pi never reached the dispatcher's usage settlement.  This
module is the missing translation layer: it turns each frame into a vocabulary
event (or an ``unknown``/``invalid`` record) and hands the result to the frozen
reference reducer in :mod:`sentinelmesh.adapter_protocol` unchanged.

Design (``.agent/design/pi-native-events.md`` sections 3, 5, 6):

* the translator is thin -- it maps frames faithfully and lets the reference
  reducer keep pairing, ordering and termination rules (no re-implementation);
* only ``agent_settled`` ends a run; a non-``stop`` final assistant message is a
  complete ``error`` terminal, never a success;
* usage is summed only from assistant ``message_end`` observations (mirrors and
  ``message_update`` snapshots never count); an illegal value leaves the whole
  stream's usage incomplete without padding zeros;
* unknown frame types and malformed known frames fail closed: they become an
  ``unknown`` record so the stream never settles and usage stays unknown;
* payloads carry structural facts only -- text, thinking, tool arguments/results
  and ``cwd`` never enter an event, so every payload is bounded by construction.

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

#: Dialect identifier for Pi's ``--mode json`` session v3 frame stream.
DIALECT = "pi-session-v3"
#: Event ``source`` for every translated event; equals the adapter id.
SOURCE = "pi"
#: The only session version this dialect understands.
SESSION_VERSION = 3
#: The only frame that terminates a run.
TERMINAL_FRAME = "agent_settled"
#: Event type for a frame whose type is outside the dialect (or after terminal).
UNKNOWN_FRAME = "pi.unknown_frame"
#: Event type for a frame that is not an object or a known type with a bad shape.
INVALID_FRAME = "pi.invalid_frame"
#: (vendor usage key, normalized dimension) pairs; cost is deliberately not a dimension.
USAGE_KEYS = (
    ("input", "input_tokens"),
    ("output", "output_tokens"),
    ("cacheRead", "cache_read_tokens"),
    ("cacheWrite", "cache_write_tokens"),
    ("totalTokens", "total_tokens"),
)

_MESSAGE_ROLES = frozenset({"system", "user", "assistant", "toolResult"})
#: Known frame types that never produce an event and are simply counted.
_IGNORED_FRAMES = frozenset({"turn_start", "turn_end", "message_start",
                             "message_update", "tool_execution_update"})


@dataclass(frozen=True)
class PiTranslation:
    """Immutable result of translating a Pi frame stream.

    ``events`` are fresh objects; ``usage`` is the cumulative sum over legal
    assistant messages and is reported even when ``usage_complete`` is false.
    """

    dialect: str
    events: Tuple[Dict[str, Any], ...]
    frames: int
    ignored: Dict[str, int]
    unknown: int
    usage: Dict[str, int]
    usage_complete: bool
    assistant_messages: int
    tool_calls: int
    tool_errors: int
    agent_cycles: int
    terminal: Optional[str]
    last_stop_reason: Optional[str]
    models: Tuple[Tuple[str, str], ...]


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
        self.usage: Dict[str, int] = {dimension: 0 for _vendor, dimension in USAGE_KEYS}
        self.usage_complete = True
        self.assistant_messages = 0
        self.tool_calls = 0
        self.tool_errors = 0
        self.agent_cycles = 0
        self.session_seen = False
        self.terminal_seen = False
        self.terminal: Optional[str] = None
        self.last_stop_reason: Optional[str] = None
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
            self._odd(line, frame, frame_type, "frame after agent_settled", UNKNOWN_FRAME)
            return
        if frame_type == "session":
            self._session(frame, line, frame_type)
        elif frame_type == "message_end":
            self._message_end(frame, line, frame_type)
        elif frame_type == "tool_execution_start":
            self._tool_start(frame, line, frame_type)
        elif frame_type == "tool_execution_end":
            self._tool_end(frame, line, frame_type)
        elif frame_type == TERMINAL_FRAME:
            self._settle(frame, line, frame_type)
        elif frame_type == "agent_start":
            self.agent_cycles += 1
            self._ignore("agent_start")
        elif frame_type == "agent_end":
            self._agent_end(frame, line, frame_type)
        elif frame_type in _IGNORED_FRAMES:
            self._ignore(frame_type)
        else:
            self._odd(line, frame, frame_type, "frame type outside the dialect", UNKNOWN_FRAME)

    def _session(self, frame: Dict[str, Any], line: int, frame_type: str) -> None:
        if self.session_seen:
            self._odd(line, frame, frame_type, "second session header", INVALID_FRAME)
            return
        self.session_seen = True
        if line != 1:
            self._odd(line, frame, frame_type, "session header is not the first frame", INVALID_FRAME)
            return
        version = frame.get("version")
        if type(version) is not int or version != SESSION_VERSION:
            self._odd(line, frame, frame_type, "session version is not 3", INVALID_FRAME)
            return
        session_id = frame.get("id")
        timestamp = frame.get("timestamp")
        if type(session_id) is not str or type(timestamp) is not str:
            self._odd(line, frame, frame_type, "session id or timestamp is not text", INVALID_FRAME)
            return
        self._emit("run_started", "L%d" % line,
                   {"dialect": DIALECT, "session_id": session_id,
                    "session_version": SESSION_VERSION, "timestamp": timestamp})

    def _message_end(self, frame: Dict[str, Any], line: int, frame_type: str) -> None:
        message = frame.get("message")
        if type(message) is not dict:
            self._odd(line, frame, frame_type, "message is not an object", INVALID_FRAME)
            return
        role = message.get("role")
        if type(role) is not str or role not in _MESSAGE_ROLES:
            self._odd(line, frame, frame_type, "message role outside the dialect", INVALID_FRAME)
            return
        if role != "assistant":
            self._ignore("message_end")
            return
        content = message.get("content")
        if type(content) is not list:
            self._odd(line, frame, frame_type, "assistant content is not a list", INVALID_FRAME)
            return
        text_blocks = 0
        thinking_blocks = 0
        tool_call_ids: list = []
        for block in content:
            if type(block) is not dict or type(block.get("type")) is not str:
                self._odd(line, frame, frame_type, "assistant content block is malformed", INVALID_FRAME)
                return
            block_type = block["type"]
            if block_type == "text":
                text_blocks += 1
            elif block_type == "thinking":
                thinking_blocks += 1
            elif block_type == "toolCall":
                call_id = block.get("id")
                if type(call_id) is not str or not call_id:
                    self._odd(line, frame, frame_type, "toolCall block without an id", INVALID_FRAME)
                    return
                tool_call_ids.append(call_id)
        stop_reason = message.get("stopReason")
        if type(stop_reason) is not str:
            self._odd(line, frame, frame_type, "stopReason is not text", INVALID_FRAME)
            return
        provider = message.get("provider")
        if type(provider) is not str:
            self._odd(line, frame, frame_type, "provider is not text", INVALID_FRAME)
            return
        model = message.get("model")
        if type(model) is not str:
            self._odd(line, frame, frame_type, "model is not text", INVALID_FRAME)
            return
        response_id = message.get("responseId")
        if response_id is not None and type(response_id) is not str:
            self._odd(line, frame, frame_type, "responseId is not text", INVALID_FRAME)
            return
        thinking_level = message.get("thinkingLevel")
        if thinking_level is not None and type(thinking_level) is not str:
            self._odd(line, frame, frame_type, "thinkingLevel is not text", INVALID_FRAME)
            return
        # Usage legality is not a frame-shape error: an illegal value leaves this
        # message's own usage unrecorded and the whole stream incomplete.
        message_usage = self._usage_of(message.get("usage"))
        if message_usage is None:
            self.usage_complete = False
        else:
            for _vendor, dimension in USAGE_KEYS:
                self.usage[dimension] += message_usage[dimension]
        self.assistant_messages += 1
        self.models.add((provider, model))
        self.last_stop_reason = stop_reason
        self._emit("assistant", "L%d.assistant" % line,
                   {"stop_reason": stop_reason, "provider": provider, "model": model,
                    "response_id": response_id, "thinking_level": thinking_level,
                    "text_blocks": text_blocks, "thinking_blocks": thinking_blocks,
                    "tool_calls": list(tool_call_ids), "usage": message_usage})
        usage_payload: Dict[str, Any] = {"assistant_messages": self.assistant_messages,
                                         "tool_calls": self.tool_calls,
                                         "complete": self.usage_complete}
        if self.usage_complete:
            for _vendor, dimension in USAGE_KEYS:
                usage_payload[dimension] = self.usage[dimension]
        self._emit("usage", "L%d.usage" % line, usage_payload)

    @staticmethod
    def _usage_of(usage_obj: Any) -> Optional[Dict[str, int]]:
        """Return this message's five dimensions, or ``None`` if any is illegal."""
        if type(usage_obj) is not dict:
            return None
        result: Dict[str, int] = {}
        for vendor, dimension in USAGE_KEYS:
            if vendor not in usage_obj:
                return None
            value = usage_obj[vendor]
            if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
                return None
            result[dimension] = value
        return result

    def _tool_start(self, frame: Dict[str, Any], line: int, frame_type: str) -> None:
        call_id = frame.get("toolCallId")
        name = frame.get("toolName")
        if type(call_id) is not str or not call_id or type(name) is not str or not name:
            self._odd(line, frame, frame_type, "toolCallId or toolName is not text", INVALID_FRAME)
            return
        self.tool_calls += 1
        self._emit("tool_started", "L%d" % line, {"tool_id": call_id, "tool": name})

    def _tool_end(self, frame: Dict[str, Any], line: int, frame_type: str) -> None:
        call_id = frame.get("toolCallId")
        name = frame.get("toolName")
        if type(call_id) is not str or not call_id or type(name) is not str or not name:
            self._odd(line, frame, frame_type, "toolCallId or toolName is not text", INVALID_FRAME)
            return
        is_error = frame.get("isError")
        if type(is_error) is not bool:
            self._odd(line, frame, frame_type, "isError is not a boolean", INVALID_FRAME)
            return
        if is_error:
            self.tool_errors += 1
        self._emit("tool_finished", "L%d" % line,
                   {"tool_id": call_id, "tool": name, "is_error": is_error})

    def _agent_end(self, frame: Dict[str, Any], line: int, frame_type: str) -> None:
        will_retry = frame.get("willRetry")
        if type(will_retry) is not bool:
            self._odd(line, frame, frame_type, "willRetry is not a boolean", INVALID_FRAME)
            return
        self._ignore("agent_end")

    def _settle(self, frame: Dict[str, Any], line: int, frame_type: str) -> None:
        self.terminal_seen = True
        self.terminal = TERMINAL_FRAME
        payload: Dict[str, Any] = {"stop_reason": self.last_stop_reason,
                                   "assistant_messages": self.assistant_messages,
                                   "tool_calls": self.tool_calls,
                                   "agent_cycles": self.agent_cycles}
        if self.last_stop_reason == "stop":
            self._emit("run_finished", "L%d" % line, payload)
        else:
            if self.assistant_messages == 0:
                payload["reason"] = "no_assistant_message"
            else:
                payload["reason"] = "stop_reason:" + self.last_stop_reason
            self._emit("error", "L%d" % line, payload)

    def result(self) -> PiTranslation:
        return PiTranslation(
            dialect=DIALECT,
            events=tuple(self.events),
            frames=self.frames,
            ignored=dict(self.ignored),
            unknown=self.unknown,
            usage=dict(self.usage),
            usage_complete=self.usage_complete,
            assistant_messages=self.assistant_messages,
            tool_calls=self.tool_calls,
            tool_errors=self.tool_errors,
            agent_cycles=self.agent_cycles,
            terminal=self.terminal,
            last_stop_reason=self.last_stop_reason,
            models=tuple(sorted(self.models)),
        )


def translate(frames: Any) -> PiTranslation:
    """Translate an iterable of Pi frames into a :class:`PiTranslation`.

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


class PiNativeAdapter(OfflineAdapter):
    """Offline adapter that translates Pi's native stream and settles it offline."""

    adapter_id = "pi"
    protocol_version = DIALECT

    def probe(self) -> Dict[str, Any]:
        report = dict(super().probe())
        report["dialect"] = DIALECT
        return report

    def translate(self, frames: Any) -> PiTranslation:
        return translate(frames)

    def run(self, invocation: Any, frames: Any) -> Any:
        if not isinstance(invocation, Invocation):
            raise AdapterError("E_ADAPTER_INVOCATION", "invocation must be an Invocation")
        if invocation.adapter_id is not None and invocation.adapter_id != self.adapter_id:
            raise AdapterError("E_ADAPTER_IDENTITY", "invocation adapter does not match the pi adapter")
        translation = translate(frames)
        # Hand the reference reducer copies so the translation result is never mutated.
        events = [copy.deepcopy(event) for event in translation.events]
        return super().run(invocation, events)


__all__ = [
    "DIALECT",
    "SOURCE",
    "SESSION_VERSION",
    "TERMINAL_FRAME",
    "UNKNOWN_FRAME",
    "INVALID_FRAME",
    "USAGE_KEYS",
    "PiTranslation",
    "PiNativeAdapter",
    "translate",
]
