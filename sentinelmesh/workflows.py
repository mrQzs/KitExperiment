"""Trusted built-in plugin catalog and workflow recipes.

The Code-Kit catalog is intentionally declarative.  This module provides the
same portable reference boundary for KitExperiment: plugin code is never
loaded dynamically, workflow definitions are validated before scheduling and
the resulting lock is content addressed.  The recipes only describe work;
they do not grant approval or start a CLI.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .core import canonical_json, sha256_bytes
from .plugins import PluginLock, PluginManifest, PluginError, resolve_plugins
from .scheduler import ContractError, validate_workflow


class WorkflowError(ValueError):
    """Stable error at the built-in catalog/workflow boundary."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")


_PLUGIN_SPECS: tuple[dict[str, Any], ...] = (
    {"id": "repo-analysis", "dependencies": [], "required_capabilities": ["read_repo"]},
    {"id": "code-change", "dependencies": ["repo-analysis"], "required_capabilities": ["write_scope"]},
    {"id": "docs-change", "dependencies": ["repo-analysis"], "required_capabilities": ["write_scope"]},
    {"id": "python-checks", "dependencies": [], "required_capabilities": ["run_checks"]},
    {"id": "docs-checks", "dependencies": [], "required_capabilities": ["run_checks"]},
    {"id": "regression-tests", "dependencies": ["python-checks"], "required_capabilities": ["run_checks"]},
    {"id": "mutation-review", "dependencies": ["regression-tests"], "required_capabilities": ["read_repo"]},
)


def _manifest(spec: Mapping[str, Any]) -> PluginManifest:
    value = {"id": spec["id"], "version": "1.0.0", "api_version": 1,
             "dependencies": list(spec.get("dependencies", ())), "resources": {},
             "required_capabilities": list(spec.get("required_capabilities", ())) }
    try:
        return PluginManifest.from_dict(value)
    except PluginError as exc:  # pragma: no cover - catalog is checked at import
        raise WorkflowError("E_CATALOG", str(exc)) from exc


BUILTIN_MANIFESTS: Mapping[str, PluginManifest] = {
    spec["id"]: _manifest(spec) for spec in _PLUGIN_SPECS
}


@dataclass(frozen=True)
class WorkflowDefinition:
    """A validated, immutable workflow graph."""

    workflow_id: str
    workflow_version: int
    nodes: tuple[Mapping[str, Any], ...]

    def to_mapping(self) -> dict[str, Any]:
        return {"workflow_id": self.workflow_id, "workflow_version": self.workflow_version,
                "nodes": [dict(node) for node in self.nodes]}

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json(self.to_mapping()))

    def validate(self) -> tuple[str, ...]:
        try:
            return tuple(validate_workflow(self.to_mapping()))
        except ContractError as exc:
            raise WorkflowError(exc.code, exc.detail) from exc


def _node(node_id: str, plugin: str, depends_on: Sequence[str]) -> dict[str, Any]:
    stages = [
        {"id": "execute", "kind": "execute", "depends_on": [], "plugin": plugin},
        {"id": "freeze", "kind": "freeze", "depends_on": ["execute"], "plugin": plugin},
        {"id": "check", "kind": "check", "depends_on": ["freeze"], "plugin": plugin},
        {"id": "verdict", "kind": "verdict", "depends_on": ["check"], "plugin": plugin},
    ]
    return {"id": node_id, "depends_on": list(depends_on), "inputs": {}, "input_bindings": {},
            "outputs": {"result": {"schema_ref": {"kind": "result", "id": f"{node_id}-result", "sha256": "0" * 64}, "stage_id": "check"}},
            "write_scope": [], "stages": stages, "retry_policy": {"max_attempts_total": 1}}


_RECIPES: Mapping[str, tuple[tuple[str, str, tuple[str, ...]], ...]] = {
    "bugfix": (("analyze", "repo-analysis", ()), ("change", "code-change", ("analyze",)),
                ("freeze", "repo-analysis", ("change",)), ("checks", "python-checks", ("freeze",)),
                ("review", "mutation-review", ("checks",))),
    "documentation": (("analyze", "repo-analysis", ()), ("docs-change", "docs-change", ("analyze",)),
                       ("freeze", "repo-analysis", ("docs-change",)), ("docs-checks", "docs-checks", ("freeze",)),
                       ("review", "mutation-review", ("docs-checks",))),
    "regression": (("analyze", "repo-analysis", ()), ("tests", "regression-tests", ("analyze",)),
                    ("mutation", "mutation-review", ("tests",)), ("review", "mutation-review", ("mutation",))),
}


def builtin_workflow(workflow_id: str, *, version: int = 1) -> WorkflowDefinition:
    if type(workflow_id) is not str or workflow_id not in _RECIPES:
        raise WorkflowError("E_WORKFLOW_UNKNOWN")
    if type(version) is not int or version < 1:
        raise WorkflowError("E_WORKFLOW_VERSION")
    definition = WorkflowDefinition(workflow_id, version,
                                    tuple(_node(node, plugin, deps) for node, plugin, deps in _RECIPES[workflow_id]))
    definition.validate()
    return definition


def builtin_catalog() -> Mapping[str, PluginManifest]:
    """Return a defensive copy of the trusted built-in manifests."""
    return dict(BUILTIN_MANIFESTS)


def lock_for_workflow(workflow_id: str, *, granted_capabilities: set[str]) -> PluginLock:
    if type(granted_capabilities) is not set:
        raise WorkflowError("E_CAPABILITIES")
    definition = builtin_workflow(workflow_id)
    requested = list(dict.fromkeys(node["stages"][0]["plugin"] for node in definition.nodes))
    try:
        return resolve_plugins(builtin_catalog(), requested, granted_capabilities=granted_capabilities)
    except PluginError as exc:
        raise WorkflowError("E_PLUGIN_LOCK", str(exc)) from exc


__all__ = ["WorkflowError", "WorkflowDefinition", "BUILTIN_MANIFESTS", "builtin_catalog",
           "builtin_workflow", "lock_for_workflow"]
