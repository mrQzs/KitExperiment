"""Small, dependency-free implementations of SentinelMesh public protocols.

The helpers in this module deliberately operate on bytes and immutable values.
They are the common boundary used by the higher-level services; they do not
implement authorisation, filesystem race protection, or billing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

MAX_SAFE_INTEGER = 2**53 - 1
DEFAULT_MAX_JSON_BYTES = 1_048_576
DEFAULT_MAX_JSON_DEPTH = 64
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ID_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")
KIND_RE = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
REQUEST_ID_RE = re.compile(r"req-[0-9a-f]{32}\Z")
RUN_ID_RE = re.compile(r"run-[0-9a-f]{32}\Z")
OBSERVED_AT_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z"
)

_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    prefix + digit
    for prefix in ("COM", "LPT")
    for digit in tuple("123456789") + ("¹", "²", "³")
}


class ProtocolError(ValueError):
    """Stable machine-readable error used at public protocol boundaries."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
        field: str | None = None,
        reason: str | None = None,
        retryable: bool = False,
    ) -> None:
        self.code = str(code)
        self.message = message or self.code
        merged: dict[str, Any] = dict(details or {})
        if field is not None:
            merged.setdefault("field", field)
        if reason is not None:
            merged.setdefault("reason", reason)
        self.details = merged
        self.retryable = bool(retryable)
        super().__init__(self.code)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable serialisable error envelope."""
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
            "retryable": self.retryable,
        }

    as_dict = to_dict


def _pointer(parent: str, key: str | int) -> str:
    escaped = str(key).replace("~", "~0").replace("/", "~1")
    return parent + "/" + escaped


def _unicode_scalar_string(value: str, field: str = "") -> None:
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ProtocolError(
            "E_JSON_UNICODE", field=field, reason="unpaired surrogate is not allowed"
        )


class _Pairs(list[tuple[str, Any]]):
    """Intermediate object representation preserving duplicate members."""


def _reject_constant(_: str) -> Any:
    raise ProtocolError("E_JSON_NON_FINITE", reason="non-finite numeric literal")


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ProtocolError("E_JSON_NON_FINITE", reason="numeric conversion overflow")
    return value


def _parse_int(text: str) -> int:
    if len(text.lstrip("-")) > 16:
        raise ProtocolError("E_INTEGER_RANGE", reason="integer outside public profile")
    value = int(text)
    if abs(value) > MAX_SAFE_INTEGER:
        raise ProtocolError("E_INTEGER_RANGE", reason="integer outside public profile")
    return value


def _check_depth(text: str, max_depth: int) -> None:
    if type(max_depth) is not int or max_depth < 1:
        raise ProtocolError("E_JSON_LIMIT", reason="max_depth must be a positive integer")
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                raise ProtocolError("E_JSON_LIMIT", reason="nesting limit exceeded")
        elif char in "]}":
            depth -= 1


def _materialize(value: Any, field: str = "") -> Any:
    if isinstance(value, _Pairs):
        result: dict[str, Any] = {}
        for key, item in value:
            child = _pointer(field, key)
            _unicode_scalar_string(key, child)
            if key in result:
                raise ProtocolError(
                    "E_JSON_DUPLICATE_KEY", field=child, reason="duplicate object member"
                )
            result[key] = _materialize(item, child)
        return result
    if isinstance(value, list):
        return [_materialize(item, _pointer(field, index)) for index, item in enumerate(value)]
    if isinstance(value, str):
        _unicode_scalar_string(value, field)
    return value


def strict_json_loads(
    raw: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_JSON_BYTES,
    max_depth: int = DEFAULT_MAX_JSON_DEPTH,
) -> Any:
    """Decode a strict UTF-8 JSON document with duplicate-key protection."""
    if type(raw) is not bytes:
        raise ProtocolError("E_JSON_INPUT_TYPE", reason="bytes input required")
    if not 1 <= len(raw) <= max_bytes:
        raise ProtocolError("E_JSON_LIMIT", reason="empty input or byte limit exceeded")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ProtocolError("E_JSON_BOM", reason="UTF-8 BOM not accepted in new protocol")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProtocolError("E_JSON_ENCODING", reason="UTF-8 decoding failed") from exc
    _check_depth(text, max_depth)
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_Pairs,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
            parse_int=_parse_int,
        )
        return _materialize(parsed)
    except ProtocolError:
        raise
    except json.JSONDecodeError as exc:
        raise ProtocolError(
            "E_JSON_SYNTAX", reason=f"syntax error at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except RecursionError as exc:
        raise ProtocolError("E_JSON_LIMIT", reason="decoder recursion limit exceeded") from exc


# Short aliases used by services and adapters.
strict_loads = strict_json_loads
parse_strict_json = strict_json_loads


def _check_value(
    value: Any,
    field: str = "",
    depth: int = 0,
    ancestors: set[int] | None = None,
) -> None:
    if depth > DEFAULT_MAX_JSON_DEPTH:
        raise ProtocolError("E_JSON_LIMIT", field=field, reason="nesting limit exceeded")
    if ancestors is None:
        ancestors = set()
    kind = type(value)
    if value is None or kind is bool:
        return
    if kind is str:
        _unicode_scalar_string(value, field)
        return
    if kind is int:
        if abs(value) > MAX_SAFE_INTEGER:
            raise ProtocolError("E_INTEGER_RANGE", field=field)
        return
    if kind is float:
        if not math.isfinite(value):
            raise ProtocolError("E_JSON_NON_FINITE", field=field)
        return
    if kind not in (list, dict):
        raise ProtocolError(
            "E_JSON_VALUE_TYPE", field=field, reason="only JSON-native values are accepted"
        )
    identity = id(value)
    if identity in ancestors:
        raise ProtocolError("E_JSON_CYCLE", field=field, reason="cyclic container")
    ancestors.add(identity)
    try:
        if kind is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise ProtocolError("E_JSON_KEY_TYPE", field=field)
                child = _pointer(field, key)
                _unicode_scalar_string(key, child)
                _check_value(item, child, depth + 1, ancestors)
        else:
            for index, item in enumerate(value):
                _check_value(item, _pointer(field, index), depth + 1, ancestors)
    finally:
        ancestors.remove(identity)


def stable_snapshot_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode a validated semantic object using the public fixed profile."""
    if type(value) is not dict:
        raise ProtocolError("E_SCHEMA_TYPE", reason="snapshot root must be an object")
    _check_value(value)
    try:
        encoded = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("E_JSON_VALUE_TYPE", reason="value is not JSON serialisable") from exc
    if len(encoded) > DEFAULT_MAX_JSON_BYTES:
        raise ProtocolError("E_JSON_LIMIT", reason="snapshot byte limit exceeded")
    return encoded


