"""Closed, provenance-bound records shared by the built-in task plugins."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import ntpath
from typing import Any, Dict, Iterable, Mapping, Tuple


FAMILIES = {"analysis-report", "change-report", "docs-change", "docs-check", "regression-plan", "mutation-review", "check-plan", "review-bundle"}
STATUSES = {"DRAFT_VALIDATED", "READY", "PASS", "FAIL", "UNKNOWN", "BLOCKED", "NOT_RUN", "REFERENCE_ONLY"}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
MUTABLE_PLUGIN_ALIASES = frozenset({"latest", "head", "current", "stable", "default", "main", "master", "dev", "tip"})
MAX_BYTES = 1_048_576
MAX_DEPTH = 32
MAX_SAFE_INTEGER = 2**53 - 1
SourceRefKey = Tuple[str, str, str, str]


class ArtifactError(ValueError):
    def __init__(self, code: str, path: str, message: str):
        self.code, self.path = code, path
        super().__init__(f"{code} at {path}: {message}")


def _fail(code: str, path: str, message: str) -> None:
    raise ArtifactError(code, path, message)


def _strict_json_value(value: Any, *, path: str = "/", level: int = 0,
                       active: set[int] | None = None) -> None:
    """Reject Python values that JSON encoders silently coerce.

    ``json.dumps`` accepts tuples and non-string mapping keys by converting
    them on the way out.  That is unsafe for artifact identities: the value
    that was validated must be the value whose bytes are hashed.  A recursive
    cycle is rejected before encoding as well, so malformed input cannot
    escape as ``RecursionError``.
    """
    if level > MAX_DEPTH:
        _fail("E_ARTIFACT_DEPTH", path, "artifact nesting exceeds limit")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > MAX_SAFE_INTEGER:
            _fail("E_ARTIFACT_JSON", path, "integer exceeds exact JSON range")
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail("E_ARTIFACT_JSON", path, "non-finite number is not JSON")
        return
    if type(value) is str:
        # ``json.dumps`` will escape a few control characters, but accepting
        # them here still lets an artifact carry ambiguous line/protocol data.
        # Lone UTF-16 surrogates are even more dangerous: the encoder can
        # produce a Python string that then fails only while UTF-8 encoding.
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F or 0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            _fail("E_ARTIFACT_JSON", path, "safe Unicode text required")
        return
    if type(value) not in (dict, list):
        _fail("E_ARTIFACT_JSON", path, "strict JSON value required")
    if active is None:
        active = set()
    marker = id(value)
    if marker in active:
        _fail("E_ARTIFACT_JSON", path, "cyclic value is not JSON")
    active.add(marker)
    try:
        if type(value) is dict:
            for key, child in value.items():
                if type(key) is not str:
                    _fail("E_ARTIFACT_JSON", path, "object keys must be strings")
                if any(ord(ch) < 0x20 or ord(ch) == 0x7F or 0xD800 <= ord(ch) <= 0xDFFF for ch in key):
                    _fail("E_ARTIFACT_JSON", path, "safe Unicode object keys required")
                _strict_json_value(child, path=f"{path}{key}/", level=level + 1, active=active)
        else:
            for index, child in enumerate(value):
                _strict_json_value(child, path=f"{path}{index}/", level=level + 1, active=active)
    finally:
        active.remove(marker)


def canonical_bytes(value: Any) -> bytes:
    _strict_json_value(value)
    try:
        raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ArtifactError("E_ARTIFACT_JSON", "/", str(exc)) from exc
    if len(raw) > MAX_BYTES:
        _fail("E_ARTIFACT_SIZE", "/", "artifact exceeds byte limit")
    return raw


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical_bytes(value)).hexdigest()


def resource_ref(value: Any, path: str = "/resource_ref") -> Dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"kind", "id", "path", "sha256"}:
        _fail("E_ARTIFACT_REF", path, "closed ResourceRef required")
    if any(not isinstance(value[x], str) or not value[x] or any(
            ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF
            for ch in value[x])
           for x in ("kind", "id", "path")):
        _fail("E_ARTIFACT_REF", path, "kind/id/path must be non-empty strings")
    if not isinstance(value["sha256"], str) or not HEX64.fullmatch(value["sha256"]):
        _fail("E_ARTIFACT_REF", f"{path}/sha256", "lowercase SHA-256 required")
    normalized = value["path"].replace("\\", "/")
    if normalized.startswith("/") or ntpath.splitdrive(normalized)[0] or any(part in {"", ".", ".."} for part in normalized.split("/")) or any(
            ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in normalized):
        _fail("E_ARTIFACT_PATH", f"{path}/path", "path must stay within the candidate")
    return {"kind": value["kind"], "id": value["id"], "path": normalized, "sha256": value["sha256"]}


def resource_ref_key(value: Any, path: str = "/resource_ref") -> SourceRefKey:
    """Return the canonical, hash-bearing key used by frozen source bytes.

    A path or logical id alone is not sufficient to identify a resource: two
    revisions may legitimately use the same path.  The key therefore includes
    every ResourceRef field in its canonical order.  Callers can use the
    resulting tuple as a dictionary key without ever embedding a mutable dict
    in the API.
    """
    normalized = resource_ref(value, path)
    return tuple(normalized[field] for field in ("kind", "id", "path", "sha256"))


def source_bytes_by_ref(value: Any, *, declared: Iterable[SourceRefKey] | None = None,
                        path: str = "/source_bytes_by_ref") -> Dict[SourceRefKey, bytes]:
    """Validate and normalize a frozen source-byte mapping.

    The public mapping is keyed by ``resource_ref_key(ref)`` tuples and holds
    immutable ``bytes`` values.  ``declared`` optionally limits keys to the
    caller's declared ``source_refs``; this prevents an unbound byte snapshot
    from being silently accepted.  The mapping is an optional builder input,
    so omitting it preserves the historical envelope-only validation path.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        _fail("E_ARTIFACT_SOURCE", path, "mapping of ResourceRef keys to bytes required")
    allowed = None if declared is None else set(declared)
    result: Dict[SourceRefKey, bytes] = {}
    for index, (key, raw) in enumerate(value.items()):
        item_path = f"{path}/{index}"
        if type(key) is not tuple or len(key) != 4 or any(type(part) is not str for part in key):
            _fail("E_ARTIFACT_SOURCE", f"{item_path}/ref", "canonical ResourceRef tuple key required")
        ref_value = {"kind": key[0], "id": key[1], "path": key[2], "sha256": key[3]}
        canonical = resource_ref_key(ref_value, f"{item_path}/ref")
        if key != canonical:
            _fail("E_ARTIFACT_SOURCE", f"{item_path}/ref", "ResourceRef tuple key must be canonical")
        if allowed is not None and canonical not in allowed:
            _fail("E_ARTIFACT_SOURCE", f"{item_path}/ref", "source bytes reference is not declared")
        if type(raw) is not bytes:
            _fail("E_ARTIFACT_SOURCE", f"{item_path}/bytes", "frozen source bytes required")
        if len(raw) > MAX_BYTES:
            _fail("E_ARTIFACT_SIZE", f"{item_path}/bytes", "source bytes exceed byte limit")
        if hashlib.sha256(raw).hexdigest() != canonical[3]:
            _fail("E_ARTIFACT_SOURCE", f"{item_path}/bytes", "frozen source bytes digest mismatch")
        result[canonical] = bytes(raw)
    return result


