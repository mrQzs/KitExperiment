"""Deterministic workflow validation and single-host scheduling primitives.

The implementation is deliberately dependency free.  ``Scheduler`` is a small,
thread-safe ledger suitable for tests and a single trusted controller; callers
must still provide authenticated observations from the platform/CLI adapters.
It mirrors the public concepts from code-kit-v2 (DAG, claim, lease, budget and
unknown usage) without pretending to start external processes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import heapq
import json
import re
import threading
import uuid
from typing import Any, Iterable, Mapping

from .runtime import ProcessIdentity, RuntimeErrorCode

_MAX_SAFE_INTEGER = 2**53 - 1
_IDENTITY_RE = re.compile(r"[a-z][a-z0-9._:-]{0,127}\Z")


class ContractError(ValueError):
    """Stable, machine-readable domain error."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")


def _check_json(value: Any, *, _ancestors: set[int] | None = None, _depth: int = 0) -> None:
    if _depth > 64:
        raise ContractError("E_JSON_LIMIT")
    if value is None or type(value) in (str, bool, int):
        if type(value) is int and abs(value) > _MAX_SAFE_INTEGER:
            raise ContractError("E_JSON_INTEGER")
        return
    if type(value) is float:
        raise ContractError("E_JSON_FLOAT")
    if _ancestors is None:
        _ancestors = set()
    if isinstance(value, (list, dict)):
        identity = id(value)
        if identity in _ancestors:
            raise ContractError("E_JSON_CYCLE")
        _ancestors.add(identity)
    if isinstance(value, list):
        for item in value:
            _check_json(item, _ancestors=_ancestors, _depth=_depth + 1)
        _ancestors.remove(identity)
    elif isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                raise ContractError("E_JSON_KEY")
            _check_json(item, _ancestors=_ancestors, _depth=_depth + 1)
        _ancestors.remove(identity)
    else:
        raise ContractError("E_JSON_TYPE")


def canonical(value: Any) -> bytes:
    _check_json(value)
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ContractError("E_JSON_OUTPUT") from exc


def digest(value: Any) -> str:
    """SHA-256 of deterministic canonical JSON."""
    return sha256(canonical(value)).hexdigest()


def exact_int(value: Any, *, positive: bool = False) -> int:
    if type(value) is not int:
        raise ContractError("E_INTEGER")
    if positive and value <= 0:
        raise ContractError("E_INTEGER")
    return value


def safe_path(value: str) -> tuple[str, ...]:
    if (not isinstance(value, str) or not value or value.startswith("/")
            or "\\" in value or ":" in value or any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F for c in value)):
        raise ContractError("E_PATH")
    parts = tuple(value.split("/"))
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
    if any(p in ("", ".", "..") or p.endswith((".", " ")) or p.split(".", 1)[0].upper() in reserved for p in parts):
        raise ContractError("E_PATH")
    return parts


def path_conflict(a: Mapping[str, str], b: Mapping[str, str]) -> bool:
    if not isinstance(a, Mapping) or not isinstance(b, Mapping) or not isinstance(a.get("path"), str) or not isinstance(b.get("path"), str):
        raise ContractError("E_PATH")
    pa, pb = safe_path(a["path"]), safe_path(b["path"])
    return pa == pb or (a.get("kind") == "tree" and pb[:len(pa)] == pa) or (
        b.get("kind") == "tree" and pa[:len(pb)] == pb)


def ref_type(ref: Mapping[str, str]) -> tuple[str, str, str]:
    if not isinstance(ref, Mapping):
        raise ContractError("E_TYPE_SHAPE")
    try:
        result = ref["kind"], ref["id"], ref["sha256"]
    except KeyError as exc:
        raise ContractError("E_TYPE_SHAPE") from exc
    if any(not isinstance(value, str) or not value for value in result):
        raise ContractError("E_TYPE_SHAPE")
    return result


def topological(items: Iterable[Mapping[str, Any]], location: str = "nodes") -> list[str]:
    """Return a stable lexicographical topological order with strict errors."""
    values = list(items)
    by_id: dict[str, Mapping[str, Any]] = {}
    for item in values:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str) or _IDENTITY_RE.fullmatch(item["id"]) is None:
            raise ContractError("E_DAG_SHAPE", location)
        identity = item["id"]
        if identity in by_id:
            raise ContractError("E_DAG_DUPLICATE", f"{location}/{identity}")
        by_id[identity] = item
    indegree = {key: 0 for key in by_id}
    children: dict[str, list[str]] = {key: [] for key in by_id}
    for item in values:
        deps = item.get("depends_on", [])
        if not isinstance(deps, list):
            raise ContractError("E_DAG_SHAPE", item["id"])
        seen: set[str] = set()
        for dep in deps:
            if not isinstance(dep, str) or _IDENTITY_RE.fullmatch(dep) is None:
                raise ContractError("E_DAG_SHAPE", item["id"])
            if dep in seen:
                raise ContractError("E_DAG_DUPLICATE_EDGE", item["id"])
            seen.add(dep)
            if dep not in by_id:
                raise ContractError("E_DAG_DANGLING", f"{item['id']}<-{dep}")
            indegree[item["id"]] += 1
            children[dep].append(item["id"])
    ready = [key for key, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    result: list[str] = []
    while ready:
        key = heapq.heappop(ready)
        result.append(key)
        for child in sorted(children[key]):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, child)
    if len(result) != len(values):
        raise ContractError("E_DAG_CYCLE", ",".join(sorted(k for k, n in indegree.items() if n)))
    return result


