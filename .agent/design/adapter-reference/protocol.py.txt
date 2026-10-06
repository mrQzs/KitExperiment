"""Versioned adapter boundary shared by runtime adapters and the dispatcher."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from ..protocols import DEFAULT_MAX_BYTES, MAX_SAFE_INTEGER, ProtocolError, check_json_value
from ..runtime import RuntimeErrorCode, validate_argv


EVENT_TYPES = {"run_started", "assistant", "tool_started", "tool_finished", "usage", "run_finished", "error"}


class AdapterError(ValueError):
    def __init__(self, code: str, message: str, *, seq: Optional[int] = None):
        self.code, self.seq = code, seq
        super().__init__(f"{code}: {message}")


def _text(value: Any, name: str) -> str:
    if type(value) is not str or not value:
        raise AdapterError("E_ADAPTER_SCHEMA", f"{name} must be a non-empty string")
    # Adapter fields are protocol text, not a shell or a log escape channel.
    # Reject all C0/C1 controls, DEL, and surrogate code points before hashing
    # or passing values to a platform adapter.  Valid non-ASCII scalar text is
    # retained byte-for-byte.
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF
           for char in value):
        raise AdapterError("E_ADAPTER_SCHEMA", f"{name} contains a control or non-scalar character")
    return value


def _text_tuple(value: Any, name: str, *, nonempty: bool = False) -> Tuple[str, ...]:
    """Validate immutable string vectors used by adapter contracts."""
    if type(value) is not tuple or (nonempty and not value):
        raise AdapterError("E_ADAPTER_SCHEMA", f"{name} must be a tuple of strings")
    result = []
    seen = set()
    for index, item in enumerate(value):
        _text(item, f"{name}[{index}]")
        if item in seen:
            raise AdapterError("E_ADAPTER_SCHEMA", f"{name} contains a duplicate entry")
        seen.add(item)
        result.append(item)
    return tuple(result)


@dataclass(frozen=True)
class AdapterDescriptor:
    adapter_id: str
    protocol_version: str
    capabilities: Tuple[str, ...]
    roles: Tuple[str, ...]
    offline: bool = False

    def __post_init__(self) -> None:
        _text(self.adapter_id, "adapter_id")
        _text(self.protocol_version, "protocol_version")
        _text_tuple(self.capabilities, "capabilities")
        _text_tuple(self.roles, "roles")
        if type(self.offline) is not bool:
            raise AdapterError("E_ADAPTER_SCHEMA", "offline must be boolean")


@dataclass(frozen=True)
class Invocation:
    request_id: str
    run_id: str
    role: str
    profile_id: str
    command: Tuple[str, ...]
    capabilities: Tuple[str, ...]
    environment_sha256: str
    adapter_id: Optional[str] = None

    def __post_init__(self) -> None:
        _text(self.request_id, "request_id"); _text(self.run_id, "run_id"); _text(self.role, "role"); _text(self.profile_id, "profile_id")
        if self.adapter_id is not None:
            _text(self.adapter_id, "adapter_id")
        if type(self.command) is not tuple:
            raise AdapterError("E_ADAPTER_SCHEMA", "command must be an argv tuple")
        try:
            validate_argv(self.command)
        except RuntimeErrorCode as exc:
            raise AdapterError("E_ADAPTER_INVOCATION", "command must be a valid argv tuple") from exc
        _text_tuple(self.capabilities, "capabilities")
        if (type(self.environment_sha256) is not str
                or len(self.environment_sha256) != 64
                or any(c not in "0123456789abcdef" for c in self.environment_sha256)):
            raise AdapterError("E_ADAPTER_INVOCATION", "environment_sha256 must be lowercase SHA-256")

    def digest(self) -> str:
        payload = {"request_id": self.request_id, "run_id": self.run_id, "role": self.role,
                   "profile_id": self.profile_id, "command": list(self.command),
                   "capabilities": list(self.capabilities), "environment_sha256": self.environment_sha256}
        if self.adapter_id is not None:
            payload["adapter_id"] = self.adapter_id
        return hashlib.sha256((json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()).hexdigest()


@dataclass(frozen=True)
class NormalizedEvent:
    seq: int
    event_id: str
    event_type: str
    source: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if type(self.seq) is not int or not 0 <= self.seq <= MAX_SAFE_INTEGER:
            raise AdapterError("E_ADAPTER_EVENT_SEQ", "seq must be a non-negative integer", seq=self.seq if type(self.seq) is int else None)
        _text(self.event_id, "event_id")
        _text(self.event_type, "event_type")
        if self.event_type not in EVENT_TYPES | {"unknown"}:
            raise AdapterError("E_ADAPTER_EVENT_TYPE", "unknown event type", seq=self.seq)
        _text(self.source, "source")
        if type(self.payload) is not dict:
            raise AdapterError("E_ADAPTER_EVENT", "payload must be an object", seq=self.seq)
        _check_payload(self.payload, self.seq)


@dataclass(frozen=True)
class AdapterResult:
    invocation_sha256: str
    events: Tuple[NormalizedEvent, ...]
    settled: bool
    status: str
    usage: Mapping[str, Any]
    raw_complete: bool


def _check_payload(payload: Any, seq: Optional[int]) -> None:
    """Require a bounded, acyclic, finite JSON object for event payloads."""
    if type(payload) is not dict:
        raise AdapterError("E_ADAPTER_EVENT", "payload must be an object", seq=seq)
    try:
        check_json_value(payload, max_bytes=DEFAULT_MAX_BYTES)
    except ProtocolError as exc:
        raise AdapterError("E_ADAPTER_EVENT", "payload must be finite, acyclic JSON", seq=seq) from exc


def normalize_events(events: Iterable[Mapping[str, Any]], *, expected_source: str, preserve_unknown: bool = False) -> Tuple[NormalizedEvent, ...]:
    _text(expected_source, "expected_source")
    if type(preserve_unknown) is not bool:
        raise AdapterError("E_ADAPTER_SCHEMA", "preserve_unknown must be boolean")
    result = []
    seen_ids = set()
    expected_seq = 0
    try:
        iterator = iter(events)
    except TypeError as exc:
        raise AdapterError("E_ADAPTER_EVENT", "events must be iterable") from exc
    for raw in iterator:
        if type(raw) is not dict:
            raise AdapterError("E_ADAPTER_EVENT", "event must be an object", seq=expected_seq)
        if any(type(key) is not str for key in raw):
            raise AdapterError("E_ADAPTER_EVENT", "event fields must have string names", seq=expected_seq)
        required = {"seq", "event_id", "type", "source", "payload"}
        missing = required - set(raw)
        if missing:
            raise AdapterError("E_ADAPTER_EVENT", "event is missing a required field", seq=expected_seq)
        if set(raw) - {"seq", "event_id", "type", "source", "payload"}:
            raise AdapterError("E_ADAPTER_EVENT", "unknown event field", seq=expected_seq)
        seq = raw.get("seq")
        if type(seq) is not int or not 0 <= seq <= MAX_SAFE_INTEGER:
            raise AdapterError("E_ADAPTER_EVENT_SEQ", "seq must be a non-negative integer", seq=expected_seq)
        if seq != expected_seq:
            raise AdapterError("E_ADAPTER_EVENT_GAP", f"expected seq {expected_seq}, got {seq}", seq=seq)
        event_id = _text(raw.get("event_id"), "event_id")
        if event_id in seen_ids:
            raise AdapterError("E_ADAPTER_EVENT_DUPLICATE", "event_id repeated", seq=seq)
        seen_ids.add(event_id)
        event_type = _text(raw.get("type"), "type")
        source = _text(raw.get("source"), "source")
        raw_payload = raw.get("payload", {})
        _check_payload(raw_payload, seq)
        if event_type not in EVENT_TYPES:
            if not preserve_unknown:
                raise AdapterError("E_ADAPTER_EVENT_TYPE", "unknown event type", seq=seq)
            try:
                check_json_value(raw, max_bytes=DEFAULT_MAX_BYTES)
            except ProtocolError as exc:
                raise AdapterError("E_ADAPTER_EVENT", "unknown event must be JSON-safe", seq=seq) from exc
            payload = {"raw_event": copy.deepcopy(dict(raw)), "unknown_type": event_type}
            event_type = "unknown"
        if source != expected_source:
            raise AdapterError("E_ADAPTER_SOURCE", "event source does not match adapter", seq=seq)
        if event_type in EVENT_TYPES:
            payload = raw_payload
        result.append(NormalizedEvent(seq, event_id, event_type, source, copy.deepcopy(dict(payload))))
        expected_seq += 1
    return tuple(result)


class AdapterRegistry:
    def __init__(self, descriptors: Iterable[AdapterDescriptor] = ()) -> None:
        self._items: Dict[str, AdapterDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: AdapterDescriptor) -> None:
        if descriptor.adapter_id in self._items:
            raise AdapterError("E_ADAPTER_DUPLICATE", "adapter id already registered")
        self._items[descriptor.adapter_id] = descriptor

    def resolve(self, adapter_id: str, *, role: Optional[str] = None) -> AdapterDescriptor:
        descriptor = self._items.get(adapter_id)
        if descriptor is None:
            raise AdapterError("E_ADAPTER_UNKNOWN", "adapter is not registered")
        if role is not None and role not in descriptor.roles:
            raise AdapterError("E_ADAPTER_CAPABILITY", "adapter does not support role")
        return descriptor

    def validate_invocation(self, invocation: Invocation) -> AdapterDescriptor:
        if invocation.adapter_id is None:
            raise AdapterError("E_ADAPTER_IDENTITY", "invocation must bind an adapter descriptor")
        descriptor = self.resolve(invocation.adapter_id, role=invocation.role)
        missing = set(descriptor.capabilities) - set(invocation.capabilities)
        if missing:
            raise AdapterError("E_ADAPTER_CAPABILITY", "invocation is missing descriptor capabilities")
        return descriptor


__all__ = ["AdapterDescriptor", "AdapterError", "AdapterRegistry", "AdapterResult", "Invocation", "NormalizedEvent", "normalize_events"]
