"""Offline integration acceptance records for module 12.

The records in this module deliberately form a read-only evidence boundary.
They freeze a candidate and its checks, verify immutable evidence members, and
reduce observations to coverage.  A delivery decision is an explicit report;
it never grants dispatch, approval, commit, or release permission.

No function here reads Git, starts a process, authenticates a collector, or
contacts a provider.  Real platform and connector evidence must be supplied
by an authorised collector and remains distinct from the deterministic
fixtures used by these helpers.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import unicodedata
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Optional, Sequence


MAX_BYTES = 1_048_576
MAX_TOTAL_BYTES = 16 * MAX_BYTES
MAX_ENTRIES = 10_000
MAX_SAFE_INTEGER = 2**53 - 1
HEX64 = re.compile(r"^[0-9a-f]{64}$")
IDENT = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
KIND = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

RUN_STATES = frozenset({"NOT_RUN", "RUNNING", "EXITED", "CANCELLED", "START_UNKNOWN"})
OUTCOMES = frozenset({"PASS", "FAIL", "ERROR", "SKIPPED", "CANCELLED", "INCOMPLETE", "WAIVED", "NOT_RUN"})
SOURCE_KINDS = frozenset({"synthetic", "processed", "captured", "local", "document"})
EVIDENCE_STATUSES = frozenset({"COMPLETE", "PARTIAL", "NONE", "BLOCKED"})
DECISIONS = frozenset({"FULL", "PARTIAL", "BLOCKED", "WITHDRAWN"})
MUTABLE_ALIASES = frozenset({"latest", "head", "current", "tip", "default", "stable"})


class IntegrationRecordError(ValueError):
    """Stable, non-secret validation failure for an integration record."""

    def __init__(self, code: str, field: str = "/", message: str = "invalid integration record") -> None:
        self.code = code
        self.field = field
        self.reason = message
        super().__init__(f"{code} at {field}: {message}")


def _fail(code: str, field: str, message: str) -> None:
    raise IntegrationRecordError(code, field, message)


def _text(value: Any, field: str, *, max_len: int = 4096) -> str:
    if type(value) is not str or not value or len(value) > max_len:
        _fail("E_TEXT", field, "non-empty text required")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or 0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        _fail("E_TEXT", field, "safe Unicode scalar text required")
    return value


def _id(value: Any, field: str) -> str:
    value = _text(value, field, max_len=128)
    if IDENT.fullmatch(value) is None or value.lower() in MUTABLE_ALIASES:
        _fail("E_ID", field, "immutable lowercase identifier required")
    return value


def _sha(value: Any, field: str) -> str:
    if type(value) is not str or HEX64.fullmatch(value) is None:
        _fail("E_HASH", field, "lowercase SHA-256 required")
    return value


def _strict(value: Any, field: str = "/", depth: int = 0, active: Optional[set[int]] = None) -> None:
    """Reject values that a JSON encoder would silently coerce."""
    if depth > 64:
        _fail("E_DEPTH", field, "JSON nesting limit exceeded")
    if value is None or type(value) is bool or type(value) is int:
        if type(value) is int and abs(value) > MAX_SAFE_INTEGER:
            _fail("E_INTEGER", field, "integer exceeds exact JSON range")
        return
    if type(value) is float:
        _fail("E_NUMBER", field, "floating point values are not accepted")
    if type(value) is str:
        _text(value, field)
        return
    if type(value) not in (dict, list):
        _fail("E_JSON", field, "strict JSON value required")
    if active is None:
        active = set()
    marker = id(value)
    if marker in active:
        _fail("E_JSON", field, "cyclic value is not JSON")
    active.add(marker)
    try:
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    _fail("E_JSON", field, "object keys must be strings")
                _text(key, field + "/key")
                _strict(item, field + "/" + key, depth + 1, active)
        else:
            for index, item in enumerate(value):
                _strict(item, f"{field}/{index}", depth + 1, active)
    finally:
        active.remove(marker)


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    if type(value) is not dict:
        _fail("E_SCHEMA_TYPE", "/", "object required")
    _strict(value)
    try:
        raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise IntegrationRecordError("E_JSON", "/", "canonical JSON encoding failed") from exc
    if len(raw) > MAX_BYTES:
        _fail("E_SIZE", "/", "record exceeds byte limit")
    return raw


def digest_bytes(raw: bytes) -> str:
    if type(raw) is not bytes:
        _fail("E_BYTES", "/", "bytes required")
    return hashlib.sha256(raw).hexdigest()


def digest(value: Any) -> str:
    return digest_bytes(value if type(value) is bytes else canonical_bytes(value))


# Familiar names used by the earlier offline gate helpers.
canonical_json = canonical_bytes
sha256_hex = digest_bytes


def resource_ref(value: Any, field: str = "/ref") -> dict[str, str]:
    if type(value) is not dict or set(value) != {"kind", "id", "path", "sha256"}:
        _fail("E_REF_FIELDS", field, "closed ResourceRef required")
    kind = _text(value["kind"], field + "/kind", max_len=64)
    if KIND.fullmatch(kind) is None:
        _fail("E_REF_KIND", field + "/kind", "invalid resource kind")
    ident = _id(value["id"], field + "/id")
    path = _text(value["path"], field + "/path", max_len=1024).replace("\\", "/")
    parts = path.split("/")
    if path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        _fail("E_REF_PATH", field + "/path", "repository-relative path required")
    if any(ch in path for ch in ":*?<>|\"\x00"):
        _fail("E_REF_PATH", field + "/path", "unsafe path")
    return {"kind": kind, "id": ident, "path": path, "sha256": _sha(value["sha256"], field + "/sha256")}


def ref_key(value: Any, field: str = "/ref") -> tuple[str, str, str, str]:
    normalized = resource_ref(value, field)
    return tuple(normalized[key] for key in ("kind", "id", "path", "sha256"))


def _refs(value: Any, field: str, *, required: bool = False) -> tuple[dict[str, str], ...]:
    if type(value) is not list or (required and not value) or len(value) > MAX_ENTRIES:
        _fail("E_REFS", field, "bounded list of ResourceRefs required")
    result = tuple(resource_ref(item, f"{field}/{index}") for index, item in enumerate(value))
    if len({ref_key(item) for item in result}) != len(result):
        _fail("E_DUPLICATE_REF", field, "duplicate ResourceRef")
    return result


def _same_ref(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return ref_key(left) == ref_key(right)


@dataclass(frozen=True)
class IntegrationTarget:
    integration_id: str
    repository_id: str
    request_id: str
    base_ref: Mapping[str, Any]
    candidate_ref: Mapping[str, Any]
    contract_refs: tuple[Mapping[str, Any], ...]
    config_ref: Optional[Mapping[str, Any]]
    plugin_lock_ref: Optional[Mapping[str, Any]]
    rule_refs: tuple[Mapping[str, Any], ...]
    check_catalog_ref: Mapping[str, Any]
    checker_refs: tuple[Mapping[str, Any], ...]
    environment_refs: tuple[Mapping[str, Any], ...]
    adapter_refs: tuple[Mapping[str, Any], ...]
    dependency_refs: tuple[Mapping[str, Any], ...]
    source_binding_versions: Mapping[str, str]
    scope_ref: Mapping[str, Any]

    def __post_init__(self) -> None:
        _id(self.integration_id, "/integration_id")
        _id(self.repository_id, "/repository_id")
        _id(self.request_id, "/request_id")
        for name, value in (("base_ref", self.base_ref), ("candidate_ref", self.candidate_ref),
                            ("check_catalog_ref", self.check_catalog_ref), ("scope_ref", self.scope_ref)):
            resource_ref(dict(value), "/" + name)
        if self.config_ref is not None:
            resource_ref(dict(self.config_ref), "/config_ref")
        if self.plugin_lock_ref is not None:
            resource_ref(dict(self.plugin_lock_ref), "/plugin_lock_ref")
        for name, values in (("contract_refs", self.contract_refs), ("rule_refs", self.rule_refs),
                             ("checker_refs", self.checker_refs), ("environment_refs", self.environment_refs),
                             ("adapter_refs", self.adapter_refs), ("dependency_refs", self.dependency_refs)):
            if type(values) is not tuple:
                _fail("E_REFS", "/" + name, "tuple required")
            _refs(list(values), "/" + name, required=name in {"contract_refs", "environment_refs"})
        if type(self.source_binding_versions) is not dict or not self.source_binding_versions:
            _fail("E_BINDINGS", "/source_binding_versions", "non-empty binding map required")
        for key, value in self.source_binding_versions.items():
            _id(key, "/source_binding_versions/key")
            _text(value, "/source_binding_versions/" + key, max_len=256)
        if _same_ref(self.base_ref, self.candidate_ref):
            _fail("E_TARGET_IDENTITY", "/candidate_ref", "base and candidate must differ")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "IntegrationTarget":
        if type(value) is not dict:
            _fail("E_SCHEMA_TYPE", "/", "IntegrationTarget object required")
        required = {"record_type", "schema_version", "integration_id", "repository_id", "request_id",
                    "base_ref", "candidate_ref", "contract_refs", "config_ref", "plugin_lock_ref",
                    "rule_refs", "check_catalog_ref", "checker_refs", "environment_refs", "adapter_refs",
                    "dependency_refs", "source_binding_versions", "scope_ref"}
        if set(value) != required or value["record_type"] != "integration-target" or value["schema_version"] != 1:
            _fail("E_SCHEMA_FIELDS", "/", "closed integration-target record required")
        return cls(value["integration_id"], value["repository_id"], value["request_id"],
                   resource_ref(value["base_ref"], "/base_ref"), resource_ref(value["candidate_ref"], "/candidate_ref"),
                   _refs(value["contract_refs"], "/contract_refs", required=True),
                   None if value["config_ref"] is None else resource_ref(value["config_ref"], "/config_ref"),
                   None if value["plugin_lock_ref"] is None else resource_ref(value["plugin_lock_ref"], "/plugin_lock_ref"),
                   _refs(value["rule_refs"], "/rule_refs"), resource_ref(value["check_catalog_ref"], "/check_catalog_ref"),
                   _refs(value["checker_refs"], "/checker_refs"), _refs(value["environment_refs"], "/environment_refs", required=True),
                   _refs(value["adapter_refs"], "/adapter_refs"), _refs(value["dependency_refs"], "/dependency_refs"),
                   value["source_binding_versions"], resource_ref(value["scope_ref"], "/scope_ref"))

    def to_mapping(self) -> dict[str, Any]:
        return {"record_type": "integration-target", "schema_version": 1,
                "integration_id": self.integration_id, "repository_id": self.repository_id,
                "request_id": self.request_id, "base_ref": dict(self.base_ref),
                "candidate_ref": dict(self.candidate_ref), "contract_refs": [dict(x) for x in self.contract_refs],
                "config_ref": None if self.config_ref is None else dict(self.config_ref),
                "plugin_lock_ref": None if self.plugin_lock_ref is None else dict(self.plugin_lock_ref),
                "rule_refs": [dict(x) for x in self.rule_refs], "check_catalog_ref": dict(self.check_catalog_ref),
                "checker_refs": [dict(x) for x in self.checker_refs], "environment_refs": [dict(x) for x in self.environment_refs],
                "adapter_refs": [dict(x) for x in self.adapter_refs], "dependency_refs": [dict(x) for x in self.dependency_refs],
                "source_binding_versions": dict(self.source_binding_versions), "scope_ref": dict(self.scope_ref)}

    @property
    def target_sha256(self) -> str:
        return digest(self.to_mapping())


def ensure_same_candidate(targets: Sequence[IntegrationTarget | Mapping[str, Any]]) -> str:
    if type(targets) not in (list, tuple) or not targets:
        _fail("E_TARGETS", "/targets", "at least one target required")
    normalized = [item if isinstance(item, IntegrationTarget) else IntegrationTarget.from_mapping(item) for item in targets]
    key = ref_key(normalized[0].candidate_ref)
    if any(ref_key(item.candidate_ref) != key for item in normalized[1:]):
        _fail("E_TARGET_CANDIDATE_MISMATCH", "/targets", "targets do not share one fixed candidate")
    return normalized[0].candidate_ref["sha256"]


@dataclass(frozen=True)
class CheckCatalog:
    catalog_id: str
    target_ref: Mapping[str, Any]
    cases: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        _id(self.catalog_id, "/catalog_id")
        target = resource_ref(dict(self.target_ref), "/target_ref")
        if type(self.cases) is not tuple or not self.cases or len(self.cases) > MAX_ENTRIES:
            _fail("E_CASES", "/cases", "non-empty cases tuple required")
        seen: set[str] = set()
        for index, case in enumerate(self.cases):
            if type(case) is not dict:
                _fail("E_CASE", f"/cases/{index}", "case object required")
            required = {"case_id", "requirement_id", "description", "target_ref", "environment_refs",
                        "command_ref", "expected", "evidence_kinds", "required", "reviewers", "timeout_seconds"}
            if set(case) != required:
                _fail("E_CASE_FIELDS", f"/cases/{index}", "closed check case required")
            case_id = _id(case["case_id"], f"/cases/{index}/case_id")
            if case_id in seen:
                _fail("E_CASE_DUPLICATE", f"/cases/{index}/case_id", "duplicate case_id")
            seen.add(case_id)
            _text(case["requirement_id"], f"/cases/{index}/requirement_id", max_len=64)
            _text(case["description"], f"/cases/{index}/description")
            case_target = resource_ref(case["target_ref"], f"/cases/{index}/target_ref")
            if not _same_ref(case_target, target):
                _fail("E_CASE_TARGET", f"/cases/{index}/target_ref", "case target differs from catalog target")
            _refs(case["environment_refs"], f"/cases/{index}/environment_refs", required=True)
            resource_ref(case["command_ref"], f"/cases/{index}/command_ref")
            _strict(case["expected"], f"/cases/{index}/expected")
            if type(case["evidence_kinds"]) is not list or not case["evidence_kinds"] or any(
                    kind not in SOURCE_KINDS for kind in case["evidence_kinds"]):
                _fail("E_CASE_EVIDENCE", f"/cases/{index}/evidence_kinds", "known evidence kinds required")
            if type(case["required"]) is not bool:
                _fail("E_BOOLEAN", f"/cases/{index}/required", "boolean required")
            if type(case["reviewers"]) is not list or any(type(x) is not str or not x for x in case["reviewers"]):
                _fail("E_CASE_REVIEWERS", f"/cases/{index}/reviewers", "reviewer list required")
            if type(case["timeout_seconds"]) is not int or not 1 <= case["timeout_seconds"] <= MAX_SAFE_INTEGER:
                _fail("E_TIMEOUT", f"/cases/{index}/timeout_seconds", "positive timeout required")
        if not any(case["required"] for case in self.cases):
            _fail("E_REQUIRED_CASE", "/cases", "at least one required case")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CheckCatalog":
        if type(value) is not dict or set(value) != {"record_type", "schema_version", "catalog_id", "target_ref", "cases"}:
            _fail("E_SCHEMA_FIELDS", "/", "closed check-catalog record required")
        if value["record_type"] != "check-catalog" or value["schema_version"] != 1:
            _fail("E_SCHEMA_VERSION", "/", "unsupported check-catalog version")
        if type(value["cases"]) is not list:
            _fail("E_CASES", "/cases", "JSON array required")
        return cls(value["catalog_id"], resource_ref(value["target_ref"], "/target_ref"),
                   tuple(dict(case) for case in value["cases"]))

    def to_mapping(self) -> dict[str, Any]:
        return {"record_type": "check-catalog", "schema_version": 1, "catalog_id": self.catalog_id,
                "target_ref": dict(self.target_ref), "cases": [dict(case) for case in self.cases]}


@dataclass(frozen=True)
class CheckObservation:
    observation_id: str
    attempt_id: str
    case_id: str
    target_ref: Mapping[str, Any]
    command_ref: Mapping[str, Any]
    exit_code: Optional[int]
    signal: Optional[str]
    stdout_ref: Optional[Mapping[str, Any]]
    stderr_ref: Optional[Mapping[str, Any]]
    protocol_status: str
    collection_status: str
    environment_ref: Mapping[str, Any]
    expected_match: Optional[bool]
    outcome: str
    failure_reason: Optional[str]
    source_kind: str
    evidence_refs: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        _id(self.observation_id, "/observation_id")
        _id(self.attempt_id, "/attempt_id")
        _id(self.case_id, "/case_id")
        resource_ref(dict(self.target_ref), "/target_ref")
        resource_ref(dict(self.command_ref), "/command_ref")
        if self.exit_code is not None and (type(self.exit_code) is not int or abs(self.exit_code) > MAX_SAFE_INTEGER):
            _fail("E_EXIT_CODE", "/exit_code", "integer or null required")
        if self.signal is not None:
            _text(self.signal, "/signal", max_len=128)
        for field, value in (("stdout_ref", self.stdout_ref), ("stderr_ref", self.stderr_ref)):
            if value is not None:
                resource_ref(dict(value), "/" + field)
        if self.protocol_status not in {"VALID", "INVALID", "UNKNOWN"}:
            _fail("E_PROTOCOL", "/protocol_status", "VALID, INVALID or UNKNOWN required")
        if self.collection_status not in {"COMPLETE", "PARTIAL", "MISSING", "CONFLICT"}:
            _fail("E_COLLECTION", "/collection_status", "invalid collection status")
        resource_ref(dict(self.environment_ref), "/environment_ref")
        if self.expected_match is not None and type(self.expected_match) is not bool:
            _fail("E_EXPECTED", "/expected_match", "boolean or null required")
        if self.outcome not in OUTCOMES:
            _fail("E_OUTCOME", "/outcome", "invalid observation outcome")
        if self.failure_reason is not None:
            _text(self.failure_reason, "/failure_reason", max_len=2048)
        if self.source_kind not in SOURCE_KINDS:
            _fail("E_SOURCE_KIND", "/source_kind", "unknown source kind")
        _refs(list(self.evidence_refs), "/evidence_refs")
        if self.outcome == "PASS" and (self.collection_status != "COMPLETE" or self.protocol_status != "VALID" or self.expected_match is not True):
            _fail("E_OBSERVATION_PASS", "/outcome", "PASS requires complete valid matching observation")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CheckObservation":
        required = {"record_type", "schema_version", "observation_id", "attempt_id", "case_id", "target_ref", "command_ref",
                    "exit_code", "signal", "stdout_ref", "stderr_ref", "protocol_status", "collection_status", "environment_ref",
                    "expected_match", "outcome", "failure_reason", "source_kind", "evidence_refs"}
        if type(value) is not dict or set(value) != required or value["record_type"] != "check-observation" or value["schema_version"] != 1:
            _fail("E_SCHEMA_FIELDS", "/", "closed check-observation record required")
        if type(value["evidence_refs"]) is not list:
            _fail("E_REFS", "/evidence_refs", "JSON array required")
        return cls(value["observation_id"], value["attempt_id"], value["case_id"], resource_ref(value["target_ref"], "/target_ref"),
                   resource_ref(value["command_ref"], "/command_ref"), value["exit_code"], value["signal"],
                   None if value["stdout_ref"] is None else resource_ref(value["stdout_ref"], "/stdout_ref"),
                   None if value["stderr_ref"] is None else resource_ref(value["stderr_ref"], "/stderr_ref"), value["protocol_status"],
                   value["collection_status"], resource_ref(value["environment_ref"], "/environment_ref"), value["expected_match"],
                   value["outcome"], value["failure_reason"], value["source_kind"],
                   _refs(value["evidence_refs"], "/evidence_refs"))

    def to_mapping(self) -> dict[str, Any]:
        return {"record_type": "check-observation", "schema_version": 1, "observation_id": self.observation_id,
                "attempt_id": self.attempt_id, "case_id": self.case_id, "target_ref": dict(self.target_ref),
                "command_ref": dict(self.command_ref), "exit_code": self.exit_code, "signal": self.signal,
                "stdout_ref": None if self.stdout_ref is None else dict(self.stdout_ref),
                "stderr_ref": None if self.stderr_ref is None else dict(self.stderr_ref),
                "protocol_status": self.protocol_status, "collection_status": self.collection_status,
                "environment_ref": dict(self.environment_ref), "expected_match": self.expected_match,
                "outcome": self.outcome, "failure_reason": self.failure_reason, "source_kind": self.source_kind,
                "evidence_refs": [dict(ref) for ref in self.evidence_refs]}


@dataclass(frozen=True)
class EvidenceManifest:
    manifest_id: str
    target_ref: Mapping[str, Any]
    entries: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        _id(self.manifest_id, "/manifest_id")
        resource_ref(dict(self.target_ref), "/target_ref")
        if type(self.entries) is not tuple or not self.entries or len(self.entries) > MAX_ENTRIES:
            _fail("E_ENTRIES", "/entries", "non-empty entry tuple required")
        seen: set[str] = set()
        folded: set[str] = set()
        ordered_paths: list[str] = []
        total = 0
        for index, entry in enumerate(self.entries):
            if type(entry) is not dict or set(entry) != {"ref", "size", "purpose", "case_ids"}:
                _fail("E_ENTRY_FIELDS", f"/entries/{index}", "closed manifest entry required")
            ref = resource_ref(entry["ref"], f"/entries/{index}/ref")
            path = ref["path"]
            folded_path = unicodedata.normalize("NFC", path).casefold()
            if path in seen or folded_path in folded:
                _fail("E_PATH_COLLISION", f"/entries/{index}/ref/path", "duplicate or folded path")
            seen.add(path); folded.add(folded_path)
            ordered_paths.append(path)
            if type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_BYTES:
                _fail("E_SIZE", f"/entries/{index}/size", "bounded non-negative size required")
            _text(entry["purpose"], f"/entries/{index}/purpose", max_len=256)
            if type(entry["case_ids"]) is not list or any(type(case_id) is not str or not case_id for case_id in entry["case_ids"]):
                _fail("E_CASE_IDS", f"/entries/{index}/case_ids", "case id list required")
            total += entry["size"]
            if total > MAX_TOTAL_BYTES:
                _fail("E_BUNDLE_SIZE", "/entries", "manifest exceeds total size")
        if ordered_paths != sorted(ordered_paths):
            _fail("E_ORDER", "/entries", "entries must be path sorted")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvidenceManifest":
        required = {"record_type", "schema_version", "manifest_id", "target_ref", "entries"}
        if type(value) is not dict or set(value) != required or value["record_type"] != "evidence-manifest" or value["schema_version"] != 1:
            _fail("E_SCHEMA_FIELDS", "/", "closed evidence-manifest record required")
        if type(value["entries"]) is not list:
            _fail("E_ENTRIES", "/entries", "JSON array required")
        return cls(value["manifest_id"], resource_ref(value["target_ref"], "/target_ref"), tuple(dict(item) for item in value["entries"]))

    def to_mapping(self) -> dict[str, Any]:
        return {"record_type": "evidence-manifest", "schema_version": 1, "manifest_id": self.manifest_id,
                "target_ref": dict(self.target_ref), "entries": [dict(item) for item in self.entries]}


def verify_manifest(manifest: EvidenceManifest | Mapping[str, Any], expected_refs: Sequence[Mapping[str, Any]],
                    blobs: Mapping[str, bytes]) -> frozenset[tuple[str, str, str, str]]:
    """Verify exact expected membership and immutable bytes for a manifest."""
    value = manifest if isinstance(manifest, EvidenceManifest) else EvidenceManifest.from_mapping(manifest)
    if type(expected_refs) not in (list, tuple) or not expected_refs:
        _fail("E_EXPECTED", "/expected_refs", "non-empty expected refs required")
    expected = {ref_key(ref) for ref in expected_refs}
    if len(expected) != len(expected_refs):
        _fail("E_EXPECTED_DUPLICATE", "/expected_refs", "duplicate expected ref")
    actual: set[tuple[str, str, str, str]] = set()
    if type(blobs) is not dict:
        _fail("E_BLOBS", "/blobs", "path-to-bytes mapping required")
    for index, entry in enumerate(value.entries):
        ref = resource_ref(entry["ref"], f"/entries/{index}/ref")
        key = ref_key(ref)
        if key not in expected:
            _fail("E_MEMBERSHIP", f"/entries/{index}", "unexpected manifest member")
        raw = blobs.get(ref["path"])
        if type(raw) is not bytes:
            _fail("E_RESOURCE_MISSING", f"/entries/{index}", "manifest member bytes missing")
        if len(raw) != entry["size"] or digest_bytes(raw) != ref["sha256"]:
            _fail("E_RESOURCE_HASH", f"/entries/{index}", "manifest member hash mismatch")
        actual.add(key)
    if actual != expected or set(blobs) != {resource_ref(ref)["path"] for ref in expected_refs}:
        _fail("E_MEMBERSHIP", "/entries", "manifest membership differs from trusted expected set")
    return frozenset(actual)


@dataclass(frozen=True)
class CoverageReport:
    target_ref: Mapping[str, Any]
    catalog_ref: Mapping[str, Any]
    required_total: int
    required_passed: int
    evidence_readiness: str
    cases: tuple[Mapping[str, Any], ...]
    unresolved_failures: tuple[str, ...]
    limitations: tuple[str, ...]
    exceptions: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        resource_ref(dict(self.target_ref), "/target_ref")
        resource_ref(dict(self.catalog_ref), "/catalog_ref")
        if type(self.required_total) is not int or type(self.required_passed) is not int or not 0 <= self.required_passed <= self.required_total:
            _fail("E_COVERAGE_COUNT", "/", "invalid coverage counts")
        if self.evidence_readiness not in EVIDENCE_STATUSES:
            _fail("E_COVERAGE_STATUS", "/evidence_readiness", "invalid readiness")
        if type(self.cases) is not tuple:
            _fail("E_COVERAGE_CASES", "/cases", "case result tuple required")
        for index, item in enumerate(self.cases):
            if type(item) is not dict or set(item) != {"case_id", "required", "status", "reason"}:
                _fail("E_COVERAGE_CASE", f"/cases/{index}", "closed case result required")
            _id(item["case_id"], f"/cases/{index}/case_id")
            if type(item["required"]) is not bool or type(item["status"]) is not str or not item["status"]:
                _fail("E_COVERAGE_CASE", f"/cases/{index}", "required/status fields invalid")
            if item["reason"] is not None:
                _text(item["reason"], f"/cases/{index}/reason", max_len=256)
        for name, values in (("unresolved_failures", self.unresolved_failures), ("limitations", self.limitations)):
            if type(values) is not tuple or any(type(value) is not str or not value for value in values):
                _fail("E_COVERAGE_LIST", "/" + name, "string tuple required")
        if type(self.exceptions) is not tuple:
            _fail("E_COVERAGE_EXCEPTIONS", "/exceptions", "exception tuple required")
        for index, item in enumerate(self.exceptions):
            if type(item) is not dict or set(item) != {"case_id", "reason", "scope"}:
                _fail("E_COVERAGE_EXCEPTION", f"/exceptions/{index}", "closed exception required")
            _id(item["case_id"], f"/exceptions/{index}/case_id")
            _text(item["reason"], f"/exceptions/{index}/reason", max_len=256)
            _strict(item["scope"], f"/exceptions/{index}/scope")

    def to_mapping(self) -> dict[str, Any]:
        return {"record_type": "coverage-report", "schema_version": 1, "target_ref": dict(self.target_ref),
                "catalog_ref": dict(self.catalog_ref), "required_total": self.required_total,
                "required_passed": self.required_passed, "evidence_readiness": self.evidence_readiness,
                "cases": [dict(item) for item in self.cases], "unresolved_failures": list(self.unresolved_failures),
                "limitations": list(self.limitations), "exceptions": [dict(item) for item in self.exceptions]}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CoverageReport":
        required = {"record_type", "schema_version", "target_ref", "catalog_ref", "required_total", "required_passed",
                    "evidence_readiness", "cases", "unresolved_failures", "limitations"}
        if type(value) is not dict or not required <= set(value) or set(value) - (required | {"exceptions"}) \
                or value["record_type"] != "coverage-report" or value["schema_version"] != 1:
            _fail("E_SCHEMA_FIELDS", "/", "closed coverage-report record required")
        return cls(resource_ref(value["target_ref"], "/target_ref"), resource_ref(value["catalog_ref"], "/catalog_ref"),
                   value["required_total"], value["required_passed"], value["evidence_readiness"],
                   tuple(dict(item) for item in value["cases"]), tuple(value["unresolved_failures"]), tuple(value["limitations"]),
                   tuple(dict(item) for item in value.get("exceptions", [])))


def evaluate_coverage(target: IntegrationTarget | Mapping[str, Any], catalog: CheckCatalog | Mapping[str, Any],
                      observations: Sequence[CheckObservation | Mapping[str, Any]], *,
                      selected_attempts: Optional[Mapping[str, str]] = None,
                      manifest: Optional[EvidenceManifest | Mapping[str, Any]] = None,
                      resolved_failures: Iterable[str] = ()) -> CoverageReport:
    """Reduce trusted catalog and observations without issuing dispatch permission."""
    target_obj = target if isinstance(target, IntegrationTarget) else IntegrationTarget.from_mapping(target)
    catalog_obj = catalog if isinstance(catalog, CheckCatalog) else CheckCatalog.from_mapping(catalog)
    if not _same_ref(catalog_obj.target_ref, target_obj.candidate_ref):
        _fail("E_TARGET_MISMATCH", "/catalog/target_ref", "catalog target differs from integration candidate")
    selected = dict(selected_attempts or {})
    by_id: dict[str, CheckObservation] = {}
    for raw in observations:
        obs = raw if isinstance(raw, CheckObservation) else CheckObservation.from_mapping(raw)
        if obs.observation_id in by_id:
            if by_id[obs.observation_id].to_mapping() != obs.to_mapping():
                _fail("E_OBSERVATION_CONFLICT", "/observations", "same observation id has different bytes")
        else:
            by_id[obs.observation_id] = obs
    if selected and set(selected) - {case["case_id"] for case in catalog_obj.cases}:
        _fail("E_SELECTION", "/selected_attempts", "unknown case selected")
    expected_keys: Optional[frozenset[tuple[str, str, str, str]]] = None
    if manifest is not None:
        expected_keys = frozenset(ref_key(entry["ref"]) for entry in (manifest.entries if isinstance(manifest, EvidenceManifest) else EvidenceManifest.from_mapping(manifest).entries))
    resolved = set(resolved_failures)
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for case in catalog_obj.cases:
        case_id = case["case_id"]
        attempt_id = selected.get(case_id)
        if attempt_id is None:
            candidates = [obs for obs in by_id.values() if obs.case_id == case_id]
            if len(candidates) == 1:
                obs = candidates[0]
            else:
                obs = None
        else:
            obs = by_id.get(attempt_id)
        status = "NOT_RUN"
        reason: Optional[str] = None
        if obs is not None:
            if obs.case_id != case_id:
                status, reason = "INCOMPLETE", "SELECTION_CASE_MISMATCH"
            elif not _same_ref(obs.target_ref, target_obj.candidate_ref):
                status, reason = "STALE", "TARGET_MISMATCH"
            elif not any(_same_ref(obs.environment_ref, environment_ref)
                         for environment_ref in case["environment_refs"]):
                status, reason = "STALE_ENVIRONMENT", "ENVIRONMENT_MISMATCH"
            elif obs.outcome in {"FAIL", "ERROR"}:
                status, reason = obs.outcome, obs.failure_reason or obs.outcome
                failures.append(obs.observation_id)
            elif obs.outcome in {"SKIPPED", "CANCELLED", "WAIVED", "NOT_RUN"}:
                status, reason = "NOT_RUN", obs.outcome
            elif obs.collection_status != "COMPLETE" or obs.protocol_status != "VALID" or obs.expected_match is not True:
                status, reason = "INCOMPLETE", "OBSERVATION_NOT_COMPLETE"
            elif expected_keys is not None and any(ref_key(ref) not in expected_keys for ref in obs.evidence_refs):
                status, reason = "INCOMPLETE", "EVIDENCE_NOT_IN_MANIFEST"
            elif obs.source_kind not in case["evidence_kinds"]:
                status, reason = "INSUFFICIENT_EVIDENCE", "SOURCE_KIND_NOT_ACCEPTED"
            else:
                status = "PASS" if obs.outcome == "PASS" else obs.outcome
            if status == "PASS":
                prior = [old.observation_id for old in by_id.values() if old.case_id == case_id and old.outcome in {"FAIL", "ERROR"} and old.observation_id != obs.observation_id]
                unresolved = [old_id for old_id in prior if old_id not in resolved]
                if unresolved:
                    status, reason = "UNRESOLVED_FAILURE", "UNRESOLVED_FAILURE"
                    failures.extend(unresolved)
        results.append({"case_id": case_id, "required": case["required"], "status": status, "reason": reason})
    required_results = [item for item in results if item["required"]]
    passed = sum(item["status"] == "PASS" for item in required_results)
    readiness = "COMPLETE" if passed == len(required_results) else ("PARTIAL" if passed else "NONE")
    limitations: list[str] = []
    if any(item["status"] in {"NOT_RUN", "INCOMPLETE", "STALE", "INSUFFICIENT_EVIDENCE"} for item in required_results):
        limitations.append("REQUIRED_CASE_GAP")
    if failures:
        limitations.append("UNRESOLVED_FAILURE")
    exceptions = tuple({"case_id": item["case_id"], "reason": item["reason"],
                        "scope": {"case_id": item["case_id"]}}
                       for item in results if item["status"] == "NOT_RUN" and item["reason"] == "WAIVED")
    return CoverageReport(target_obj.candidate_ref, catalog_obj.target_ref, len(required_results), passed, readiness,
                          tuple(results), tuple(dict.fromkeys(failures)), tuple(dict.fromkeys(limitations)), exceptions)


@dataclass(frozen=True)
class DeliveryDecision:
    decision: str
    target_ref: Mapping[str, Any]
    coverage_ref: Mapping[str, Any]
    scope: Mapping[str, Any]
    reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    accepted: bool
    dispatch_authorized: bool = False
    integration_authorized: bool = False

    def __post_init__(self) -> None:
        if self.decision not in DECISIONS:
            _fail("E_DECISION", "/decision", "invalid delivery decision")
        resource_ref(dict(self.target_ref), "/target_ref")
        resource_ref(dict(self.coverage_ref), "/coverage_ref")
        _strict(dict(self.scope), "/scope")
        if type(self.accepted) is not bool or self.dispatch_authorized is not False or self.integration_authorized is not False:
            _fail("E_AUTHORITY", "/", "delivery reports cannot grant execution or integration authority")
        for name, values in (("reasons", self.reasons), ("limitations", self.limitations)):
            if type(values) is not tuple or any(type(value) is not str or not value for value in values):
                _fail("E_DECISION_LIST", "/" + name, "string tuple required")

    def to_mapping(self) -> dict[str, Any]:
        return {"record_type": "delivery-decision", "schema_version": 1, "decision": self.decision,
                "target_ref": dict(self.target_ref), "coverage_ref": dict(self.coverage_ref), "scope": dict(self.scope),
                "reasons": list(self.reasons), "limitations": list(self.limitations), "accepted": self.accepted,
                "dispatch_authorized": False, "integration_authorized": False}


def make_delivery_decision(coverage: CoverageReport | Mapping[str, Any], *, coverage_ref: Mapping[str, Any],
                           scope: Mapping[str, Any], accepted: bool, withdrawn: bool = False,
                           reasons: Iterable[str] = ()) -> DeliveryDecision:
    report = coverage if isinstance(coverage, CoverageReport) else CoverageReport.from_mapping(coverage)
    resource_ref(dict(coverage_ref), "/coverage_ref")
    if type(accepted) is not bool or type(withdrawn) is not bool:
        _fail("E_AUTHORITY", "/accepted", "boolean required")
    base_reasons = list(reasons)
    if withdrawn:
        decision = "WITHDRAWN"
        base_reasons.append("WITHDRAWN_BY_AUTHORITY")
    elif report.evidence_readiness == "COMPLETE" and accepted and not report.unresolved_failures:
        decision = "FULL"
    elif report.evidence_readiness in {"PARTIAL", "COMPLETE"} and accepted:
        decision = "PARTIAL"
        base_reasons.append("REQUIRED_GAP_OR_FAILURE")
    else:
        decision = "BLOCKED"
        base_reasons.append("ACCEPTANCE_OR_EVIDENCE_MISSING")
    return DeliveryDecision(decision, report.target_ref, dict(coverage_ref), dict(scope), tuple(dict.fromkeys(base_reasons)),
                            tuple(report.limitations), accepted)


# Functional aliases keep the validation seam convenient for callers that use
# the repository's existing ``validate_*`` naming convention.
def validate_integration_target(value: Mapping[str, Any]) -> IntegrationTarget:
    return IntegrationTarget.from_mapping(value)


def validate_check_catalog(value: Mapping[str, Any]) -> CheckCatalog:
    return CheckCatalog.from_mapping(value)


def validate_check_observation(value: Mapping[str, Any]) -> CheckObservation:
    return CheckObservation.from_mapping(value)


def validate_evidence_manifest(value: Mapping[str, Any]) -> EvidenceManifest:
    return EvidenceManifest.from_mapping(value)


def validate_coverage_report(value: Mapping[str, Any]) -> CoverageReport:
    return CoverageReport.from_mapping(value)


def validate_delivery_decision(value: Mapping[str, Any]) -> DeliveryDecision:
    if type(value) is not dict:
        _fail("E_SCHEMA_TYPE", "/", "delivery decision object required")
    required = {"record_type", "schema_version", "decision", "target_ref", "coverage_ref", "scope", "reasons",
                "limitations", "accepted", "dispatch_authorized", "integration_authorized"}
    if set(value) != required or value["record_type"] != "delivery-decision" or value["schema_version"] != 1:
        _fail("E_SCHEMA_FIELDS", "/", "closed delivery-decision record required")
    return DeliveryDecision(value["decision"], resource_ref(value["target_ref"], "/target_ref"),
                            resource_ref(value["coverage_ref"], "/coverage_ref"), value["scope"],
                            tuple(value["reasons"]), tuple(value["limitations"]), value["accepted"],
                            value["dispatch_authorized"], value["integration_authorized"])


__all__ = [
    "DECISIONS", "EVIDENCE_STATUSES", "IntegrationRecordError", "IntegrationTarget", "CheckCatalog",
    "CheckObservation", "EvidenceManifest", "CoverageReport", "DeliveryDecision", "canonical_bytes", "canonical_json",
    "digest", "digest_bytes", "sha256_hex", "resource_ref", "ref_key", "ensure_same_candidate", "verify_manifest",
    "evaluate_coverage", "make_delivery_decision", "MAX_BYTES", "MAX_ENTRIES", "SOURCE_KINDS",
    "validate_integration_target", "validate_check_catalog", "validate_check_observation",
    "validate_evidence_manifest", "validate_coverage_report", "validate_delivery_decision",
]
