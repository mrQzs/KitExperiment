import hashlib
import json

import pytest

from sentinelmesh.planner import (ContractIR, DraftStore, PlannerError, PublicationStore,
                                   build_candidate, make_resource_ref, render_contract_ir)
from sentinelmesh.planner_protocol import strict_planner_json_loads, validate_bundle, validate_planning_bundle
from sentinelmesh.protocols import ProtocolError
from sentinelmesh.workflows import WorkflowError, builtin_workflow, lock_for_workflow


def _ir():
    return {
        "task_id": "offline-contract", "plan_version": 1, "title": "Offline contract",
        "repository": "KitExperiment", "base_sha": "a" * 40, "risk_level": "low",
        "authorization": {"executor": ["local_test"], "integration": []},
        "executor": {"provider": "offline", "model": "stub", "thinking": "high",
                      "tools": ["python"], "context_files": "project"},
        "scope": {"allowed_modified_files": [], "allowed_new_files": ["tests/fixture.json"],
                  "allowed_deleted_files": [], "forbidden_paths": ["AGENTS.md"],
                  "dependency_changes": "forbidden"},
        "setup": [], "checks": [{"id": "V01", "argv": ["python", "-m", "compileall", "-q", "sentinelmesh"],
                                  "cwd": ".", "timeout_seconds": 60, "expected_exit_code": 0, "required": True}],
        "definition_of_done": [{"id": "D01", "text": "rendered", "evidence": ["V01"]}],
        "budget": {"max_attempts_total": 1, "max_wall_seconds_per_attempt": 60,
                   "max_wall_seconds_total": 60, "cost_budget": None},
    }


def test_planning_bundle_rejects_duplicate_and_nonfinite_json():
    for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1} {"b":2}'):
        with pytest.raises(ProtocolError):
            strict_planner_json_loads(raw)


def test_contract_ir_is_single_source_for_json_and_markdown_and_cas():
    ir = ContractIR.from_mapping(_ir())
    markdown, machine = render_contract_ir(ir)
    assert b"offline-contract" in markdown
    assert json.loads(machine)["task_id"] == "offline-contract"
    drafts = DraftStore()
    first = drafts.create_draft("draft-1", ir.to_mapping())
    drafts.revise("draft-1", ir.to_mapping(), expected_head=first.revision)
    with pytest.raises(PlannerError, match="E_CAS_CONFLICT"):
        drafts.revise("draft-1", ir.to_mapping(), expected_head=1)
    candidate = build_candidate(
        candidate_id="candidate-1", draft=drafts.head("draft-1"),
        source_ref=make_resource_ref("planner-input", "source", "inputs/source.json", raw=b"source"),
        target_ref=make_resource_ref("contract-target", "target", "contracts/target.json", raw=b"target"),
        md_bytes=markdown, json_bytes=machine)
    store = PublicationStore()
    receipt = store.publish(candidate, review_ref={"decision": "APPROVED", "approval_id": "approval-1"},
                            idempotency_key="publish-1")
    assert store.publish(candidate, review_ref={"decision": "APPROVED", "approval_id": "approval-1"},
                         idempotency_key="publish-1") == receipt
    with pytest.raises(PlannerError, match="E_IDEMPOTENCY_CONFLICT"):
        store.publish(candidate, review_ref={"decision": "APPROVED", "approval_id": "approval-2"},
                      idempotency_key="publish-1")


def test_builtin_workflows_validate_and_capabilities_are_explicit():
    for name in ("bugfix", "documentation", "regression"):
        workflow = builtin_workflow(name)
        assert workflow.validate()
        lock = lock_for_workflow(name, granted_capabilities={"read_repo", "write_scope", "run_checks"})
        assert lock.digest and lock.ordered_plugins
    with pytest.raises(WorkflowError):
        lock_for_workflow("bugfix", granted_capabilities={"read_repo"})
