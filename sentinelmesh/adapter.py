"""Adapter descriptors and vendor-event normalization boundaries."""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping

from .core import ProtocolError, strict_loads
from .config import Profile
from .runtime import Invocation


class AdapterError(ProtocolError):
    pass


@dataclass(frozen=True)
class CapabilityReport:
    adapter_id: str
    revision: str
    roles: frozenset[str]
    platforms: frozenset[str]
    supports_usage: bool
    supports_cancel: bool
    source: str = "synthetic"

    def __post_init__(self) -> None:
        if not isinstance(self.adapter_id, str) or not self.adapter_id.strip():
            raise AdapterError("E_CAPABILITY_SHAPE")
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise AdapterError("E_CAPABILITY_SHAPE")
        if not isinstance(self.source, str) or not self.source.strip() or "\x00" in self.source:
            raise AdapterError("E_CAPABILITY_SHAPE")
        if not isinstance(self.roles, frozenset) or any(
            not isinstance(role, str) or not role for role in self.roles
        ):
            raise AdapterError("E_CAPABILITY_SHAPE")
        if not isinstance(self.platforms, frozenset) or any(
            not isinstance(platform, str) or not platform for platform in self.platforms
        ):
            raise AdapterError("E_CAPABILITY_SHAPE")
        if type(self.supports_usage) is not bool or type(self.supports_cancel) is not bool:
            raise AdapterError("E_CAPABILITY_SHAPE")

    def require(self, *, role: str, platform: str) -> None:
        if not isinstance(role, str) or not isinstance(platform, str):
            raise AdapterError("E_CAPABILITY_MISSING")
        if role not in self.roles or platform not in self.platforms:
            raise AdapterError("E_CAPABILITY_MISSING")


@dataclass(frozen=True)
class AdapterRequest:
    request_id: str
    profile_id: str
    role: str
    platform: str
    cwd: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        for value, code in (
            (self.request_id, "E_REQUEST_ID"),
            (self.profile_id, "E_PROFILE_ID"),
            (self.role, "E_ROLE"),
            (self.platform, "E_PLATFORM"),
            (self.cwd, "E_CWD"),
        ):
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise AdapterError(code)
        if not os.path.isdir(self.cwd):
            raise AdapterError("E_CWD")
        args = self.args
        if isinstance(args, (str, bytes)):
            raise AdapterError("E_ARGS")
        if not isinstance(args, tuple):
            try:
                args = tuple(args)
            except TypeError as exc:
                raise AdapterError("E_ARGS") from exc
            object.__setattr__(self, "args", args)
        if any(not isinstance(arg, str) or "\x00" in arg for arg in args):
            raise AdapterError("E_ARGS")
        env = {} if self.env is None else self.env
        if not isinstance(env, Mapping):
            raise AdapterError("E_ENV")
        normalized: dict[str, str] = {}
        for key, value in env.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or "\x00" in key
                or "\x00" in value
            ):
                raise AdapterError("E_ENV")
            normalized[key] = value
        object.__setattr__(self, "env", normalized)


@dataclass(frozen=True)
class NormalizedRecord:
    type: str
    payload: Mapping[str, Any]
    source_line: int
    source_kind: str = "vendor"

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type.strip() or len(self.type) > 96:
            raise AdapterError("E_VENDOR_EVENT_SHAPE")
        if not isinstance(self.payload, Mapping):
            raise AdapterError("E_VENDOR_EVENT_SHAPE")
        if type(self.source_line) is not int or self.source_line < 1:
            raise AdapterError("E_VENDOR_EVENT_SHAPE")
        if not isinstance(self.source_kind, str) or not self.source_kind.strip():
            raise AdapterError("E_VENDOR_EVENT_SHAPE")


class Adapter:
    descriptor: CapabilityReport

    def build_invocation(self, profile: Profile, request: AdapterRequest) -> Invocation:
        raise NotImplementedError

    def normalize(self, raw: bytes) -> tuple[NormalizedRecord, ...]:
        if type(raw) is not bytes:
            raise AdapterError("E_VENDOR_INPUT")
        records: list[NormalizedRecord] = []
        for line_no, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            value = strict_loads(line)
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("type"), str)
                or not value["type"].strip()
                or len(value["type"]) > 96
                or "payload" not in value
                or not isinstance(value["payload"], dict)
            ):
                raise AdapterError("E_VENDOR_EVENT_SHAPE")
            records.append(NormalizedRecord(value["type"], value.get("payload", {}), line_no))
        return tuple(records)


class PiAdapter(Adapter):
    descriptor = CapabilityReport("pi", "pi-v1", frozenset({"planner", "executor", "observer"}), frozenset({"linux", "macos", "windows"}), True, True)

    def build_invocation(self, profile: Profile, request: AdapterRequest) -> Invocation:
        self.descriptor.require(role=request.role, platform=request.platform)
        if profile.role != request.role or profile.adapter != self.descriptor.adapter_id:
            raise AdapterError("E_PROFILE_ADAPTER_MISMATCH")
        return Invocation(("pi",) + profile.fixed_args + request.args, request.cwd, dict(request.env or {}), profile.timeout_seconds)


class ClaudeCodeAdapter(Adapter):
    descriptor = CapabilityReport("claude-code", "claude-code-v1", frozenset({"planner", "executor"}), frozenset({"linux", "macos", "windows"}), True, True)

    def build_invocation(self, profile: Profile, request: AdapterRequest) -> Invocation:
        self.descriptor.require(role=request.role, platform=request.platform)
        if profile.role != request.role or profile.adapter != self.descriptor.adapter_id:
            raise AdapterError("E_PROFILE_ADAPTER_MISMATCH")
        return Invocation(("claude",) + profile.fixed_args + request.args, request.cwd, dict(request.env or {}), profile.timeout_seconds)


class AdapterRegistry:
    def __init__(self, adapters: Mapping[str, Adapter] | None = None):
        self._adapters = dict(adapters or {"pi": PiAdapter(), "claude-code": ClaudeCodeAdapter()})

    def descriptor(self, adapter_id: str) -> CapabilityReport:
        try:
            return self._adapters[adapter_id].descriptor
        except KeyError as exc:
            raise AdapterError("E_ADAPTER_UNREGISTERED") from exc

    def adapter(self, adapter_id: str) -> Adapter:
        try:
            return self._adapters[adapter_id]
        except KeyError as exc:
            raise AdapterError("E_ADAPTER_UNREGISTERED") from exc

    def probe(self, adapter_id: str, *, source: str = "synthetic") -> CapabilityReport:
        descriptor = self.descriptor(adapter_id)
        return CapabilityReport(descriptor.adapter_id, descriptor.revision, descriptor.roles, descriptor.platforms, descriptor.supports_usage, descriptor.supports_cancel, source)
