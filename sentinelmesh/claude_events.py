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

Design (``.agent/design/claude-tools.md`` sections 3.2, 5 and 9):

* the translator is thin -- it maps frames faithfully and lets the reference
  reducer keep pairing, ordering and termination rules (no re-implementation);
* ``system`` with ``subtype: init`` is the single ``run_started`` and must be the
  first frame; it carries the session id, the model and the CLI version;
* ``assistant`` (``message.role == "assistant"``) is an ``assistant`` event
  carrying block counts and the message id -- never any text or thinking -- and
  each ``tool_use`` block then becomes one ``tool_started`` (``tool_id``,
  ``tool``) after it, in block order; a repeated ``tool_use`` id, a missing id or
  name, or any other block fails the whole frame closed;
* ``user`` (``message.role == "user"``) carries the ``tool_result`` blocks: each
  becomes one ``tool_finished`` (``tool_id``, ``tool``, ``is_error`` -- absent
  means false) that must close an already-open tool; a result for a tool that is
  not open, a repeated id in one frame or a non-boolean ``is_error`` fails the
  frame closed.  The reducer pairs ``tool_started``/``tool_finished`` and refuses
  a terminal that arrives with a tool still open (``E_ADAPTER_TOOL``);
* a ``user`` text message marked ``isSynthetic``, ``system`` ``informational``
  and ``thinking_tokens`` notices and ``rate_limit_event`` are counted and
  ignored; a ``system`` ``permission_denied`` is ignored and also counted in
  ``permission_denials``; a ``system`` ``model_refusal_fallback`` is accepted
  (``fallbacks``) and its fallback model joins ``models``.  A frame with a
  ``parent_tool_use_id`` (a subagent), a plain ``user`` text message, another
  ``system`` subtype or any other frame type still fail closed;
* ``result`` is the only terminal.  ``subtype == "success"`` with
  ``is_error: false`` and at least one assistant message is ``run_finished``;
  any other well-formed ``result`` is a complete ``error`` terminal with a
  ``reason``.  A ``result`` whose ``subtype``, ``is_error`` or ``num_turns`` is
  malformed (``num_turns`` must be an integer in ``0..MAX_SAFE_INTEGER``) is an
  invalid record and no terminal at all;
* usage is settled from the terminal ``result``'s ``modelUsage`` alone -- the
  four counters of every model summed per dimension, plus their sum as
  ``total_tokens`` -- because that sum is the billed total (it includes retracted
  turns and the fallback model); ``result.usage`` is no longer read.  Each
  per-message observation is also summed per ``message.id`` into
  ``message_usage``; an illegal counter in a well-formed ``result`` still ends
  the run, with its usage unknown;
* unknown frame types, unknown ``system`` subtypes, subagent frames, frames after
  ``result`` and malformed known frames all fail closed: they become an
  ``unknown`` record, so the stream never settles and no usage is recorded;
* payloads carry structural facts only -- text, thinking and tool input never
  enter an event, so every payload is bounded by construction.

