"""Restricted, pure T3A reservation and cumulative-usage projection.

This is an in-memory value model, not durable storage or an atomic concurrent
ledger.  The caller supplies trustworthy cumulative observations and proof
facts; this module does not authenticate providers or prove process state.
Each operation/dimension has exactly one stable economic call key.  Deltas,
provider aliases, invoice allocation, foreign exchange, and bounded unknown
usage are deliberately unsupported.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping

from .protocols import MAX_SAFE_INTEGER

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_KINDS = frozenset(("cumulative", "final", "unknown"))
_OP_STATES = frozenset(("reserved", "provisional", "reconciled", "unknown_liability", "cancelled_not_started"))
_MAX_ITEMS = 256


class BudgetError(ValueError):
    def __init__(self, code: str, message: str = "budget fact rejected") -> None:
        self.code = code
        super().__init__(message)


def _text(value: Any, name: str, *, pattern: re.Pattern[str] = _ID) -> str:
    if type(value) is not str or not value or len(value) > 128 or pattern.fullmatch(value) is None:
        raise BudgetError("E_USAGE_VALUE", name)
    return value


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > MAX_SAFE_INTEGER:
        raise BudgetError("E_USAGE_VALUE", name)
    return value


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise BudgetError("E_USAGE_VALUE", name)
    return value


def _mapping(values: Mapping[str, int], name: str, *, nonempty: bool = False) -> Mapping[str, int]:
    if not isinstance(values, Mapping) or len(values) > _MAX_ITEMS or (nonempty and not values):
        raise BudgetError("E_USAGE_VALUE", name)
    result = {_text(key, name, pattern=_DIM): _integer(value, name) for key, value in values.items()}
    return MappingProxyType(result)


@dataclass(frozen=True)
class BudgetPolicy:
    """One request's limits and optional cumulative task sublimits."""

    limits: Mapping[str, int]
    task_limits: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    policy_ref: str = "policy-v1"
    unknown_policy: str = "stop"

    def __post_init__(self) -> None:
        object.__setattr__(self, "limits", _mapping(self.limits, "limits", nonempty=True))
        if not isinstance(self.task_limits, Mapping) or len(self.task_limits) > _MAX_ITEMS:
            raise BudgetError("E_USAGE_VALUE", "task_limits")
        tasks = {}
        for task, limits in self.task_limits.items():
            task = _text(task, "task_id")
            limits = _mapping(limits, "task_limits")
            if any(dim not in self.limits or cap > self.limits[dim] for dim, cap in limits.items()):
                raise BudgetError("E_USAGE_VALUE", "task limits must be bounded by request limits")
            tasks[task] = limits
        object.__setattr__(self, "task_limits", MappingProxyType(tasks))
        object.__setattr__(self, "policy_ref", _text(self.policy_ref, "policy_ref"))
        if type(self.unknown_policy) is not str or self.unknown_policy != "stop":
            raise BudgetError("E_USAGE_VALUE", "only stop unknown_policy is supported")


