"""Offline planning draft, candidate and publication primitives for 09.

The planner boundary is deliberately storage agnostic.  It gives the real
dispatcher a small, deterministic authority layer without starting a model or
writing ``.agent`` files.  Bytes are frozen before review and every published
contract is addressed by both its candidate and its immutable member hashes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from . import protocols


MAX_BUNDLE_BYTES = 1_048_576
MAX_TASKS = 128
MAX_REVISIONS = 10_000
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
IDENTITY_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
TASK_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class PlannerError(ValueError):
    """Stable, non-secret error at the planner/publication boundary."""

    def __init__(self, code: str, message: str, field: str = "/") -> None:
        self.code, self.field = code, field
        super().__init__(f"{code} at {field}: {message}")


def _fail(code: str, message: str, field: str = "/") -> None:
    raise PlannerError(code, message, field)


def _json_value(value: Any) -> None:
    try:
        protocols.check_json_value(value, max_bytes=MAX_BUNDLE_BYTES)
    except protocols.ProtocolError as exc:
        _fail(exc.code, exc.reason, exc.field or "/")


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Return the one JSON byte representation used for hashes and receipts."""
    if type(value) is not dict:
        _fail("E_SCHEMA_TYPE", "object required")
    _json_value(value)
    try:
        raw = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        _fail("E_JSON_ENCODING", str(exc))
    if len(raw) > MAX_BUNDLE_BYTES:
        _fail("E_JSON_LIMIT", "canonical JSON exceeds byte limit")
    return raw


def sha256_bytes(raw: bytes) -> str:
    if type(raw) is not bytes:
        _fail("E_RESOURCE_BYTES", "exact bytes required")
    return hashlib.sha256(raw).hexdigest()


def _text(value: Any, field: str, *, pattern: re.Pattern[str] | None = None,
          max_len: int = 256) -> str:
    if type(value) is not str or not value or len(value) > max_len:
        _fail("E_SCHEMA_FIELDS", "non-empty text required", field)
    if any(ord(ch) < 0x20 or 0x7f <= ord(ch) <= 0x9f or 0xd800 <= ord(ch) <= 0xdfff
           for ch in value):
        _fail("E_JSON_UNICODE", "Unicode scalar text required", field)
    if pattern is not None and pattern.fullmatch(value) is None:
        _fail("E_ID_SYNTAX", "invalid identifier", field)
    return value


def validate_resource_ref(ref: Mapping[str, Any], field: str = "/ref") -> Dict[str, Any]:
    """Validate a closed, immutable ResourceRef without reading its path."""
    if type(ref) is not dict or set(ref) != {"kind", "id", "path", "sha256"}:
        _fail("E_RESOURCE_FIELD", "closed ResourceRef required", field)
    kind = _text(ref["kind"], field + "/kind", pattern=re.compile(r"^[a-z][a-z0-9-]{0,63}$"))
    ident = _text(ref["id"], field + "/id", pattern=IDENTITY_RE)
    if ident.lower() in {"latest", "head", "current"}:
        _fail("E_RESOURCE_FIELD", "mutable aliases are forbidden", field + "/id")
    path = _text(ref["path"], field + "/path", max_len=4096)
    try:
        path = protocols.validate_business_path(path, field=field + "/path")
    except protocols.ProtocolError as exc:
        _fail(exc.code, exc.reason, exc.field or field + "/path")
    digest = _text(ref["sha256"], field + "/sha256", pattern=HEX64)
    return {"kind": kind, "id": ident, "path": path, "sha256": digest}


def make_resource_ref(kind: str, ident: str, path: str, raw: bytes | None = None,
                      digest: str | None = None) -> Dict[str, Any]:
    if raw is not None and digest is not None:
        _fail("E_RESOURCE_FIELD", "raw and digest are mutually exclusive")
    digest = sha256_bytes(raw) if raw is not None else digest
    if digest is None:
        _fail("E_RESOURCE_HASH", "raw or digest is required")
    return validate_resource_ref({"kind": kind, "id": ident, "path": path, "sha256": digest})


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


@dataclass(frozen=True)
class ContractIR:
    """The closed intermediate representation rendered to v1 MD and JSON."""

    task_id: str
    plan_version: int
    title: str
    repository: str
    base_sha: str
    risk_level: str
    authorization: Mapping[str, Sequence[str]]
    executor: Mapping[str, Any]
    scope: Mapping[str, Any]
    setup: Sequence[Mapping[str, Any]]
    checks: Sequence[Mapping[str, Any]]
    definition_of_done: Sequence[Mapping[str, Any]]
    budget: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContractIR":
        if type(value) is not dict:
            _fail("E_SCHEMA_TYPE", "ContractIR object required")
        required = {"task_id", "plan_version", "title", "repository", "base_sha",
                    "risk_level", "authorization", "executor", "scope", "setup",
                    "checks", "definition_of_done", "budget"}
        allowed = required | {"schema_version", "contract_family"}
        if set(value) not in (required, allowed):
            _fail("E_SCHEMA_FIELDS", "ContractIR fields must be closed")
        if "schema_version" in value and value["schema_version"] != 1:
            _fail("E_SCHEMA_VERSION", "ContractIR schema_version must be 1", "/schema_version")
        if "contract_family" in value and value["contract_family"] != "planner-v2":
            _fail("E_SCHEMA_FIELDS", "unsupported ContractIR family", "/contract_family")
        task = _text(value["task_id"], "/task_id", pattern=TASK_RE)
        if type(value["plan_version"]) is not int or value["plan_version"] < 1:
            _fail("E_SCHEMA_FIELDS", "positive plan_version required", "/plan_version")
        title = _text(value["title"], "/title", max_len=120)
        repository = _text(value["repository"], "/repository")
        base = _text(value["base_sha"], "/base_sha", pattern=SHA_RE, max_len=64)
        if value["risk_level"] not in {"low", "medium", "high"}:
            _fail("E_SCHEMA_FIELDS", "risk level required", "/risk_level")
        for key in ("authorization", "executor", "scope", "budget"):
            if type(value[key]) is not dict:
                _fail("E_SCHEMA_TYPE", "object required", "/" + key)
        for key in ("setup", "checks", "definition_of_done"):
            if type(value[key]) is not list:
                _fail("E_SCHEMA_TYPE", "array required", "/" + key)
        _json_value(dict(value))
        checks = []
        for item in value["checks"]:
            if type(item) is not dict:
                _fail("E_SCHEMA_TYPE", "check object required", "/checks")
            normalized = _copy(item)
            normalized.setdefault("cwd", ".")
            normalized.setdefault("timeout_seconds", 600)
            normalized.setdefault("expected_exit_code", 0)
            normalized.setdefault("required", True)
            checks.append(normalized)
        return cls(task, value["plan_version"], title, repository, base,
                   value["risk_level"], _copy(value["authorization"]), _copy(value["executor"]),
                   _copy(value["scope"]), _copy(value["setup"]), checks,
                   _copy(value["definition_of_done"]), _copy(value["budget"]))

    def to_mapping(self) -> Dict[str, Any]:
        return {"schema_version": 1, "contract_family": "planner-v2", "task_id": self.task_id, "plan_version": self.plan_version,
                "title": self.title, "repository": self.repository, "base_sha": self.base_sha,
                "risk_level": self.risk_level, "authorization": _copy(self.authorization),
                "executor": _copy(self.executor), "scope": _copy(self.scope), "setup": _copy(self.setup),
                "checks": _copy(self.checks), "definition_of_done": _copy(self.definition_of_done),
                "budget": _copy(self.budget)}


