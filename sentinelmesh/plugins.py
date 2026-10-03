"""Declarative plugin manifests and deterministic content locks."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from .core import canonical_json, sha256_bytes


class PluginError(ValueError):
    pass


_ID_RE = re.compile(r"[a-z][a-z0-9._:-]{0,127}\Z")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}\Z")


def _safe_path(path: str) -> bool:
    if not isinstance(path, str) or not path or path.startswith(("/", "\\")) or ":" in path:
        return False
    parts = path.replace("\\", "/").split("/")
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{n}" for prefix in ("COM", "LPT") for n in range(1, 10)}
    return all(
        part not in ("", ".", "..")
        and "\x00" not in part
        and not any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF for char in part)
        and not part.endswith((".", " "))
        and part.split(".", 1)[0].upper() not in reserved
        for part in parts
    )


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    version: str
    api_version: int
    dependencies: tuple[str, ...]
    resources: Mapping[str, str]
    required_capabilities: frozenset[str]
    entrypoint: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PluginManifest":
        if not isinstance(value, Mapping):
            raise PluginError("E_PLUGIN_REQUIRED")
        allowed = {"id", "version", "api_version", "dependencies", "resources", "required_capabilities", "entrypoint"}
        unknown = set(value) - allowed
        if unknown:
            raise PluginError(f"E_PLUGIN_UNKNOWN_FIELD: {sorted(unknown)}")
        pid, version = value.get("id"), value.get("version")
        if not isinstance(pid, str) or _ID_RE.fullmatch(pid) is None or not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None:
            raise PluginError("E_PLUGIN_REQUIRED")
        api = value.get("api_version")
        if api != 1:
            raise PluginError("E_PLUGIN_API_VERSION")
        deps = value.get("dependencies", [])
        if not isinstance(deps, list) or any(not isinstance(x, str) or _ID_RE.fullmatch(x) is None for x in deps) or len(set(deps)) != len(deps):
            raise PluginError("E_PLUGIN_DEPENDENCIES")
        resources = value.get("resources", {})
        if not isinstance(resources, dict) or any(not _safe_path(k) or not isinstance(v, str) or re.fullmatch(r"[0-9a-f]{64}", v) is None for k, v in resources.items()):
            raise PluginError("E_PLUGIN_RESOURCES")
        caps = value.get("required_capabilities", [])
        if not isinstance(caps, list) or any(not isinstance(x, str) or not x or any(ord(char) < 0x20 or ord(char) == 0x7F for char in x) for x in caps) or len(set(caps)) != len(caps):
            raise PluginError("E_PLUGIN_CAPABILITIES")
        entrypoint = value.get("entrypoint")
        if entrypoint is not None:
            raise PluginError("E_PLUGIN_EXECUTABLE_ENTRYPOINT")
        return cls(pid, version, 1, tuple(deps), dict(resources), frozenset(caps), None)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.plugin_id, "version": self.version, "api_version": self.api_version, "dependencies": list(self.dependencies), "resources": dict(sorted(self.resources.items())), "required_capabilities": sorted(self.required_capabilities)}


@dataclass(frozen=True)
class PluginLock:
    lock_id: str
    ordered_plugins: tuple[str, ...]
    manifests: tuple[Mapping[str, Any], ...]
    resources: Mapping[str, str]
    capabilities: frozenset[str]
    digest: str


def resolve_plugins(manifests: Mapping[str, PluginManifest], requested: list[str], *, granted_capabilities: set[str] | None = None) -> PluginLock:
    if not isinstance(manifests, Mapping) or not isinstance(requested, list):
        raise PluginError("E_PLUGIN_REQUEST")
    if any(not isinstance(pid, str) or _ID_RE.fullmatch(pid) is None for pid in requested) or len(set(requested)) != len(requested):
        raise PluginError("E_PLUGIN_REQUEST")
    if granted_capabilities is not None and (
        not isinstance(granted_capabilities, set)
        or any(not isinstance(cap, str) or not cap for cap in granted_capabilities)
    ):
        raise PluginError("E_PLUGIN_CAPABILITIES")
    for key, manifest in manifests.items():
        if not isinstance(key, str) or not isinstance(manifest, PluginManifest) or key != manifest.plugin_id:
            raise PluginError("E_PLUGIN_MANIFEST")
    granted = set(granted_capabilities or set())
    visited: set[str] = set()
    active: set[str] = set()
    order: list[str] = []

    def visit(pid: str) -> None:
        if pid in active:
            raise PluginError(f"E_PLUGIN_CYCLE: {pid}")
        if pid in visited:
            return
        if pid not in manifests:
            raise PluginError(f"E_PLUGIN_DEPENDENCY_MISSING: {pid}")
        active.add(pid)
        manifest = manifests[pid]
        for dep in sorted(manifest.dependencies):
            visit(dep)
        active.remove(pid)
        visited.add(pid)
        order.append(pid)

    for pid in sorted(requested):
        visit(pid)
    selected = [manifests[pid] for pid in order]
    needs = set().union(*(m.required_capabilities for m in selected)) if selected else set()
    if not needs.issubset(granted):
        raise PluginError(f"E_PLUGIN_CAPABILITY_DENIED: {sorted(needs - granted)}")
    resources: dict[str, str] = {}
    for m in selected:
        for path, digest in sorted(m.resources.items()):
            if path in resources and resources[path] != digest:
                raise PluginError(f"E_PLUGIN_RESOURCE_CONFLICT: {path}")
            resources[path] = digest
    payload = {"plugins": [m.as_dict() for m in selected], "resources": dict(sorted(resources.items())), "capabilities": sorted(needs)}
    return PluginLock("plock-" + sha256_bytes(canonical_json(payload))[:24], tuple(order), tuple(m.as_dict() for m in selected), resources, frozenset(needs), sha256_bytes(canonical_json(payload)))