def ancestors(items: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    values = list(items)
    by_id = {x["id"]: x for x in values}
    result: dict[str, set[str]] = {}
    for key in topological(values):
        result[key] = set()
        for dependency in by_id[key].get("depends_on", []):
            result[key].add(dependency)
            result[key].update(result[dependency])
    return result


def _stage_validate(stages: list[Mapping[str, Any]], task_id: str) -> None:
    if not stages:
        raise ContractError("E_STAGE_PROFILE", task_id)
    order = topological(stages, f"nodes/{task_id}/stages")
    by_id = {x["id"]: x for x in stages}
    prev = ancestors(stages)
    groups = {kind: [x["id"] for x in stages if x.get("kind") == kind]
              for kind in ("execute", "freeze", "check", "verdict")}
    if not groups["execute"] or not groups["check"] or len(groups["freeze"]) != 1 or len(groups["verdict"]) != 1:
        raise ContractError("E_STAGE_PROFILE", task_id)
    freeze, verdict = groups["freeze"][0], groups["verdict"][0]
    if not set(groups["execute"]).issubset(prev[freeze]):
        raise ContractError("E_STAGE_ORDER", freeze)
    for check in groups["check"]:
        if freeze not in prev[check] or check not in prev[verdict]:
            raise ContractError("E_STAGE_ORDER", check)
    # no output may depend on a future stage
    for stage in stages:
        if stage.get("kind") not in {"execute", "freeze", "check", "verdict"}:
            raise ContractError("E_STAGE_KIND", stage["id"])
    if order != sorted(order):
        # Topological order itself is stable; this branch documents that input order
        # is never used for scheduling (no error is raised).
        pass


def validate_workflow(value: Mapping[str, Any], schema: Any | None = None) -> list[str]:
    """Validate the restricted workflow profile and return stable node order.

    If a JSON schema validator is supplied, callers may validate it before this
    function.  Structural/domain checks remain here so they are deterministic and
    usable without the optional ``jsonschema`` package.
    """
    if not isinstance(value, Mapping):
        raise ContractError("E_SCHEMA")
    if schema is not None:
        try:
            from jsonschema import Draft202012Validator  # optional dependency
            errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(e.path))
            if errors:
                raise ContractError("E_SCHEMA", "/".join(str(x) for x in errors[0].path))
        except ImportError:
            # Structural validation below is still useful in the stdlib-only
            # deployment; callers wanting full JSON Schema diagnostics should
            # install jsonschema and pass the same schema again.
            pass
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ContractError("E_SCHEMA", "nodes")
    result = topological(nodes)
    by_id = {n["id"]: n for n in nodes}
    for node in nodes:
        inputs = node.get("inputs", {})
        bindings = node.get("input_bindings", {})
        outputs = node.get("outputs", {})
        if not isinstance(inputs, Mapping) or not isinstance(bindings, Mapping) or not isinstance(outputs, Mapping):
            raise ContractError("E_SCHEMA", node["id"])
        if not set(bindings).issubset(inputs):
            raise ContractError("E_INPUT_UNKNOWN", node["id"])
        for port, spec in inputs.items():
            binding = bindings.get(port)
            if binding is None:
                if isinstance(spec, Mapping) and spec.get("required", True):
                    raise ContractError("E_INPUT_MISSING", f"{node['id']}/{port}")
                continue
            if not isinstance(binding, Mapping):
                raise ContractError("E_INPUT_BINDING", f"{node['id']}/{port}")
            if binding.get("kind") == "task-output":
                parent = binding.get("task_id")
                if parent not in node.get("depends_on", []):
                    raise ContractError("E_INPUT_EDGE", f"{node['id']}/{port}")
                p = by_id.get(parent, {}).get("outputs", {}).get(binding.get("port"))
                if p is None:
                    raise ContractError("E_OUTPUT_MISSING", f"{parent}/{binding.get('port')}")
                actual = p.get("schema_ref") if isinstance(p, Mapping) else p
            else:
                actual = binding.get("schema_ref")
            expected = spec.get("schema_ref") if isinstance(spec, Mapping) else spec
            if actual is None or expected is None or (isinstance(actual, Mapping) and isinstance(expected, Mapping)
                    and ref_type(actual) != ref_type(expected)) or actual != expected:
                raise ContractError("E_TYPE_UNPROVEN", f"{node['id']}/{port}")
        for out in outputs.values():
            if isinstance(out, Mapping) and out.get("stage_id") not in {s.get("id") for s in node.get("stages", [])}:
                raise ContractError("E_OUTPUT_STAGE", node["id"])
        for scope in node.get("write_scope", []):
            if isinstance(scope, Mapping):
                safe_path(scope.get("path", ""))
        retry = node.get("retry_policy")
        if retry is not None and (not isinstance(retry, Mapping) or type(retry.get("max_attempts_total")) is not int or retry["max_attempts_total"] <= 0):
            raise ContractError("E_INTEGER", f"{node['id']}/retry_policy")
        _stage_validate(node.get("stages", []), node["id"])
    return result


