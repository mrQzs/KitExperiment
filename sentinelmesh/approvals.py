"""Approval records and exact input locks for SentinelMesh."""
from __future__ import annotations

from dataclasses import dataclass
import copy
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Mapping
from uuid import uuid4

from .core import canonical_json, sha256_bytes, validate_resource_ref


class ApprovalError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest_refs(refs: Mapping[str, str] | list[Mapping[str, Any]]) -> str:
    if isinstance(refs, Mapping):
        value = dict(sorted(refs.items()))
    else:
        value = sorted((dict(x) for x in refs), key=lambda x: (str(x.get("kind")), str(x.get("id")), str(x.get("sha256"))))
    # The public canonical serializer intentionally accepts an object root;
    # wrapping the ordered list keeps the digest deterministic without changing
    # the caller-facing lock shape.
    return sha256_bytes(canonical_json({"refs": value}))


def _normalize_refs(refs: Any) -> list[dict[str, Any]]:
    if not isinstance(refs, list) or not refs:
        raise ApprovalError("E_APPROVAL_REFS")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for index, ref in enumerate(refs):
        if not isinstance(ref, Mapping):
            raise ApprovalError("E_APPROVAL_REFS")
        item = copy.deepcopy(dict(ref))
        try:
            validate_resource_ref(item)
        except Exception as exc:
            raise ApprovalError("E_APPROVAL_REFS") from exc
        key = (item["kind"], item["id"], item["sha256"])
        if key in seen:
            raise ApprovalError("E_APPROVAL_REFS")
        seen.add(key)
        normalized.append(item)
    return normalized


def _text(value: Any, code: str, *, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if not isinstance(value, str) or (not optional and not value) or any(
        ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    ):
        raise ApprovalError(code)
    return value


@dataclass(frozen=True)
class InputLock:
    lock_id: str
    generation: int
    refs: tuple[Mapping[str, Any], ...]
    digest: str
    binding: Mapping[str, Any]


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    subject_id: str
    lock: InputLock
    actor: str
    created_at: str
    status: str = "APPROVED"
    reason: str = ""
    revoked_at: str | None = None


class ApprovalStore:
    """In-memory reference store. Production wiring should persist the same CAS rules transactionally."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._generation = 0
        self._records: dict[str, ApprovalRecord] = {}
        self._current: dict[str, str] = {}

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def approve(self, subject_id: str, refs: list[Mapping[str, Any]], *, actor: str, expected_generation: int | None = None, binding: Mapping[str, Any] | None = None, reason: str = "") -> ApprovalRecord:
        _text(subject_id, "E_APPROVAL_REQUIRED")
        _text(actor, "E_APPROVAL_REQUIRED")
        _text(reason, "E_APPROVAL_REASON", optional=True)
        normalized_refs = _normalize_refs(refs)
        if binding is not None and not isinstance(binding, Mapping):
            raise ApprovalError("E_APPROVAL_BINDING")
        if expected_generation is not None and (
            type(expected_generation) is not int or expected_generation < 0
        ):
            raise ApprovalError("E_HEAD_CONFLICT")
        digest = _digest_refs(normalized_refs)
        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                raise ApprovalError("E_HEAD_CONFLICT")
            current = self._current.get(subject_id)
            if current:
                old = self._records[current]
                if old.lock.digest == digest and old.status == "APPROVED":
                    return old
                self._records[current] = ApprovalRecord(**{**old.__dict__, "status": "SUPERSEDED"})
            self._generation += 1
            lock = InputLock(f"lock-{uuid4().hex}", self._generation, tuple(copy.deepcopy(x) for x in normalized_refs), digest, copy.deepcopy(dict(binding or {})))
            record = ApprovalRecord(f"apr-{uuid4().hex}", subject_id, lock, actor, _now(), reason=reason)
            self._records[record.approval_id] = record
            self._current[subject_id] = record.approval_id
            return record

    def revoke(self, approval_id: str, *, actor: str, reason: str) -> ApprovalRecord:
        _text(approval_id, "E_APPROVAL_UNKNOWN")
        _text(actor, "E_APPROVAL_ACTOR")
        _text(reason, "E_APPROVAL_REASON")
        with self._lock:
            if approval_id not in self._records:
                raise ApprovalError("E_APPROVAL_UNKNOWN")
            old = self._records[approval_id]
            if old.status == "REVOKED":
                return old
            updated = ApprovalRecord(**{**old.__dict__, "status": "REVOKED", "revoked_at": _now(), "reason": reason})
            self._records[approval_id] = updated
            if self._current.get(old.subject_id) == approval_id:
                del self._current[old.subject_id]
            return updated

    def validate(self, approval_id: str, refs: list[Mapping[str, Any]], *, binding: Mapping[str, Any] | None = None) -> ApprovalRecord:
        _text(approval_id, "E_APPROVAL_UNKNOWN")
        normalized_refs = _normalize_refs(refs)
        if binding is not None and not isinstance(binding, Mapping):
            raise ApprovalError("E_APPROVAL_BINDING")
        with self._lock:
            record = self._records.get(approval_id)
            if record is None:
                raise ApprovalError("E_APPROVAL_UNKNOWN")
            if record.status != "APPROVED":
                raise ApprovalError("E_APPROVAL_STALE")
            if _digest_refs(normalized_refs) != record.lock.digest:
                raise ApprovalError("E_INPUT_DRIFT")
            if binding is not None and dict(binding) != dict(record.lock.binding):
                raise ApprovalError("E_BINDING_DRIFT")
            return record

    def current(self, subject_id: str) -> ApprovalRecord | None:
        with self._lock:
            aid = self._current.get(subject_id)
            return self._records.get(aid) if aid else None
