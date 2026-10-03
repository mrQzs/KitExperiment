"""Offline runtime guards for the 09 planning boundary.

The functions in this module consume already trusted observations.  They do
not start a CLI, resolve a provider, read credentials, or grant approval.
They model the small pieces that must remain explicit when a planner result is
handed from 04/05 into the 09 draft and publication flow.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence

from . import protocols
from .budget import BudgetPolicy, BudgetState, UsageObservation


class PlannerRuntimeError(ValueError):
    """Stable error family for offline planner lifecycle checks."""

    def __init__(self, code: str, message: str = "planner runtime input rejected") -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise PlannerRuntimeError("E_RUNTIME_FACT", f"{field} must be boolean")
    return value


def readiness_projection(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Return three independent readiness decisions.

    ``can_refine`` only concerns accepted inputs and a usable baseline;
    ``can_start_planner`` additionally requires planner authorization,
    environment, budget, locks and an open request; ``can_execute`` further
    requires current contract/approval and execution capabilities.  Unknown is
    represented by ``None`` and is therefore a wait, never a pass.
    """
    if type(facts) is not dict:
        raise PlannerRuntimeError("E_RUNTIME_FACT", "facts object required")
    groups = {
        "refine": ("inputs_accepted", "baseline_present", "baseline_authorized"),
        "planner": ("planner_authorized", "planner_environment_ok", "planner_budget_ok",
                    "input_locks_ok", "request_open"),
        "execute": ("contract_current", "approval_current", "capabilities_ok",
                     "execution_authorized", "budget_ok", "conflicts_clear"),
    }
    expected = {item for names in groups.values() for item in names}
    if set(facts) != expected:
        raise PlannerRuntimeError("E_RUNTIME_FACT", "fact set must be closed")
    values = {name: (None if facts[name] is None else _bool(facts[name], name)) for name in expected}
    reasons: list[str] = []

    def gate(label: str, names: Sequence[str]) -> bool:
        result = True
        for name in names:
            value = values[name]
            if value is not True:
                result = False
                reasons.append(("WAIT_" if value is None else "BLOCK_") + label.upper() + "_" + name.upper())
        return result

    can_refine = gate("refine", groups["refine"])
    can_start = can_refine and gate("planner", groups["planner"]) if can_refine else False
    can_execute = can_start and gate("execute", groups["execute"]) if can_start else False
    return {"can_refine": can_refine, "can_start_planner": can_start,
            "can_execute": can_execute, "reasons": tuple(reasons)}


def _strict_bytes(value: Any) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise PlannerRuntimeError("E_RESULT_BYTES", "result payload must be bytes")
    try:
        return bytes(value)
    except Exception as exc:  # pragma: no cover - defensive bytes boundary
        raise PlannerRuntimeError("E_RESULT_BYTES", "result payload is unreadable") from exc