def render_contract_ir(ir: ContractIR | Mapping[str, Any]) -> Tuple[bytes, bytes]:
    """Render the same IR into deterministic legacy v1 JSON and Markdown bytes."""
    if not isinstance(ir, ContractIR):
        ir = ContractIR.from_mapping(ir)
    obj = ir.to_mapping()
    json_bytes = canonical_json_bytes(obj)
    auth = ir.authorization
    ex = ir.executor
    scope = ir.scope
    def lines(items: Iterable[Any]) -> str:
        return "\n".join("- " + (json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, dict) else str(item))
                         for item in items) or "- none"
    checks = "\n".join("- %s: `%s` · cwd: `.` · timeout: %ss · expected exit code: %s" %
                          (c.get("id", "V01"), " ".join(c.get("argv", [])),
                           c.get("timeout_seconds", 600), c.get("expected_exit_code", 0))
                      for c in ir.checks) or "- none"
    dod = "\n".join("- %s: %s; evidence %s" % (d.get("id", "D01"), d.get("text", ""),
                                                  ", ".join(d.get("evidence", []))) for d in ir.definition_of_done) or "- D01: contract rendered; evidence scope"
    md = "\n".join([
        f"# TASK: {ir.task_id}", "", "## Identity",
        f"- plan_version: {ir.plan_version}", "- contract_family: planner-v2", f"- repository: {ir.repository}",
        f"- base_sha: {ir.base_sha}", f"- risk_level: {ir.risk_level}",
        "- authorized_actions: " + (", ".join(auth.get("executor", ())) or "none"),
        "- integration_actions: " + (", ".join(auth.get("integration", ())) or "none"),
        f"- executor: {ex.get('provider')}/{ex.get('model')} · thinking {ex.get('thinking', 'high')}", "",
        "## Goal", ir.title, "", "## Non-goals", "- No provider or process execution.", "",
        "## Repository facts", f"- repository: {ir.repository}", f"- base: {ir.base_sha}", "",
        "## Scope", "- allowed_modified_files:", lines(scope.get("allowed_modified_files", [])),
        "- allowed_new_files:", lines(scope.get("allowed_new_files", [])),
        "- allowed_deleted_files:", lines(scope.get("allowed_deleted_files", [])),
        "- forbidden_paths:", lines(scope.get("forbidden_paths", [])),
        f"- dependency_changes: {scope.get('dependency_changes', 'forbidden')}", "",
        "## Behavior contract", "- Normal input: deterministic ContractIR rendering.",
        "- Boundary input: malformed or mismatched members are rejected.",
        "- Error and retry: callers must correct the immutable input; idempotent retries return the original receipt.", "",
        "## Steps", "1. Validate and freeze the IR.", "2. Review and publish the exact bytes.", "",
        "## Validation commands", checks, "", "## Definition of done", dod, "",
        "## Stop conditions", "- Frozen bytes, schema or review binding mismatch.", "",
        "## Report back", "- Record validation output and immutable member digests.", "",
        "## Forbidden", "- No provider calls, process starts, credentials or managed-directory writes.", "",
        "## Budget", f"- max_attempts_total: {ir.budget.get('max_attempts_total', 1)}",
        f"- max_wall_seconds_per_attempt: {ir.budget.get('max_wall_seconds_per_attempt', 60)}",
        f"- max_wall_seconds_total: {ir.budget.get('max_wall_seconds_total', 60)}", "",
    ]) + "\n"
    return md.encode("utf-8"), json_bytes


@dataclass(frozen=True)
class DraftRevision:
    draft_id: str
    revision: int
    graph_version: int
    state: str
    ir: Mapping[str, Any]
    ir_sha256: str
    parent_revision: int | None