@dataclass(frozen=True)
class BudgetOperation:
    operation_id: str
    request_id: str
    task_id: str
    reservation: Mapping[str, int]
    state: str = "reserved"
    generation: int = 1
    finished: bool = False
    cancellation_conflict: bool = False

    def __post_init__(self) -> None:
        for name in ("operation_id", "request_id", "task_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "reservation", _mapping(self.reservation, "reservation", nonempty=True))
        if type(self.state) is not str or self.state not in _OP_STATES:
            raise BudgetError("E_USAGE_VALUE", "state")
        _integer(self.generation, "generation", minimum=1)
        _boolean(self.finished, "finished")
        _boolean(self.cancellation_conflict, "cancellation_conflict")


@dataclass(frozen=True)
class UsageObservation:
    observation_id: str
    operation_id: str
    dimension: str
    kind: str
    amount: int | None
    revision: int = 1
    economic_call_key: str | None = None
    provider: str | None = None
    billing_event_id: str | None = None
    currency: str | None = None
    final: bool = False
    correction: bool = False
    supersedes: str | None = None
    unknown_reason: str | None = None
    evidence_ref: str | None = None
    request_id: str | None = None
    task_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("observation_id", "operation_id", "economic_call_key", "evidence_ref"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "dimension", _text(self.dimension, "dimension", pattern=_DIM))
        if type(self.kind) is not str or self.kind not in _KINDS:
            raise BudgetError("E_USAGE_VALUE", "only cumulative/final/unknown observations are supported")
        _integer(self.revision, "revision", minimum=1)
        _boolean(self.final, "final")
        _boolean(self.correction, "correction")
        if self.final != (self.kind == "final"):
            raise BudgetError("E_USAGE_VALUE", "final flag and kind must agree")
        if self.kind == "unknown":
            if self.amount is not None:
                raise BudgetError("E_USAGE_VALUE", "unknown amount must be None")
            if not self.unknown_reason:
                raise BudgetError("E_USAGE_UNKNOWN_REASON", "unknown_reason")
            _text(self.unknown_reason, "unknown_reason")
        else:
            _integer(self.amount, "amount")
            if self.unknown_reason is not None:
                raise BudgetError("E_USAGE_VALUE", "known observation cannot carry unknown_reason")
        if self.provider is not None or self.billing_event_id is not None:
            raise BudgetError("E_USAGE_VALUE", "provider/billing aliases are not supported")
        for name in ("currency", "supersedes", "request_id", "task_id"):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)
        if self.dimension.startswith("charge:"):
            if self.currency != self.dimension.split(":", 1)[1]:
                raise BudgetError("E_USAGE_CURRENCY_MISMATCH", "currency dimension")
        elif self.currency is not None:
            raise BudgetError("E_USAGE_CURRENCY_MISMATCH", "currency on non-charge dimension")
        if self.correction and self.supersedes is None:
            raise BudgetError("E_USAGE_CONFLICT", "correction requires supersedes")
        if self.supersedes is not None and not self.correction:
            raise BudgetError("E_USAGE_CONFLICT", "supersedes requires correction")


@dataclass(frozen=True)
class DimensionSnapshot:
    dimension: str
    limit: int | None
    known: int
    held: int
    unknown_operations: tuple[str, ...]
    exposure: int
    raw_remaining: int | None
    enforcement: str
    coverage: str


@dataclass(frozen=True)
class BudgetSnapshot:
    as_of_seq: int
    policy_ref: str
    dimensions: Mapping[str, DimensionSnapshot]
    stop_reasons: tuple[str, ...]
    coverage: str


@dataclass(frozen=True)
class BudgetDecision:
    status: str
    replayed: bool = False
    historical: bool = False
    changed_dimensions: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def _effective_payload(obs: UsageObservation) -> tuple[Any, ...]:
    """Independent evidence may mirror one revision, but cannot revise it."""
    return (obs.operation_id, obs.dimension, obs.economic_call_key, obs.revision,
            obs.kind, obs.amount, obs.currency, obs.final, obs.correction,
            obs.supersedes, obs.unknown_reason)


def _canonical_rows(observations: tuple[UsageObservation, ...], operation_id: str,
                    dimension: str) -> tuple[UsageObservation, ...]:
    """First observation identifies a revision; equivalent mirrors are history."""
    revisions: dict[int, UsageObservation] = {}
    for obs in observations:
        if obs.operation_id == operation_id and obs.dimension == dimension:
            revisions.setdefault(obs.revision, obs)
    return tuple(revisions[rev] for rev in sorted(revisions))


def _current(observations: tuple[UsageObservation, ...], operation_id: str,
             dimension: str) -> tuple[UsageObservation | None, int]:
    rows = _canonical_rows(observations, operation_id, dimension)
    if not rows:
        return None, 0
    current = rows[-1]
    if current.kind != "unknown":
        return current, current.amount or 0
    # An unknown revision retains the last known subtotal that was observed
    # before that unknown fact arrived.  A late, lower revision is history and
    # must not silently rewrite the current projection.
    current_index = next(
        index
        for index, obs in enumerate(observations)
        if obs.operation_id == operation_id
        and obs.dimension == dimension
        and obs.revision == current.revision
    )
    prior_known: dict[int, int] = {}
    for obs in observations[:current_index]:
        if obs.operation_id == operation_id and obs.dimension == dimension and obs.kind != "unknown":
            prior_known.setdefault(obs.revision, obs.amount or 0)
    known = prior_known[max(prior_known)] if prior_known else 0
    return current, known


