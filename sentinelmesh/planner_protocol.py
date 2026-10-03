#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline protocol checks for the 09 planning/contract boundary.

This module deliberately has no I/O and no model/provider dependency.  It validates
the records emitted by a planner before the core is allowed to persist a draft.  A
planner can suggest facts, graphs and contract templates; it cannot manufacture
identity, a baseline, approval or an executable contract.
"""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Iterable, Mapping

from .protocols import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_DEPTH,
    MAX_SAFE_INTEGER,
    ProtocolError,
    check_json_value,
    strict_json_loads,
    validate_business_path,
)

PLANNER_SCHEMA_VERSION = 1
MAX_TASKS = 128
MAX_STAGES = 64
MAX_FACTS = 256
MAX_EVIDENCE = 32
MAX_TEXT = 16_384
MAX_REFS = 256
_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_KIND = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_HEX_SHA_IN_TEXT = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_ALLOWED_REF = frozenset(("kind", "id", "sha256", "path"))


def _pointer(base: str, name: str | int) -> str:
    token = str(name).replace("~", "~0").replace("/", "~1")
    return f"{base}/{token}" if base else f"/{token}"


def _fail(field: str, reason: str, code: str = "E_SCHEMA_FIELDS") -> None:
    raise ProtocolError(code, field=field or None, phase="planner", reason=reason)


def _obj(value: Any, field: str) -> dict[str, Any]:
    if type(value) is not dict:
        _fail(field, "object_required", "E_SCHEMA_TYPE")
    return value


def _array(value: Any, field: str, maximum: int | None = None) -> list[Any]:
    if type(value) is not list:
        _fail(field, "array_required", "E_SCHEMA_TYPE")
    if maximum is not None and len(value) > maximum:
        _fail(field, "too_many_items")
    return value


def _unique_scalars(value: Any, field: str, maximum: int | None = None,
                    minimum: int = 0) -> list[Any]:
    """Validate a scalar array with the JSON Schema ``uniqueItems`` rule."""
    result = _array(value, field, maximum)
    if len(result) < minimum:
        _fail(field, "too_few_items")
    seen: list[Any] = []
    for index, item in enumerate(result):
        if any(item == prior for prior in seen):
            _fail(_pointer(field, index), "duplicate_item")
        seen.append(item)
    return result


def _closed(obj: Mapping[str, Any], required: Iterable[str], optional: Iterable[str] = (), field: str = "") -> None:
    required = tuple(required)
    optional = frozenset(optional)
    for name in required:
        if name not in obj:
            _fail(_pointer(field, name), "missing_field")
    allowed = set(required) | set(optional)
    for name in obj:
        if type(name) is not str or name not in allowed:
            _fail(_pointer(field, name), "unknown_field")


def _str(value: Any, field: str, *, minimum: int = 1, maximum: int = MAX_TEXT) -> str:
    if type(value) is not str:
        _fail(field, "string_required", "E_SCHEMA_TYPE")
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        _fail(field, "unpaired_surrogate", "E_JSON_UNICODE")
    if not minimum <= len(value) <= maximum:
        _fail(field, "string_length_out_of_range")
    return value


def _id(value: Any, field: str) -> str:
    value = _str(value, field, maximum=64)
    if _ID.fullmatch(value) is None:
        _fail(field, "identifier_syntax", "E_ID_SYNTAX")
    return value


def _integer(value: Any, field: str, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    if type(value) is not int:
        _fail(field, "exact_integer_required", "E_SCHEMA_TYPE")
    if not minimum <= value <= maximum:
        _fail(field, "integer_out_of_range", "E_INTEGER_RANGE")
    return value


def _enum(value: Any, field: str, allowed: Iterable[str]) -> str:
    value = _str(value, field)
    if value not in allowed:
        _fail(field, "enum_value_not_allowed")
    return value


def validate_resource_ref(ref: Any, *, field: str = "/ref") -> dict[str, Any]:
    """Validate the closed, portable ResourceRef shape without trusting a registry."""
    ref = _obj(ref, field)
    _closed(ref, ("kind", "id", "sha256", "path"), field=field)
    if _KIND.fullmatch(ref["kind"] if type(ref["kind"]) is str else "") is None:
        _fail(_pointer(field, "kind"), "resource_kind_syntax", "E_RESOURCE_FIELD")
    if _RESOURCE_ID.fullmatch(ref["id"] if type(ref["id"]) is str else "") is None:
        _fail(_pointer(field, "id"), "resource_id_syntax", "E_RESOURCE_FIELD")
    if _SHA.fullmatch(ref["sha256"] if type(ref["sha256"]) is str else "") is None:
        _fail(_pointer(field, "sha256"), "sha256_syntax", "E_RESOURCE_FIELD")
    validate_business_path(ref["path"], field=_pointer(field, "path"))
    return copy.deepcopy(ref)


def validate_source_anchor(anchor: Any, *, field: str = "/anchor", source_bytes: bytes | None = None) -> dict[str, Any]:
    anchor = _obj(anchor, field)
    _closed(anchor, ("ref", "start_byte", "end_byte", "quote_sha256"), field=field)
    ref = validate_resource_ref(anchor["ref"], field=_pointer(field, "ref"))
    start = _integer(anchor["start_byte"], _pointer(field, "start_byte"))
    end = _integer(anchor["end_byte"], _pointer(field, "end_byte"), minimum=1)
    if end <= start:
        _fail(_pointer(field, "end_byte"), "half_open_range_must_be_nonempty")
    if _SHA.fullmatch(anchor["quote_sha256"] if type(anchor["quote_sha256"]) is str else "") is None:
        _fail(_pointer(field, "quote_sha256"), "sha256_syntax", "E_RESOURCE_FIELD")
    if source_bytes is not None:
        if type(source_bytes) is not bytes:
            _fail(field, "source_bytes_required", "E_RESOURCE_BYTES")
        if end > len(source_bytes):
            _fail(field, "range_out_of_source_bounds", "E_RESOURCE_HASH_MISMATCH")
        if hashlib.sha256(source_bytes).hexdigest() != ref["sha256"]:
            _fail(_pointer(field, "ref"), "source_digest_mismatch", "E_RESOURCE_HASH_MISMATCH")
        if hashlib.sha256(source_bytes[start:end]).hexdigest() != anchor["quote_sha256"]:
            _fail(_pointer(field, "quote_sha256"), "quote_digest_mismatch", "E_RESOURCE_HASH_MISMATCH")
    return {"ref": ref, "start_byte": start, "end_byte": end, "quote_sha256": anchor["quote_sha256"]}


def strict_planner_json_loads(raw: bytes, *, max_bytes: int = DEFAULT_MAX_BYTES,
                              max_depth: int = DEFAULT_MAX_DEPTH) -> dict[str, Any]:
    """Strictly decode one planner JSON document and require an object root."""
    value = strict_json_loads(raw, max_bytes=max_bytes, max_depth=max_depth)
    if type(value) is not dict:
        _fail("", "planner_record_must_be_object", "E_SCHEMA_TYPE")
    return value


def _header(value: dict[str, Any], record_type: str, field: str = "") -> None:
    if "record_type" not in value:
        _fail(_pointer(field, "record_type"), "missing_field")
    if "schema_version" not in value:
        _fail(_pointer(field, "schema_version"), "missing_field")
    if value.get("record_type") != record_type:
        _fail(_pointer(field, "record_type"), "record_type_mismatch", "E_SCHEMA_RECORD_TYPE")
    _integer(value.get("schema_version"), _pointer(field, "schema_version"), minimum=1, maximum=MAX_SAFE_INTEGER)
    if value["schema_version"] != PLANNER_SCHEMA_VERSION:
        _fail(_pointer(field, "schema_version"), "unsupported_schema_version", "E_SCHEMA_VERSION_UNSUPPORTED")


def _refs(value: Any, field: str, *, minimum: int = 0, maximum: int = MAX_REFS) -> list[dict[str, Any]]:
    result = _array(value, field, maximum)
    if len(result) < minimum:
        _fail(field, "too_few_items")
    seen: set[tuple[Any, ...]] = set()
    out = []
    for index, ref in enumerate(result):
        item = validate_resource_ref(ref, field=_pointer(field, index))
        # A logical reference may not occur twice with a different digest/path;
        # otherwise requirement coverage and provenance would be ambiguous.
        key = (item["kind"], item["id"])
        if key in seen:
            _fail(_pointer(field, index), "duplicate_resource_ref")
        seen.add(key)
        out.append(item)
    return out


_PLANNER_REQUIRED = (
    "record_type", "schema_version", "request_id", "run_id", "operation_id", "mode", "target_task_id",
    "requirements_ref", "context_manifest_ref", "graph_ref", "baseline_ref", "planner_config_ref",
    "environment_ref", "catalog_ref", "rules_refs", "decision_refs", "output_contract_ref",
    "planning_budget_ref", "authorization_refs", "repair_policy_ref", "stop_policy_ref",
)


def validate_planner_request(value: Any) -> dict[str, Any]:
    value = _obj(value, "")
    _closed(value, _PLANNER_REQUIRED)
    _header(value, "planner-request")
    _id(value["request_id"], "/request_id")
    _id(value["run_id"], "/run_id")
    _id(value["operation_id"], "/operation_id")
    mode = _enum(value["mode"], "/mode", ("graph", "refine"))
    if mode == "graph" and value["target_task_id"] is not None:
        _fail("/target_task_id", "graph_target_must_be_null")
    if mode == "refine" and value["target_task_id"] is None:
        _fail("/target_task_id", "refine_target_required")
    if value["target_task_id"] is not None:
        _id(value["target_task_id"], "/target_task_id")
    for name in ("requirements_ref", "context_manifest_ref", "baseline_ref", "planner_config_ref",
                 "environment_ref", "catalog_ref", "output_contract_ref", "planning_budget_ref",
                 "repair_policy_ref", "stop_policy_ref"):
        if value[name] is None:
            _fail(_pointer("", name), "resource_ref_required", "E_RESOURCE_FIELD")
        validate_resource_ref(value[name], field=_pointer("", name))
    if mode == "graph" and value["graph_ref"] is not None:
        _fail("/graph_ref", "graph_request_graph_ref_must_be_null")
    if mode == "refine" and value["graph_ref"] is None:
        _fail("/graph_ref", "refine_graph_ref_required")
    _refs(value["rules_refs"], "/rules_refs", maximum=MAX_REFS)
    _refs(value["decision_refs"], "/decision_refs", maximum=MAX_REFS)
    _refs(value["authorization_refs"], "/authorization_refs", maximum=MAX_REFS)
    return copy.deepcopy(value)


def validate_context_manifest(value: Any) -> dict[str, Any]:
    """Validate a closed context manifest.  Unknown resource discovery is rejected."""
    value = _obj(value, "")
    required = ("record_type", "schema_version", "request_id", "resources")
    _closed(value, required, optional=("baseline_ref", "requirements_ref", "environment_ref", "rules_refs", "decision_refs"))
    _header(value, "context-manifest")
    _id(value["request_id"], "/request_id")
    _refs(value["resources"], "/resources", minimum=1, maximum=512)
    for name in ("baseline_ref", "requirements_ref", "environment_ref"):
        if name in value and value[name] is not None:
            validate_resource_ref(value[name], field=_pointer("", name))
    for name in ("rules_refs", "decision_refs"):
        if name in value:
            _refs(value[name], _pointer("", name), maximum=MAX_REFS)
    return copy.deepcopy(value)


def _validate_evidence(item: Any, field: str) -> dict[str, Any]:
    item = _obj(item, field)
    _closed(item, ("ref", "start_byte", "end_byte", "quote_sha256"), field=field)
    return validate_source_anchor(item, field=field)


def _validate_fact(item: Any, field: str) -> dict[str, Any]:
    item = _obj(item, field)
    _closed(item, ("id", "text", "evidence_class", "evidence"), field=field)
    _id(item["id"], _pointer(field, "id")); _str(item["text"], _pointer(field, "text"))
    _enum(item["evidence_class"], _pointer(field, "evidence_class"), ("source_observation", "runtime_observation"))
    evidence = _array(item["evidence"], _pointer(field, "evidence"), MAX_EVIDENCE)
    if not evidence:
        _fail(_pointer(field, "evidence"), "fact_requires_evidence")
    return {**copy.deepcopy(item), "evidence": [_validate_evidence(v, _pointer(field, "evidence") + f"/{i}") for i, v in enumerate(evidence)]}


def _validate_task_graph(graph: Any, requirement_ids: set[str]) -> dict[str, Any]:
    graph = _obj(graph, "/task_graph")
    _closed(graph, ("tasks",), field="/task_graph")
    tasks = _array(graph["tasks"], "/task_graph/tasks", MAX_TASKS)
    if not tasks:
        _fail("/task_graph/tasks", "task_graph_must_not_be_empty")
    ids: set[str] = set(); normalized: list[dict[str, Any]] = []
    for i, task in enumerate(tasks):
        f = f"/task_graph/tasks/{i}"; task = _obj(task, f)
        req = ("id", "title", "depends_on", "requirement_ids", "scope_paths", "acceptance_intent", "stages", "estimated_calls")
        _closed(task, req, field=f); tid = _id(task["id"], _pointer(f, "id"))
        if tid in ids: _fail(_pointer(f, "id"), "duplicate_task_id")
        ids.add(tid)
        _str(task["title"], _pointer(f, "title")); deps = _array(task["depends_on"], _pointer(f, "depends_on"), MAX_TASKS)
        deps = _unique_scalars(deps, _pointer(f, "depends_on"), MAX_TASKS)
        dep_ids = [_id(v, _pointer(f"{f}/depends_on", j)) for j, v in enumerate(deps)]
        if tid in dep_ids: _fail(_pointer(f, "depends_on"), "self_dependency")
        reqs = _unique_scalars(task["requirement_ids"], _pointer(f, "requirement_ids"), MAX_REFS, 1)
        if not reqs: _fail(_pointer(f, "requirement_ids"), "task_requires_requirement")
        req_ids = [_id(v, _pointer(f"{f}/requirement_ids", j)) for j, v in enumerate(reqs)]
        unknown_req = [v for v in req_ids if v not in requirement_ids]
        if unknown_req: _fail(_pointer(f, "requirement_ids"), "unknown_requirement_id")
        scopes = _unique_scalars(task["scope_paths"], _pointer(f, "scope_paths"), MAX_TASKS, 1)
        if not scopes: _fail(_pointer(f, "scope_paths"), "scope_required")
        for j, path in enumerate(scopes): validate_business_path(path, field=f"{f}/scope_paths/{j}")
        intents = _array(task["acceptance_intent"], _pointer(f, "acceptance_intent"), MAX_EVIDENCE)
        if not intents: _fail(_pointer(f, "acceptance_intent"), "acceptance_intent_required")
        for j, text in enumerate(intents): _str(text, f"{f}/acceptance_intent/{j}")
        stages = _array(task["stages"], _pointer(f, "stages"), MAX_STAGES)
        if not stages: _fail(_pointer(f, "stages"), "stages_required")
        stage_ids: set[str] = set(); normalized_stages = []
        for j, stage in enumerate(stages):
            sf = f"{f}/stages/{j}"; stage = _obj(stage, sf)
            _closed(stage, ("id", "kind", "depends_on", "plugin_step_ref"), field=sf)
            sid = _id(stage["id"], f"{sf}/id")
            if sid in stage_ids: _fail(f"{sf}/id", "duplicate_stage_id")
            stage_ids.add(sid); _enum(stage["kind"], f"{sf}/kind", ("investigate", "change", "freeze", "check", "verdict"))
            sdeps = _unique_scalars(stage["depends_on"], f"{sf}/depends_on", MAX_STAGES)
            for k, dep in enumerate(sdeps):
                dep = _id(dep, f"{sf}/depends_on/{k}")
                if dep == sid: _fail(f"{sf}/depends_on", "self_dependency")
            if stage["plugin_step_ref"] is not None: validate_resource_ref(stage["plugin_step_ref"], field=f"{sf}/plugin_step_ref")
            normalized_stages.append(copy.deepcopy(stage))
        _assert_acyclic([(s["id"], s["depends_on"]) for s in normalized_stages], f"{f}/stages")
        _integer(task["estimated_calls"], _pointer(f, "estimated_calls"), 0, 10000)
        normalized.append({**copy.deepcopy(task), "id": tid, "depends_on": dep_ids, "requirement_ids": req_ids, "stages": normalized_stages})
    _assert_acyclic([(t["id"], t["depends_on"]) for t in normalized], "/task_graph/tasks")
    covered = {r for task in normalized for r in task["requirement_ids"]}
    if requirement_ids - covered: _fail("/task_graph/tasks", "requirements_not_covered")
    return {"tasks": normalized}


def _assert_acyclic(edges: Iterable[tuple[str, list[str]]], field: str) -> None:
    graph = {node: deps for node, deps in edges}; visiting: set[str] = set(); done: set[str] = set()
    def visit(node: str) -> None:
        if node in visiting: _fail(field, "dependency_cycle")
        if node in done: return
        visiting.add(node)
        for dep in graph.get(node, []):
            if dep not in graph: _fail(field, "dependency_endpoint_missing")
            visit(dep)
        visiting.remove(node); done.add(node)
    for node in graph: visit(node)


def _validate_draft(draft: Any, field: str) -> dict[str, Any]:
    draft = _obj(draft, field)
    req = ("task_id", "completeness", "baseline", "scope_paths", "checks_ref", "executor_profile_ref", "environment_ref", "plugin_lock_ref", "narrative")
    _closed(draft, req, field=field); task_id = _id(draft["task_id"], f"{field}/task_id")
    completeness = _enum(draft["completeness"], f"{field}/completeness", ("template", "complete"))
    base = _obj(draft["baseline"], f"{field}/baseline"); state = base.get("state")
    if state == "unbound":
        _closed(base, ("state", "reason"), field=f"{field}/baseline"); _str(base["reason"], f"{field}/baseline/reason")
        if _HEX_SHA_IN_TEXT.search(base["reason"]): _fail(f"{field}/baseline/reason", "unbound_contains_sha")
        if completeness == "complete": _fail(f"{field}/completeness", "complete_draft_requires_bound_baseline")
    elif state == "bound":
        _closed(base, ("state", "binding_ref"), field=f"{field}/baseline"); validate_resource_ref(base["binding_ref"], field=f"{field}/baseline/binding_ref")
    else: _fail(f"{field}/baseline/state", "baseline_state_invalid")
    scopes = _unique_scalars(draft["scope_paths"], f"{field}/scope_paths", MAX_TASKS, 1)
    for i, path in enumerate(scopes): validate_business_path(path, field=f"{field}/scope_paths/{i}")
    for name in ("checks_ref", "executor_profile_ref", "environment_ref", "plugin_lock_ref"):
        if draft[name] is not None: validate_resource_ref(draft[name], field=f"{field}/{name}")
    narrative = _obj(draft["narrative"], f"{field}/narrative"); _closed(narrative, ("goal", "non_goals", "behavior", "acceptance"), field=f"{field}/narrative")
    _str(narrative["goal"], f"{field}/narrative/goal")
    for name, minimum, maximum in (("non_goals", 0, 64), ("behavior", 1, 64), ("acceptance", 1, 64)):
        arr = _array(narrative[name], f"{field}/narrative/{name}", maximum)
        if len(arr) < minimum: _fail(f"{field}/narrative/{name}", "too_few_items")
        for i, text in enumerate(arr): _str(text, f"{field}/narrative/{name}/{i}")
    return copy.deepcopy(draft)


def validate_planning_bundle(value: Any) -> dict[str, Any]:
    value = _obj(value, "")
    required = ("record_type", "schema_version", "request_id", "planner_request_ref", "mode", "graph_ref", "target_task_id", "investigation_baseline_ref", "requirement_refs", "facts", "assumptions", "questions", "risks", "task_graph", "contract_drafts", "budget_proposal", "stop_conditions")
    _closed(value, required); _header(value, "planning-bundle")
    _id(value["request_id"], "/request_id"); validate_resource_ref(value["planner_request_ref"], field="/planner_request_ref")
    mode = _enum(value["mode"], "/mode", ("graph", "refine"))
    if mode == "graph" and (value["graph_ref"] is not None or value["target_task_id"] is not None): _fail("/graph_ref", "graph_mode_refs_must_be_null")
    if mode == "refine" and (value["graph_ref"] is None or value["target_task_id"] is None): _fail("/graph_ref", "refine_mode_refs_required")
    if value["graph_ref"] is not None: validate_resource_ref(value["graph_ref"], field="/graph_ref")
    if value["target_task_id"] is not None: _id(value["target_task_id"], "/target_task_id")
    validate_resource_ref(value["investigation_baseline_ref"], field="/investigation_baseline_ref")
    refs = _refs(value["requirement_refs"], "/requirement_refs", minimum=1)
    facts = _array(value["facts"], "/facts", MAX_FACTS); facts_out = [_validate_fact(f, f"/facts/{i}") for i, f in enumerate(facts)]
    # Assumptions/questions/risks share a deliberately strict, closed shape.
    ids = {"assumptions": set(), "questions": set(), "risks": set()}
    for name, maximum in (("assumptions", 128), ("questions", 128), ("risks", 128)):
        arr = _array(value[name], f"/{name}", maximum); seen = ids[name]
        for i, item in enumerate(arr):
            f = f"/{name}/{i}"; item = _obj(item, f)
            if name == "assumptions":
                _closed(item, ("id", "text", "task_ids", "state", "decision_ref"), field=f); _str(item["text"], f + "/text"); _enum(item["state"], f + "/state", ("unconfirmed", "confirmed", "rejected"))
            elif name == "questions":
                _closed(item, ("id", "text", "task_ids", "blocks", "state", "decision_ref"), field=f); _str(item["text"], f + "/text"); _enum(item["state"], f + "/state", ("open", "resolved")); blocks = _unique_scalars(item["blocks"], f + "/blocks", 3, 1)
                if not blocks: _fail(f + "/blocks", "blocks_required")
                for j, block in enumerate(blocks): _enum(block, f + f"/blocks/{j}", ("graph_review", "refine", "publish"))
            else:
                _closed(item, ("id", "description", "task_ids", "impact", "mitigation", "decision_ref"), field=f); _str(item["description"], f + "/description"); _str(item["mitigation"], f + "/mitigation"); _enum(item["impact"], f + "/impact", ("low", "medium", "high", "unknown"))
            iid = _id(item["id"], f + "/id")
            if iid in seen: _fail(f + "/id", "duplicate_entry_id")
            seen.add(iid)
            tids = _unique_scalars(item["task_ids"], f + "/task_ids", MAX_TASKS)
            for j, tid in enumerate(tids): _id(tid, f + "/task_ids/{j}")
            if item["decision_ref"] is not None: validate_resource_ref(item["decision_ref"], field=f + "/decision_ref")
    req_ids = {r["id"] for r in refs}; graph = _validate_task_graph(value["task_graph"], req_ids)
    graph_task_ids = {task["id"] for task in graph["tasks"]}
    if len({fact["id"] for fact in facts_out}) != len(facts_out):
        _fail("/facts", "duplicate_context_entry_id")
    all_context_ids: set[str] = {fact["id"] for fact in facts_out}
    for name in ("assumptions", "questions", "risks"):
        for i, item in enumerate(value[name]):
            item_id = item["id"]
            if item_id in all_context_ids:
                _fail(f"/{name}/{i}/id", "duplicate_context_entry_id")
            all_context_ids.add(item_id)
            for j, task_id in enumerate(item["task_ids"]):
                if task_id not in graph_task_ids:
                    _fail(f"/{name}/{i}/task_ids/{j}", "unknown_task_id", "E_CONTEXT_BINDING")
    drafts = _array(value["contract_drafts"], "/contract_drafts", MAX_TASKS)
    if not drafts:
        _fail("/contract_drafts", "contract_draft_required")
    drafts_out = [_validate_draft(d, f"/contract_drafts/{i}") for i, d in enumerate(drafts)]
    draft_task_ids = [draft["task_id"] for draft in drafts_out]
    if len(draft_task_ids) != len(set(draft_task_ids)):
        _fail("/contract_drafts", "duplicate_draft_task_id")
    if any(task_id not in graph_task_ids for task_id in draft_task_ids):
        _fail("/contract_drafts", "draft_task_not_in_graph", "E_CONTEXT_BINDING")
    if mode == "graph" and any(d["completeness"] != "template" for d in drafts_out): _fail("/contract_drafts", "graph_mode_only_allows_templates")
    if mode == "refine":
        matches = [d for d in drafts_out if d["task_id"] == value["target_task_id"]]
        if len(matches) != 1: _fail("/contract_drafts", "refine_requires_one_target_draft")
    budget = _obj(value["budget_proposal"], "/budget_proposal"); _closed(budget, ("max_calls", "max_seconds", "max_repair_calls"), field="/budget_proposal")
    _integer(budget["max_calls"], "/budget_proposal/max_calls", 1); _integer(budget["max_seconds"], "/budget_proposal/max_seconds", 1); _integer(budget["max_repair_calls"], "/budget_proposal/max_repair_calls", 0, 100)
    stops = _unique_scalars(value["stop_conditions"], "/stop_conditions", 6, 1)
    for i, stop in enumerate(stops): _enum(stop, f"/stop_conditions/{i}", ("budget_exhausted", "repair_limit", "no_baseline", "decision_required", "authorization_missing", "protocol_incomplete"))
    return {**copy.deepcopy(value), "facts": facts_out, "task_graph": graph, "contract_drafts": drafts_out}


def _same_ref(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in ("kind", "id", "sha256", "path"))


def validate_planning_binding(request: Any, context: Any, bundle: Any | None = None) -> dict[str, Any]:
    """Validate cross-record request/context/bundle identity and input closure."""
    normalized_request = validate_planner_request(request)
    normalized_context = validate_context_manifest(context)
    if normalized_request["request_id"] != normalized_context["request_id"]:
        _fail("/request_id", "request_context_id_mismatch", "E_CONTEXT_BINDING")
    resources = normalized_context["resources"]
    required_refs = [normalized_request[name] for name in
                     ("requirements_ref", "baseline_ref", "planner_config_ref", "environment_ref",
                      "catalog_ref", "output_contract_ref", "planning_budget_ref",
                      "repair_policy_ref", "stop_policy_ref")]
    required_refs.extend(normalized_request["rules_refs"])
    required_refs.extend(normalized_request["decision_refs"])
    required_refs.extend(normalized_request["authorization_refs"])
    for ref in required_refs:
        if not any(_same_ref(ref, candidate) for candidate in resources):
            _fail("/resources", "request_ref_missing_from_context", "E_CONTEXT_BINDING")
    if bundle is None:
        return {"request": normalized_request, "context": normalized_context}
    normalized_bundle = validate_planning_bundle(bundle)
    if normalized_bundle["request_id"] != normalized_request["request_id"]:
        _fail("/request_id", "request_bundle_id_mismatch", "E_CONTEXT_BINDING")
    if normalized_bundle["mode"] != normalized_request["mode"]:
        _fail("/mode", "request_bundle_mode_mismatch", "E_CONTEXT_BINDING")
    if normalized_bundle["target_task_id"] != normalized_request["target_task_id"]:
        _fail("/target_task_id", "request_bundle_target_mismatch", "E_CONTEXT_BINDING")
    if not any(_same_ref(normalized_request["requirements_ref"], ref)
               for ref in normalized_bundle["requirement_refs"]):
        _fail("/requirement_refs", "request_requirements_not_in_bundle", "E_CONTEXT_BINDING")
    if not _same_ref(normalized_request["baseline_ref"], normalized_bundle["investigation_baseline_ref"]):
        _fail("/investigation_baseline_ref", "request_bundle_baseline_mismatch", "E_CONTEXT_BINDING")
    if normalized_request["graph_ref"] is not None and not _same_ref(
            normalized_request["graph_ref"], normalized_bundle["graph_ref"]):
        _fail("/graph_ref", "request_bundle_graph_mismatch", "E_CONTEXT_BINDING")
    return {"request": normalized_request, "context": normalized_context, "bundle": normalized_bundle}


def validate_bundle(value: Any) -> dict[str, Any]:
    """Return a stable diagnostic report instead of raising for a bundle."""
    try:
        validate_planning_bundle(value)
    except ProtocolError as exc:
        return {"record_type": "bundle-validation-report", "schema_version": 1, "valid": False,
                "semantic_support_verified": False, "errors": [{"code": exc.code, "field": exc.field, "reason": exc.reason}]}
    return {"record_type": "bundle-validation-report", "schema_version": 1, "valid": True,
            "semantic_support_verified": False, "errors": []}


def validate_planning_json(raw: bytes) -> dict[str, Any]:
    return validate_planning_bundle(strict_planner_json_loads(raw))


__all__ = [
    "PLANNER_SCHEMA_VERSION", "PlannerProtocolError", "strict_planner_json_loads", "validate_resource_ref",
    "validate_source_anchor", "validate_planner_request", "validate_context_manifest", "validate_planning_bundle",
    "validate_planning_binding",
    "validate_bundle", "validate_planning_json",
]

# Compatibility alias used by early 09 callers; errors remain ProtocolError with stable codes.
PlannerProtocolError = ProtocolError