class DraftStore:
    """In-memory immutable revisions with CAS and non-reused version slots."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._revisions: Dict[str, list[DraftRevision]] = {}
        self._slots: Dict[str, set[int]] = {}
        self._draft_tasks: Dict[str, str] = {}
        self._task_versions: Dict[str, set[int]] = {}

    def create_draft(self, draft_id: str, ir: Mapping[str, Any], *, graph_version: int = 1,
                     expected_head: int | None = None) -> DraftRevision:
        _text(draft_id, "/draft_id", pattern=IDENTITY_RE)
        if type(graph_version) is not int or graph_version < 1:
            _fail("E_SCHEMA_FIELDS", "positive graph_version required", "/graph_version")
        normalized = ContractIR.from_mapping(ir).to_mapping()
        with self._lock:
            task_id = normalized["task_id"]
            existing_task = self._draft_tasks.get(draft_id)
            if existing_task is not None and existing_task != task_id:
                _fail("E_DRAFT_BINDING", "draft cannot change task identity", "/task_id")
            rows = self._revisions.setdefault(draft_id, [])
            current = rows[-1].revision if rows else 0
            if rows and expected_head is None:
                _fail("E_CAS_REQUIRED", "existing draft requires expected_head", "/expected_head")
            if expected_head is not None and expected_head != current:
                _fail("E_CAS_CONFLICT", "draft head changed", "/expected_head")
            versions = self._task_versions.setdefault(task_id, set())
            prior_version = rows[-1].ir["plan_version"] if rows else None
            # A revision may retain its current slot, but moving to another
            # slot allocates it exactly once and can never move back to an
            # already-consumed slot.
            if normalized["plan_version"] != prior_version:
                if normalized["plan_version"] in versions:
                    _fail("E_VERSION_REUSED", "plan version slot already used", "/plan_version")
                versions.add(normalized["plan_version"])
            if existing_task is None:
                self._draft_tasks[draft_id] = task_id
            rev = current + 1
            if rev in self._slots.setdefault(draft_id, set()):
                _fail("E_VERSION_REUSED", "revision slot already used", "/revision")
            item = DraftRevision(draft_id, rev, graph_version, "DRAFT", normalized,
                                 sha256_bytes(canonical_json_bytes(normalized)), current or None)
            rows.append(item); self._slots[draft_id].add(rev)
            return _copy(item)

    def revise(self, draft_id: str, ir: Mapping[str, Any], *, expected_head: int,
               graph_version: int | None = None) -> DraftRevision:
        rows = self._revisions.get(draft_id, [])
        if not rows or rows[-1].revision != expected_head:
            _fail("E_CAS_CONFLICT", "draft head changed", "/expected_head")
        return self.create_draft(draft_id, ir, graph_version=graph_version or rows[-1].graph_version,
                                 expected_head=expected_head)

    def head(self, draft_id: str) -> DraftRevision | None:
        rows = self._revisions.get(draft_id, [])
        return _copy(rows[-1]) if rows else None

    def get(self, draft_id: str, revision: int) -> DraftRevision:
        for row in self._revisions.get(draft_id, []):
            if row.revision == revision:
                return _copy(row)
        _fail("E_DRAFT_MISSING", "draft revision does not exist", "/revision")


@dataclass(frozen=True)
class ContractCandidate:
    candidate_id: str
    draft_id: str
    draft_revision: int
    task_id: str
    plan_version: int
    source_ref: Mapping[str, Any]
    target_ref: Mapping[str, Any]
    members: Tuple[Mapping[str, Any], Mapping[str, Any]]
    input_refs: Tuple[Mapping[str, Any], ...]
    summary: str
    state: str = "CANDIDATE"
    _member_fingerprints: Tuple[Tuple[Any, ...], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        fingerprints = []
        try:
            for member in self.members:
                raw = member.get("bytes") if isinstance(member, dict) else None
                fingerprints.append((member.get("representation") if isinstance(member, dict) else None,
                                     member.get("sha256") if isinstance(member, dict) else None,
                                     sha256_bytes(raw) if type(raw) is bytes else None))
        except Exception:
            fingerprints = []
        object.__setattr__(self, "_member_fingerprints", tuple(fingerprints))

    def to_mapping(self) -> Dict[str, Any]:
        members = []
        for member in self.members:
            members.append({k: _copy(v) for k, v in member.items() if k != "bytes"})
        return {"record_type": "contract-candidate", "schema_version": 1,
                "candidate_id": self.candidate_id, "draft_id": self.draft_id,
                "draft_revision": self.draft_revision, "task_id": self.task_id,
                "plan_version": self.plan_version, "source_ref": _copy(self.source_ref),
                "target_ref": _copy(self.target_ref), "members": members,
                "input_refs": [_copy(x) for x in self.input_refs], "summary": self.summary,
                "state": self.state}


def build_candidate(*, candidate_id: str, draft: DraftRevision,
                    source_ref: Mapping[str, Any], target_ref: Mapping[str, Any],
                    md_bytes: bytes, json_bytes: bytes, input_refs: Sequence[Mapping[str, Any]] = (),
                    summary: str = "") -> ContractCandidate:
    _text(candidate_id, "/candidate_id", pattern=IDENTITY_RE)
    src = validate_resource_ref(source_ref, "/source_ref")
    target = validate_resource_ref(target_ref, "/target_ref")
    if canonical_json_bytes(src) == canonical_json_bytes(target):
        _fail("E_CANDIDATE_BINDING", "source and target ResourceRef must be distinct", "/target_ref")
    if type(md_bytes) is not bytes or type(json_bytes) is not bytes:
        _fail("E_RESOURCE_BYTES", "candidate members must be bytes")
    if not md_bytes or not json_bytes:
        _fail("E_RESOURCE_BYTES", "candidate members cannot be empty")
    ir = ContractIR.from_mapping(draft.ir)
    expected_md, expected_json = render_contract_ir(ir)
    if md_bytes != expected_md or json_bytes != expected_json:
        _fail("E_CANDIDATE_BYTES", "members are not the frozen ContractIR rendering")
    members = ({"representation": "markdown", "ref": make_resource_ref("contract-markdown", candidate_id + "-md", "contracts/%s.md" % ir.task_id, md_bytes),
                "sha256": sha256_bytes(md_bytes), "bytes": md_bytes},
               {"representation": "json", "ref": make_resource_ref("contract-json", candidate_id + "-json", "contracts/%s.json" % ir.task_id, json_bytes),
                "sha256": sha256_bytes(json_bytes), "bytes": json_bytes})
    refs = tuple(validate_resource_ref(x, "/input_refs") for x in input_refs)
    ref_keys = [canonical_json_bytes(ref) for ref in refs]
    if len(ref_keys) != len(set(ref_keys)):
        _fail("E_CANDIDATE_BINDING", "input_refs must be unique", "/input_refs")
    return ContractCandidate(candidate_id, draft.draft_id, draft.revision, ir.task_id, ir.plan_version,
                              src, target, members, refs, _text(summary or "contract candidate", "/summary", max_len=1024))


def _validate_candidate_instance(candidate: ContractCandidate) -> None:
    """Validate the in-memory bytes as well as the serialized envelope."""
    if not isinstance(candidate, ContractCandidate):
        _fail("E_CANDIDATE_TYPE", "ContractCandidate required")
    if type(candidate.members) is not tuple or len(candidate.members) != 2:
        _fail("E_CANDIDATE_MEMBERS", "exactly markdown and json members required", "/members")
    if candidate._member_fingerprints and len(candidate._member_fingerprints) == len(candidate.members):
        current_fingerprints = []
        for member in candidate.members:
            raw = member.get("bytes") if isinstance(member, dict) else None
            current_fingerprints.append((member.get("representation") if isinstance(member, dict) else None,
                                         member.get("sha256") if isinstance(member, dict) else None,
                                         sha256_bytes(raw) if type(raw) is bytes else None))
        if tuple(current_fingerprints) != candidate._member_fingerprints:
            _fail("E_CANDIDATE_MUTATED", "candidate member bytes or identity changed after allocation", "/members")
    for index, expected_rep in enumerate(("markdown", "json")):
        member = candidate.members[index]
        if type(member) is not dict or set(member) != {"representation", "ref", "sha256", "bytes"}:
            _fail("E_CANDIDATE_MEMBERS", "member fields must be closed", f"/members/{index}")
        if member["representation"] != expected_rep:
            _fail("E_CANDIDATE_MEMBERS", "members must be ordered markdown then json", f"/members/{index}/representation")
        ref = validate_resource_ref(member["ref"], f"/members/{index}/ref")
        raw = member["bytes"]
        if type(raw) is not bytes:
            _fail("E_RESOURCE_BYTES", "candidate member bytes required", f"/members/{index}/bytes")
        digest = sha256_bytes(raw)
        if member["sha256"] != digest or ref["sha256"] != digest:
            _fail("E_CANDIDATE_BYTES", "member digest does not match frozen bytes", f"/members/{index}")
        _text(member["sha256"], f"/members/{index}/sha256", pattern=HEX64)


def validate_contract_candidate(value: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate the serialized candidate envelope (member bytes are external)."""
    if type(value) is not dict:
        _fail("E_SCHEMA_TYPE", "candidate object required")
    required = {"record_type", "schema_version", "candidate_id", "draft_id", "draft_revision",
                "task_id", "plan_version", "source_ref", "target_ref", "members", "input_refs", "summary", "state"}
    if set(value) != required:
        _fail("E_SCHEMA_FIELDS", "candidate fields must be closed")
    if value["record_type"] != "contract-candidate" or value["schema_version"] != 1:
        _fail("E_SCHEMA_VERSION", "contract-candidate v1 required")
    _text(value["candidate_id"], "/candidate_id", pattern=IDENTITY_RE)
    _text(value["draft_id"], "/draft_id", pattern=IDENTITY_RE)
    _text(value["task_id"], "/task_id", pattern=TASK_RE)
    if type(value["draft_revision"]) is not int or value["draft_revision"] < 1:
        _fail("E_SCHEMA_FIELDS", "positive draft revision required", "/draft_revision")
    if type(value["plan_version"]) is not int or value["plan_version"] < 1:
        _fail("E_SCHEMA_FIELDS", "positive plan version required", "/plan_version")
    validate_resource_ref(value["source_ref"], "/source_ref")
    validate_resource_ref(value["target_ref"], "/target_ref")
    if canonical_json_bytes(value["source_ref"]) == canonical_json_bytes(value["target_ref"]):
        _fail("E_CANDIDATE_BINDING", "source and target ResourceRef must be distinct", "/target_ref")
    members = value["members"]
    if type(members) is not list or len(members) != 2:
        _fail("E_CANDIDATE_MEMBERS", "exactly markdown and json members required", "/members")
    expected_reps = ("markdown", "json")
    for i, member in enumerate(members):
        if type(member) is not dict or set(member) != {"representation", "ref", "sha256"}:
            _fail("E_CANDIDATE_MEMBERS", "member fields must be closed", f"/members/{i}")
        rep = member["representation"]
        if rep != expected_reps[i]:
            _fail("E_CANDIDATE_MEMBERS", "members must be ordered markdown then json", f"/members/{i}/representation")
        ref = validate_resource_ref(member["ref"], f"/members/{i}/ref")
        _text(member["sha256"], f"/members/{i}/sha256", pattern=HEX64)
        if ref["sha256"] != member["sha256"]:
            _fail("E_CANDIDATE_BYTES", "member ref digest must match member digest", f"/members/{i}")
    if type(value["input_refs"]) is not list:
        _fail("E_SCHEMA_TYPE", "input_refs array required", "/input_refs")
    for i, ref in enumerate(value["input_refs"]):
        validate_resource_ref(ref, f"/input_refs/{i}")
    input_keys = [canonical_json_bytes(ref) for ref in value["input_refs"]]
    if len(input_keys) != len(set(input_keys)):
        _fail("E_CANDIDATE_BINDING", "input_refs must be unique", "/input_refs")
    _text(value["summary"], "/summary", max_len=1024)
    if value["state"] not in {"CANDIDATE", "WITHDRAWN", "PUBLISHED"}:
        _fail("E_SCHEMA_FIELDS", "invalid candidate state", "/state")
    _json_value(value)
    return _copy(value)