def extract_unique_result(events: Iterable[Mapping[str, Any]], *, max_bytes: int = protocols.DEFAULT_MAX_BYTES) -> dict[str, Any]:
    """Extract exactly one final structured result from adapter events.

    Replaying the same identified final event is idempotent.  Two different
    final event identities are ambiguous even when their JSON payloads are
    byte-for-byte equal; silently choosing the last result would make a lost
    or reordered stream look valid.
    """
    if isinstance(events, (str, bytes, bytearray)):
        raise PlannerRuntimeError("E_RESULT_EVENTS", "event sequence required")
    finals: dict[str, tuple[bytes, Mapping[str, Any]]] = {}
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise PlannerRuntimeError("E_RESULT_EVENTS", f"event {index} must be an object")
        if set(event) - {"event_id", "event_type", "payload", "complete", "terminated", "execution_mode"}:
            raise PlannerRuntimeError("E_RESULT_EVENTS", f"event {index} has unknown fields")
        event_type = event.get("event_type")
        if event_type not in ("planning.result", "final", "result"):
            continue
        event_id = event.get("event_id")
        if type(event_id) is not str or not event_id:
            raise PlannerRuntimeError("E_RESULT_ID", "final event identity required")
        # A final payload without an explicit terminal observation is an
        # unknown termination and cannot be accepted as a settled result.
        if event.get("complete") is not True or event.get("terminated") is not True:
            raise PlannerRuntimeError("E_RESULT_INCOMPLETE", "final event is not complete")
        payload = event.get("payload")
        if isinstance(payload, Mapping):
            # The adapter may provide a decoded JSON object.  Canonical bytes
            # are still computed here so replay conflicts remain visible.
            try:
                protocols.check_json_value(payload, max_bytes=max_bytes)
                raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            except (protocols.ProtocolError, TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
                raise PlannerRuntimeError("E_RESULT_JSON", "final payload is not strict JSON") from exc
        else:
            raw = _strict_bytes(payload)
            try:
                protocols.strict_json_loads(raw, max_bytes=max_bytes)
            except protocols.ProtocolError as exc:
                raise PlannerRuntimeError(exc.code, exc.reason) from exc
        if len(raw) > max_bytes:
            raise PlannerRuntimeError("E_RESULT_LIMIT", "final payload exceeds the byte limit")
        previous = finals.get(event_id)
        if previous is not None:
            if previous[0] != raw:
                raise PlannerRuntimeError("E_RESULT_REPLAY_CONFLICT", "same final event id has different bytes")
            continue
        finals[event_id] = (raw, event)
    if not finals:
        raise PlannerRuntimeError("E_RESULT_MISSING", "no complete final result")
    if len(finals) != 1:
        raise PlannerRuntimeError("E_RESULT_AMBIGUOUS", "multiple distinct final results")
    event = next(iter(finals.values()))[1]
    payload = event["payload"]
    if isinstance(payload, Mapping):
        return dict(payload)
    try:
        parsed = protocols.strict_json_loads(_strict_bytes(payload), max_bytes=max_bytes)
    except protocols.ProtocolError as exc:
        raise PlannerRuntimeError(exc.code, exc.reason) from exc
    if not isinstance(parsed, dict):
        raise PlannerRuntimeError("E_RESULT_JSON", "final result must be an object")
    return parsed


@dataclass
class RepairPolicy:
    max_calls: int = 3
    max_repair_calls: int = 1
    max_bytes: int = protocols.DEFAULT_MAX_BYTES

    def __post_init__(self) -> None:
        if type(self.max_calls) is not int or type(self.max_repair_calls) is not int \
                or self.max_calls < 1 or self.max_repair_calls < 0 \
                or self.max_repair_calls >= self.max_calls:
            raise PlannerRuntimeError("E_REPAIR_POLICY", "repair calls must be bounded below total calls")
        if type(self.max_bytes) is not int or self.max_bytes < 1:
            raise PlannerRuntimeError("E_REPAIR_POLICY", "positive byte limit required")


@dataclass
class RepairController:
    policy: RepairPolicy = field(default_factory=RepairPolicy)
    calls: int = 0
    repair_calls: int = 0
    fingerprints: set[str] = field(default_factory=set)
    stopped: str | None = None

    def allow(self, *, error_family: str, field: str = "", semantic_digest: str = "") -> bool:
        if self.stopped:
            return False
        fingerprint = hashlib.sha256((error_family + "\0" + field + "\0" + semantic_digest).encode()).hexdigest()
        if fingerprint in self.fingerprints:
            self.stopped = "REPAIR_LOOP"
            return False
        if self.calls >= self.policy.max_calls or self.repair_calls >= self.policy.max_repair_calls:
            self.stopped = "REPAIR_LIMIT"
            return False
        self.fingerprints.add(fingerprint)
        self.calls += 1
        self.repair_calls += 1
        return True


@dataclass
class RequestBudget:
    """Small deterministic request-root budget ledger used by offline fixtures."""

    request_id: str
    limit: int
    used: int = 0
    reserved: int = 0
    stopped: str | None = None
    generation: int = 1

    def reserve(self, amount: int, *, generation: int | None = None) -> int:
        if type(amount) is not int or amount <= 0:
            raise PlannerRuntimeError("E_BUDGET_AMOUNT", "positive reservation required")
        if generation is not None and generation != self.generation:
            raise PlannerRuntimeError("E_GENERATION", "stale controller generation")
        if self.stopped:
            raise PlannerRuntimeError("E_BUDGET_STOPPED", "request is stopped")
        if self.used + self.reserved + amount > self.limit:
            raise PlannerRuntimeError("E_BUDGET_EXHAUSTED", "request budget exhausted")
        self.reserved += amount
        return self.reserved

    def settle(self, amount: int, *, generation: int | None = None) -> int:
        if type(amount) is not int or amount < 0 or amount > self.reserved:
            raise PlannerRuntimeError("E_BUDGET_AMOUNT", "settlement exceeds reservation")
        if generation is not None and generation != self.generation:
            raise PlannerRuntimeError("E_GENERATION", "stale controller generation")
        self.reserved -= amount
        self.used += amount
        return self.used

    def stop(self, reason: str) -> None:
        if type(reason) is not str or not reason:
            raise PlannerRuntimeError("E_STOP_REASON", "stop reason required")
        self.stopped = reason

    def restart_controller(self) -> int:
        self.generation += 1
        return self.generation


@dataclass(frozen=True)
class PlanningBudgetLedger:
    """09 adapter over the core 07 ``BudgetState`` request ledger.

    Planner repair, refinement and execution handoff must reserve against one
    request root.  This adapter deliberately exposes no independent counters;
    every reservation and observation is delegated to ``BudgetState`` so
    unknown usage, late observations, cancellation conflicts and stop epochs
    remain visible to the same core ledger.
    """

    state: BudgetState
    request_id: str
    task_id: str
    generation: int = 1

    def __post_init__(self) -> None:
        if type(self.state) is not BudgetState or type(self.request_id) is not str or not self.request_id:
            raise PlannerRuntimeError("E_BUDGET_BINDING", "PlanningBudgetLedger requires a core BudgetState and request id")
        if type(self.task_id) is not str or not self.task_id or type(self.generation) is not int or self.generation < 1:
            raise PlannerRuntimeError("E_BUDGET_BINDING", "task and generation are required")
        if self.state.request_id is not None and self.state.request_id != self.request_id:
            raise PlannerRuntimeError("E_BUDGET_BINDING", "core budget state belongs to another request")

    @classmethod
    def calls(cls, request_id: str, task_id: str, limit: int) -> "PlanningBudgetLedger":
        if type(limit) is not int or limit < 1:
            raise PlannerRuntimeError("E_BUDGET_AMOUNT", "positive calls limit required")
        return cls(BudgetState(BudgetPolicy({"calls": limit})), request_id, task_id)

    def reserve(self, operation_id: str, amount: int) -> tuple["PlanningBudgetLedger", str]:
        try:
            state, decision = self.state.reserve_for_operation(operation_id, self.request_id, self.task_id,
                                                               {"calls": amount}, generation=self.generation)
        except Exception as exc:
            raise PlannerRuntimeError(getattr(exc, "code", "E_BUDGET"), str(exc)) from exc
        return replace(self, state=state), decision.status

    def observe(self, operation_id: str, amount: int | None, *, observation_id: str,
                unknown_reason: str | None = None, final: bool = True) -> tuple["PlanningBudgetLedger", str]:
        kind = "unknown" if amount is None else ("final" if final else "cumulative")
        observation = UsageObservation(observation_id=observation_id, operation_id=operation_id,
                                       dimension="calls", kind=kind, amount=amount,
                                       economic_call_key=operation_id, final=(kind == "final"),
                                       unknown_reason=unknown_reason, evidence_ref=observation_id,
                                       request_id=self.request_id,
                                       task_id=self.task_id)
        try:
            state, decision = self.state.record_usage_observation(observation)
        except Exception as exc:
            raise PlannerRuntimeError(getattr(exc, "code", "E_BUDGET"), str(exc)) from exc
        return replace(self, state=state), decision.status

    def settle(self, operation_id: str) -> tuple["PlanningBudgetLedger", str]:
        try:
            state, decision = self.state.settle_operation(operation_id)
        except Exception as exc:
            raise PlannerRuntimeError(getattr(exc, "code", "E_BUDGET"), str(exc)) from exc
        return replace(self, state=state), decision.status

    def require_operation(self, operation_id: str, *, active_states: Sequence[str] = ("reserved", "provisional")) -> None:
        """Require an active reservation owned by this request/task/generation.

        Role gates may call this before dispatch so an unreserved executor
        cannot accidentally bypass the shared 07 request budget.  The method
        is read-only and deliberately leaves the historical no-argument gate
        usable for pure profile fixtures.
        """
        if type(operation_id) is not str or not operation_id:
            raise PlannerRuntimeError("E_BUDGET_OPERATION", "operation id is required")
        if not isinstance(active_states, Sequence) or isinstance(active_states, (str, bytes)):
            raise PlannerRuntimeError("E_BUDGET_OPERATION", "active states must be a sequence")
        if self.state.stop_decision()[0]:
            raise PlannerRuntimeError("E_BUDGET_STOPPED", "budget is stopped")
        operation = next((item for item in self.state.operations if item.operation_id == operation_id), None)
        if operation is None:
            raise PlannerRuntimeError("E_BUDGET_OPERATION", "operation is not reserved")
        if operation.request_id != self.request_id or operation.task_id != self.task_id:
            raise PlannerRuntimeError("E_BUDGET_SCOPE", "operation belongs to another request/task")
        if operation.generation != self.generation:
            raise PlannerRuntimeError("E_GENERATION", "operation uses a stale controller generation")
        if operation.finished or operation.state not in tuple(active_states):
            raise PlannerRuntimeError("E_BUDGET_OPERATION", "operation is not active")

    def stop(self, reason: str) -> "PlanningBudgetLedger":
        try:
            return replace(self, state=self.state.request_stop(reason))
        except Exception as exc:
            raise PlannerRuntimeError(getattr(exc, "code", "E_BUDGET"), str(exc)) from exc

    def snapshot(self) -> Any:
        return self.state.snapshot()


__all__ = ["PlannerRuntimeError", "readiness_projection", "extract_unique_result",
           "RepairPolicy", "RepairController", "RequestBudget", "PlanningBudgetLedger"]