def source_anchor(value: Any, source_bytes: bytes | None = None) -> Dict[str, Any]:
    """Validate a source anchor and, when supplied, bind it to resource bytes.

    The artifact envelope stores only a ``ResourceRef`` and the half-open byte
    range.  Syntax validation alone cannot prove that ``excerpt_sha256`` is
    the bytes at that range (or that the referenced resource itself is the
    bytes being inspected), so callers that have the frozen source should pass
    it here.  Omitting ``source_bytes`` preserves envelope-only validation for
    deserializers that do not yet have the resource snapshot.
    """
    if not isinstance(value, dict) or set(value) != {"ref", "start_byte", "end_byte", "excerpt_sha256"}:
        _fail("E_ARTIFACT_SOURCE", "/source_anchor", "closed source anchor required")
    result = resource_ref(value["ref"], "/source_anchor/ref")
    if (type(value["start_byte"]) is not int or type(value["end_byte"]) is not int
            or not 0 <= value["start_byte"] <= MAX_SAFE_INTEGER
            or not 0 < value["end_byte"] <= MAX_SAFE_INTEGER
            or value["end_byte"] <= value["start_byte"]):
        _fail("E_ARTIFACT_SOURCE", "/source_anchor", "invalid byte range")
    if not isinstance(value["excerpt_sha256"], str) or not HEX64.fullmatch(value["excerpt_sha256"]):
        _fail("E_ARTIFACT_SOURCE", "/source_anchor/excerpt_sha256", "lowercase SHA-256 required")
    if source_bytes is not None:
        if type(source_bytes) is not bytes:
            _fail("E_ARTIFACT_SOURCE", "/source_anchor/source_bytes", "source bytes required")
        if hashlib.sha256(source_bytes).hexdigest() != result["sha256"]:
            _fail("E_ARTIFACT_SOURCE", "/source_anchor/ref/sha256", "source resource digest mismatch")
        start, end = value["start_byte"], value["end_byte"]
        if end > len(source_bytes):
            _fail("E_ARTIFACT_SOURCE", "/source_anchor", "byte range exceeds source resource")
        if hashlib.sha256(source_bytes[start:end]).hexdigest() != value["excerpt_sha256"]:
            _fail("E_ARTIFACT_SOURCE", "/source_anchor/excerpt_sha256", "excerpt digest mismatch")
    return {"ref": result, "start_byte": value["start_byte"], "end_byte": value["end_byte"], "excerpt_sha256": value["excerpt_sha256"]}