@dataclass(frozen=True)
class PublicationReceipt:
    publication_id: str
    candidate_id: str
    task_id: str
    plan_version: int
    md_sha256: str
    json_sha256: str
    review_ref: Mapping[str, Any]
    idempotency_key: str
    generation: int
    state: str
    outbox_seq: int

    def to_mapping(self) -> Dict[str, Any]:
        return {"record_type": "publication-receipt", "schema_version": 1,
                "publication_id": self.publication_id, "candidate_id": self.candidate_id,
                "task_id": self.task_id, "plan_version": self.plan_version,
                "members": {"markdown_sha256": self.md_sha256, "json_sha256": self.json_sha256},
                "review_ref": _copy(self.review_ref), "idempotency_key": self.idempotency_key,
                "generation": self.generation, "state": self.state, "outbox_seq": self.outbox_seq}


def validate_publication_receipt(value: Mapping[str, Any]) -> Dict[str, Any]:
    if type(value) is not dict:
        _fail("E_SCHEMA_TYPE", "publication receipt object required")
    required = {"record_type", "schema_version", "publication_id", "candidate_id", "task_id",
                "plan_version", "members", "review_ref", "idempotency_key", "generation", "state", "outbox_seq"}
    if set(value) != required:
        _fail("E_SCHEMA_FIELDS", "publication receipt fields must be closed")
    if value["record_type"] != "publication-receipt" or value["schema_version"] != 1:
        _fail("E_SCHEMA_VERSION", "publication-receipt v1 required")
    for key in ("publication_id", "candidate_id", "idempotency_key"):
        _text(value[key], "/" + key, pattern=IDENTITY_RE)
    _text(value["task_id"], "/task_id", pattern=TASK_RE)
    if type(value["plan_version"]) is not int or value["plan_version"] < 1:
        _fail("E_SCHEMA_FIELDS", "positive plan version required", "/plan_version")
    members = value["members"]
    if type(members) is not dict or set(members) != {"markdown_sha256", "json_sha256"}:
        _fail("E_SCHEMA_FIELDS", "both member hashes required", "/members")
    _text(members["markdown_sha256"], "/members/markdown_sha256", pattern=HEX64)
    _text(members["json_sha256"], "/members/json_sha256", pattern=HEX64)
    review = value["review_ref"]
    if type(review) is not dict or review.get("decision") != "APPROVED":
        _fail("E_REVIEW_BINDING", "approved review reference required", "/review_ref")
    if "approval_id" not in review and "approval_ref" not in review:
        _fail("E_REVIEW_BINDING", "approval reference required", "/review_ref")
    if "candidate_id" in review and review["candidate_id"] != value["candidate_id"]:
        _fail("E_REVIEW_BINDING", "review candidate id does not match receipt", "/review_ref/candidate_id")
    approval_ref = review.get("approval_ref")
    if approval_ref is not None:
        if type(approval_ref) is not dict or set(approval_ref) != {"scope", "generation", "sha256"}:
            _fail("E_REVIEW_BINDING", "approval_ref must be a closed currentness proof", "/review_ref/approval_ref")
        _text(approval_ref["scope"], "/review_ref/approval_ref/scope", pattern=IDENTITY_RE)
        if type(approval_ref["generation"]) is not int or approval_ref["generation"] < 1:
            _fail("E_REVIEW_BINDING", "approval_ref generation required", "/review_ref/approval_ref/generation")
        _text(approval_ref["sha256"], "/review_ref/approval_ref/sha256", pattern=HEX64)
    if type(value["generation"]) is not int or value["generation"] < 1:
        _fail("E_SCHEMA_FIELDS", "positive generation required", "/generation")
    if value["state"] != "PUBLISHED" or type(value["outbox_seq"]) is not int or value["outbox_seq"] < 1:
        _fail("E_SCHEMA_FIELDS", "published state and positive outbox sequence required")
    _json_value(value)
    return _copy(value)