def canonical_json(value: Mapping[str, Any]) -> bytes:
    """Alias for the fixed UTF-8 JSON byte representation."""
    return stable_snapshot_bytes(value)


stable_json_bytes = stable_snapshot_bytes


def sha256_bytes(raw: bytes) -> str:
    """Return a lowercase SHA-256 digest for exact bytes."""
    if type(raw) is not bytes:
        raise ProtocolError("E_RESOURCE_BYTES", reason="bytes input required")
    return hashlib.sha256(raw).hexdigest()


def _validate_business_path(value: str, *, field: str = "/path") -> str:
    if type(value) is not str or not value:
        raise ProtocolError("E_PATH_SYNTAX", field=field, reason="nonempty string required")
    _unicode_scalar_string(value, field)
    if len(value.encode("utf-8")) > 4096:
        raise ProtocolError("E_PATH_SYNTAX", field=field, reason="path length limit")
    if value.startswith("/") or any(c in value for c in '\\:<>' + '"|?*'):
        raise ProtocolError("E_PATH_SYNTAX", field=field, reason="nonportable path syntax")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ProtocolError("E_PATH_SYNTAX", field=field, reason="control character")
    for segment in value.split("/"):
        if (
            segment in ("", ".", "..")
            or segment.endswith((".", " "))
            or len(segment.encode("utf-8")) > 255
        ):
            raise ProtocolError("E_PATH_SYNTAX", field=field, reason="invalid path component")
        if segment.split(".", 1)[0].upper() in _RESERVED_NAMES:
            raise ProtocolError("E_PATH_RESERVED", field=field, reason="reserved device name")
    return value


