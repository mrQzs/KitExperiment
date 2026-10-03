"""Offline integration gates for the 07/S5 boundary.

The 07 core has several small, deliberately pure projections.  They are useful
fixtures, but they are not evidence that a real 03/04/05/06/09/10 connector is
installed or authorized.  This module is the narrow seam between those
projections and an eventual integration layer:

* contract and candidate identities are fixed by content hashes;
* an uncommitted candidate never creates a commit implicitly;
* 09 readiness and 10 brief projections remain read-only and linked to the
  same state high-water marks; and
* absent or synthetic production connectors are reported as BLOCKED.

Everything here consumes caller-supplied facts.  It never reads Git, starts a
process, resolves a provider, reads credentials, writes an outbox, or commits
files.  ``PASS`` therefore means that the supplied offline facts are
internally consistent; production readiness is a separate, explicit result.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from . import protocols


class IntegrationGateError(ValueError):
    """Invalid input to an offline integration gate."""

    def __init__(self, code: str, field: str = "", reason: str = "invalid integration gate input") -> None:
        self.code = code
        self.field = field
        self.reason = reason
        super().__init__(f"{code} {field}: {reason}" if field else f"{code}: {reason}")


PASS = "PASS"
BLOCKED = "BLOCKED"
WAIT = "WAIT"
PRODUCTION_BLOCKED = "PRODUCTION_BLOCKED"
OFFLINE_WIRED = "OFFLINE_WIRED"

REQUIRED_CONNECTORS = ("03", "04", "05", "06", "09", "10")
MUTABLE_ALIASES = frozenset(("latest", "head", "current", "tip", "default"))


@dataclass(frozen=True)
class GateDecision:
    """Deterministic decision and evidence from one gate."""

    decision: str
    reasons: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = MappingProxyType({})

    @property
    def ok(self) -> bool:
        return self.decision in (PASS, OFFLINE_WIRED)


def canonical_json(value: Any) -> bytes:
    """Encode a JSON value canonically for a stable identity hash."""
    try:
        protocols.check_json_value(value, max_bytes=protocols.DEFAULT_MAX_BYTES - 1)
        return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (protocols.ProtocolError, TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise IntegrationGateError("E_CANONICAL_JSON", reason="JSON value is not canonicalizable") from exc


def sha256_hex(value: bytes | bytearray | memoryview) -> str:
    """Return a lower-case SHA-256 digest for bytes-like content."""
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise IntegrationGateError("E_BYTES", reason="bytes required")
    return hashlib.sha256(bytes(value)).hexdigest()


def contract_digest(contract: Any) -> str:
    """Hash a contract object or its exact serialized bytes.

    The dispatcher records the hash of ``contract.json`` bytes.  Mapping input
    is normalized for deterministic fixtures; bytes input preserves the file
    hash exactly so a gate can verify dispatcher metadata without rewriting
    the contract first.
    """
    if isinstance(contract, (bytes, bytearray, memoryview)):
        return sha256_hex(contract)
    if not isinstance(contract, Mapping):
        raise IntegrationGateError("E_CONTRACT", "/contract", "contract object or serialized bytes required")
    return sha256_hex(canonical_json(contract))


def _digest(value: Any, field: str) -> str:
    if type(value) is not str or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise IntegrationGateError("E_HASH", field, "lowercase SHA-256 required")
    return value


def _tree_oid(value: Any, field: str) -> str:
    if type(value) is not str or len(value) not in (40, 64) \
            or any(ch not in "0123456789abcdef" for ch in value):
        raise IntegrationGateError("E_TREE_OID", field, "lowercase SHA-1 or SHA-256 tree id required")
    return value


def _text(value: Any, field: str) -> str:
    if (type(value) is not str or not value or any(
            ord(ch) < 0x20 or ord(ch) == 0x7F or 0xD800 <= ord(ch) <= 0xDFFF
            for ch in value)):
        raise IntegrationGateError("E_TEXT", field, "nonempty text required")
    return value


def _identity_ref(value: Any, field: str) -> tuple[str, str, str]:
    if not isinstance(value, Mapping):
        raise IntegrationGateError("E_REFERENCE", field, "reference object required")
    required = {"kind", "id", "sha256"}
    if not required.issubset(value):
        raise IntegrationGateError("E_REFERENCE", field, "kind, id and sha256 required")
    kind = _text(value["kind"], f"{field}/kind")
    identity = _text(value["id"], f"{field}/id")
    if identity.lower() in MUTABLE_ALIASES:
        raise IntegrationGateError("E_REFERENCE_ALIAS", f"{field}/id", "mutable alias is forbidden")
    digest = _digest(value["sha256"], f"{field}/sha256")
    return kind, identity, digest


def verify_contract_hash(contract: Any, expected_sha256: str | None,
                         *, declared_ref: Mapping[str, Any] | None = None) -> GateDecision:
    """Verify a contract against a fixed hash.

    A missing expected hash is ``WAIT`` rather than success.  If a declaration
    contains a resource reference its identity digest must agree with the same
    expected value; a path or version alias cannot stand in for a digest.
    """
    if expected_sha256 is None:
        return GateDecision(WAIT, ("CONTRACT_HASH_UNKNOWN",), MappingProxyType({"actual_sha256": contract_digest(contract)}))
    expected = _digest(expected_sha256, "/expected_sha256")
    actual = contract_digest(contract)
    reasons: list[str] = []
    if actual != expected:
        reasons.append("CONTRACT_HASH_MISMATCH")
    if declared_ref is not None:
        try:
            if not isinstance(declared_ref, Mapping):
                raise IntegrationGateError("E_REFERENCE", "/contract_ref", "reference object required")
            _kind, _id, declared = _identity_ref(declared_ref, "/contract_ref")
            if declared != expected:
                reasons.append("CONTRACT_REF_HASH_MISMATCH")
        except IntegrationGateError as exc:
            reasons.append(exc.code)
    evidence = MappingProxyType({"expected_sha256": expected, "actual_sha256": actual})
    return GateDecision(PASS if not reasons else BLOCKED, tuple(dict.fromkeys(reasons)), evidence)


# Alias used by callers that describe this as identity rather than hashing.
verify_contract_identity = verify_contract_hash
contract_hash = contract_digest


def _candidate_ref(value: Any, field: str) -> tuple[str, str, str, str | None]:
    """Normalize a candidate ResourceRef while retaining its path binding.

    The compact identity used by connector evidence has no path, but a
    candidate ResourceRef does.  Dropping that field would let a same-id/file
    hash from a different location pass a fixed-candidate gate.
    """
    kind, identity, digest = _identity_ref(value, field)
    path = value.get("path") if isinstance(value, Mapping) else None
    if path is not None:
        if type(path) is not str or not path or "\x00" in path:
            raise IntegrationGateError("E_REFERENCE", f"{field}/path", "candidate path must be non-empty text")
        normalized = path.replace("\\", "/")
        if (normalized.startswith("/") or any(part in {"", ".", ".."} for part in normalized.split("/"))
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized)):
            raise IntegrationGateError("E_REFERENCE", f"{field}/path", "candidate path must be relative")
        path = normalized
    return kind, identity, digest, path


def verify_fixed_candidate(expected_ref: Mapping[str, Any], candidate: Mapping[str, Any]) -> GateDecision:
    """Verify one candidate is the recorded fixed candidate.

    ``candidate`` must contain ``roundtrip_verified`` and matching fixed
    ``tree``/``tree_sha`` and ``patch_sha256`` values when the expected record
    is a dispatcher ``candidate.json``.  Published candidate resources may
    use ``candidate_ref`` (``ref`` is accepted as a compatibility spelling).
    Missing runtime observations wait; explicit mismatches block.
    """
    if not isinstance(expected_ref, Mapping):
        raise IntegrationGateError("E_REFERENCE", "/expected_candidate_ref", "reference object required")
    # Dispatcher candidate.json records a tree and patch hash directly.  A
    # published candidate resource may additionally carry a ResourceRef.  Both
    # forms are fixed identities; the former is intentionally not upgraded to
    # a mutable alias.
    embedded_ref = expected_ref.get("candidate_ref", expected_ref.get("ref"))
    if embedded_ref is None and {"kind", "id", "sha256"}.issubset(expected_ref):
        embedded_ref = expected_ref
    expected_identity: tuple[str, str, str, str | None] | None = None
    if embedded_ref is not None:
        expected_identity = _candidate_ref(embedded_ref, "/expected_candidate_ref")
    expected_tree = expected_ref.get("tree_sha", expected_ref.get("tree"))
    expected_patch = expected_ref.get("patch_sha256")
    if expected_identity is None and expected_tree is None and expected_patch is None:
        raise IntegrationGateError("E_REFERENCE", "/expected_candidate_ref", "fixed candidate identity required")
    if expected_tree is not None:
        expected_tree = _tree_oid(expected_tree, "/expected_candidate_ref/tree")
    if expected_patch is not None:
        expected_patch = _digest(expected_patch, "/expected_candidate_ref/patch_sha256")
    if not isinstance(candidate, Mapping):
        raise IntegrationGateError("E_CANDIDATE", "/candidate", "candidate object required")
    actual_ref_value = candidate.get("candidate_ref", candidate.get("ref"))
    reasons: list[str] = []
    evidence: dict[str, Any] = {"expected_ref": expected_identity,
                                "identity_mode": "resource_ref" if expected_identity else "tree_patch"}
    if actual_ref_value is None:
        # A dispatcher candidate.json is identified externally by the expected
        # resource reference; its payload need only carry the fixed tree/patch
        # evidence.  If neither payload identity is present, keep waiting for
        # the reference instead of silently accepting it.
        if expected_identity is not None and expected_tree is None and expected_patch is None:
            reasons.append("CANDIDATE_REFERENCE_UNKNOWN")
    else:
        try:
            actual_ref = _candidate_ref(actual_ref_value, "/candidate/candidate_ref")
            evidence["actual_ref"] = actual_ref
            if expected_identity is not None:
                if expected_identity[3] is not None and actual_ref[3] is None:
                    reasons.append("CANDIDATE_REFERENCE_UNKNOWN")
                elif actual_ref != expected_identity:
                    reasons.append("CANDIDATE_REFERENCE_MISMATCH")
        except IntegrationGateError as exc:
            reasons.append(exc.code)

    for key, expected_value, reason in (("tree_sha", expected_tree, "CANDIDATE_TREE_MISMATCH"),
                                        ("patch_sha256", expected_patch, "CANDIDATE_PATCH_MISMATCH")):
        if expected_value is not None:
            actual_value = candidate.get(key)
            if actual_value is None and key == "tree_sha":
                actual_value = candidate.get("tree")
            if actual_value is None:
                reasons.append("CANDIDATE_%s_UNKNOWN" % key.upper())
            elif type(actual_value) is not str or actual_value != expected_value:
                reasons.append(reason)
            evidence[f"expected_{key}"] = expected_value
            evidence[f"actual_{key}"] = actual_value

    roundtrip = candidate.get("roundtrip_verified")
    if roundtrip is not True:
        reasons.append("CANDIDATE_ROUNDTRIP_REJECTED" if roundtrip is False else "CANDIDATE_ROUNDTRIP_UNKNOWN")
    evidence["roundtrip_verified"] = roundtrip
    return GateDecision(PASS if not reasons else (WAIT if all(r.endswith("_UNKNOWN") for r in reasons) else BLOCKED),
                        tuple(dict.fromkeys(reasons)), MappingProxyType(evidence))


def evaluate_commit_permission(*, has_uncommitted_changes: bool | None,
                               integration_actions: Iterable[str] = (),
                               commit_sha: str | None = None) -> GateDecision:
    """Apply the no-implicit-commit boundary to a candidate patch.

    ``local_commit`` is the explicit executor permission.  A generic
    integration ``commit`` action is intentionally not treated as permission
    to mutate an uncommitted worktree.  This gate only records whether the
    supplied permission/evidence is sufficient; it never creates a commit.
    """
    if has_uncommitted_changes is not None and type(has_uncommitted_changes) is not bool:
        raise IntegrationGateError("E_BOOL", "/has_uncommitted_changes", "boolean or null required")
    if integration_actions is None or isinstance(integration_actions, (str, bytes)):
        raise IntegrationGateError("E_ACTIONS", "/integration_actions", "action sequence required")
    try:
        actions = tuple(_text(action, "/integration_actions") for action in integration_actions)
    except TypeError as exc:
        raise IntegrationGateError("E_ACTIONS", "/integration_actions", "action sequence required") from exc
    if commit_sha is not None:
        _digest(commit_sha, "/commit_sha")
    if has_uncommitted_changes is None:
        return GateDecision(WAIT, ("WORKTREE_STATUS_UNKNOWN",), MappingProxyType({"actions": actions}))
    if not has_uncommitted_changes:
        return GateDecision(PASS, ("NO_COMMIT_REQUIRED",), MappingProxyType({"actions": actions, "commit_sha": commit_sha}))
    if "local_commit" not in actions:
        return GateDecision(BLOCKED, ("COMMIT_PERMISSION_REQUIRED",), MappingProxyType({"actions": actions}))
    if commit_sha is None:
        return GateDecision(WAIT, ("COMMIT_EVIDENCE_UNKNOWN",), MappingProxyType({"actions": actions}))
    return GateDecision(PASS, (), MappingProxyType({"actions": actions, "commit_sha": commit_sha}))


def _as_projection(value: Any, field: str) -> Mapping[str, Any]:
    if hasattr(value, "__dict__") and all(hasattr(value, name) for name in ("can_refine", "can_start_planner", "can_execute", "reasons")):
        return {name: getattr(value, name) for name in ("can_refine", "can_start_planner", "can_execute", "reasons")}
    if type(value) is not dict:
        raise IntegrationGateError("E_PROJECTION", field, "projection object required")
    return value


def verify_09_10_wiring(*, readiness: Any, state: Mapping[str, Any], brief: Mapping[str, Any]) -> GateDecision:
    """Check offline 09 readiness → 10 brief wiring without granting dispatch."""
    projection = _as_projection(readiness, "/readiness")
    reasons: list[str] = []
    if type(state) is not dict or type(brief) is not dict:
        raise IntegrationGateError("E_PROJECTION", "/state", "state and brief objects required")
    for field in ("can_refine", "can_start_planner", "can_execute"):
        if type(projection.get(field)) is not bool:
            reasons.append("READINESS_%s_UNKNOWN" % field.upper())
    if projection.get("can_execute") is True and projection.get("can_start_planner") is not True:
        reasons.append("READINESS_LEVEL_ORDER_INVALID")
    if not isinstance(projection.get("reasons", ()), (tuple, list)):
        reasons.append("READINESS_REASONS_INVALID")

    for field in ("indexed_cursor", "applied_cursor"):
        value = state.get(field)
        if type(value) is not int or not 0 <= value <= protocols.MAX_SAFE_INTEGER:
            reasons.append("STATE_%s_UNKNOWN" % field.upper())
    if type(state.get("indexed_cursor")) is int and type(state.get("applied_cursor")) is int \
            and state["applied_cursor"] > state["indexed_cursor"]:
        reasons.append("STATE_CURSOR_ORDER_INVALID")
    if not _nonempty_text(state.get("verification_ref")):
        reasons.append("STATE_VERIFICATION_REF_UNKNOWN")
    if not _nonempty_text(state.get("as_of")):
        reasons.append("STATE_AS_OF_UNKNOWN")
    source_vector = state.get("source_vector")
    if (not isinstance(source_vector, Mapping)
            or any(type(source) is not str or not _nonempty_text(source)
                   or type(seq) is not int or not 0 <= seq <= protocols.MAX_SAFE_INTEGER
                   or (type(state.get("indexed_cursor")) is int and seq > state["indexed_cursor"])
                   for source, seq in source_vector.items())):
        reasons.append("STATE_SOURCE_VECTOR_INVALID")
    if type(state.get("source_coverage")) is not str or state.get("source_coverage") not in {"complete", "partial", "unknown"}:
        reasons.append("STATE_SOURCE_COVERAGE_INVALID")
    if type(state.get("budget_coverage")) is not str or state.get("budget_coverage") not in {"complete", "partial", "unknown"}:
        reasons.append("STATE_BUDGET_COVERAGE_INVALID")
    if type(state.get("health")) is not str or state.get("health") not in {"healthy", "degraded"}:
        reasons.append("STATE_HEALTH_INVALID")
    elif state.get("health") == "healthy" and (
            state.get("source_coverage") != "complete"
            or state.get("budget_coverage") != "complete"
            or state.get("issues") not in ([], ())):
        reasons.append("STATE_HEALTH_CONTRADICTION")
    state_issues = state.get("issues", [])
    if (isinstance(state_issues, (str, bytes))
            or not isinstance(state_issues, Sequence)
            or any(not _nonempty_text(item) for item in state_issues)):
        reasons.append("STATE_ISSUES_INVALID")
    if type(state.get("truncated", False)) is not bool:
        reasons.append("STATE_TRUNCATION_INVALID")

    # 10 is a read model.  It cannot turn a brief into an execution permit.
    if brief.get("dispatch_eligibility") != "not_evaluated":
        reasons.append("BRIEF_DISPATCH_FIELD_NOT_NEUTRAL")
    if type(brief.get("health")) is not str or brief.get("health") not in {"healthy", "degraded"}:
        reasons.append("BRIEF_HEALTH_INVALID")
    if type(brief.get("source_coverage")) is not str or brief.get("source_coverage") not in {"complete", "partial", "unknown"}:
        reasons.append("BRIEF_SOURCE_COVERAGE_INVALID")
    if (isinstance(brief.get("issues"), (str, bytes))
            or not isinstance(brief.get("issues"), Sequence)
            or any(type(item) is not str or not item for item in brief.get("issues", ()) )):
        reasons.append("BRIEF_ISSUES_INVALID")
    if type(brief.get("truncated")) is not bool:
        reasons.append("BRIEF_TRUNCATION_INVALID")
    if brief.get("source_coverage") != state.get("source_coverage"):
        reasons.append("BRIEF_SOURCE_COVERAGE_MISMATCH")
    if brief.get("health") != state.get("health"):
        reasons.append("BRIEF_HEALTH_MISMATCH")
    expected_budget = "预算快照已核验" if state.get("budget_coverage") == "complete" else "[未知] 无可核验预算快照"
    if brief.get("budget") != expected_budget:
        reasons.append("BRIEF_BUDGET_COVERAGE_MISMATCH")
    if brief.get("truncated") is not bool(state.get("truncated", False)):
        reasons.append("BRIEF_TRUNCATION_MISMATCH")
    if "truncated" not in brief:
        reasons.append("BRIEF_TRUNCATION_UNKNOWN")
    evidence = MappingProxyType({
        "readiness": dict(projection),
        "state_verification_ref": state.get("verification_ref"),
        "indexed_cursor": state.get("indexed_cursor"),
        "applied_cursor": state.get("applied_cursor"),
        "dispatch_eligibility": brief.get("dispatch_eligibility"),
        "offline_only": True,
    })
    return GateDecision(OFFLINE_WIRED if not reasons else BLOCKED,
                        tuple(dict.fromkeys(reasons)), evidence)


def evaluate_production_connectors(connectors: Mapping[str, Mapping[str, Any]] | None,
                                   *, required: Sequence[str] = REQUIRED_CONNECTORS) -> GateDecision:
    """Require explicit production evidence for each cross-stage connector.

    Synthetic/offline fixtures are retained in evidence and always produce a
    production ``BLOCKED`` result.  No caller can obtain a production PASS by
    omitting a connector or by setting ``available`` without ``production``.
    """
    if (not isinstance(required, Sequence) or isinstance(required, (str, bytes)) or not required
            or any(type(stage) is not str or not stage for stage in required)
            or len(set(required)) != len(required)):
        raise IntegrationGateError("E_CONNECTORS", "/required", "unique connector stage names required")
    if connectors is None:
        return GateDecision(PRODUCTION_BLOCKED,
                            tuple("CONNECTOR_%s_MISSING" % stage for stage in required),
                            MappingProxyType({"required": tuple(required), "connectors": {}}))
    if type(connectors) is not dict:
        raise IntegrationGateError("E_CONNECTORS", "/connectors", "connector map required")
    reasons: list[str] = []
    evidence: dict[str, Any] = {"required": tuple(required), "connectors": {}}
    connector_ids: set[str] = set()
    unknown_stages = sorted(set(connectors) - set(required), key=lambda item: (type(item).__name__, repr(item)))
    for stage in unknown_stages:
        reasons.append("CONNECTOR_%s_UNEXPECTED" % stage if type(stage) is str else "CONNECTOR_UNKNOWN_KEY")
    for stage in required:
        item = connectors.get(stage)
        if type(item) is not dict:
            reasons.append("CONNECTOR_%s_MISSING" % stage)
            continue
        status = item.get("status")
        production = item.get("production")
        available = item.get("available")
        connector_id = item.get("connector_id")
        evidence_ref = item.get("evidence_ref")
        normalized_ref = None
        shape_ok = True
        # Offline/synthetic declarations are intentionally small and only need
        # to explain why production is blocked.  The stricter closed identity
        # and evidence contract applies once a caller claims production=True.
        if production is True:
            shape_ok = set(item) == {"connector_id", "status", "production", "available", "evidence_ref"}
            if not shape_ok:
                reasons.append("CONNECTOR_%s_SCHEMA_INVALID" % stage)
            if not _nonempty_text(connector_id) or connector_id.lower() in MUTABLE_ALIASES:
                reasons.append("CONNECTOR_%s_IDENTITY_INVALID" % stage)
            elif connector_id in connector_ids:
                reasons.append("CONNECTOR_%s_IDENTITY_DUPLICATE" % stage)
            else:
                connector_ids.add(connector_id)
            if not isinstance(evidence_ref, Mapping):
                reasons.append("CONNECTOR_%s_EVIDENCE_UNKNOWN" % stage)
            else:
                try:
                    kind, identity, digest = _identity_ref(evidence_ref, "/connectors/%s/evidence_ref" % stage)
                    normalized_ref = {"kind": kind, "id": identity, "sha256": digest}
                except IntegrationGateError:
                    reasons.append("CONNECTOR_%s_EVIDENCE_INVALID" % stage)
        evidence["connectors"][stage] = {
            "connector_id": connector_id, "status": status, "production": production,
            "available": available, "evidence_ref": normalized_ref,
        }
        if (production is True and (not shape_ok or normalized_ref is None or not _nonempty_text(connector_id))) \
                or production is not True or available is not True or status != "verified":
            if (type(status) is str and status in {"offline_fixture", "synthetic", "local"}) or production is False:
                reasons.append("CONNECTOR_%s_OFFLINE_ONLY" % stage)
            else:
                reasons.append("CONNECTOR_%s_NOT_VERIFIED" % stage)
    return GateDecision(PASS if not reasons else PRODUCTION_BLOCKED,
                        tuple(dict.fromkeys(reasons)), MappingProxyType(evidence))


def evaluate_integration_gates(*, contract: Any, expected_contract_sha256: str | None,
                               candidate_ref: Mapping[str, Any], candidate: Mapping[str, Any],
                               has_uncommitted_changes: bool | None,
                               integration_actions: Iterable[str], readiness: Any,
                               state: Mapping[str, Any], brief: Mapping[str, Any],
                               connectors: Mapping[str, Mapping[str, Any]] | None) -> GateDecision:
    """Compose all S5.3 offline and production-boundary decisions."""
    decisions = (
        verify_contract_hash(contract, expected_contract_sha256),
        verify_fixed_candidate(candidate_ref, candidate),
        evaluate_commit_permission(has_uncommitted_changes=has_uncommitted_changes,
                                   integration_actions=integration_actions,
                                   commit_sha=candidate.get("commit_sha")),
        verify_09_10_wiring(readiness=readiness, state=state, brief=brief),
        evaluate_production_connectors(connectors),
    )
    reasons = tuple(dict.fromkeys(reason for item in decisions for reason in item.reasons))
    evidence = MappingProxyType({
        "contract": decisions[0].evidence,
        "candidate": decisions[1].evidence,
        "commit": decisions[2].evidence,
        "wiring": decisions[3].evidence,
        "production": decisions[4].evidence,
        "production_decision": decisions[4].decision,
    })
    # Offline wiring can be sound while the actual cross-stage connectors are
    # absent.  Keep that distinction explicit in the composite result.
    if decisions[4].decision == PRODUCTION_BLOCKED:
        decision = PRODUCTION_BLOCKED
    elif any(item.decision == BLOCKED for item in decisions):
        decision = BLOCKED
    elif any(item.decision == WAIT for item in decisions):
        decision = WAIT
    else:
        decision = PASS
    return GateDecision(decision, reasons, evidence)


def _nonempty_text(value: Any) -> bool:
    return type(value) is str and bool(value) and all(
        ord(ch) >= 0x20 and not 0x7F <= ord(ch) <= 0x9F and not 0xD800 <= ord(ch) <= 0xDFFF
        for ch in value)


# Descriptive aliases keep call sites readable without creating alternate
# semantics or a second implementation.
fixed_candidate_gate = verify_fixed_candidate
commit_gate = evaluate_commit_permission
production_connector_gate = evaluate_production_connectors
projection_wiring_gate = verify_09_10_wiring


__all__ = [
    "BLOCKED", "GateDecision", "IntegrationGateError", "OFFLINE_WIRED", "PASS", "PRODUCTION_BLOCKED",
    "REQUIRED_CONNECTORS", "WAIT", "canonical_json", "contract_digest", "contract_hash",
    "evaluate_commit_permission",
    "evaluate_integration_gates", "evaluate_production_connectors", "sha256_hex", "verify_09_10_wiring",
    "verify_contract_hash", "verify_contract_identity", "verify_fixed_candidate", "fixed_candidate_gate",
    "commit_gate", "production_connector_gate", "projection_wiring_gate",
]
