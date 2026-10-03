"""Read-only state, brief and outbox/cursor projections for 08."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence
from .builtin_artifacts import ArtifactError
from . import protocols


_MAX_SAFE_INTEGER = protocols.MAX_SAFE_INTEGER


def _projection_text(value: Any, field: str) -> str:
    """Validate identity text before it enters an event digest or snapshot."""
    if type(value) is not str or not value or any(
            ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
            or 0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        _err("E_EVENT", f"{field} must be non-empty scalar text")
    return value


def _err(code: str, message: str) -> None:
    raise ArtifactError(code, "/projection", message)


def _check_json_value(value: Any) -> None:
    """Require the strict JSON value subset used by projection identities.

    ``json.dumps`` alone accepts a few Python values which are not JSON-native
    (for example tuple values and integer mapping keys are coerced).  A
    projection event ID must be computed from the exact value
    that is persisted, so those coercions would make two different records
    appear identical.  Reuse the protocol's size/depth/cycle checks and map its
    errors to the artifact boundary's stable error family.
    """
    try:
        protocols.check_json_value(value, max_bytes=protocols.DEFAULT_MAX_BYTES)
    except protocols.ProtocolError as exc:
        raise ArtifactError("E_EVENT", "/projection", "strict bounded JSON value required") from exc

def _event_id(source: str, event_type: str, payload: Mapping[str, Any]) -> str:
    _projection_text(source, "source")
    _projection_text(event_type, "event_type")
    if not isinstance(payload, Mapping):
        _err("E_EVENT", "event payload mapping required")
    _check_json_value({"source": source, "type": event_type, "payload": dict(payload)})
    try:
        raw = json.dumps({"source": source, "type": event_type, "payload": payload}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise ArtifactError("E_EVENT", "/projection", "event payload must be canonical JSON") from exc
    return sha256(raw).hexdigest()

@dataclass(frozen=True)
class ProjectionEvent:
    seq: int
    source: str
    event_type: str
    payload: Mapping[str, Any]
    event_id: str | None = None

class CursorProjection:
    """Contiguous cursor consumer with idempotent event IDs and rebuild support."""
    def __init__(self, *, generation: int = 1) -> None:
        if type(generation) is not int or not 1 <= generation <= _MAX_SAFE_INTEGER:
            _err("E_CURSOR", "positive generation required")
        self.generation = generation
        self.cursor = 0
        self.source_vector: dict[str, int] = {}
        self.events: dict[str, dict[str, Any]] = {}

    def consume(self, events: Sequence[ProjectionEvent], *, generation: int) -> str:
        if type(generation) is not int or generation != self.generation:
            _err("E_FENCED", "stale consumer generation")
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            _err("E_EVENT", "event sequence required")
        expected = self.cursor + 1
        trial = dict(self.events); trial_sources = dict(self.source_vector)
        applied = False
        for event in events:
            if (not isinstance(event, ProjectionEvent) or type(event.seq) is not int
                    or not 1 <= event.seq <= _MAX_SAFE_INTEGER):
                _err("E_EVENT", "valid projection event required")
            if not isinstance(event.payload, Mapping):
                _err("E_EVENT", "event payload mapping required")
            if event.event_id is not None and (type(event.event_id) is not str or not event.event_id
                                               or any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
                                                      or 0xD800 <= ord(ch) <= 0xDFFF for ch in event.event_id)):
                _err("E_EVENT_ID", "event id must be non-empty text")
            calculated = _event_id(event.source, event.event_type, event.payload)
            eid = event.event_id or calculated
            if eid != calculated:
                _err("E_EVENT_ID", "event id mismatch")
            if eid in trial:
                previous = trial[eid]
                if (previous["payload"] != dict(event.payload)
                        or previous["seq"] != event.seq):
                    _err("E_EVENT_CONFLICT", "event ID reused with different payload or sequence")
                continue
            if event.seq != expected:
                _err("E_EVENT_GAP", "outbox sequence must be contiguous")
            trial[eid] = {"seq": event.seq, "source": event.source, "type": event.event_type,
                          "payload": deepcopy(dict(event.payload))}
            trial_sources[event.source] = event.seq
            expected += 1; applied = True
        if applied:
            self.events, self.source_vector, self.cursor = trial, trial_sources, expected - 1
            return "APPLIED"
        return "REPLAY"

    def rebuild(self, authority_rows: Sequence[Mapping[str, Any]], *, generation: int) -> None:
        if type(generation) is not int or generation != self.generation:
            _err("E_FENCED", "stale consumer generation")
        if not isinstance(authority_rows, Sequence) or isinstance(authority_rows, (str, bytes)) or any(not isinstance(row, Mapping) for row in authority_rows):
            _err("E_REBUILD", "authority rows must be a sequence of mappings")
        rows = [dict(row) for row in authority_rows]
        try:
            _check_json_value(rows)
        except ArtifactError as exc:
            raise ArtifactError("E_REBUILD", "/projection", "authority rows must contain strict JSON") from exc
        self.events = {"authority": {"seq": 0, "source": "authority", "type": "rebuild",
                                      "payload": {"rows": deepcopy(rows)}}}
        self.source_vector = {}; self.cursor = 0

def project_state(*, cursor: int, source_vector: Mapping[str, int], status: str,
                  issues: Sequence[str] = (), coverage: str = "complete") -> dict[str, Any]:
    if (type(cursor) is not int or not 0 <= cursor <= _MAX_SAFE_INTEGER
            or type(coverage) is not str or coverage not in {"complete", "partial", "unknown"}):
        _err("E_STATE", "invalid cursor or coverage")
    if type(status) is not str or status not in {"READY", "PASS", "FAIL", "BLOCKED", "NOT_RUN", "UNKNOWN"}:
        _err("E_STATE", "invalid status")
    if (not isinstance(source_vector, Mapping)
            or any(type(key) is not str or not key or any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
                                                          or 0xD800 <= ord(ch) <= 0xDFFF for ch in key)
                   or type(value) is not int or value < 0 or value > cursor
                   for key, value in source_vector.items())):
        _err("E_STATE", "source vector must map names to non-negative cursors")
    if (isinstance(issues, (str, bytes)) or not isinstance(issues, Sequence)
            or any(type(item) is not str or not item
                   or any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
                          or 0xD800 <= ord(ch) <= 0xDFFF for ch in item)
                   for item in issues)):
        _err("E_STATE", "issues must be a sequence of non-empty strings")
    issues = list(issues)
    healthy = status in {"READY", "PASS"} and coverage == "complete" and not issues
    return {"cursor": cursor, "source_vector": dict(source_vector), "status": status,
            "coverage": coverage, "issues": issues, "health": "healthy" if healthy else "degraded",
            "dispatch_eligibility": "not_evaluated"}

def render_brief(state: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(state, Mapping):
        _err("E_BRIEF", "state mapping required")
    if set(state) - {"cursor", "source_vector", "status", "coverage", "issues", "health", "dispatch_eligibility"}:
        _err("E_BRIEF", "state fields must be closed")
    if state.get("dispatch_eligibility", "not_evaluated") != "not_evaluated":
        _err("E_BRIEF", "state cannot grant dispatch eligibility")
    status = state.get("status", "UNKNOWN")
    health = state.get("health", "degraded")
    coverage = state.get("coverage", "unknown")
    if type(status) is not str or status not in {"READY", "PASS", "FAIL", "BLOCKED", "NOT_RUN", "UNKNOWN"}:
        _err("E_BRIEF", "invalid status")
    if type(health) is not str or health not in {"healthy", "degraded"}:
        _err("E_BRIEF", "invalid health")
    if (type(state.get("cursor", 0)) is not int
            or not 0 <= state.get("cursor", 0) <= _MAX_SAFE_INTEGER):
        _err("E_BRIEF", "invalid cursor")
    source_vector = state.get("source_vector", {})
    if (not isinstance(source_vector, Mapping)
            or any(type(key) is not str or not key or any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
                                                          or 0xD800 <= ord(ch) <= 0xDFFF for ch in key)
                   or type(value) is not int
                   or value < 0 or value > state.get("cursor", 0)
                   for key, value in source_vector.items())):
        _err("E_BRIEF", "invalid source vector")
    if type(coverage) is not str or coverage not in {"complete", "partial", "unknown"}:
        _err("E_BRIEF", "invalid coverage")
    if (isinstance(state.get("issues", []), (str, bytes))
            or not isinstance(state.get("issues", []), Sequence)
            or any(type(item) is not str or not item
                   or any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
                          or 0xD800 <= ord(ch) <= 0xDFFF for ch in item)
                   for item in state.get("issues", []))):
        _err("E_BRIEF", "issues must be a sequence of non-empty strings")
    if health == "healthy" and (status not in {"READY", "PASS"} or coverage != "complete" or state.get("issues", [])):
        _err("E_BRIEF", "healthy state contradicts its status, coverage or issues")
    return {"status": status, "health": health,
            "cursor": state.get("cursor", 0), "coverage": coverage,
            "issues": list(state.get("issues", [])), "dispatch_eligibility": "not_evaluated"}

__all__ = ["CursorProjection", "ProjectionEvent", "project_state", "render_brief"]