def readiness(facts: Mapping[str, bool]) -> dict[str, Any]:
    names = ("inputs_accepted", "baseline_present", "baseline_authorized", "contract_current",
             "approval_current", "locks_match", "capabilities_ok", "authorization_ok",
             "budget_ok", "conflicts_clear", "request_open")
    if set(facts) != set(names) or any(type(v) is not bool for v in facts.values()):
        raise ContractError("E_FACT_SHAPE")
    return {"can_refine": all(facts[x] for x in names[:3]),
            "can_execute": all(facts.values()),
            "reasons": ["WAIT_" + x.upper() for x in names if not facts[x]]}


@dataclass
class _Usage:
    call_key: str
    revision: int
    amount: int | None
    final: bool
    evidence_hash: str


@dataclass
class _Operation:
    operation_id: str
    request_id: str
    task_id: str
    plan_version: int
    guard_version: int
    idempotency_key: str
    request_digest: str
    owner: str
    generation: int
    lease_until: int
    state: str
    reservation: dict[str, int]
    resource_keys: tuple[str, ...]
    candidate_hash: str | None = None
    usage: dict[str, _Usage] = field(default_factory=dict)


@dataclass
class _Request:
    repo_id: str
    caps: dict[str, int]
    max_concurrency: int
    stopped: str | None = None


@dataclass
class _Task:
    caps: dict[str, int]
    max_attempts: int
    guard_version: int = 1
    fixture_eligible: bool = True


