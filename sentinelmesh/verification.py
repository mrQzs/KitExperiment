"""Independent verification catalog and fail-closed evidence aggregation."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ResultStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ERROR = "ERROR"


class VerificationError(ValueError):
    pass


@dataclass(frozen=True)
class CheckCase:
    case_id: str
    requirement_refs: tuple[str, ...]
    description: str
    required: bool = True
    target_ref: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip():
            raise VerificationError("E_CASE_REQUIRED")
        if not isinstance(self.requirement_refs, tuple):
            try:
                object.__setattr__(self, "requirement_refs", tuple(self.requirement_refs))
            except TypeError as exc:
                raise VerificationError("E_CASE_REQUIREMENTS") from exc
        if any(not isinstance(ref, str) or not ref.strip() for ref in self.requirement_refs):
            raise VerificationError("E_CASE_REQUIREMENTS")
        if not isinstance(self.description, str) or not self.description.strip():
            raise VerificationError("E_CASE_REQUIRED")
        if type(self.required) is not bool or not isinstance(self.target_ref, str):
            raise VerificationError("E_CASE_SHAPE")


@dataclass(frozen=True)
class CheckObservation:
    case_id: str
    status: ResultStatus
    oracle: str
    observation: str
    artifact_refs: tuple[str, ...] = ()
    fixture_source: str = "synthetic"
    environment_id: str = ""
    reviewer: str = ""
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id.strip() or not isinstance(self.oracle, str) or not self.oracle.strip():
            raise VerificationError("E_OBSERVATION_REQUIRED")
        if not isinstance(self.status, ResultStatus):
            try:
                object.__setattr__(self, "status", ResultStatus(self.status))
            except (TypeError, ValueError) as exc:
                raise VerificationError("E_OBSERVATION_STATUS") from exc
        if not isinstance(self.observation, str) or not self.observation.strip():
            raise VerificationError("E_OBSERVATION_REQUIRED")
        if not isinstance(self.artifact_refs, tuple):
            try:
                object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))
            except TypeError as exc:
                raise VerificationError("E_OBSERVATION_ARTIFACTS") from exc
        if any(not isinstance(ref, str) or not ref.strip() for ref in self.artifact_refs):
            raise VerificationError("E_OBSERVATION_ARTIFACTS")
        if not isinstance(self.fixture_source, str) or self.fixture_source not in {"synthetic", "captured", "native_observed", "documented"}:
            raise VerificationError("E_OBSERVATION_FIXTURE")
        if not isinstance(self.environment_id, str) or not isinstance(self.reviewer, str):
            raise VerificationError("E_OBSERVATION_SHAPE")
        if self.failure_code is not None and (not isinstance(self.failure_code, str) or not self.failure_code.strip()):
            raise VerificationError("E_OBSERVATION_FAILURE_CODE")
        if self.status == ResultStatus.PASS and (not self.artifact_refs or not self.reviewer):
            raise VerificationError("E_PASS_EVIDENCE_INCOMPLETE")


class VerificationCatalog:
    def __init__(self, cases: list[CheckCase] | None = None):
        self._cases: dict[str, CheckCase] = {}
        self._observations: dict[str, CheckObservation] = {}
        for case in cases or []:
            self.register(case)

    def register(self, case: CheckCase) -> None:
        if not isinstance(case, CheckCase):
            raise VerificationError("E_CASE_SHAPE")
        if case.case_id in self._cases:
            raise VerificationError("E_CASE_DUPLICATE")
        self._cases[case.case_id] = case

    def observe(self, observation: CheckObservation) -> None:
        if not isinstance(observation, CheckObservation):
            raise VerificationError("E_OBSERVATION_SHAPE")
        if observation.case_id not in self._cases:
            raise VerificationError("E_CASE_UNKNOWN")
        old = self._observations.get(observation.case_id)
        if old and old != observation:
            raise VerificationError("E_OBSERVATION_CONFLICT")
        self._observations[observation.case_id] = observation

    def summary(self) -> dict[str, Any]:
        required = [c for c in self._cases.values() if c.required]
        counts = {status.value: 0 for status in ResultStatus}
        for item in self._observations.values():
            counts[item.status.value] += 1
        missing = sorted(c.case_id for c in required if c.case_id not in self._observations)
        failed_required = sorted(c.case_id for c in required if c.case_id in self._observations and self._observations[c.case_id].status != ResultStatus.PASS)
        return {"total": len(self._cases), "required": len(required), "counts": counts, "missing_required": missing, "failed_required": failed_required, "evidence_readiness": "COMPLETE" if not missing else "PARTIAL", "can_dispatch": not missing and not failed_required}

    def observations(self) -> tuple[CheckObservation, ...]:
        return tuple(self._observations[k] for k in sorted(self._observations))


__all__ = ["ResultStatus", "VerificationError", "CheckCase", "CheckObservation", "VerificationCatalog"]