Two recordings (2026-10-16, ``tests/fixtures/claude_stream/``) cover the dialect:
a single turn without tools, and a six-turn run with tool calls, a permission
denial, a refusal fallback that retracts three frames and split assistant frames.
Subagents, compaction, hooks, API retries, ``stream_event``, image blocks and
write/exec tools have no sample here and are therefore refused as unknown frames,
exactly as pi's translator refuses what it has never seen.

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
#: (per-message usage key, normalized dimension) pairs; cost is deliberately not a dimension.
USAGE_KEYS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
    ("cache_creation_input_tokens", "cache_write_tokens"),
)
#: (per-model ``modelUsage`` key, normalized dimension) pairs for the billed total.
MODEL_USAGE_KEYS = (
    ("inputTokens", "input_tokens"),
    ("outputTokens", "output_tokens"),
    ("cacheReadInputTokens", "cache_read_tokens"),
    ("cacheCreationInputTokens", "cache_write_tokens"),
)
#: The four token dimensions, in a fixed order, and their summed dimension.
_DIMENSIONS = tuple(dimension for _vendor, dimension in USAGE_KEYS)
#: The dimension summed from the four vendor counters.
TOTAL_DIMENSION = "total_tokens"
#: The only ``system`` subtype that opens a run.
_INIT_SUBTYPE = "init"
#: The ``system`` subtype of a permission denial, counted separately.
_PERMISSION_DENIED_SUBTYPE = "permission_denied"
#: The ``system`` subtype of a refusal-triggered model fallback.
_FALLBACK_SUBTYPE = "model_refusal_fallback"
#: The ``system`` subtypes that are counted and ignored, with no other effect.
_IGNORED_SUBTYPES = frozenset({"informational", "thinking_tokens"})
#: Known frame types that never produce an event and are simply counted.
_IGNORED_FRAMES = frozenset({"rate_limit_event"})
#: The assistant content block that opens a tool call.
_TOOL_USE_BLOCK = "tool_use"
#: The user content block that closes a tool call.
_TOOL_RESULT_BLOCK = "tool_result"
#: The only ``result`` subtype that can end a run successfully.
_SUCCESS_SUBTYPE = "success"


