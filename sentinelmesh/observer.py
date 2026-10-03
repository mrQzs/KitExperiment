"""Optional, deterministic Observer facade. It never mutates project facts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class ObserverError(ValueError):
    pass


@dataclass(frozen=True)
class ObserverRequest:
    request_id: str
    source_cursor: str
    source_refs: tuple[str, ...]
    max_items: int = 20

    def __post_init__(self) -> None:
        # A request is a bounded, read-only cursor over already selected input.
        # Reject malformed values here instead of allowing ``summarize`` to
        # silently truncate or reinterpret them.
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ObserverError("E_OBSERVER_REQUEST_ID")
        if not isinstance(self.source_cursor, str) or not self.source_cursor.strip():
            raise ObserverError("E_OBSERVER_CURSOR")
        if isinstance(self.source_refs, (str, bytes)):
            raise ObserverError("E_OBSERVER_SOURCE_REFS")
        if not isinstance(self.source_refs, tuple):
            try:
                object.__setattr__(self, "source_refs", tuple(self.source_refs))
            except TypeError as exc:
                raise ObserverError("E_OBSERVER_SOURCE_REFS") from exc
        if not self.source_refs or any(
            not isinstance(ref, str) or not ref.strip() for ref in self.source_refs
        ):
            raise ObserverError("E_OBSERVER_SOURCE_REFS")
        if type(self.max_items) is not int or not 1 <= self.max_items <= 1000:
            raise ObserverError("E_OBSERVER_LIMIT")


@dataclass(frozen=True)
class ObserverResult:
    request_id: str
    source_cursor: str
    facts: tuple[Mapping[str, Any], ...]
    hypotheses: tuple[Mapping[str, Any], ...]
    risks: tuple[Mapping[str, Any], ...]
    open_questions: tuple[Mapping[str, Any], ...]
    suggested_next_actions: tuple[Mapping[str, Any], ...]
    omitted: tuple[str, ...]
    degraded: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ObserverError("E_OBSERVER_RESULT_ID")
        if not isinstance(self.source_cursor, str) or not self.source_cursor.strip():
            raise ObserverError("E_OBSERVER_RESULT_CURSOR")
        for name in ("facts", "hypotheses", "risks", "open_questions", "suggested_next_actions"):
            value = getattr(self, name)
            if not isinstance(value, tuple):
                try:
                    value = tuple(value)
                except TypeError as exc:
                    raise ObserverError("E_OBSERVER_RESULT_SHAPE") from exc
                object.__setattr__(self, name, value)
            if any(not isinstance(row, Mapping) for row in value):
                raise ObserverError("E_OBSERVER_RESULT_SHAPE")
        if not isinstance(self.omitted, tuple):
            try:
                object.__setattr__(self, "omitted", tuple(self.omitted))
            except TypeError as exc:
                raise ObserverError("E_OBSERVER_RESULT_SHAPE") from exc
        if any(not isinstance(item, str) or not item for item in self.omitted):
            raise ObserverError("E_OBSERVER_RESULT_SHAPE")
        if type(self.degraded) is not bool:
            raise ObserverError("E_OBSERVER_RESULT_SHAPE")
        for row in self.facts:
            if not isinstance(row.get("text"), str) or not row["text"].strip():
                raise ObserverError("E_OBSERVER_FACT")
            if not isinstance(row.get("source_ref"), str) or not row["source_ref"].strip():
                raise ObserverError("E_OBSERVER_FACT_SOURCE")


class DeterministicObserver:
    """A no-model implementation used when Observer is disabled or unavailable."""

    def summarize(self, request: ObserverRequest, events: list[Mapping[str, Any]]) -> ObserverResult:
        if not isinstance(request, ObserverRequest):
            raise ObserverError("E_OBSERVER_INPUT")
        if not isinstance(events, list):
            raise ObserverError("E_OBSERVER_EVENTS")
        if any(not isinstance(event, Mapping) for event in events):
            raise ObserverError("E_OBSERVER_EVENT_SHAPE")
        selected = events[: request.max_items]
        # Every selected fact must retain a source reference.  ``zip`` without
        # this check would silently drop facts when a caller supplied too few
        # references, creating an unverifiable summary.
        if len(request.source_refs) < len(selected):
            raise ObserverError("E_OBSERVER_SOURCE_CLOSURE")
        facts = tuple(
            {"text": str(event.get("type", "event")), "source_ref": request.source_refs[index]}
            for index, event in enumerate(selected)
        )
        omitted = tuple(f"event:{i}" for i in range(len(selected), len(events)))
        return ObserverResult(request.request_id, request.source_cursor, facts, (), (), (), (), omitted, degraded=True)