LIVE = {"RESERVED", "HANDOFF_ACCEPTED", "RUNNING", "START_UNKNOWN"}
UNRESOLVED = {"START_UNKNOWN", "REVIEW_PENDING", "ACCEPTED", "REJECTED"}
# Terminal state for a handoff that is *proven* not to have started.  It is
# neither live nor unresolved: the operation is closed, but its attempt and its
# already-observed usage still count.
NOT_STARTED = "NOT_STARTED"
# Reasons accepted by ``Scheduler.unknown``; the value is part of the event.
UNKNOWN_REASONS = frozenset({"START_UNKNOWN", "CLEANUP_UNCONFIRMED"})
# Outcomes accepted by ``Scheduler.recover`` (evidence of a fenced run).
RECOVERY_OUTCOMES = frozenset({"NOT_STARTED", "START_UNKNOWN", "FINISHED"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_FIELDS = frozenset({"pid", "boot_id", "executable_sha256", "worktree_id"})


class Scheduler:
    """Atomic, single-host scheduler/ledger.

    All mutating operations are serialized by an RLock, giving claim + resource
    hold + budget reservation one atomic decision.  ``events`` is append-only and
    intended for the state projector; it is not a replacement for durable WAL.
    """

    def __init__(self, *, generation: int = 1):
        exact_int(generation, positive=True)
        self._generation = generation
        self._requests: dict[str, _Request] = {}
        self._tasks: dict[tuple[str, str], _Task] = {}
        self._operations: dict[str, _Operation] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._resource_holds: dict[tuple[str, str], str] = {}
        self._events: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(json.loads(json.dumps(self._events)))

    def _emit(self, operation_id: str | None, event_type: str, payload: Any, *, encoded: bool = False) -> None:
        if not encoded:
            payload = self._encode_payload(payload)
        self._events.append({"seq": len(self._events) + 1, "operation_id": operation_id,
                             "type": event_type, "generation": self._generation,
                             "payload": payload})

    @staticmethod
    def _encode_payload(payload: Any) -> Any:
        """Canonical-encode an event payload *before* the ledger is touched.

        Every check that can fail -- ``canonical()`` included -- therefore runs
        before the first mutation, so a rejected call leaves the ledger (events
        included) exactly as it was.
        """
        return json.loads(canonical(payload).decode())

    @staticmethod
    def _caps(caps: Mapping[str, int]) -> dict[str, int]:
        if not isinstance(caps, Mapping) or not caps:
            raise ContractError("E_BUDGET_SHAPE")
        result: dict[str, int] = {}
        for key, value in caps.items():
            if type(key) is not str or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", key):
                raise ContractError("E_BUDGET_DIMENSION")
            exact_int(value)
            if value < 0:
                raise ContractError("E_BUDGET_SHAPE")
            result[key] = value
        return result

    def seed_request(self, request: str, repo: str, caps: Mapping[str, int], concurrency: int = 1) -> None:
        if type(request) is not str or type(repo) is not str:
            raise ContractError("E_REQUEST_SHAPE")
        caps2 = self._caps(caps); exact_int(concurrency, positive=True)
        with self._lock:
            if request in self._requests:
                raise ContractError("E_REQUEST_EXISTS")
            payload = self._encode_payload({"request_id": request, "repo_id": repo, "caps": caps2})
            self._requests[request] = _Request(repo, caps2, concurrency)
            self._emit(None, "request.created", payload, encoded=True)

    def seed_task(self, request: str, task: str, caps: Mapping[str, int], max_attempts: int = 3) -> None:
        if type(request) is not str:
            raise ContractError("E_REQUEST_UNKNOWN")
        if type(task) is not str:
            raise ContractError("E_TASK_SHAPE")
        caps2 = self._caps(caps); exact_int(max_attempts, positive=True)
        with self._lock:
            root = self._requests.get(request)
            if root is None:
                raise ContractError("E_REQUEST_UNKNOWN")
            if set(caps2) != set(root.caps) or any(caps2[k] > root.caps[k] for k in caps2):
                raise ContractError("E_SUBBUDGET")
            key = (request, task)
            if key in self._tasks:
                raise ContractError("E_TASK_EXISTS")
            self._tasks[key] = _Task(caps2, max_attempts)

    def _snapshot(self, request: str, task: str | None = None) -> dict[str, dict[str, int]]:
        root = self._requests.get(request)
        if root is None:
            raise ContractError("E_REQUEST_UNKNOWN")
        caps = root.caps if task is None else self._tasks.get((request, task), _Task({} ,1)).caps
        if task is not None and (request, task) not in self._tasks:
            raise ContractError("E_SCOPE_UNKNOWN")
        totals = {d: {"limit": limit, "known": 0, "held": 0, "unknown_operations": 0} for d, limit in caps.items()}
        for op in self._operations.values():
            if op.request_id != request or (task is not None and op.task_id != task) or op.state == "CANCELLED_NOT_STARTED":
                continue
            for dim, reserved in op.reservation.items():
                if dim not in totals:
                    continue
                obs = op.usage.get(dim)
                known = 0 if obs is None or obs.amount is None else obs.amount
                if op.state == NOT_STARTED:
                    # A handoff proven not to have started keeps only the usage
                    # already observed (the dispatch recorded at handoff); the
                    # remaining reservation is given back and the operation is
                    # never counted as unresolved.
                    totals[dim]["known"] += known
                    continue
                complete = obs is not None and obs.amount is not None and obs.final
                totals[dim]["known"] += known
                totals[dim]["held"] += 0 if complete else max(reserved - known, 0)
                if not complete and op.state in UNRESOLVED:
                    totals[dim]["unknown_operations"] += 1
        for row in totals.values():
            row["exposure"] = row["known"] + row["held"]
        return totals

    def snapshot(self, request: str, task: str | None = None) -> dict[str, dict[str, int]]:
        with self._lock:
            return json.loads(json.dumps(self._snapshot(request, task)))

    def _epoch(self, generation: int) -> None:
        # ``1.0`` equals ``1``, ``True`` equals ``1`` and an ``int`` subclass is
        # equal to its base value, so equality alone would let a foreign
        # generation through: only an exact ``int`` equal to the current
        # generation is accepted.
        if type(generation) is not int or generation != self._generation:
            raise ContractError("E_FENCED")

    def _owned(self, operation: str, owner: str, generation: int) -> _Operation:
        # Generation, operation, owner: an unhashable operation id must not reach
        # the dict lookup, and a foreign owner must not be compared as equal.
        self._epoch(generation)
        if type(operation) is not str:
            raise ContractError("E_OPERATION_UNKNOWN")
        op = self._operations.get(operation)
        if op is None:
            raise ContractError("E_OPERATION_UNKNOWN")
        if type(owner) is not str:
            raise ContractError("E_FENCED")
        if op.owner != owner or op.generation != generation:
            raise ContractError("E_FENCED")
        return op

    def claim(self, request: str, task: str, plan: int, key: str, owner: str,
              generation: int, guard_version: int, reservation: Mapping[str, int],
              resource_keys: list[str], now: int = 0, lease: int = 30,
              fail_before_commit: bool = False) -> dict[str, Any]:
        # The scope (request/task), the idempotency key and the owner must be
        # exact ``str`` before the intent is fingerprinted and encoded.
        if type(request) is not str or type(task) is not str:
            raise ContractError("E_SCOPE_UNKNOWN")
        if type(key) is not str or type(owner) is not str:
            raise ContractError("E_CLAIM_SHAPE")
        reservation2 = self._caps(reservation)
        for n in (plan, generation, guard_version, now, lease): exact_int(n)
        if plan <= 0 or generation <= 0 or guard_version <= 0 or lease <= 0 or reservation2.get("dispatches") != 1:
            raise ContractError("E_CLAIM_SHAPE")
        if (not isinstance(resource_keys, list)
                or any(type(x) is not str for x in resource_keys)
                or len(set(resource_keys)) != len(resource_keys)
                or any(not x or len(x) > 200 for x in resource_keys)):
            raise ContractError("E_RESOURCE_KEYS")
        intent = {"request": request, "task": task, "plan": plan, "owner": owner,
                  "generation": generation, "guard": guard_version, "reservation": reservation2,
                  "resources": sorted(resource_keys), "lease": lease}
        fingerprint = digest(intent)
        with self._lock:
            existing_id = self._idempotency.get((request, key))
            if existing_id is not None:
                old = self._operations[existing_id]
                if old.request_digest != fingerprint:
                    raise ContractError("E_IDEMPOTENCY_CONFLICT")
                return {"operation_id": old.operation_id, "state": old.state, "replayed": True}
            self._epoch(generation)
            root = self._requests.get(request); item = self._tasks.get((request, task))
            if root is None or item is None: raise ContractError("E_SCOPE_UNKNOWN")
            if root.stopped: raise ContractError("E_STOPPED")
            if item.guard_version != guard_version or not item.fixture_eligible: raise ContractError("E_GUARD_STALE")
            if any(op.request_id == request and op.task_id == task and op.state in LIVE | {"REVIEW_PENDING"}
                   for op in self._operations.values()): raise ContractError("E_ACTIVE_TASK")
            if sum(op.request_id == request and op.task_id == task for op in self._operations.values()) >= item.max_attempts:
                raise ContractError("E_ATTEMPTS")
            if sum(op.request_id == request and op.state in LIVE for op in self._operations.values()) >= root.max_concurrency:
                raise ContractError("E_CONCURRENCY")
            for scope in (None, task):
                totals = self._snapshot(request, scope)
                if set(reservation2) != set(totals): raise ContractError("E_BUDGET_DIMENSION")
                if any(row["unknown_operations"] for row in totals.values()): raise ContractError("E_USAGE_UNKNOWN")
                if any(row["exposure"] + reservation2[d] > row["limit"] for d, row in totals.items()):
                    raise ContractError("E_BUDGET_INSUFFICIENT")
            for resource in resource_keys:
                if (root.repo_id, resource) in self._resource_holds: raise ContractError("E_RESOURCE_BUSY")
            operation_id = "op-" + uuid.uuid4().hex
            op = _Operation(operation_id, request, task, plan, guard_version, key, fingerprint, owner,
                            generation, now + lease, "RESERVED", reservation2, tuple(sorted(resource_keys)))
            # Everything below is under the same lock.  Failpoints leave no partial mutation.
            if fail_before_commit: raise ContractError("E_SYNTHETIC_FAILPOINT")
            self._operations[operation_id] = op; self._idempotency[(request, key)] = operation_id
            for resource in op.resource_keys: self._resource_holds[(root.repo_id, resource)] = operation_id
            self._emit(operation_id, "scheduler.claimed", intent)
            return {"operation_id": operation_id, "state": op.state, "replayed": False}

    # Explicit name for callers that model claim as a reservation operation.
    reserve = claim

    def handoff(self, operation: str, owner: str, generation: int, now: int = 0) -> None:
        exact_int(now)
        with self._lock:
            op = self._owned(operation, owner, generation)
            if op.state != "RESERVED" or now >= op.lease_until: raise ContractError("E_HANDOFF_STATE")
            root = self._requests[op.request_id]; task = self._tasks[(op.request_id, op.task_id)]
            if root.stopped: raise ContractError("E_STOPPED")
            if task.guard_version != op.guard_version or not task.fixture_eligible: raise ContractError("E_GUARD_STALE")
            for scope in (None, op.task_id):
                snap = self._snapshot(op.request_id, scope)
                if any(v["unknown_operations"] or v["exposure"] > v["limit"] for v in snap.values()):
                    raise ContractError("E_BUDGET_GUARD")
            payload = self._encode_payload({"generation": generation})
            dispatch_evidence = digest({"fixture": "local-handoff", "operation": operation})
            op.state = "HANDOFF_ACCEPTED"
            # Dispatch is a local, non-model metric.  Record it exactly once at
            # handoff so a completed operation does not retain an artificial
            # reservation or become ``usage_unknown`` for the dispatches
            # dimension.
            op.usage["dispatches"] = _Usage(
                call_key="local:" + operation, revision=1, amount=1,
                final=True, evidence_hash=dispatch_evidence,
            )
            self._emit(operation, "scheduler.handoff_accepted", payload, encoded=True)

    @staticmethod
    def _identity_copy(process_identity: Any) -> dict[str, Any]:
        """Validate the process identity mapping and return a private copy.

        "Is a mapping" means ``type(value) is dict``: a dict subclass is not
        accepted.  The copy is taken before any further check so only the copy
        is inspected (a caller mutating the argument concurrently cannot bypass
        validation), and every key of the copy must be an exact ``str``.
        """
        if type(process_identity) is not dict:
            raise ContractError("E_IDENTITY_SHAPE")
        copy = dict(process_identity)
        if set(copy) != set(_IDENTITY_FIELDS) or any(type(key) is not str for key in copy):
            raise ContractError("E_IDENTITY_SHAPE")
        try:
            ProcessIdentity(**copy)
        except RuntimeErrorCode as exc:
            raise ContractError("E_IDENTITY_SHAPE", exc.code) from exc
        return copy

    def mark_running(self, operation: str, owner: str, generation: int, *,
                     process_identity: Mapping[str, Any] | None = None) -> None:
        identity = None if process_identity is None else self._identity_copy(process_identity)
        with self._lock:
            op = self._owned(operation, owner, generation)
            if op.state != "HANDOFF_ACCEPTED": raise ContractError("E_START_STATE")
            payload = self._encode_payload({} if identity is None else {"process_identity": identity})
            op.state = "RUNNING"
            self._emit(operation, "run.started", payload, encoded=True)

    def renew(self, operation: str, owner: str, generation: int, now: int, extension: int) -> None:
        exact_int(now); exact_int(extension)
        if extension <= 0: raise ContractError("E_LEASE")
        with self._lock:
            op = self._owned(operation, owner, generation)
            if op.state not in LIVE - {"START_UNKNOWN"} or now >= op.lease_until: raise ContractError("E_LEASE")
            op.lease_until = max(op.lease_until, now + extension)

    def _check_usage(self, op: _Operation, current: _Usage | None, dimension: str, call_key: str,
                     revision: int, amount: int | None, final: bool, evidence_hash: str,
                     correction: bool = False) -> tuple[Any, str, _Usage | None]:
        """Validate one observation *without* touching the ledger.

        Returns the canonical event payload, the result and the record to store
        (``None`` when the recorded usage is kept: a replay or an older
        revision).  Every check -- the exact types, the conflicts with the
        already recorded usage and the canonical encoding of the payload -- runs
        here, before anything is recorded.
        """
        exact_int(revision)
        if (revision <= 0 or type(dimension) is not str or type(call_key) is not str
                or not isinstance(final, bool) or not isinstance(correction, bool)):
            raise ContractError("E_OBSERVATION_SHAPE")
        if amount is not None and (type(amount) is not int or amount < 0): raise ContractError("E_OBSERVATION_SHAPE")
        if final and amount is None: raise ContractError("E_UNKNOWN_FINAL")
        if dimension == "dispatches": raise ContractError("E_LOCAL_METRIC_RESERVED")
        if dimension not in op.reservation: raise ContractError("E_OBSERVATION_SCOPE")
        if type(evidence_hash) is not str or not _HEX64.fullmatch(evidence_hash): raise ContractError("E_OBSERVATION_SHAPE")
        if current and current.call_key != call_key: raise ContractError("E_USAGE_KEY")
        if current and revision == current.revision:
            if (current.amount, current.final, current.evidence_hash) != (amount, final, evidence_hash):
                raise ContractError("E_USAGE_CONFLICT")
            return {}, "REPLAY", None
        if current and revision < current.revision: return {}, "HISTORICAL_ONLY", None
        if current and ((current.amount is not None and (amount is None or amount < current.amount))
                        or (current.final and (not final or amount != current.amount))) and not correction:
            raise ContractError("E_USAGE_CORRECTION_REQUIRED")
        payload = self._encode_payload({"dimension": dimension, "call_key": call_key, "revision": revision,
                                        "amount": amount, "final": final, "correction": correction,
                                        "evidence_hash": evidence_hash})
        return payload, "APPLIED", _Usage(call_key, revision, amount, final, evidence_hash)

    def _record_usage(self, op: _Operation, dimension: str, payload: Any, record: _Usage | None) -> None:
        """Apply an observation validated by :meth:`_check_usage`."""
        if record is None:
            return
        op.usage[dimension] = record
        self._emit(op.operation_id, "usage.observed", payload, encoded=True)

    def observe(self, operation: str, dimension: str, call_key: str, revision: int,
                amount: int | None, final: bool, evidence_hash: str, correction: bool = False) -> str:
        with self._lock:
            if type(operation) is not str:
                raise ContractError("E_OPERATION_UNKNOWN")
            op = self._operations.get(operation)
            if op is None: raise ContractError("E_OPERATION_UNKNOWN")
            payload, result, record = self._check_usage(op, op.usage.get(dimension), dimension, call_key,
                                                        revision, amount, final, evidence_hash, correction)
            self._record_usage(op, dimension, payload, record)
            self._enforce(op.request_id, op.task_id)
            return result

    def settle(self, operation: str, dimension: str | Mapping[str, int], call_key: str | None = None,
               revision: int = 1, amount: int | None = None, final: bool = True,
               evidence_hash: str | None = None, correction: bool = False) -> Any:
        """Record final usage; accepts either one dimension or a mapping.

        A mapping is copied first and every entry is validated against the usage
        recorded by the earlier entries of the same call before the first one is
        recorded: either the whole mapping is recorded or nothing is.
        """
        if isinstance(dimension, Mapping):
            return self._settle_mapping(operation, dimension, revision, final, evidence_hash, correction)
        # The operation and the dimension are checked before the default call key
        # and the default evidence hash are derived from them.
        if type(operation) is not str:
            raise ContractError("E_OPERATION_UNKNOWN")
        if type(dimension) is not str:
            raise ContractError("E_OBSERVATION_SHAPE")
        return self.observe(operation, dimension, call_key or f"local:{operation}:{dimension}", revision,
                            amount, final,
                            evidence_hash or digest({"operation": operation, "dimension": dimension, "amount": amount}),
                            correction)

    def _settle_mapping(self, operation: str, dimensions: Mapping[str, int], revision: int, final: bool,
                        evidence_hash: str | None, correction: bool) -> dict[str, str]:
        if type(operation) is not str:
            raise ContractError("E_OPERATION_UNKNOWN")
        # Only this copy is used from here on.
        items = list(dimensions.items())
        for key, _value in items:
            if type(key) is not str:
                raise ContractError("E_OBSERVATION_SHAPE")
        with self._lock:
            op = self._operations.get(operation)
            if op is None: raise ContractError("E_OPERATION_UNKNOWN")
            recorded = dict(op.usage)
            planned: list[tuple[str, Any, _Usage | None, str]] = []
            for key, value in items:
                evidence = evidence_hash or digest({"operation": operation, "dimension": key, "value": value})
                payload, result, record = self._check_usage(op, recorded.get(key), key, f"local:{operation}:{key}",
                                                            revision, value, final, evidence, correction)
                if record is not None:
                    recorded[key] = record
                planned.append((key, payload, record, result))
            results: dict[str, str] = {}
            for key, payload, record, result in planned:
                self._record_usage(op, key, payload, record)
                results[key] = result
                self._enforce(op.request_id, op.task_id)
            return results

    def _enforce(self, request: str, task: str) -> None:
        reason = None
        for scope in (None, task):
            snap = self._snapshot(request, scope)
            if any(v["exposure"] > v["limit"] for v in snap.values()): reason = "BUDGET_EXCEEDED"; break
            if any(v["unknown_operations"] for v in snap.values()): reason = "USAGE_UNKNOWN"
        if reason:
            root = self._requests[request]
            if root.stopped is None: root.stopped = reason
            self._emit(None, "request.stop_observed", {"request_id": request, "reason": reason})

    def finish(self, operation: str, owner: str, generation: int, candidate_hash: str, cleanup_verified: bool) -> None:
        if type(candidate_hash) is not str or not _HEX64.fullmatch(candidate_hash) or cleanup_verified is not True:
            raise ContractError("E_CLEANUP_OR_CANDIDATE")
        with self._lock:
            op = self._owned(operation, owner, generation)
            if op.state != "RUNNING": raise ContractError("E_FINISH_STATE")
            payload = self._encode_payload({"candidate": candidate_hash})
            op.state = "REVIEW_PENDING"; op.candidate_hash = candidate_hash
            self._release_resources(op)
            self._emit(operation, "run.finished_for_review", payload, encoded=True)
            self._enforce(op.request_id, op.task_id)

    def fixture_verdict(self, operation: str, candidate_hash: str, accepted: bool, evidence_hash: str) -> None:
        if type(accepted) is not bool or type(evidence_hash) is not str or not _HEX64.fullmatch(evidence_hash):
            raise ContractError("E_VERDICT_SHAPE")
        with self._lock:
            if type(operation) is not str: raise ContractError("E_VERDICT_CANDIDATE")
            op = self._operations.get(operation)
            if (op is None or op.state != "REVIEW_PENDING" or type(candidate_hash) is not str
                    or op.candidate_hash != candidate_hash):
                raise ContractError("E_VERDICT_CANDIDATE")
            state = "ACCEPTED" if accepted else "REJECTED"
            payload = self._encode_payload({"state": state, "evidence": evidence_hash})
            op.state = state
            self._emit(operation, "run.verdict", payload, encoded=True)

    def unknown(self, operation: str, owner: str, generation: int, *,
                 reason: str = "START_UNKNOWN") -> None:
        if type(reason) is not str or reason not in UNKNOWN_REASONS:
            raise ContractError("E_UNKNOWN_REASON")
        with self._lock:
            op = self._owned(operation, owner, generation)
            if op.state not in {"HANDOFF_ACCEPTED", "RUNNING"}: raise ContractError("E_UNKNOWN_STATE")
            payload = self._encode_payload({"reason": reason})
            op.state = "START_UNKNOWN"; self._requests[op.request_id].stopped = self._requests[op.request_id].stopped or "START_UNKNOWN"
            self._emit(operation, "scheduler.start_unknown", payload, encoded=True)

    def release_proven_unstarted(self, operation: str, owner: str, generation: int, *,
                                 evidence_hash: str) -> None:
        """Close a handoff that is *proven* not to have started.

        Unlike :meth:`cancel_unstarted` this accepts an operation that already
        reached HANDOFF_ACCEPTED (the candidate was never executed, e.g. exec
        failed or the supervisor rejected the input before the start).  The
        attempt still counts and the dispatch recorded at handoff stays
        counted, but the unused reservation and the resource holds are given
        back.  The request is *not* stopped.
        """
        if type(evidence_hash) is not str or not _HEX64.fullmatch(evidence_hash):
            raise ContractError("E_EVIDENCE_HASH")
        with self._lock:
            op = self._owned(operation, owner, generation)
            if op.state != "HANDOFF_ACCEPTED": raise ContractError("E_NOT_STARTED_STATE")
            payload = self._encode_payload({"evidence": evidence_hash})
            op.state = NOT_STARTED
            self._release_resources(op)
            self._emit(operation, "scheduler.not_started", payload, encoded=True)

    def recover(self, operation: str, owner: str, generation: int, *, outcome: str,
                evidence_hash: str, candidate_hash: str | None = None) -> None:
        """Take over an in-flight operation of a fenced generation from evidence.

        A new controller generation reconstructs what happened to a run it did
        not start.  Every input is validated before anything is mutated; the
        operation must be fenced out (``op.generation < generation``), its
        request must already be stopped, and the evidence outcome decides the
        resulting state.
        """
        if type(outcome) is not str or outcome not in RECOVERY_OUTCOMES:
            raise ContractError("E_RECOVERY_OUTCOME")
        if type(evidence_hash) is not str or not _HEX64.fullmatch(evidence_hash):
            raise ContractError("E_EVIDENCE_HASH")
        if outcome == "FINISHED":
            if type(candidate_hash) is not str or not _HEX64.fullmatch(candidate_hash):
                raise ContractError("E_CLEANUP_OR_CANDIDATE")
        elif candidate_hash is not None:
            raise ContractError("E_CLEANUP_OR_CANDIDATE")
        with self._lock:
            if type(generation) is not int or generation != self._generation:
                raise ContractError("E_FENCED")
            # The new owner is checked with the generation: a foreign owner type
            # is a fenced call, not a silently stored value.
            if type(owner) is not str:
                raise ContractError("E_FENCED")
            if type(operation) is not str:
                raise ContractError("E_OPERATION_UNKNOWN")
            op = self._operations.get(operation)
            if op is None: raise ContractError("E_OPERATION_UNKNOWN")
            if op.generation >= generation: raise ContractError("E_RECOVERY_NOT_FENCED")
            if op.state not in {"HANDOFF_ACCEPTED", "RUNNING", "START_UNKNOWN"}:
                raise ContractError("E_RECOVERY_STATE")
            if self._requests[op.request_id].stopped is None:
                raise ContractError("E_RECOVERY_NOT_STOPPED")
            if outcome == "NOT_STARTED" and op.state == "RUNNING":
                raise ContractError("E_RECOVERY_CONFLICT")
            from_generation = op.generation
            if outcome == "NOT_STARTED":
                state = NOT_STARTED
            elif outcome == "START_UNKNOWN":
                state = "START_UNKNOWN"
            else:
                state = "REVIEW_PENDING"
            # The payload names the taking-over owner and is encoded before the
            # operation is rewritten.
            payload: dict[str, Any] = {"from_generation": from_generation, "outcome": outcome,
                                       "evidence": evidence_hash, "state": state, "owner": owner}
            if outcome == "FINISHED":
                payload["candidate"] = candidate_hash
            payload = self._encode_payload(payload)
            op.owner = owner
            op.generation = generation
            op.state = state
            if outcome == "NOT_STARTED":
                self._release_resources(op)
            elif outcome == "FINISHED":
                op.candidate_hash = candidate_hash
                self._release_resources(op)
            self._emit(operation, "scheduler.recovered", payload, encoded=True)

    def _release_resources(self, op: _Operation) -> None:
        root = self._requests[op.request_id]
        for key in op.resource_keys:
            if self._resource_holds.get((root.repo_id, key)) == op.operation_id:
                self._resource_holds.pop((root.repo_id, key), None)

    def cancel_unstarted(self, operation: str, generation: int) -> None:
        with self._lock:
            self._epoch(generation)
            if type(operation) is not str: raise ContractError("E_OPERATION_UNKNOWN")
            op = self._operations.get(operation)
            if op is None: raise ContractError("E_OPERATION_UNKNOWN")
            if op.state != "RESERVED": raise ContractError("E_CANNOT_PROVE_UNSTARTED")
            if op.usage: raise ContractError("E_UNSTARTED_EVIDENCE_CONFLICT")
            op.state = "CANCELLED_NOT_STARTED"; self._release_resources(op)
            self._emit(operation, "scheduler.cancelled_before_handoff", {})

    def stop(self, request: str, reason: str = "OPERATOR_STOP") -> None:
        if type(request) is not str:
            raise ContractError("E_REQUEST_UNKNOWN")
        if type(reason) is not str or not reason:
            raise ContractError("E_STOP_REASON")
        with self._lock:
            if request not in self._requests: raise ContractError("E_REQUEST_UNKNOWN")
            payload = self._encode_payload({"request_id": request, "reason": reason})
            root = self._requests[request]; root.stopped = root.stopped or reason
            self._emit(None, "request.stopped", payload, encoded=True)

    def rollover(self) -> int:
        with self._lock:
            self._generation += 1
            for root in self._requests.values(): root.stopped = root.stopped or "RECOVERY_REQUIRED"
            self._emit(None, "controller.fenced", {"generation": self._generation})
            return self._generation


# Compatibility alias used by the reference design.
Ledger = Scheduler


__all__ = ["ContractError", "Scheduler", "Ledger", "canonical", "digest", "exact_int",
           "safe_path", "path_conflict", "ref_type", "topological", "ancestors",
           "validate_workflow", "readiness"]