@dataclass(frozen=True)
class ClaudeTranslation:
    """Immutable result of translating a Claude Code frame stream.

    ``events`` are fresh objects.  ``usage`` holds the five dimensions of the
    terminal ``result`` and is reported only when the stream had no odd frame at
    all and the CLI's own counters were legal; ``usage_complete`` says the same.
    ``reported_usage`` is the same billed total, but is never cleared by an odd
    frame; ``message_usage`` sums the legal per-message observations (one per
    ``message.id``), ``illegal_message_usage`` counts the messages left out, and
    ``tool_calls``, ``tool_errors``, ``permission_denials`` and ``fallbacks``
    count what the recording showed.
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
    reported_usage: Optional[Dict[str, int]]
    message_usage: Dict[str, int]
    illegal_message_usage: int
    tool_calls: int
    tool_errors: int
    permission_denials: int
    fallbacks: int


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
        self.init_seen = False
        self.terminal_seen = False
        self.terminal: Optional[str] = None
        self.subtype: Optional[str] = None
        self.is_error: Optional[bool] = None
        self.num_turns: Optional[int] = None
        self.models: set = set()
        # claude-tools: per-message usage (last accepted frame wins), the open
        # tool table (id -> name) and every tool id this stream has ever opened.
        self._message_ids: set = set()
        self._message_usage: Dict[str, Optional[Dict[str, int]]] = {}
        self._open_tools: Dict[str, str] = {}
        self._tool_ids: set = set()
        # claude-tools: the billed total and the four running counters.
        self.reported_usage: Optional[Dict[str, int]] = None
        self.tool_calls = 0
        self.tool_errors = 0
        self.permission_denials = 0
        self.fallbacks = 0

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
        elif frame_type == "user":
            self._user(frame, line, frame_type)
        elif frame_type == TERMINAL_FRAME:
            self._result(frame, line, frame_type)
        elif frame_type in _IGNORED_FRAMES:
            self._ignore(frame_type)
        else:
            self._odd(line, frame, frame_type, "frame type outside the dialect", UNKNOWN_FRAME)

    def _system(self, frame: Dict[str, Any], line: int, frame_type: str) -> None:
        subtype = frame.get("subtype")
        if subtype == _INIT_SUBTYPE:
            self._init(frame, line, frame_type)
        elif subtype == _PERMISSION_DENIED_SUBTYPE:
            tool_use_id = frame.get("tool_use_id")
            if type(tool_use_id) is not str or not tool_use_id:
                self._odd(line, frame, frame_type, "permission denial has no tool id", INVALID_FRAME)
                return
            self.permission_denials += 1
            self._ignore("system/%s" % subtype)
        elif subtype in _IGNORED_SUBTYPES:
            self._ignore("system/%s" % subtype)
        elif subtype == _FALLBACK_SUBTYPE:
            original_model = frame.get("original_model")
            fallback_model = frame.get("fallback_model")
            if type(original_model) is not str or not original_model \
                    or type(fallback_model) is not str or not fallback_model:
                self._odd(line, frame, frame_type, "fallback models are not text", INVALID_FRAME)
                return
            self.fallbacks += 1
            self.models.add(fallback_model)
        else:
            # Another subtype, or one that is not text at all: no sample, fail closed.
            self._odd(line, frame, frame_type, "system subtype is outside the dialect", UNKNOWN_FRAME)

    def _init(self, frame: Dict[str, Any], line: int, frame_type: str) -> None:
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
        if frame.get("parent_tool_use_id") is not None:
            # A subagent frame: no sample, so fail closed.
            self._odd(line, frame, frame_type, "assistant frame of a subagent", UNKNOWN_FRAME)
            return
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
        tool_uses: list = []
        local_ids: set = set()
        for block in content:
            if type(block) is not dict or type(block.get("type")) is not str:
                self._odd(line, frame, frame_type, "assistant content block is malformed", INVALID_FRAME)
                return
            block_type = block["type"]
            if block_type == "text":
                text_blocks += 1
            elif block_type == "thinking":
                thinking_blocks += 1
            elif block_type == _TOOL_USE_BLOCK:
                tool_id = block.get("id")
                tool_name = block.get("name")
                if type(tool_id) is not str or not tool_id or type(tool_name) is not str or not tool_name:
                    self._odd(line, frame, frame_type, "tool_use id or name is not text", INVALID_FRAME)
                    return
                if tool_id in self._tool_ids or tool_id in local_ids:
                    # Seen in an earlier accepted frame or an earlier block here.
                    self._odd(line, frame, frame_type, "tool_use id already seen in this stream",
                              INVALID_FRAME)
                    return
                local_ids.add(tool_id)
                tool_uses.append((tool_id, tool_name))
            else:
                # Images, redacted and any other block: no sample, so fail closed.
                self._odd(line, frame, frame_type, "assistant content block is outside the dialect",
                          UNKNOWN_FRAME)
                return
        model = message.get("model")
        message_id = message.get("id")
        if type(model) is not str or type(message_id) is not str:
            self._odd(line, frame, frame_type, "model or message id is not text", INVALID_FRAME)
            return
        # The frame is accepted as a whole: commit its ids, tools and message usage.
        self._tool_ids.update(local_ids)
        for tool_id, tool_name in tool_uses:
            self._open_tools[tool_id] = tool_name
        self._message_ids.add(message_id)
        self._message_usage[message_id] = self._four_dimensions(message.get("usage"), USAGE_KEYS)
        self.models.add(model)
        # The block text itself never enters the event: only the counts do.
        self._emit("assistant", "L%d" % line,
                   {"model": model, "message_id": message_id,
                    "text_blocks": text_blocks, "thinking_blocks": thinking_blocks,
                    "tool_use_blocks": len(tool_uses)})
        for index, (tool_id, tool_name) in enumerate(tool_uses, start=1):
            self._emit("tool_started", "L%d.tool%d" % (line, index),
                       {"tool_id": tool_id, "tool": tool_name})
        self.tool_calls += len(tool_uses)

    def _user(self, frame: Dict[str, Any], line: int, frame_type: str) -> None:
        if frame.get("parent_tool_use_id") is not None:
            self._odd(line, frame, frame_type, "user frame of a subagent", UNKNOWN_FRAME)
            return
        message = frame.get("message")
        if type(message) is not dict or message.get("role") != "user":
            self._odd(line, frame, frame_type, "message is not a user message", INVALID_FRAME)
            return
        content = message.get("content")
        if type(content) is not list:
            self._odd(line, frame, frame_type, "user content is not a list", UNKNOWN_FRAME)
            return
        if frame.get("isSynthetic") is True:
            # A synthesised text turn the CLI inserted (e.g. after a refusal).
            if all(type(block) is dict and block.get("type") == "text" for block in content):
                self._ignore("user/synthetic")
                return
            self._odd(line, frame, frame_type, "synthetic user message is not plain text", UNKNOWN_FRAME)
            return
        if not content or any(type(block) is not dict or block.get("type") != _TOOL_RESULT_BLOCK
                              for block in content):
            self._odd(line, frame, frame_type, "user content is not all tool results", UNKNOWN_FRAME)
            return
        seen_in_frame: set = set()
        for block in content:
            tool_use_id = block.get("tool_use_id")
            is_error = block.get("is_error", False)
            if type(tool_use_id) is not str or not tool_use_id or tool_use_id not in self._open_tools \
                    or tool_use_id in seen_in_frame:
                self._odd(line, frame, frame_type, "tool result is not for an open tool", INVALID_FRAME)
                return
            if type(is_error) is not bool:
                self._odd(line, frame, frame_type, "tool result is_error is not a boolean", INVALID_FRAME)
                return
            seen_in_frame.add(tool_use_id)
        # The frame is accepted: close each tool in block order.
        for index, block in enumerate(content, start=1):
            tool_use_id = block["tool_use_id"]
            is_error = block.get("is_error", False)
            tool_name = self._open_tools.pop(tool_use_id)
            self._emit("tool_finished", "L%d.tool%d" % (line, index),
                       {"tool_id": tool_use_id, "tool": tool_name, "is_error": is_error})
            if is_error:
                self.tool_errors += 1

    def _result(self, frame: Dict[str, Any], line: int, frame_type: str) -> None:
        # The frame is the terminal one whatever its shape: nothing after it
        # belongs to this run.
        self.terminal_seen = True
        subtype = frame.get("subtype")
        is_error = frame.get("is_error")
        num_turns = frame.get("num_turns")
        if type(subtype) is not str or type(is_error) is not bool or type(num_turns) is not int \
                or not 0 <= num_turns <= MAX_SAFE_INTEGER:
            self._odd(line, frame, frame_type, "result subtype, is_error or num_turns is malformed",
                      INVALID_FRAME)
            return
        self.terminal = TERMINAL_FRAME
        self.subtype = subtype
        self.is_error = is_error
        self.num_turns = num_turns
        # The billed total is the sum of the per-model counters; ``result.usage``
        # is not read.  ``reported_usage`` survives an odd frame; the settlement
        # (``usage``) does not.
        self.reported_usage = self._model_usage(frame.get("modelUsage"))
        if self.reported_usage is not None and self.unknown == 0:
            self.usage = dict(self.reported_usage)
            self.usage_complete = True
        else:
            self.usage = {}
            self.usage_complete = False
        assistant_messages = len(self._message_ids)
        usage_payload: Dict[str, Any] = {"assistant_messages": assistant_messages,
                                         "complete": self.usage_complete}
        if self.usage_complete:
            usage_payload.update(self.usage)
        self._emit("usage", "L%d.usage" % line, usage_payload)
        payload: Dict[str, Any] = {"subtype": subtype, "num_turns": num_turns,
                                   "assistant_messages": assistant_messages,
                                   "tool_calls": self.tool_calls, "tool_errors": self.tool_errors,
                                   "permission_denials": self.permission_denials,
                                   "fallbacks": self.fallbacks}
        if subtype == _SUCCESS_SUBTYPE and not is_error and assistant_messages > 0:
            self._emit("run_finished", "L%d" % line, payload)
        else:
            if subtype == _SUCCESS_SUBTYPE and not is_error and assistant_messages == 0:
                payload["reason"] = "no_assistant_message"
            else:
                payload["reason"] = "subtype:%s is_error:%s" % (subtype, "true" if is_error else "false")
            self._emit("error", "L%d" % line, payload)

    @staticmethod
    def _four_dimensions(usage_obj: Any, keys: Tuple[Tuple[str, str], ...]) -> Optional[Dict[str, int]]:
        """The four dimensions of one usage object, or ``None`` if any is illegal.

        Each vendor key must be present with an exact integer (a boolean is not
        one) within ``0..MAX_SAFE_INTEGER``; any other key is ignored.
        """
        if type(usage_obj) is not dict:
            return None
        values: Dict[str, int] = {}
        for vendor, dimension in keys:
            if vendor not in usage_obj:
                return None
            value = usage_obj[vendor]
            if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
                return None
            values[dimension] = value
        return values

    @classmethod
    def _model_usage(cls, model_usage_obj: Any) -> Optional[Dict[str, int]]:
        """The billed total from ``result.modelUsage``, or ``None`` if illegal.

        ``modelUsage`` must be a non-empty object whose every entry carries the
        four counters as exact integers in ``0..MAX_SAFE_INTEGER``; the counters
        are summed per dimension and each sum, and the four-way total, must stay
        inside the same bound.
        """
        if type(model_usage_obj) is not dict or not model_usage_obj:
            return None
        totals: Dict[str, int] = {dimension: 0 for dimension in _DIMENSIONS}
        for entry in model_usage_obj.values():
            dims = cls._four_dimensions(entry, MODEL_USAGE_KEYS)
            if dims is None:
                return None
            for dimension, value in dims.items():
                totals[dimension] += value
        total = 0
        for dimension in _DIMENSIONS:
            if totals[dimension] > MAX_SAFE_INTEGER:
                return None
            total += totals[dimension]
        if total > MAX_SAFE_INTEGER:
            return None
        result = dict(totals)
        result[TOTAL_DIMENSION] = total
        return result

    def _message_usage_totals(self) -> Tuple[Dict[str, int], int]:
        """The summed per-message usage and the count of messages left out.

        Each legal message (its last accepted frame) contributes its four
        dimensions; a message whose usage was illegal is left out and counted.
        If any dimension sum, or the total, would exceed ``MAX_SAFE_INTEGER``,
        every message is treated as illegal and the sum is the empty object.
        """
        totals: Dict[str, int] = {dimension: 0 for dimension in _DIMENSIONS}
        illegal = 0
        for dims in self._message_usage.values():
            if dims is None:
                illegal += 1
            else:
                for dimension, value in dims.items():
                    totals[dimension] += value
        total = 0
        for dimension in _DIMENSIONS:
            if totals[dimension] > MAX_SAFE_INTEGER:
                return {}, len(self._message_usage)
            total += totals[dimension]
        if total > MAX_SAFE_INTEGER:
            return {}, len(self._message_usage)
        result = dict(totals)
        result[TOTAL_DIMENSION] = total
        return result, illegal

    def result(self) -> ClaudeTranslation:
        message_usage, illegal_message_usage = self._message_usage_totals()
        return ClaudeTranslation(
            dialect=DIALECT,
            events=tuple(self.events),
            frames=self.frames,
            ignored=dict(self.ignored),
            unknown=self.unknown,
            usage=dict(self.usage),
            usage_complete=self.usage_complete,
            assistant_messages=len(self._message_ids),
            terminal=self.terminal,
            subtype=self.subtype,
            is_error=self.is_error,
            num_turns=self.num_turns,
            models=tuple(sorted(self.models)),
            reported_usage=(None if self.reported_usage is None else dict(self.reported_usage)),
            message_usage=message_usage,
            illegal_message_usage=illegal_message_usage,
            tool_calls=self.tool_calls,
            tool_errors=self.tool_errors,
            permission_denials=self.permission_denials,
            fallbacks=self.fallbacks,
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
    "MODEL_USAGE_KEYS",
    "TOTAL_DIMENSION",
    "ClaudeTranslation",
    "ClaudeStreamAdapter",
    "translate",
]