class PublicationStore:
    """Offline publication authority.  Registration precedes its outbox event."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._receipts: Dict[str, PublicationReceipt] = {}
        self._candidates: Dict[str, ContractCandidate] = {}
        self._by_key: Dict[str, str] = {}
        self._bytes: Dict[str, bytes] = {}
        self._heads: Dict[str, Tuple[int, str]] = {}
        self._published_versions: Dict[str, set[int]] = {}
        self._outbox: list[Dict[str, Any]] = []

    @property
    def outbox(self) -> Tuple[Mapping[str, Any], ...]:
        with self._lock:
            return tuple(_copy(self._outbox))

    def register_candidate(self, candidate: ContractCandidate) -> ContractCandidate:
        """Record a frozen candidate without making it executable or published."""
        _validate_candidate_instance(candidate)
        record = validate_contract_candidate(candidate.to_mapping())
        with self._lock:
            prior = self._candidates.get(candidate.candidate_id)
            if prior is not None and prior.to_mapping() != record:
                _fail("E_CANDIDATE_CONFLICT", "candidate id is already bound to different bytes")
            self._candidates[candidate.candidate_id] = _copy(candidate)
            return _copy(candidate)

    def candidate(self, candidate_id: str) -> ContractCandidate:
        try:
            return _copy(self._candidates[candidate_id])
        except KeyError:
            _fail("E_CANDIDATE_MISSING", "candidate does not exist", "/candidate_id")

    def publish(self, candidate: ContractCandidate, *, review_ref: Mapping[str, Any],
                idempotency_key: str, expected_generation: int = 0,
                publication_id: str | None = None) -> PublicationReceipt:
        _text(idempotency_key, "/idempotency_key", pattern=IDENTITY_RE)
        self.register_candidate(candidate)
        # Work from the deep-copied candidate held by the store.  Callers may
        # mutate their own frozen dataclass's nested dictionaries after build;
        # those bytes must never be used for a receipt or projection.
        candidate = self.candidate(candidate.candidate_id)
        if candidate.state != "CANDIDATE":
            _fail("E_CANDIDATE_STATE", "only an unpublished candidate may be published", "/state")
        if type(review_ref) is not dict:
            _fail("E_REVIEW_BINDING", "review reference object required", "/review_ref")
        if review_ref.get("decision") != "APPROVED":
            _fail("E_REVIEW_BINDING", "current APPROVED review required", "/review_ref/decision")
        if "approval_id" not in review_ref and "approval_ref" not in review_ref:
            _fail("E_REVIEW_BINDING", "approval reference required", "/review_ref")
        md_hash = candidate.members[0]["sha256"]; json_hash = candidate.members[1]["sha256"]
        if review_ref.get("candidate_id") is not None and review_ref.get("candidate_id") != candidate.candidate_id:
            _fail("E_REVIEW_BINDING", "review is bound to another candidate", "/review_ref/candidate_id")
        member_binding = review_ref.get("members")
        if member_binding is not None and member_binding != {"markdown_sha256": md_hash, "json_sha256": json_hash}:
            _fail("E_REVIEW_BINDING", "review member digests do not match candidate", "/review_ref/members")
        bound_review = _copy(dict(review_ref))
        bound_review.setdefault("candidate_id", candidate.candidate_id)
        # The idempotency key covers the complete reviewed action.  Binding it
        # only to the candidate would let a retry with a different approval
        # payload replay the original publication receipt and bypass the
        # caller's currentness/approval checks.  Canonicalize once before
        # entering the store lock so malformed review data is rejected at the
        # same boundary as the rest of the publication request.
        review_binding = canonical_json_bytes(bound_review)
        with self._lock:
            prior_id = self._by_key.get(idempotency_key)
            if prior_id:
                prior = self._receipts[prior_id]
                if (prior.candidate_id, prior.md_sha256, prior.json_sha256) != (candidate.candidate_id, md_hash, json_hash):
                    _fail("E_IDEMPOTENCY_CONFLICT", "idempotency key reused with another candidate")
                if canonical_json_bytes(dict(prior.review_ref)) != review_binding:
                    _fail("E_IDEMPOTENCY_CONFLICT", "idempotency key reused with another review")
                return _copy(prior)
            current_gen, current_pub = self._heads.get(candidate.task_id, (0, ""))
            if expected_generation != current_gen:
                _fail("E_CAS_CONFLICT", "publication head changed", "/expected_generation")
            if candidate.plan_version in self._published_versions.setdefault(candidate.task_id, set()):
                _fail("E_VERSION_REUSED", "published plan version slot cannot be reused", "/plan_version")
            pub_id = publication_id or "pub-" + uuid.uuid4().hex
            _text(pub_id, "/publication_id", pattern=IDENTITY_RE)
            existing = self._receipts.get(pub_id)
            if existing is not None:
                if (existing.candidate_id, existing.md_sha256, existing.json_sha256,
                        existing.idempotency_key) == (candidate.candidate_id, md_hash, json_hash, idempotency_key):
                    return _copy(existing)
                _fail("E_PUBLICATION_CONFLICT", "publication id is already bound to another publication", "/publication_id")
            # Both members are installed before the receipt/outbox is visible.
            self._bytes[md_hash] = candidate.members[0]["bytes"]
            self._bytes[json_hash] = candidate.members[1]["bytes"]
            seq = len(self._outbox) + 1
            receipt = PublicationReceipt(pub_id, candidate.candidate_id, candidate.task_id,
                                         candidate.plan_version, md_hash, json_hash,
                                         bound_review, idempotency_key, current_gen + 1,
                                         "PUBLISHED", seq)
            self._receipts[pub_id] = receipt; self._by_key[idempotency_key] = pub_id
            self._published_versions[candidate.task_id].add(candidate.plan_version)
            self._heads[candidate.task_id] = (receipt.generation, pub_id)
            self._outbox.append({"seq": seq, "event_type": "contract-published",
                                 "publication_id": pub_id, "task_id": candidate.task_id,
                                 "generation": receipt.generation,
                                 "payload": receipt.to_mapping()})
            return _copy(receipt)

    def get(self, publication_id: str) -> PublicationReceipt:
        try:
            return _copy(self._receipts[publication_id])
        except KeyError:
            _fail("E_PUBLICATION_MISSING", "publication does not exist", "/publication_id")

    def publish_to_repository(self, repo_root: str | Path, candidate: ContractCandidate, *,
                              review_ref: Mapping[str, Any], idempotency_key: str,
                              expected_generation: int = 0,
                              publication_id: str | None = None) -> PublicationReceipt:
        """Register then project an approved candidate into a repository.

        The in-memory registration is authoritative for this offline service;
        the receipt and both contract members are projected only after their
        immutable bytes are complete.  A consumer can therefore reject a
        missing/half-written sidecar instead of treating a file pair as a
        published contract.
        """
        root = Path(repo_root).resolve()
        plans = root / ".agent" / "plans"
        publications = root / ".agent" / "publications"
        plans.mkdir(parents=True, exist_ok=True)
        publications.mkdir(parents=True, exist_ok=True)
        receipt = self.publish(candidate, review_ref=review_ref,
                               idempotency_key=idempotency_key,
                               expected_generation=expected_generation,
                               publication_id=publication_id)

        def atomic_bytes(path: Path, raw: bytes) -> None:
            temp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
            try:
                temp.write_bytes(raw)
                os.replace(str(temp), str(path))
            finally:
                if temp.exists():
                    temp.unlink()

        canonical = self.candidate(candidate.candidate_id)
        md = canonical.members[0]["bytes"]
        machine = canonical.members[1]["bytes"]
        receipt_raw = (json.dumps(receipt.to_mapping(), ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        ledger_path = publications / "outbox.jsonl"
        prior_ledger = ledger_path.read_bytes() if ledger_path.exists() else b""
        ledger_lines = [line for line in prior_ledger.splitlines() if line]
        if not any(line == receipt_raw.rstrip(b"\n") for line in ledger_lines):
            ledger_bytes = prior_ledger
            if ledger_bytes and not ledger_bytes.endswith(b"\n"):
                ledger_bytes += b"\n"
            atomic_bytes(ledger_path, ledger_bytes + receipt_raw)
        md_path = plans / (f"{candidate.task_id}.v{candidate.plan_version}.md")
        json_path = plans / (f"{candidate.task_id}.v{candidate.plan_version}.json")
        atomic_bytes(md_path, md)
        atomic_bytes(json_path, machine)
        receipt_record = receipt.to_mapping()
        atomic_bytes(publications / (f"{candidate.task_id}.v{candidate.plan_version}.json"),
                     receipt_raw)
        return receipt

    def bytes_for(self, publication_id: str, representation: str) -> bytes:
        receipt = self.get(publication_id)
        digest = receipt.md_sha256 if representation in {"md", "markdown"} else receipt.json_sha256 if representation == "json" else None
        if digest is None:
            _fail("E_SCHEMA_FIELDS", "representation must be markdown or json", "/representation")
        raw = self._bytes.get(digest)
        if raw is None or sha256_bytes(raw) != digest:
            _fail("E_PUBLICATION_CORRUPT", "published bytes are missing or changed")
        return bytes(raw)

    def recover(self, publication_id: str) -> PublicationReceipt:
        """Rebuild a lost in-memory receipt from the immutable outbox event."""
        if publication_id in self._receipts:
            return self.get(publication_id)
        for event in self._outbox:
            if event.get("publication_id") == publication_id:
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    break
                receipt = PublicationReceipt(payload["publication_id"], payload["candidate_id"], payload["task_id"],
                                             payload["plan_version"], payload["members"]["markdown_sha256"],
                                             payload["members"]["json_sha256"], payload["review_ref"],
                                             payload["idempotency_key"], payload["generation"], payload["state"], payload["outbox_seq"])
                self._receipts[publication_id] = receipt
                self._by_key[receipt.idempotency_key] = publication_id
                self._published_versions.setdefault(receipt.task_id, set()).add(receipt.plan_version)
                current = self._heads.get(receipt.task_id)
                if current is None or receipt.generation > current[0]:
                    self._heads[receipt.task_id] = (receipt.generation, receipt.publication_id)
                return _copy(receipt)
        _fail("E_PUBLICATION_MISSING", "publication cannot be recovered", "/publication_id")

    def recover_from_repository(self, repo_root: str | Path, publication_id: str) -> PublicationReceipt:
        """Recover a receipt and its exact pair after a fresh process starts."""
        root = Path(repo_root).resolve()
        publications = root / ".agent" / "publications"
        matches: list[dict[str, Any]] = []
        for path in sorted(publications.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                value = validate_publication_receipt(protocols.strict_json_loads(path.read_bytes()))
            except (OSError, ValueError, PlannerError, protocols.ProtocolError):
                continue
            if value.get("publication_id") == publication_id:
                matches.append(value)
        if len(matches) != 1:
            _fail("E_PUBLICATION_MISSING", "publication receipt is missing or ambiguous", "/publication_id")
        value = matches[0]
        ledger_path = publications / "outbox.jsonl"
        ledger_matches = []
        if ledger_path.is_file() and not ledger_path.is_symlink():
            for line in ledger_path.read_bytes().splitlines():
                if not line:
                    continue
                try:
                    item = validate_publication_receipt(protocols.strict_json_loads(line))
                except (UnicodeDecodeError, ValueError, PlannerError, protocols.ProtocolError):
                    continue
                if item.get("publication_id") == publication_id:
                    ledger_matches.append(item)
        if len(ledger_matches) != 1 or ledger_matches[0] != value:
            _fail("E_PUBLICATION_MISSING", "publication receipt is not present in persistent outbox", "/publication_id")
        task_version = f"{value['task_id']}.v{value['plan_version']}"
        md_path = root / ".agent" / "plans" / (task_version + ".md")
        json_path = root / ".agent" / "plans" / (task_version + ".json")
        md = md_path.read_bytes() if md_path.is_file() else b""
        machine = json_path.read_bytes() if json_path.is_file() else b""
        if sha256_bytes(md) != value["members"]["markdown_sha256"] or sha256_bytes(machine) != value["members"]["json_sha256"]:
            _fail("E_PUBLICATION_CORRUPT", "published contract pair is missing or changed")
        receipt = PublicationReceipt(value["publication_id"], value["candidate_id"], value["task_id"], value["plan_version"],
                                     value["members"]["markdown_sha256"], value["members"]["json_sha256"], value["review_ref"],
                                     value["idempotency_key"], value["generation"], value["state"], value["outbox_seq"])
        with self._lock:
            self._receipts[publication_id] = receipt
            self._by_key[receipt.idempotency_key] = publication_id
            self._bytes[receipt.md_sha256] = bytes(md); self._bytes[receipt.json_sha256] = bytes(machine)
            self._published_versions.setdefault(receipt.task_id, set()).add(receipt.plan_version)
            self._heads[receipt.task_id] = (receipt.generation, receipt.publication_id)
        return _copy(receipt)


class PlanningService:
    """Convenience facade used by an eventual CLI; all operations stay offline."""

    def __init__(self, drafts: DraftStore | None = None, publications: PublicationStore | None = None,
                 approval_ledger: Any | None = None) -> None:
        self.drafts = drafts or DraftStore()
        self.publications = publications or PublicationStore()
        self.approval_ledger = approval_ledger
        self._reviews: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _candidate_approval_refs(candidate: ContractCandidate) -> list[Mapping[str, Any]]:
        return [candidate.source_ref, candidate.target_ref, *candidate.input_refs]

    def _check_current_review(self, candidate: ContractCandidate, review_ref: Mapping[str, Any], *, lock: bool) -> Dict[str, Any]:
        """Bind optional 03 ApprovalLedger currentness and input-lock checks."""
        if self.approval_ledger is None:
            return _copy(dict(review_ref))
        required = ("scope", "generation", "record_sha256", "input_refs")
        if any(name not in review_ref for name in required):
            _fail("E_REVIEW_CURRENT", "ApprovalLedger currentness fields are required", "/review_ref")
        try:
            head = self.approval_ledger.assert_current(
                scope=review_ref["scope"], generation=review_ref["generation"],
                record_sha256=review_ref["record_sha256"])
        except Exception as exc:
            _fail("E_REVIEW_CURRENT", str(exc), "/review_ref")
        approval_id = review_ref.get("approval_id")
        if approval_id is not None and head.record.get("approval_id") != approval_id:
            _fail("E_REVIEW_CURRENT", "review approval id is stale", "/review_ref/approval_id")
        approved_refs = review_ref["input_refs"]
        if type(approved_refs) is not list:
            _fail("E_REVIEW_CURRENT", "review input_refs must be an array", "/review_ref/input_refs")
        try:
            expected = {canonical_json_bytes(dict(ref)) for ref in self._candidate_approval_refs(candidate)}
            supplied = {canonical_json_bytes(dict(ref)) for ref in approved_refs}
        except (TypeError, ValueError) as exc:
            _fail("E_REVIEW_CURRENT", str(exc), "/review_ref/input_refs")
        if supplied != expected:
            _fail("E_REVIEW_CURRENT", "review input closure does not match candidate", "/review_ref/input_refs")
        result = _copy(dict(review_ref))
        if lock:
            receipt_id = result.get("input_lock_receipt_id")
            lock_operation_id = result.get("input_lock_operation_id")
            if type(receipt_id) is not str or type(lock_operation_id) is not str:
                _fail("E_REVIEW_CURRENT", "input lock receipt and operation ids are required", "/review_ref")
            try:
                input_lock = self.approval_ledger.lock(
                    scope=result["scope"], receipt_id=receipt_id,
                    operation_id=lock_operation_id, bindings=approved_refs)
            except Exception as exc:
                _fail("E_REVIEW_CURRENT", str(exc), "/review_ref/input_refs")
            result["input_lock_ref"] = _copy({
                "receipt_id": input_lock.receipt_id,
                "approval_ref": input_lock.approval_ref,
                "head_generation": input_lock.head_generation,
                "receipt_sha256": input_lock.receipt_sha256,
            })
        else:
            lock_ref = result.get("input_lock_ref")
            if type(lock_ref) is not dict or lock_ref.get("head_generation") != head.generation:
                _fail("E_REVIEW_CURRENT", "current input lock is required before publish", "/review_ref/input_lock_ref")
            approval_ref = lock_ref.get("approval_ref")
            if (type(approval_ref) is not dict or approval_ref.get("scope") != result["scope"]
                    or approval_ref.get("generation") != head.generation
                    or approval_ref.get("sha256") != head.record_sha256):
                _fail("E_REVIEW_CURRENT", "input lock is bound to a stale approval", "/review_ref/input_lock_ref")
            # Currentness alone is not permission.  Publishing consumes the
            # reviewed action, which also enforces reviewed_actions,
            # not_before, expires_at and the ledger's idempotent generation
            # fence.  A deterministic operation key makes a retry after a
            # response loss safe without creating a second budget/action.
            try:
                consumed = self.approval_ledger.consume_once(
                    scope=result["scope"], action="publish_contract", input_refs=approved_refs,
                    operation_id="publication-review-" + candidate.candidate_id,
                    expected_generation=head.generation)
            except Exception as exc:
                _fail("E_REVIEW_CURRENT", str(exc), "/review_ref")
            if consumed is not True:
                _fail("E_REVIEW_CURRENT", "publish_contract approval is not currently consumable", "/review_ref")
        return result

    def request_planning(self, draft_id: str, ir: Mapping[str, Any], **kwargs: Any) -> DraftRevision:
        return self.drafts.create_draft(draft_id, ir, **kwargs)

    def ingest_planning_result(self, draft_id: str, ir: Mapping[str, Any], *, expected_head: int | None = None,
                               graph_version: int = 1) -> DraftRevision:
        if expected_head is None:
            return self.request_planning(draft_id, ir, graph_version=graph_version)
        return self.drafts.revise(draft_id, ir, expected_head=expected_head, graph_version=graph_version)

    def allocate_contract_candidate(self, **kwargs: Any) -> ContractCandidate:
        candidate = build_candidate(**kwargs)
        return self.publications.register_candidate(candidate)

    def publish_contract(self, candidate: ContractCandidate, *, repo_root: str | Path | None = None,
                         **kwargs: Any) -> PublicationReceipt:
        stored_review = self._reviews.get(candidate.candidate_id)
        if self.approval_ledger is not None:
            if stored_review is None:
                _fail("E_REVIEW_BINDING", "submit_plan_review is required before publish")
            if "review_ref" in kwargs and kwargs["review_ref"] != stored_review:
                _fail("E_REVIEW_BINDING", "published review must be the bound review", "/review_ref")
            kwargs["review_ref"] = _copy(stored_review)
        if "review_ref" not in kwargs:
            review_ref = stored_review
            if review_ref is None:
                _fail("E_REVIEW_BINDING", "submit_plan_review is required before publish")
            kwargs["review_ref"] = _copy(review_ref)
        kwargs["review_ref"] = self._check_current_review(candidate, kwargs["review_ref"], lock=False)
        if repo_root is not None:
            return self.publications.publish_to_repository(repo_root, candidate, **kwargs)
        return self.publications.publish(candidate, **kwargs)

    def submit_plan_review(self, candidate: ContractCandidate, review_ref: Mapping[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        """Review is a binding operation; publication remains a separate explicit call."""
        if type(review_ref) is not dict or review_ref.get("decision") != "APPROVED":
            _fail("E_REVIEW_BINDING", "APPROVED review required")
        if review_ref.get("candidate_id") not in (None, candidate.candidate_id):
            _fail("E_REVIEW_BINDING", "review is bound to another candidate")
        bound_review = self._check_current_review(candidate, review_ref, lock=True)
        self.publications.register_candidate(candidate)
        self._reviews[candidate.candidate_id] = _copy(bound_review)
        return _copy(bound_review)

    def get_publication(self, publication_id: str) -> PublicationReceipt:
        return self.publications.get(publication_id)

    def recover_publication(self, publication_id: str, *, repo_root: str | Path | None = None) -> PublicationReceipt:
        if repo_root is not None:
            return self.publications.recover_from_repository(repo_root, publication_id)
        return self.publications.recover(publication_id)


# Explicit aliases keep callers independent of the in-memory implementation name.
DraftRevisionStore = DraftStore
ContractPublicationStore = PublicationStore


__all__ = ["ContractIR", "ContractCandidate", "DraftRevision", "DraftStore", "DraftRevisionStore",
           "PublicationReceipt", "PublicationStore", "ContractPublicationStore", "PlanningService",
           "PlannerError", "canonical_json_bytes", "sha256_bytes", "validate_resource_ref",
           "make_resource_ref", "render_contract_ir", "build_candidate", "validate_contract_candidate",
           "validate_publication_receipt"]