validate_business_path = _validate_business_path


@dataclass(frozen=True)
class ResourceRef:
    """Logical identity of immutable bytes (and optional repository path)."""

    kind: str
    id: str
    sha256: str
    path: str | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not str or not KIND_RE.fullmatch(self.kind):
            raise ProtocolError("E_RESOURCE_FIELD", field="/kind")
        if type(self.id) is not str or not ID_RE.fullmatch(self.id):
            raise ProtocolError("E_RESOURCE_FIELD", field="/id")
        if type(self.sha256) is not str or not SHA256_RE.fullmatch(self.sha256):
            raise ProtocolError("E_RESOURCE_FIELD", field="/sha256")
        if self.path is not None:
            _validate_business_path(self.path)

    def to_dict(self) -> dict[str, str]:
        result: dict[str, str] = {"kind": self.kind, "id": self.id, "sha256": self.sha256}
        if self.path is not None:
            result["path"] = self.path
        return result

    as_dict = to_dict

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        known_kinds: set[str] | None = None,
        file_kinds: set[str] | None = None,
    ) -> "ResourceRef":
        validate_resource_ref(value, known_kinds=known_kinds, file_kinds=file_kinds)
        return cls(value["kind"], value["id"], value["sha256"], value.get("path"))

    @classmethod
    def from_bytes(
        cls, kind: str, id: str, raw: bytes, *, path: str | None = None
    ) -> "ResourceRef":
        return cls(kind, id, sha256_bytes(raw), path)


def validate_resource_ref(
    ref: Mapping[str, Any],
    *,
    known_kinds: set[str] | None = None,
    file_kinds: set[str] | None = None,
) -> None:
    """Validate ResourceRef shape and optional kind registration."""
    if type(ref) is not dict:
        raise ProtocolError("E_SCHEMA_TYPE", reason="ResourceRef must be an object")
    required = {"kind", "id", "sha256"}
    if not required <= set(ref) or set(ref) - required - {"path"}:
        raise ProtocolError("E_SCHEMA_FIELDS", reason="missing or unknown ResourceRef field")
    for field, regex in (("kind", KIND_RE), ("id", ID_RE), ("sha256", SHA256_RE)):
        if type(ref[field]) is not str or regex.fullmatch(ref[field]) is None:
            raise ProtocolError("E_RESOURCE_FIELD", field="/" + field)
    if known_kinds is not None and ref["kind"] not in known_kinds:
        raise ProtocolError("E_RESOURCE_KIND", field="/kind")
    if file_kinds is not None and ref["kind"] in file_kinds and "path" not in ref:
        raise ProtocolError("E_RESOURCE_PATH_REQUIRED", field="/path")
    if "path" in ref:
        _validate_business_path(ref["path"])


def verify_resource_bytes(
    ref: ResourceRef | Mapping[str, Any],
    raw: bytes,
    *,
    known_kinds: set[str] | None = None,
    file_kinds: set[str] | None = None,
) -> None:
    """Verify exact bytes against a resource's SHA-256 digest."""
    value = ref.to_dict() if isinstance(ref, ResourceRef) else ref
    validate_resource_ref(value, known_kinds=known_kinds, file_kinds=file_kinds)
    if type(raw) is not bytes:
        raise ProtocolError("E_RESOURCE_BYTES", reason="bytes input required")
    if not hmac.compare_digest(sha256_bytes(raw), value["sha256"]):
        raise ProtocolError("E_RESOURCE_HASH_MISMATCH", field="/sha256")


