"""Read-only 09/10 lifecycle projection.

The projection keeps planning, review, publication and execution facts
orthogonal. It is a brief for consumers, never an approval or dispatch grant;
the cursor implementation is reused from the 08 deterministic outbox seam.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .builtin_projections import CursorProjection, ProjectionEvent


class PlannerProjectionError(ValueError):
    def __init__(self, code: str, message: str = "planner projection rejected input") -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


PLANNING_STATES = {"REQUESTED", "PLANNING", "DRAFT_READY", "NEEDS_PLAN_REVIEW", "APPROVED"}
REVIEW_STATES = {"NONE", "PENDING", "APPROVED", "REJECTED", "REVOKED", "STALE"}
PUBLICATION_STATES = {"NONE", "RESERVED", "CANDIDATE_SEALED", "REGISTERED", "PAIR_COMPLETE", "PUBLISHED", "INCOMPLETE"}
EXECUTION_STATES = {"NOT_STARTED", "STARTED", "START_UNKNOWN", "EXITED", "BLOCKED"}


@dataclass(frozen=True)
class PlanningProjection:
    planning_state: str
    review_state: str
    publication_state: str
    execution_state: str
    currentness: str
    dispatch_eligibility: str = "not_evaluated"
    cursor: int = 0
    issues: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {"planning_state": self.planning_state, "review_state": self.review_state,
                "publication_state": self.publication_state, "execution_state": self.execution_state,
                "currentness": self.currentness, "dispatch_eligibility": self.dispatch_eligibility,
                "cursor": self.cursor, "issues": list(self.issues)}


def project_planning(*, planning_state: str, review_state: str = "NONE",
                     publication_state: str = "NONE", execution_state: str = "NOT_STARTED",
                     currentness: str = "UNKNOWN", cursor: int = 0,
                     issues: Sequence[str] = ()) -> PlanningProjection:
    if planning_state not in PLANNING_STATES:
        raise PlannerProjectionError("E_PROJECTION_STATE", "unknown planning state")
    if review_state not in REVIEW_STATES or publication_state not in PUBLICATION_STATES \
            or execution_state not in EXECUTION_STATES:
        raise PlannerProjectionError("E_PROJECTION_STATE", "unknown orthogonal state")
    if currentness not in {"CURRENT", "SUPERSEDED", "REVOKED", "STALE", "INCOMPLETE", "UNKNOWN"}:
        raise PlannerProjectionError("E_PROJECTION_STATE", "unknown currentness")
    if type(cursor) is not int or cursor < 0:
        raise PlannerProjectionError("E_PROJECTION_CURSOR", "non-negative cursor required")
    if isinstance(issues, (str, bytes)) or any(type(item) is not str or not item for item in issues):
        raise PlannerProjectionError("E_PROJECTION_ISSUES", "issues must be non-empty strings")
    return PlanningProjection(planning_state, review_state, publication_state, execution_state,
                              currentness, "not_evaluated", cursor, tuple(issues))


class PlannerOutboxCursor:
    """10-compatible cursor that cannot turn a projection into permission."""

    def __init__(self, *, generation: int = 1) -> None:
        self._cursor = CursorProjection(generation=generation)

    @property
    def cursor(self) -> int:
        return self._cursor.cursor

    @property
    def generation(self) -> int:
        return self._cursor.generation

    def consume(self, events: Sequence[ProjectionEvent], *, generation: int) -> str:
        return self._cursor.consume(events, generation=generation)

    def rebuild(self, rows: Sequence[Mapping[str, Any]], *, generation: int) -> None:
        self._cursor.rebuild(rows, generation=generation)

    def brief(self, projection: PlanningProjection) -> dict[str, Any]:
        result = projection.to_mapping()
        result["cursor"] = self.cursor
        result["generation"] = self.generation
        result["dispatch_eligibility"] = "not_evaluated"
        return deepcopy(result)


__all__ = ["PlannerProjectionError", "PlanningProjection", "PlannerOutboxCursor",
           "project_planning", "PLANNING_STATES", "REVIEW_STATES", "PUBLICATION_STATES", "EXECUTION_STATES"]