@dataclass(frozen=True)
class BudgetState:
    """Create an empty root with BudgetState(policy); history is API-owned."""

    policy: BudgetPolicy
    operations: tuple[BudgetOperation, ...] = field(default=(), init=False)
    observations: tuple[UsageObservation, ...] = field(default=(), init=False)
    stop_reasons: tuple[str, ...] = field(default=(), init=False)
    stop_epoch: int = field(default=0, init=False)
    sequence: int = field(default=0, init=False)
    request_id: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if type(self.policy) is not BudgetPolicy:
            raise BudgetError("E_USAGE_VALUE", "policy")

    def _evolve(self, **changes: Any) -> "BudgetState":
        result = copy.copy(self)
        for name, value in changes.items():
            if name not in {"operations", "observations", "stop_reasons", "stop_epoch", "sequence", "request_id"}:
                raise BudgetError("E_USAGE_VALUE", "invalid state evolution")
            object.__setattr__(result, name, value)
        return result

    def _operation(self, operation_id: str) -> BudgetOperation:
        _text(operation_id, "operation_id")
        for op in self.operations:
            if op.operation_id == operation_id:
                return op
        raise BudgetError("E_USAGE_SCOPE_UNKNOWN", "operation")

    def _project(self, operations: tuple[BudgetOperation, ...], limits: Mapping[str, int]) -> BudgetSnapshot:
        dimensions = {}
        aggregate_coverage = "complete"
        for dimension in sorted(self.policy.limits):
            known = held = 0
            unknown: list[str] = []
            provisional = False
            for op in operations:
                current, subtotal = _current(self.observations, op.operation_id, dimension)
                # Even facts contradicting an earlier cancellation remain chargeable.
                known += subtotal
                cancelled_without_fact = op.state == "cancelled_not_started" and current is None
                closed = current is not None and current.final
                if cancelled_without_fact:
                    continue
                if not closed:
                    held += max(op.reservation[dimension] - subtotal, 0)
                    if (current is not None and current.kind == "unknown") or op.finished:
                        unknown.append(op.operation_id)
                    else:
                        provisional = True
                if op.cancellation_conflict and op.operation_id not in unknown:
                    unknown.append(op.operation_id)
            exposure = known + held
            limit = limits.get(dimension)
            remaining = None if limit is None else limit - exposure
            overflow = any(value > MAX_SAFE_INTEGER for value in (known, held, exposure))
            coverage = "incomplete" if unknown else ("provisional" if provisional else "complete")
            if coverage == "incomplete" or (coverage == "provisional" and aggregate_coverage == "complete"):
                aggregate_coverage = coverage
            enforcement = "stop" if overflow or (remaining is not None and remaining < 0) else ("unknown" if unknown else "allow")
            dimensions[dimension] = DimensionSnapshot(dimension, limit, known, held, tuple(sorted(unknown)), exposure, remaining, enforcement, coverage)
        return BudgetSnapshot(self.sequence, self.policy.policy_ref, MappingProxyType(dimensions), self.stop_reasons, aggregate_coverage)

    def snapshot(self, *, task_id: str | None = None) -> BudgetSnapshot:
        if task_id is None:
            return self._project(self.operations, self.policy.limits)
        _text(task_id, "task_id")
        return self._project(tuple(op for op in self.operations if op.task_id == task_id), self.policy.task_limits.get(task_id, {}))

    def task_snapshot(self, task_id: str) -> BudgetSnapshot:
        return self.snapshot(task_id=task_id)

    def stop_decision(self) -> tuple[bool, tuple[str, ...]]:
        reasons = list(self.stop_reasons)

        def add(reason: str) -> None:
            if reason not in reasons:
                reasons.append(reason)

        snapshots = [(self.snapshot(), "BUDGET_EXCEEDED")]
        snapshots.extend((self.task_snapshot(task), "TASK_BUDGET_EXCEEDED") for task in self.policy.task_limits)
        for snapshot, exceeded in snapshots:
            for item in snapshot.dimensions.values():
                if any(value > MAX_SAFE_INTEGER for value in (item.known, item.held, item.exposure)):
                    add("INTEGER_OVERFLOW")
                if item.raw_remaining is not None and item.raw_remaining < 0:
                    add(exceeded)
                if item.unknown_operations:
                    add("USAGE_UNKNOWN")
        if any(op.cancellation_conflict for op in self.operations):
            add("RECOVERY_REQUIRED")
        return bool(reasons), tuple(reasons)

    def _enforce(self) -> "BudgetState":
        stopped, reasons = self.stop_decision()
        if stopped:
            return self._evolve(stop_reasons=reasons, stop_epoch=self.stop_epoch or 1)
        return self

    def request_stop(self, reason: str) -> "BudgetState":
        reason = _text(reason, "stop_reason")
        if reason in self.stop_reasons:
            return self
        return self._evolve(stop_reasons=self.stop_reasons + (reason,), stop_epoch=self.stop_epoch or 1, sequence=self.sequence + 1)

    def reserve_for_operation(self, operation_id: str, request_id: str, task_id: str,
                              reservation: Mapping[str, int], *, generation: int = 1) -> tuple["BudgetState", BudgetDecision]:
        op = BudgetOperation(operation_id, request_id, task_id, reservation, generation=generation)
        if set(op.reservation) != set(self.policy.limits):
            raise BudgetError("E_USAGE_VALUE", "reservation must explicitly cover every policy dimension")
        if self.request_id is not None and request_id != self.request_id:
            raise BudgetError("E_USAGE_SCOPE_UNKNOWN", "request binding")
        for old in self.operations:
            if old.operation_id == operation_id:
                if (old.request_id, old.task_id, old.reservation, old.generation) == (request_id, task_id, op.reservation, generation):
                    return self, BudgetDecision("REPLAY", replayed=True)
                raise BudgetError("E_USAGE_CONFLICT", "operation replay conflict")
        if self.stop_decision()[0]:
            raise BudgetError("E_BUDGET_STOPPED", "budget stopped")
        snapshot = self.snapshot()
        task_snapshot = self.task_snapshot(task_id)
        for dimension, amount in op.reservation.items():
            if snapshot.dimensions[dimension].exposure + amount > self.policy.limits[dimension]:
                raise BudgetError("E_BUDGET_EXCEEDED", dimension)
            task_limit = self.policy.task_limits.get(task_id, {}).get(dimension)
            if task_limit is not None and task_snapshot.dimensions[dimension].exposure + amount > task_limit:
                raise BudgetError("E_TASK_BUDGET_EXCEEDED", dimension)
        state = self._evolve(operations=self.operations + (op,), request_id=request_id, sequence=self.sequence + 1)
        return state, BudgetDecision("RESERVED", changed_dimensions=tuple(sorted(op.reservation)))

    def record_usage_observation(self, observation: UsageObservation) -> tuple["BudgetState", BudgetDecision]:
        if type(observation) is not UsageObservation:
            raise BudgetError("E_USAGE_VALUE", "observation")
        op = self._operation(observation.operation_id)
        if observation.dimension not in self.policy.limits:
            raise BudgetError("E_USAGE_SCOPE_UNKNOWN", "dimension")
        if observation.request_id is not None and observation.request_id != op.request_id:
            raise BudgetError("E_USAGE_SCOPE_UNKNOWN", "request binding")
        if observation.task_id is not None and observation.task_id != op.task_id:
            raise BudgetError("E_USAGE_SCOPE_UNKNOWN", "task binding")
        for existing in self.observations:
            if existing.observation_id == observation.observation_id:
                if existing == observation:
                    return self, BudgetDecision("REPLAY", replayed=True)
                raise BudgetError("E_USAGE_CONFLICT", "observation replay conflict")
            if existing.economic_call_key == observation.economic_call_key and existing.operation_id != op.operation_id:
                raise BudgetError("E_USAGE_SCOPE_UNKNOWN", "economic call key already belongs to another operation")
        rows = _canonical_rows(self.observations, op.operation_id, observation.dimension)
        if rows and rows[0].economic_call_key != observation.economic_call_key:
            raise BudgetError("E_USAGE_CONFLICT", "one cumulative series is allowed per operation/dimension")
        current = rows[-1] if rows else None
        same_revision = next((row for row in rows if row.revision == observation.revision), None)
        if same_revision is not None:
            if _effective_payload(same_revision) != _effective_payload(observation):
                raise BudgetError("E_USAGE_CONFLICT", "revision conflict")
            state = self._evolve(observations=self.observations + (observation,), sequence=self.sequence + 1)
            return state, BudgetDecision("MIRROR", historical=True)
        if observation.supersedes is not None:
            target = next((row for row in self.observations if row.observation_id == observation.supersedes), None)
            if target is None or (target.operation_id, target.dimension, target.economic_call_key) != (observation.operation_id, observation.dimension, observation.economic_call_key) or target.revision >= observation.revision:
                raise BudgetError("E_USAGE_CONFLICT", "supersedes must identify a lower revision in this series")
        historical = current is not None and observation.revision < current.revision
        if not historical and current is not None:
            _, previous_known = _current(self.observations, op.operation_id, observation.dimension)
            decreases = observation.amount is not None and observation.amount < previous_known
            if (current.final or decreases) and (not observation.correction or observation.supersedes != current.observation_id):
                raise BudgetError("E_USAGE_CONFLICT", "decrease or post-final revision requires correction of current observation")
            if observation.correction and observation.supersedes != current.observation_id:
                raise BudgetError("E_USAGE_CONFLICT", "current correction must supersede the current observation")
        state = self._evolve(observations=self.observations + (observation,), sequence=self.sequence + 1)
        if historical:
            # History cannot change the current subtotal, including its retained
            # known amount while the latest observation is unknown.
            return state, BudgetDecision("HISTORICAL_ONLY", historical=True)
        conflict = op.cancellation_conflict or op.state == "cancelled_not_started"
        state = state._replace_operation(replace(op, cancellation_conflict=conflict))
        state = state._refresh_operation(op.operation_id)._enforce()
        return state, BudgetDecision("APPLIED", changed_dimensions=(observation.dimension,), reasons=state.stop_reasons)

    def _replace_operation(self, operation: BudgetOperation) -> "BudgetState":
        return self._evolve(operations=tuple(operation if op.operation_id == operation.operation_id else op for op in self.operations))

    def _refresh_operation(self, operation_id: str) -> "BudgetState":
        op = self._operation(operation_id)
        currents = [_current(self.observations, op.operation_id, dim)[0] for dim in self.policy.limits]
        all_closed = all(current is not None and current.final for current in currents)
        unknown = op.cancellation_conflict or any(current is not None and current.kind == "unknown" for current in currents) or (op.finished and not all_closed)
        state = "unknown_liability" if unknown else ("reconciled" if all_closed else "provisional")
        return self._replace_operation(replace(op, state=state))

    def settle_operation(self, operation_id: str) -> tuple["BudgetState", BudgetDecision]:
        op = self._operation(operation_id)
        if op.finished:
            return self, BudgetDecision("REPLAY", replayed=True, reasons=self.stop_reasons)
        state = self._replace_operation(replace(op, finished=True))
        state = state._evolve(sequence=self.sequence + 1)._refresh_operation(operation_id)._enforce()
        return state, BudgetDecision("SETTLED", reasons=state.stop_reasons)

    def release_proven_unstarted(self, operation_id: str, *, not_started_proven: bool | None = None) -> tuple["BudgetState", BudgetDecision]:
        """Consume an explicit caller proof fact; this does not prove OS state."""
        if not_started_proven is not True:
            raise BudgetError("E_RELEASE_NOT_PROVEN", "explicit not_started_proven=True required")
        op = self._operation(operation_id)
        if op.state == "cancelled_not_started":
            return self, BudgetDecision("REPLAY", replayed=True)
        if op.state != "reserved" or op.finished or any(obs.operation_id == operation_id for obs in self.observations):
            raise BudgetError("E_RELEASE_NOT_PROVEN", "operation has observations or was started/finished")
        state = self._replace_operation(replace(op, state="cancelled_not_started", finished=True))
        return state._evolve(sequence=self.sequence + 1), BudgetDecision("RELEASED")


def project_budget(state: BudgetState) -> BudgetSnapshot:
    return state.snapshot()


def reserve_for_operation(state: BudgetState, operation_id: str, request_id: str, task_id: str,
                          reservation: Mapping[str, int], *, generation: int = 1) -> tuple[BudgetState, BudgetDecision]:
    return state.reserve_for_operation(operation_id, request_id, task_id, reservation, generation=generation)


def record_usage_observation(state: BudgetState, observation: UsageObservation) -> tuple[BudgetState, BudgetDecision]:
    return state.record_usage_observation(observation)


def settle_operation(state: BudgetState, operation_id: str) -> tuple[BudgetState, BudgetDecision]:
    return state.settle_operation(operation_id)


def release_proven_unstarted(state: BudgetState, operation_id: str, *, not_started_proven: bool | None = None) -> tuple[BudgetState, BudgetDecision]:
    return state.release_proven_unstarted(operation_id, not_started_proven=not_started_proven)


def stable_observation_fingerprint(observation: UsageObservation) -> str:
    if type(observation) is not UsageObservation:
        raise BudgetError("E_USAGE_VALUE", "observation")
    payload = json.dumps(observation.__dict__, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