def require_schema_version(value: Mapping[str, Any], supported: set[int]) -> int:
    version = value.get("schema_version")
    if type(version) is not int or version < 1:
        raise ProtocolError("E_SCHEMA_VERSION_TYPE", field="/schema_version")
    if version not in supported:
        raise ProtocolError("E_SCHEMA_VERSION_UNSUPPORTED", field="/schema_version")
    return version


def _validate_identity(value: str, regex: re.Pattern[str], field: str) -> None:
    if type(value) is not str or regex.fullmatch(value) is None:
        raise ProtocolError("E_SCHEMA_FIELD", field=field, reason="invalid identity")


def new_request_id() -> str:
    return "req-" + uuid.uuid4().hex


def new_run_id() -> str:
    return "run-" + uuid.uuid4().hex


def format_observed_at(value: datetime | None = None) -> str:
    """Format UTC time in the public six-microsecond representation."""
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    instant = instant.astimezone(timezone.utc)
    return instant.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class EventEnvelope:
    """Validated public run-event envelope."""

    request_id: str
    run_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    role: str | None = None
    observed_at: str | None = None
    source_kind: str = "core"
    raw_refs: tuple[dict[str, Any], ...] = ()
    integrity: str = "complete"
    adapter_revision: str | None = None
    schema_version: int = 1
    record_type: str = "run-event"

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1 or self.record_type != "run-event":
            raise ProtocolError("E_SCHEMA_VERSION_UNSUPPORTED", field="/schema_version")
        _validate_identity(self.request_id, REQUEST_ID_RE, "/request_id")
        _validate_identity(self.run_id, RUN_ID_RE, "/run_id")
        if type(self.seq) is not int or not 1 <= self.seq <= MAX_SAFE_INTEGER:
            raise ProtocolError("E_SCHEMA_FIELD", field="/seq", reason="positive integer required")
        if type(self.type) is not str or not 1 <= len(self.type) <= 96:
            raise ProtocolError("E_SCHEMA_FIELD", field="/type")
        if self.role not in (None, "planner", "executor", "observer"):
            raise ProtocolError("E_SCHEMA_FIELD", field="/role")
        if self.source_kind not in ("core", "vendor"):
            raise ProtocolError("E_SCHEMA_FIELD", field="/source_kind")
        if type(self.payload) is not dict:
            raise ProtocolError("E_SCHEMA_TYPE", field="/payload")
        _check_value(self.payload, "/payload")
        if self.observed_at is None:
            object.__setattr__(self, "observed_at", format_observed_at())
        if type(self.observed_at) is not str or OBSERVED_AT_RE.fullmatch(self.observed_at) is None:
            raise ProtocolError("E_SCHEMA_FIELD", field="/observed_at")
        if self.integrity not in ("complete", "incomplete", "pending"):
            raise ProtocolError("E_SCHEMA_FIELD", field="/integrity")
        if self.adapter_revision is not None and not (
            type(self.adapter_revision) is str and 1 <= len(self.adapter_revision) <= 128
        ):
            raise ProtocolError("E_SCHEMA_FIELD", field="/adapter_revision")
        if not isinstance(self.raw_refs, tuple):
            try:
                object.__setattr__(self, "raw_refs", tuple(self.raw_refs))
            except TypeError as exc:
                raise ProtocolError("E_SCHEMA_TYPE", field="/raw_refs") from exc
        for index, span in enumerate(self.raw_refs):
            _validate_raw_span(span, f"/raw_refs/{index}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "record_type": self.record_type,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "seq": self.seq,
            "observed_at": self.observed_at,
            "role": self.role,
            "type": self.type,
            "source_kind": self.source_kind,
            "payload": self.payload,
            "raw_refs": list(self.raw_refs),
            "integrity": self.integrity,
            "adapter_revision": self.adapter_revision,
        }

    as_dict = to_dict

    def bytes(self) -> bytes:
        return stable_snapshot_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EventEnvelope":
        if type(value) is not dict:
            raise ProtocolError("E_SCHEMA_TYPE", reason="event must be an object")
        expected = {
            "schema_version", "record_type", "request_id", "run_id", "seq", "observed_at",
            "role", "type", "source_kind", "payload", "raw_refs", "integrity", "adapter_revision",
        }
        if set(value) != expected:
            raise ProtocolError("E_SCHEMA_FIELDS", reason="missing or unknown event field")
        return cls(
            request_id=value["request_id"], run_id=value["run_id"], seq=value["seq"],
            type=value["type"], payload=value["payload"], role=value["role"],
            observed_at=value["observed_at"], source_kind=value["source_kind"],
            raw_refs=tuple(value["raw_refs"]), integrity=value["integrity"],
            adapter_revision=value["adapter_revision"], schema_version=value["schema_version"],
            record_type=value["record_type"],
        )


