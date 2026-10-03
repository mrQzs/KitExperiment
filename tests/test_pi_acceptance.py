"""Offline acceptance fixtures for the pi-acceptance contract (v2, attempt a002).

Every fixture in this module is deterministic: fixed ResourceRef identities,
fixed SHA-256 digests and strict JSON.  Nothing here starts a provider CLI,
reads credentials, touches the network, or mutates the product implementation.
The assertions follow one rule: the unmodified baseline fixture must validate
first, and each rejection case changes exactly the condition under test so a
different rule cannot mask the gap.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError

import pytest

from sentinelmesh.baseline import BaselineCapture, BaselineError, BaselineSnapshot
from sentinelmesh.core import stable_snapshot_bytes
from sentinelmesh.integration_gates import (
    BLOCKED,
    OFFLINE_WIRED,
    PASS,
    PRODUCTION_BLOCKED,
    WAIT,
    evaluate_commit_permission,
    evaluate_integration_gates,
    evaluate_production_connectors,
    verify_09_10_wiring,
    verify_contract_hash,
    verify_fixed_candidate,
)
from sentinelmesh.planner import (
    ContractIR,
    DraftStore,
    PlannerError,
    PublicationStore,
    build_candidate,
    make_resource_ref,
    render_contract_ir,
    validate_resource_ref,
)
from sentinelmesh.integration_gates import canonical_json
from sentinelmesh.planner_protocol import (
    ProtocolError,
    strict_planner_json_loads,
    validate_planning_binding,
    validate_planning_bundle,
    validate_source_anchor,
)
from sentinelmesh.workflows import WorkflowError, builtin_catalog, builtin_workflow, lock_for_workflow


# --------------------------------------------------------------------------
# Fixed offline fixtures
# --------------------------------------------------------------------------

def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


BASELINE_ROOT = "D:/Code/KitExperiment"


def _baseline(**overrides):
    """A deterministic, immutable repository snapshot (no Git subprocess)."""
    values = {
        "repository_root": BASELINE_ROOT,
        "git_dir": BASELINE_ROOT + "/.git",
        "object_format": "sha1",
        "head": "979aea067fb5bf3595612a49e38aa3edf94e16d6",
        "status": (),
        "tracked": ({"path": "AGENTS.md", "kind": "file", "bytes": 2048,
                     "sha256": _digest("AGENTS.md")},),
        "untracked": (),
        "submodules": (),
        "toolchain": {"python": "3.12.0", "platform": "windows", "git": "2.44.0",
                      "executables": {"git": "git", "python": "python",
                                      "pi": None, "claude": None}},
        "complete": True,
        "issues": (),
        "observed_at": "2026-01-01T00:00:00+00:00",
    }
    values.update(overrides)
    return BaselineSnapshot(**values)


def _ref(kind: str, ident: str, digest: str | None = None, path: str = "inputs/fixture.json") -> dict:
    return {"kind": kind, "id": ident, "sha256": digest or _digest(kind + ":" + ident),
            "path": path}


SOURCE_BYTES = b"sentinelmesh offline fixture bytes"
SOURCE_REF = _ref("evidence", "src-1", _digest("sentinelmesh offline fixture bytes"),
                  "inputs/source.txt")


def _fact() -> dict:
    return {"id": "fact-1", "text": "baseline HEAD is committed and clean",
            "evidence_class": "source_observation",
            "evidence": [{"ref": dict(SOURCE_REF), "start_byte": 0, "end_byte": 12,
                          "quote_sha256": _digest("sentinelmesh")}]}


def _task(task_id: str, depends_on=()) -> dict:
    return {"id": task_id, "title": "Task " + task_id, "depends_on": list(depends_on),
            "requirement_ids": ["req-a", "req-b"], "scope_paths": ["tests/test_pi_acceptance.py"],
            "acceptance_intent": ["offline acceptance evidence exists"],
            "stages": [
                {"id": "investigate", "kind": "investigate", "depends_on": [], "plugin_step_ref": None},
                {"id": "change", "kind": "change", "depends_on": ["investigate"], "plugin_step_ref": None},
                {"id": "check", "kind": "check", "depends_on": ["change"], "plugin_step_ref": None},
            ],
            "estimated_calls": 1}


def _draft() -> dict:
    return {"task_id": "task-1", "completeness": "template",
            "baseline": {"state": "unbound", "reason": "planning stage has not bound a baseline yet"},
            "scope_paths": ["tests/test_pi_acceptance.py"],
            "checks_ref": _ref("checks", "checks-1"), "executor_profile_ref": _ref("profile", "executor-1"),
            "environment_ref": _ref("environment", "env-1"), "plugin_lock_ref": _ref("plugin-lock", "lock-1"),
            "narrative": {"goal": "Add offline acceptance coverage", "non_goals": ["no provider calls"],
                          "behavior": ["fail closed on missing evidence"],
                          "acceptance": ["V01..V04 pass offline"]}}


def _planner_request() -> dict:
    return {"record_type": "planner-request", "schema_version": 1,
            "request_id": "req-1", "run_id": "run-1", "operation_id": "op-1",
            "mode": "graph", "target_task_id": None, "graph_ref": None,
            "requirements_ref": _ref("requirements", "req-a"),
            "context_manifest_ref": _ref("context", "ctx"),
            "baseline_ref": _ref("baseline", "base-1"),
            "planner_config_ref": _ref("config", "planner-config"),
            "environment_ref": _ref("environment", "env-1"),
            "catalog_ref": _ref("catalog", "plugins"),
            "rules_refs": [_ref("rules", "rules-1")],
            "decision_refs": [_ref("decision", "decision-1")],
            "output_contract_ref": _ref("contract-template", "template"),
            "planning_budget_ref": _ref("budget", "budget-1"),
            "authorization_refs": [_ref("authorization", "auth-1")],
            "repair_policy_ref": _ref("policy", "repair"),
            "stop_policy_ref": _ref("policy", "stop")}


def _context_manifest() -> dict:
    single = ("requirements_ref", "context_manifest_ref", "baseline_ref", "planner_config_ref",
              "environment_ref", "catalog_ref", "output_contract_ref", "planning_budget_ref",
              "repair_policy_ref", "stop_policy_ref")
    request = _planner_request()
    resources = [dict(request[name]) for name in single]
    resources += [dict(ref) for ref in request["rules_refs"] + request["decision_refs"] + request["authorization_refs"]]
    return {"record_type": "context-manifest", "schema_version": 1,
            "request_id": "req-1", "resources": resources,
            "baseline_ref": dict(request["baseline_ref"]),
            "requirements_ref": dict(request["requirements_ref"]),
            "environment_ref": dict(request["environment_ref"]),
            "rules_refs": [dict(ref) for ref in request["rules_refs"]],
            "decision_refs": [dict(ref) for ref in request["decision_refs"]]}


def _planning_bundle() -> dict:
    return {"record_type": "planning-bundle", "schema_version": 1, "request_id": "req-1",
            "planner_request_ref": _ref("planner-request", "req-1"),
            "mode": "graph", "graph_ref": None, "target_task_id": None,
            "investigation_baseline_ref": _ref("baseline", "base-1"),
            "requirement_refs": [_ref("requirements", "req-a"), _ref("requirements", "req-b")],
            "facts": [_fact()], "assumptions": [], "questions": [], "risks": [],
            "task_graph": {"tasks": [_task("task-1")]},
            "contract_drafts": [_draft()],
            "budget_proposal": {"max_calls": 4, "max_seconds": 600, "max_repair_calls": 1},
            "stop_conditions": ["budget_exhausted", "no_baseline"]}


def _contract_ir() -> dict:
    return {"task_id": "pi-acceptance", "plan_version": 2, "title": "Offline acceptance",
            "repository": "KitExperiment", "base_sha": "979aea067fb5bf3595612a49e38aa3edf94e16d6",
            "risk_level": "low",
            "authorization": {"executor": ["local_edit", "local_test"], "integration": []},
            "executor": {"provider": "bb", "model": "step-5-preview", "thinking": "high"},
            "scope": {"allowed_modified_files": [], "allowed_new_files": ["tests/test_pi_acceptance.py"],
                      "allowed_deleted_files": [], "forbidden_paths": ["AGENTS.md", "sentinelmesh"],
                      "dependency_changes": "forbidden"},
            "setup": [],
            "checks": [{"id": "V02", "argv": ["python", "-m", "pytest", "-q"], "cwd": ".",
                        "timeout_seconds": 180, "expected_exit_code": 0, "required": True}],
            "definition_of_done": [{"id": "D01", "text": "offline acceptance coverage added",
                                    "evidence": ["V01", "V02"]}],
            "budget": {"max_attempts_total": 1, "max_wall_seconds_per_attempt": 600,
                       "max_wall_seconds_total": 600, "cost_budget": "tracking-only"}}


def _candidate():
    ir = ContractIR.from_mapping(_contract_ir())
    md_bytes, json_bytes = render_contract_ir(ir)
    drafts = DraftStore()
    revision = drafts.create_draft("draft-1", ir.to_mapping())
    return build_candidate(
        candidate_id="candidate-1", draft=revision,
        source_ref=make_resource_ref("planner-input", "source", "inputs/source.json", raw=b"source"),
        target_ref=make_resource_ref("contract-target", "target", "contracts/target.json", raw=b"target"),
        md_bytes=md_bytes, json_bytes=json_bytes)


def _wiring_state() -> dict:
    return {"indexed_cursor": 5, "applied_cursor": 5, "verification_ref": "ver-1",
            "as_of": "2026-01-01T00:00:00Z", "source_vector": {"git": 5},
            "source_coverage": "complete", "budget_coverage": "complete",
            "health": "healthy", "issues": [], "truncated": False}


def _wiring_brief() -> dict:
    return {"dispatch_eligibility": "not_evaluated", "health": "healthy",
            "source_coverage": "complete", "issues": [],
            "budget": "预算快照已核验", "truncated": False}


READINESS = {"can_refine": True, "can_start_planner": True, "can_execute": False, "reasons": []}


def _offline_connectors() -> dict:
    return {stage: {"status": "offline_fixture", "production": False, "available": True,
                    "connector_id": stage + "-offline"} for stage in ("03", "04", "05", "06", "09", "10")}


def _production_connectors() -> dict:
    return {stage: {"connector_id": stage + "-connector", "status": "verified", "production": True,
                    "available": True,
                    "evidence_ref": {"kind": "connector-evidence", "id": stage + "-evidence",
                                     "sha256": _digest(stage)}}
            for stage in ("03", "04", "05", "06", "09", "10")}


# --------------------------------------------------------------------------
# Fixed baseline invariants
# --------------------------------------------------------------------------

def test_baseline_digest_is_fixed_and_excludes_diagnostic_timestamps():
    first = _baseline()
    later = _baseline(observed_at="2026-06-06T12:00:00+00:00")

    semantic = first.semantic_dict()
    expected_digest = hashlib.sha256(
        (json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    allow_nan=False) + "\n").encode("utf-8")).hexdigest()
    assert first.digest == expected_digest
    assert first.digest == _baseline().digest == later.digest
    assert first.baseline_id == "base-" + expected_digest[:32]
    assert first.compare(later)["same"] is True

    ref = first.resource_ref()
    assert (ref.kind, ref.id, ref.sha256) == ("baseline", first.baseline_id, first.digest)
    assert canonical_json(semantic) == stable_snapshot_bytes(semantic)
    with pytest.raises(FrozenInstanceError):
        ref.sha256 = "0" * 64


def test_baseline_snapshot_is_immutable_and_drift_is_reported():
    snapshot = _baseline()
    with pytest.raises(FrozenInstanceError):
        snapshot.head = "0" * 40

    comparison = snapshot.compare(_baseline(head="b" * 40, status=({"xy": " M", "path": "AGENTS.md",
                                                                   "orig_path": None, "path_utf8": True},)))
    assert comparison["same"] is False
    assert set(comparison["changed"]) == {"head", "status"}
    assert comparison["before"] == snapshot.digest
    with pytest.raises(BaselineError, match="E_BASELINE_SHAPE"):
        snapshot.compare({"head": "b" * 40})


def test_baseline_shape_is_fail_closed():
    for overrides, code in (({"schema_version": 2}, "E_BASELINE_SCHEMA"),
                            ({"object_format": "md5"}, "E_BASELINE_SHAPE"),
                            ({"head": ""}, "E_BASELINE_SHAPE"),
                            ({"complete": "yes"}, "E_BASELINE_SHAPE"),
                            ({"tracked": []}, "E_BASELINE_SHAPE"),
                            ({"toolchain": []}, "E_BASELINE_SHAPE")):
        with pytest.raises(BaselineError, match=code):
            _baseline(**overrides)
    with pytest.raises(BaselineError, match="E_BASELINE_LIMIT"):
        BaselineCapture(max_output_bytes=0)


def test_baseline_capture_rejects_non_repository_and_bad_timeout(tmp_path):
    with pytest.raises(BaselineError, match="E_BASELINE_NOT_REPOSITORY"):
        BaselineCapture().capture(tmp_path)
    with pytest.raises(BaselineError, match="E_BASELINE_TIMEOUT"):
        BaselineCapture(timeout_seconds=0)


# --------------------------------------------------------------------------
# PlanningBundle / Contract IR invariants
# --------------------------------------------------------------------------

def test_planning_bundle_fixture_is_valid_and_digest_bound():
    bundle = _planning_bundle()
    normalized = validate_planning_bundle(copy.deepcopy(bundle))
    assert normalized["facts"][0]["id"] == "fact-1"
    # Requirement coverage and draft binding are enforced by the same validator.
    assert list(normalized["task_graph"]["tasks"][0]["requirement_ids"]) == ["req-a", "req-b"]

    anchor = normalized["facts"][0]["evidence"][0]
    checked = validate_source_anchor(dict(anchor), source_bytes=SOURCE_BYTES)
    assert checked["quote_sha256"] == _digest("sentinelmesh")
    with pytest.raises(ProtocolError) as ctx:
        validate_source_anchor({**anchor, "quote_sha256": _digest("other")}, source_bytes=SOURCE_BYTES)
    assert (ctx.value.code, ctx.value.reason) == ("E_RESOURCE_HASH_MISMATCH", "quote_digest_mismatch")

    raw = (json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    assert strict_planner_json_loads(raw) == bundle
    for broken in (b'{"a":1,"a":2}', b'{"a":1} {"b":2}', b'\xef\xbb\xbf{}'):
        with pytest.raises(ProtocolError):
            strict_planner_json_loads(broken)


def test_planning_bundle_rejections_change_one_condition_each():
    valid = _planning_bundle()
    validate_planning_bundle(copy.deepcopy(valid))

    cases = {
        "fact_requires_evidence": lambda b: b["facts"][0].update({"evidence": []}),
        "dependency_cycle": lambda b: b["task_graph"]["tasks"][0].update(
            {"stages": [{"id": "a", "kind": "change", "depends_on": ["b"], "plugin_step_ref": None},
                        {"id": "b", "kind": "check", "depends_on": ["a"], "plugin_step_ref": None}]}),
        "unknown_requirement_id": lambda b: b["task_graph"]["tasks"][0].update({"requirement_ids": ["req-missing"]}),
        "requirements_not_covered": lambda b: b["task_graph"]["tasks"][0].update({"requirement_ids": ["req-a"]}),
        "duplicate_resource_ref": lambda b: b.update({"requirement_refs": [b["requirement_refs"][0],
                                                                          dict(b["requirement_refs"][0])]}),
        "graph_mode_only_allows_templates": lambda b: b["contract_drafts"][0].update(
            {"completeness": "complete",
             "baseline": {"state": "bound", "binding_ref": _ref("baseline", "base-1")}}),
        "unbound_contains_sha": lambda b: b["contract_drafts"][0]["baseline"].update(
            {"reason": "hashed as " + _digest("baseline")}),
    }
    for reason, mutate in cases.items():
        broken = copy.deepcopy(valid)
        mutate(broken)
        with pytest.raises(ProtocolError) as ctx:
            validate_planning_bundle(broken)
        assert ctx.value.reason == reason
        # The unmodified fixture must still validate, so no other rule fired.
        validate_planning_bundle(copy.deepcopy(valid))

    with pytest.raises(ProtocolError) as ctx:
        validate_planning_bundle({**copy.deepcopy(valid), "contract_drafts": [
            _draft(), {**_draft(), "task_id": "task-9"}]})
    assert ctx.value.reason == "draft_task_not_in_graph"
    assert ctx.value.code == "E_CONTEXT_BINDING"


def test_planning_binding_requires_matching_request_context_and_bundle():
    request, context, bundle = _planner_request(), _context_manifest(), _planning_bundle()
    bound = validate_planning_binding(copy.deepcopy(request), copy.deepcopy(context),
                                      copy.deepcopy(bundle))
    assert bound["bundle"]["request_id"] == "req-1"

    for mutate, code, reason in (
        (lambda r, c, b: b.update({"request_id": "req-2"}), "E_CONTEXT_BINDING", "request_bundle_id_mismatch"),
        (lambda r, c, b: b.update({"investigation_baseline_ref": _ref("baseline", "base-2")}),
         "E_CONTEXT_BINDING", "request_bundle_baseline_mismatch"),
        (lambda r, c, b: (r.update({"mode": "refine", "target_task_id": "task-1",
                                   "graph_ref": _ref("graph", "graph-1")}),
                           b.update({"mode": "graph"})),
         "E_CONTEXT_BINDING", "request_bundle_mode_mismatch"),
        (lambda r, c, b: (r.update({"mode": "refine", "target_task_id": "task-9",
                                   "graph_ref": _ref("graph", "graph-1")}),
                           b.update({"mode": "refine", "target_task_id": "task-1",
                                     "graph_ref": _ref("graph", "graph-1")})),
         "E_CONTEXT_BINDING", "request_bundle_target_mismatch"),
        (lambda r, c, b: (r.update({"requirements_ref": _ref("requirements", "req-c")}),
                           c.update({"resources": [_ref("requirements", "req-c") if ref["id"] == "req-a"
                                                  else ref for ref in c["resources"]]})),
         "E_CONTEXT_BINDING", "request_requirements_not_in_bundle"),
        (lambda r, c, b: c.update({"resources": [ref for ref in c["resources"]
                                                 if ref["id"] != "auth-1"]}),
         "E_CONTEXT_BINDING", "request_ref_missing_from_context"),
    ):
        req, ctx_, bnd = copy.deepcopy(request), copy.deepcopy(context), copy.deepcopy(bundle)
        mutate(req, ctx_, bnd)
        with pytest.raises(ProtocolError) as ctx:
            validate_planning_binding(req, ctx_, bnd)
        assert (ctx.value.code, ctx.value.reason) == (code, reason)

    validate_planning_binding(copy.deepcopy(request), copy.deepcopy(context))


def test_contract_ir_is_one_source_for_both_contract_members():
    ir = ContractIR.from_mapping(_contract_ir())
    first_md, first_json = render_contract_ir(ir)
    second_md, second_json = render_contract_ir(ir)
    assert first_md == second_md and first_json == second_json
    assert json.loads(first_json.decode("utf-8")) == ir.to_mapping()
    assert b"# TASK: pi-acceptance" in first_md
    assert b"allowed_new_files" in first_md and b"tests/test_pi_acceptance.py" in first_md
    assert first_json == canonical_json(ir.to_mapping())
    assert first_json.endswith(b"\n") and first_json.count(b"\n") == 1


def test_resource_refs_reject_mutable_aliases_and_open_shapes():
    ref = validate_resource_ref({"kind": "baseline", "id": "base-1", "path": "inputs/b.json",
                                 "sha256": _digest("b")})
    assert ref["id"] == "base-1"
    for bad, code in (({"kind": "baseline", "id": "latest", "path": "inputs/b.json",
                        "sha256": _digest("b")}, "E_RESOURCE_FIELD"),
                      ({"kind": "baseline", "id": "base-1", "path": "/abs/b.json",
                        "sha256": _digest("b")}, "E_PATH_SYNTAX"),
                      ({"kind": "baseline", "id": "base-1", "path": "inputs/b.json",
                        "sha256": _digest("b"), "extra": 1}, "E_RESOURCE_FIELD")):
        with pytest.raises(PlannerError, match=code):
            validate_resource_ref(bad)
    with pytest.raises(PlannerError, match="E_RESOURCE_HASH"):
        make_resource_ref("baseline", "base-1", "inputs/b.json")


def test_contract_candidate_requires_frozen_ir_bytes():
    ir = ContractIR.from_mapping(_contract_ir())
    md_bytes, json_bytes = render_contract_ir(ir)
    drafts = DraftStore()
    revision = drafts.create_draft("draft-1", ir.to_mapping())
    source = make_resource_ref("planner-input", "source", "inputs/source.json", raw=b"source")
    target = make_resource_ref("contract-target", "target", "contracts/target.json", raw=b"target")
    good = build_candidate(candidate_id="candidate-1", draft=revision, source_ref=source,
                           target_ref=target, md_bytes=md_bytes, json_bytes=json_bytes)
    assert PublicationStore().register_candidate(good).candidate_id == "candidate-1"
    assert good.members[0]["sha256"] == _digest(md_bytes.decode("utf-8"))
    assert good.members[1]["sha256"] == _digest(json_bytes.decode("utf-8"))

    for mutate, code in (
        (lambda kw: kw.update({"md_bytes": md_bytes + b"tampered"}), "E_CANDIDATE_BYTES"),
        (lambda kw: kw.update({"json_bytes": b"{}"}), "E_CANDIDATE_BYTES"),
        (lambda kw: kw.update({"json_bytes": render_contract_ir(
            ContractIR.from_mapping({**_contract_ir(), "title": "Different title"}))[1]}),
         "E_CANDIDATE_BYTES"),
        (lambda kw: kw.update({"target_ref": dict(source)}), "E_CANDIDATE_BINDING"),
        (lambda kw: kw.update({"input_refs": [dict(source), dict(source)]}), "E_CANDIDATE_BINDING"),
    ):
        kwargs = {"candidate_id": "candidate-1", "draft": revision, "source_ref": dict(source),
                  "target_ref": dict(target), "md_bytes": md_bytes, "json_bytes": json_bytes}
        mutate(kwargs)
        with pytest.raises(PlannerError, match=code):
            build_candidate(**kwargs)

    # Bytes frozen before review: mutating the in-memory member after allocation
    # must be rejected instead of being published.
    tampered = build_candidate(candidate_id="candidate-2", draft=revision, source_ref=source,
                               target_ref=target, md_bytes=md_bytes, json_bytes=json_bytes)
    tampered.members[0]["bytes"] = b"rewritten after allocation"
    with pytest.raises(PlannerError, match="E_CANDIDATE_MUTATED"):
        PublicationStore().register_candidate(tampered)


def test_publication_idempotency_requires_an_identical_reviewed_action():
    store = PublicationStore()
    candidate = _candidate()
    review = {"decision": "APPROVED", "approval_id": "approval-1"}
    receipt = store.publish(candidate, review_ref=dict(review), idempotency_key="publish-1")
    assert store.publish(candidate, review_ref=dict(review),
                         idempotency_key="publish-1") == receipt
    assert store.outbox[0]["payload"] == receipt.to_mapping()

    with pytest.raises(PlannerError, match="E_IDEMPOTENCY_CONFLICT"):
        store.publish(candidate, review_ref={"decision": "APPROVED", "approval_id": "approval-2"},
                      idempotency_key="publish-1")
    with pytest.raises(PlannerError, match="E_IDEMPOTENCY_CONFLICT"):
        store.publish(candidate, review_ref={"decision": "APPROVED", "approval_id": "approval-1",
                                             "approval_ref": {"scope": "contract", "generation": 1,
                                                              "sha256": _digest("contract")}},
                      idempotency_key="publish-1")
    with pytest.raises(PlannerError, match="E_REVIEW_BINDING"):
        store.publish(candidate, review_ref={"decision": "APPROVED", "approval_id": "approval-1",
                                             "candidate_id": "other-candidate"},
                      idempotency_key="publish-1")
    with pytest.raises(PlannerError, match="E_REVIEW_BINDING"):
        store.publish(candidate, review_ref={"decision": "PENDING", "approval_id": "approval-1"},
                      idempotency_key="publish-2")
    with pytest.raises(PlannerError, match="E_CAS_CONFLICT"):
        store.publish(candidate, review_ref=dict(review), idempotency_key="publish-3",
                      expected_generation=7)
    with pytest.raises(PlannerError, match="E_VERSION_REUSED"):
        store.publish(candidate, review_ref=dict(review), idempotency_key="publish-4",
                      expected_generation=receipt.generation)
    assert store.recover(receipt.publication_id) == receipt
    assert store.bytes_for(receipt.publication_id, "json") == candidate.members[1]["bytes"]


def test_draft_store_cas_and_version_slots_are_fail_closed():
    drafts = DraftStore()
    first = drafts.create_draft("draft-1", _contract_ir())
    assert drafts.head("draft-1").revision == 1
    with pytest.raises(PlannerError, match="E_CAS_REQUIRED"):
        drafts.create_draft("draft-1", _contract_ir())
    with pytest.raises(PlannerError, match="E_CAS_CONFLICT"):
        drafts.revise("draft-1", _contract_ir(), expected_head=9)
    with pytest.raises(PlannerError, match="E_DRAFT_MISSING"):
        drafts.get("draft-1", 5)
    with pytest.raises(PlannerError, match="E_DRAFT_BINDING"):
        drafts.create_draft("draft-1", {**_contract_ir(), "task_id": "other-task"}, expected_head=1)

    # A plan_version slot is allocated at most once per task; reusing it is rejected.
    bumped = {**_contract_ir(), "plan_version": 3}
    drafts.revise("draft-1", bumped, expected_head=1)
    with pytest.raises(PlannerError, match="E_VERSION_REUSED"):
        drafts.revise("draft-1", _contract_ir(), expected_head=2)
    # Retaining the current slot is an idempotent revision, not a reuse.
    assert drafts.revise("draft-1", bumped, expected_head=2).revision == 3


# --------------------------------------------------------------------------
# Built-in workflow invariants
# --------------------------------------------------------------------------

RECIPE_ORDER = {
    "bugfix": ["analyze", "change", "freeze", "checks", "review"],
    "documentation": ["analyze", "docs-change", "freeze", "docs-checks", "review"],
    "regression": ["analyze", "tests", "mutation", "review"],
}
ALL_CAPABILITIES = {"read_repo", "write_scope", "run_checks"}
RECIPE_PLUGINS = {
    "bugfix": {"repo-analysis", "code-change", "python-checks", "regression-tests", "mutation-review"},
    "documentation": {"repo-analysis", "docs-change", "docs-checks", "python-checks",
                     "regression-tests", "mutation-review"},
    "regression": {"repo-analysis", "python-checks", "regression-tests", "mutation-review"},
}


def test_builtin_workflows_keep_dag_stage_order_and_capability_lock():
    for name, order in RECIPE_ORDER.items():
        definition = builtin_workflow(name)
        assert definition.workflow_id == name and definition.workflow_version == 1
        assert definition.validate()
        assert [node["id"] for node in definition.nodes] == order
        for node in definition.nodes:
            assert [stage["kind"] for stage in node["stages"]] == ["execute", "freeze", "check", "verdict"]
            deps = [stage["id"] for stage in node["stages"] if stage["id"] in
                    [d for stage in node["stages"] for d in stage["depends_on"]]]
            assert deps
        # Declared node dependencies only point backwards, so the recipe stays acyclic.
        seen = set()
        for node in definition.nodes:
            assert set(node["depends_on"]) <= seen
            seen.add(node["id"])
        expected_digest = hashlib.sha256(
            (json.dumps(definition.to_mapping(), ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")) + "\n").encode("utf-8")).hexdigest()
        assert definition.digest == expected_digest
        assert builtin_workflow(name).digest == definition.digest

        catalog = builtin_catalog()
        lock = lock_for_workflow(name, granted_capabilities=set(ALL_CAPABILITIES))
        assert set(lock.ordered_plugins) == RECIPE_PLUGINS[name]
        assert lock.capabilities <= ALL_CAPABILITIES
        for plugin_id in lock.ordered_plugins:
            assert catalog[plugin_id].required_capabilities <= lock.capabilities
        assert lock.digest == lock_for_workflow(name, granted_capabilities=set(ALL_CAPABILITIES)).digest


def test_workflow_capability_lock_denies_missing_capabilities():
    assert lock_for_workflow("bugfix", granted_capabilities=set(ALL_CAPABILITIES))
    for caps in ({"read_repo", "write_scope"}, {"read_repo", "run_checks"},
                 {"write_scope", "run_checks"}):
        with pytest.raises(WorkflowError, match="E_PLUGIN_LOCK"):
            lock_for_workflow("bugfix", granted_capabilities=caps)
    with pytest.raises(WorkflowError, match="E_WORKFLOW_UNKNOWN"):
        builtin_workflow("unknown")
    with pytest.raises(WorkflowError, match="E_WORKFLOW_VERSION"):
        builtin_workflow("bugfix", version=0)
    with pytest.raises(WorkflowError, match="E_CAPABILITIES"):
        lock_for_workflow("bugfix", granted_capabilities=["read_repo", "write_scope", "run_checks"])


# --------------------------------------------------------------------------
# Production connector fail-closed gates
# --------------------------------------------------------------------------

def test_production_connector_gate_stays_blocked_for_missing_or_synthetic_evidence():
    stages = ("03", "04", "05", "06", "09", "10")

    missing = evaluate_production_connectors(None)
    assert missing.decision == PRODUCTION_BLOCKED
    assert missing.reasons == tuple("CONNECTOR_%s_MISSING" % s for s in stages)

    synthetic = _offline_connectors()
    synthetic["09"] = {"status": "synthetic", "production": False, "available": True,
                       "connector_id": "09-synthetic"}
    decision = evaluate_production_connectors(synthetic)
    assert decision.decision == PRODUCTION_BLOCKED
    assert "CONNECTOR_09_OFFLINE_ONLY" in decision.reasons
    assert decision.evidence["connectors"]["09"]["status"] == "synthetic"

    # ``available`` alone never upgrades a connector to production.
    available = evaluate_production_connectors(
        {s: {"status": "verified", "production": False, "available": True,
             "connector_id": s + "-connector"} for s in stages})
    assert available.decision == PRODUCTION_BLOCKED
    assert all(r.endswith("_OFFLINE_ONLY") for r in available.reasons)

    # A production claim without evidence or with an alias identity stays blocked.
    unevidenced = {s: dict(_production_connectors()[s], evidence_ref=None) for s in stages}
    assert evaluate_production_connectors(unevidenced).decision == PRODUCTION_BLOCKED
    aliased = evaluate_production_connectors(
        {**_offline_connectors(), "03": {"status": "verified", "production": True, "available": True,
                                         "connector_id": "latest", "evidence_ref":
                                             {"kind": "connector-evidence", "id": "03",
                                              "sha256": _digest("03")}}})
    assert "CONNECTOR_03_IDENTITY_INVALID" in aliased.reasons

    # Only a closed production declaration with immutable evidence can pass, and
    # the gate itself documents that this is internal consistency, not a real
    # production connector (no CLI, credential or network was touched here).
    complete = evaluate_production_connectors(_production_connectors())
    assert complete.decision == PASS
    assert complete.evidence["connectors"]["03"]["evidence_ref"]["sha256"] == _digest("03")

    with pytest.raises(ValueError):
        evaluate_production_connectors(_offline_connectors(), required=())
    with pytest.raises(ValueError):
        evaluate_production_connectors(_offline_connectors(), required=["03", "03"])


def test_contract_candidate_commit_and_wiring_gates_fail_closed():
    contract = _contract_ir()
    expected = hashlib.sha256(canonical_json(contract)).hexdigest()

    assert verify_contract_hash(contract, None).decision == WAIT
    assert verify_contract_hash(contract, _digest("other")).decision == BLOCKED
    matched = verify_contract_hash(contract, expected,
                                   declared_ref={"kind": "contract-json", "id": "candidate-1-json",
                                                 "sha256": expected})
    assert matched.decision == PASS and matched.reasons == ()
    aliased = verify_contract_hash(contract, expected,
                                   declared_ref={"kind": "contract-json", "id": "latest",
                                                 "sha256": expected})
    assert aliased.decision == BLOCKED
    assert aliased.reasons == ("E_REFERENCE_ALIAS",)
    mismatched = verify_contract_hash(contract, expected,
                                      declared_ref={"kind": "contract-json", "id": "candidate-1-json",
                                                    "sha256": _digest("declared")})
    assert mismatched.decision == BLOCKED
    assert mismatched.reasons == ("CONTRACT_REF_HASH_MISMATCH",)

    expected_ref = {"candidate_ref": {"kind": "candidate", "id": "candidate-1",
                                      "sha256": _digest("candidate"), "path": "candidates/cand.json"},
                    "tree_sha": "a" * 40, "patch_sha256": _digest("patch")}
    actual = {"candidate_ref": dict(expected_ref["candidate_ref"]), "tree_sha": "a" * 40,
              "patch_sha256": _digest("patch"), "roundtrip_verified": True}
    assert verify_fixed_candidate(expected_ref, dict(actual)).decision == PASS
    moved = dict(actual, candidate_ref={**expected_ref["candidate_ref"], "path": "other/cand.json"})
    assert verify_fixed_candidate(expected_ref, moved).decision == BLOCKED
    assert verify_fixed_candidate(expected_ref, dict(actual, roundtrip_verified=False)).decision == BLOCKED
    assert verify_fixed_candidate(expected_ref, {k: v for k, v in actual.items()
                                                 if k != "roundtrip_verified"}).decision == WAIT
    assert verify_fixed_candidate(expected_ref, dict(actual, tree_sha="b" * 40)).decision == BLOCKED
    with pytest.raises(ValueError):
        verify_fixed_candidate({}, dict(actual))

    unknown_status = evaluate_commit_permission(has_uncommitted_changes=None)
    assert unknown_status.decision == WAIT and unknown_status.reasons == ("WORKTREE_STATUS_UNKNOWN",)
    assert evaluate_commit_permission(has_uncommitted_changes=False).decision == PASS
    assert evaluate_commit_permission(has_uncommitted_changes=True,
                                      integration_actions=("local_edit",)).decision == BLOCKED
    pending = evaluate_commit_permission(has_uncommitted_changes=True,
                                         integration_actions=("local_commit",))
    assert pending.decision == WAIT and pending.reasons == ("COMMIT_EVIDENCE_UNKNOWN",)
    committed = evaluate_commit_permission(has_uncommitted_changes=True,
                                           integration_actions=("local_commit",),
                                           commit_sha=expected)
    assert committed.decision == PASS and committed.reasons == ()

    state, brief = _wiring_state(), _wiring_brief()
    assert verify_09_10_wiring(readiness=READINESS, state=dict(state),
                               brief=dict(brief)).decision == OFFLINE_WIRED
    # 10 is a read model: a brief can never become an execution permit.
    dispatch = verify_09_10_wiring(readiness=READINESS, state=dict(state),
                                   brief=dict(brief, dispatch_eligibility="eligible"))
    assert dispatch.decision == BLOCKED
    assert "BRIEF_DISPATCH_FIELD_NOT_NEUTRAL" in dispatch.reasons
    assert verify_09_10_wiring(readiness=READINESS, state=dict(state),
                               brief=dict(brief, health="degraded")).decision == BLOCKED
    assert "STATE_HEALTH_CONTRADICTION" in verify_09_10_wiring(
        readiness=READINESS, state=dict(state, source_coverage="partial"),
        brief=dict(brief)).reasons
    assert "STATE_CURSOR_ORDER_INVALID" in verify_09_10_wiring(
        readiness=READINESS, state=dict(state, applied_cursor=9), brief=dict(brief)).reasons


def test_composite_gate_keeps_offline_and_production_decisions_separate():
    contract = _contract_ir()
    expected = hashlib.sha256(canonical_json(contract)).hexdigest()
    candidate_ref = {"candidate_ref": {"kind": "candidate", "id": "candidate-1",
                                       "sha256": _digest("candidate"),
                                       "path": "candidates/cand.json"}}
    candidate = {"candidate_ref": dict(candidate_ref["candidate_ref"]), "roundtrip_verified": True}
    common = dict(contract=contract, expected_contract_sha256=expected,
                  candidate_ref=candidate_ref, candidate=candidate,
                  has_uncommitted_changes=False, integration_actions=(),
                  readiness=READINESS, state=_wiring_state(), brief=_wiring_brief())

    offline = evaluate_integration_gates(connectors=None, **common)
    assert offline.decision == PRODUCTION_BLOCKED
    assert offline.evidence["production_decision"] == PRODUCTION_BLOCKED
    assert offline.evidence["wiring"]["offline_only"] is True
    assert offline.evidence["commit"]["actions"] == ()
    connector_reasons = [r for r in offline.reasons if r.startswith("CONNECTOR_")]
    assert connector_reasons == ["CONNECTOR_%s_MISSING" % s for s in ("03", "04", "05", "06", "09", "10")]
    assert set(offline.reasons) - set(connector_reasons) == {"NO_COMMIT_REQUIRED"}

    synthetic = evaluate_integration_gates(connectors=_offline_connectors(), **common)
    assert synthetic.decision == PRODUCTION_BLOCKED
    assert all(r.endswith("_OFFLINE_ONLY") for r in synthetic.reasons if r.startswith("CONNECTOR_"))

    wired = evaluate_integration_gates(connectors=_production_connectors(), **common)
    assert wired.decision == PASS
    assert wired.reasons == ("NO_COMMIT_REQUIRED",)

    # A single wrong field anywhere keeps the composite fail-closed.
    broken = evaluate_integration_gates(connectors=_production_connectors(),
                                        **{**common, "expected_contract_sha256": _digest("other")})
    assert broken.decision == BLOCKED and "CONTRACT_HASH_MISMATCH" in broken.reasons