def _producer(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"kind", "id", "version"}:
        _fail("E_ARTIFACT_PRODUCER", "/producer", "closed producer identity required")
    if any(not isinstance(value[x], str) or not value[x] or any(
            ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF
            for ch in value[x])
           for x in ("kind", "id", "version")):
        _fail("E_ARTIFACT_PRODUCER", "/producer", "producer fields must be non-empty strings")
    return dict(value)


def validate_artifact(record: Mapping[str, Any]) -> Dict[str, Any]:
    required = {"record_type", "schema_version", "artifact_id", "producer", "status", "source_refs", "candidate_ref", "environment_ref", "payload"}
    if not isinstance(record, dict) or set(record) != required:
        _fail("E_ARTIFACT_SCHEMA", "/", "artifact has unknown or missing fields")
    if type(record["record_type"]) is not str or record["record_type"] not in FAMILIES or record["schema_version"] != 1:
        _fail("E_ARTIFACT_VERSION", "/record_type", "unsupported artifact family/version")
    if (not isinstance(record["artifact_id"], str) or not record["artifact_id"]
            or any(ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF
                   for ch in record["artifact_id"])):
        _fail("E_ARTIFACT_SCHEMA", "/artifact_id", "non-empty artifact id required")
    _producer(record["producer"])
    if type(record["status"]) is not str or record["status"] not in STATUSES:
        _fail("E_ARTIFACT_STATUS", "/status", "unknown artifact status")
    refs = record["source_refs"]
    if not isinstance(refs, list):
        _fail("E_ARTIFACT_REF", "/source_refs", "source refs list required")
    normalized = [resource_ref(x, f"/source_refs/{i}") for i, x in enumerate(refs)]
    identities = {(x["kind"], x["id"], x["path"], x["sha256"]) for x in normalized}
    if len(identities) != len(normalized):
        _fail("E_ARTIFACT_REF", "/source_refs", "unique canonical source refs required")
    candidate = None if record["candidate_ref"] is None else resource_ref(record["candidate_ref"], "/candidate_ref")
    environment = None if record["environment_ref"] is None else resource_ref(record["environment_ref"], "/environment_ref")
    if not isinstance(record["payload"], dict):
        _fail("E_ARTIFACT_SCHEMA", "/payload", "payload must be an object")
    canonical_bytes(record)
    result = copy.deepcopy(dict(record)); result["source_refs"] = normalized; result["candidate_ref"] = candidate; result["environment_ref"] = environment
    return result


def validate_catalog(catalog: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(catalog, dict) or set(catalog) != {"record_type", "schema_version", "plugins"}:
        _fail("E_CATALOG_SCHEMA", "/", "closed catalog required")
    if catalog["record_type"] != "builtin-task-plugin-catalog" or catalog["schema_version"] != 1 or not isinstance(catalog["plugins"], list):
        _fail("E_CATALOG_SCHEMA", "/", "invalid catalog header")
    ids = []
    normalized_rows = []
    for i, item in enumerate(catalog["plugins"]):
        if not isinstance(item, dict) or set(item) != {"plugin_id", "version", "manifest_ref", "lock_ref"}:
            _fail("E_CATALOG_SCHEMA", f"/plugins/{i}", "catalog item is not closed")
        if (not isinstance(item["plugin_id"], str) or not PLUGIN_ID_RE.fullmatch(item["plugin_id"])
                or item["plugin_id"].lower() in MUTABLE_PLUGIN_ALIASES):
            _fail("E_CATALOG_SCHEMA", f"/plugins/{i}/plugin_id", "lowercase plugin id required")
        if not isinstance(item["version"], str) or not VERSION_RE.fullmatch(item["version"]):
            _fail("E_CATALOG_SCHEMA", f"/plugins/{i}/version", "exact semantic version required")
        ids.append(item["plugin_id"])
        normalized_rows.append({"plugin_id": item["plugin_id"], "version": item["version"],
                                "manifest_ref": resource_ref(item["manifest_ref"], f"/plugins/{i}/manifest_ref"),
                                "lock_ref": resource_ref(item["lock_ref"], f"/plugins/{i}/lock_ref")})
    if len(set(ids)) != len(ids):
        _fail("E_CATALOG_SCHEMA", "/plugins", "duplicate plugin id")
    result = copy.deepcopy(dict(catalog))
    result["plugins"] = normalized_rows
    canonical_bytes(result)
    return result


__all__ = ["ArtifactError", "FAMILIES", "SourceRefKey", "canonical_bytes", "digest", "resource_ref", "resource_ref_key", "source_bytes_by_ref", "source_anchor", "validate_artifact", "validate_catalog"]