def _validate_raw_span(value: Mapping[str, Any], field: str) -> None:
    if type(value) is not dict or set(value) != {"ref", "byte_start", "byte_end", "stream"}:
        raise ProtocolError("E_SCHEMA_FIELDS", field=field, reason="invalid raw span")
    validate_resource_ref(value["ref"])
    start, end = value["byte_start"], value["byte_end"]
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise ProtocolError("E_SCHEMA_FIELD", field=field, reason="invalid byte range")
    if type(value["stream"]) is not str or not value["stream"]:
        raise ProtocolError("E_SCHEMA_FIELD", field=field + "/stream")


def raw_span(ref: ResourceRef | Mapping[str, Any], byte_start: int, byte_end: int, stream: str) -> dict[str, Any]:
    """Create a half-open raw evidence span."""
    reference = ref.to_dict() if isinstance(ref, ResourceRef) else dict(ref)
    span = {"ref": reference, "byte_start": byte_start, "byte_end": byte_end, "stream": stream}
    _validate_raw_span(span, "")
    return span


class EventStore:
    """In-memory append-only event index with seq gap and replay checks."""

    def __init__(self) -> None:
        self._events: dict[str, dict[int, tuple[str, EventEnvelope]]] = {}

    def append(self, event: EventEnvelope | Mapping[str, Any]) -> EventEnvelope:
        item = event if isinstance(event, EventEnvelope) else EventEnvelope.from_dict(event)
        stream = self._events.setdefault(item.run_id, {})
        digest = sha256_bytes(item.bytes())
        existing = stream.get(item.seq)
        if existing is not None:
            if hmac.compare_digest(existing[0], digest):
                return existing[1]
            raise ProtocolError("E_EVENT_REPLAY_CONFLICT", field="/seq", reason="different bytes for sequence")
        expected = (max(stream) + 1) if stream else 1
        if item.seq != expected:
            raise ProtocolError("E_EVENT_GAP", field="/seq", reason=f"expected {expected}, got {item.seq}")
        stream[item.seq] = (digest, item)
        return item

    def append_dict(self, value: Mapping[str, Any]) -> EventEnvelope:
        return self.append(value)

    def events(self, run_id: str) -> tuple[EventEnvelope, ...]:
        stream = self._events.get(run_id, {})
        return tuple(stream[index][1] for index in sorted(stream))

    def last_seq(self, run_id: str) -> int:
        stream = self._events.get(run_id, {})
        return max(stream, default=0)

    def __len__(self) -> int:
        return sum(len(stream) for stream in self._events.values())


def make_event(
    request_id: str,
    run_id: str,
    seq: int,
    event_type: str,
    payload: dict[str, Any],
    **kwargs: Any,
) -> EventEnvelope:
    """Convenience factory preserving the public event field names."""
    return EventEnvelope(request_id, run_id, seq, event_type, payload, **kwargs)


__all__ = [
    "MAX_SAFE_INTEGER", "ProtocolError", "ResourceRef", "EventEnvelope", "EventStore",
    "strict_json_loads", "strict_loads", "parse_strict_json", "stable_snapshot_bytes",
    "stable_json_bytes", "canonical_json", "sha256_bytes", "validate_business_path",
    "validate_resource_ref", "verify_resource_bytes", "require_schema_version", "raw_span",
    "new_request_id", "new_run_id", "format_observed_at", "make_event",
]
