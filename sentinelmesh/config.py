"""Configuration and role binding for the SentinelMesh prototype."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

try:
    from .core import ProtocolError, canonical_json, sha256_bytes, strict_loads
except ImportError:  # pragma: no cover
    ProtocolError = ValueError
    import json, hashlib
    def strict_loads(data: bytes | str, **_: Any) -> Any:
        return json.loads(data)
    def canonical_json(value: Any) -> bytes:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    def sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


class ConfigError(ProtocolError):
    """Configuration failed a structural or semantic guard."""


@dataclass(frozen=True)
class Profile:
    profile_id: str
    adapter: str
    model: str
    role: str
    fixed_args: tuple[str, ...] = ()
    allowed_tools: frozenset[str] = frozenset()
    allowed_paths: tuple[str, ...] = ()
    shell: str | None = None
    timeout_seconds: float = 300.0
    budget_tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "adapter": self.adapter,
            "model": self.model,
            "role": self.role,
            "fixed_args": list(self.fixed_args),
            "allowed_tools": sorted(self.allowed_tools),
            "allowed_paths": list(self.allowed_paths),
            "shell": self.shell,
            "timeout_seconds": self.timeout_seconds,
            "budget_tokens": self.budget_tokens,
        }


@dataclass(frozen=True)
class EffectiveConfig:
    profile: Profile
    source_refs: tuple[str, ...]
    semantic_hash: str
    environment_plan: Mapping[str, Any] = field(default_factory=dict)


class ConfigResolver:
    """Pure resolver: it never probes, downloads, starts or reads credentials."""

    def __init__(self, registry: Mapping[str, Mapping[str, Any]], *, registered_adapters: set[str] | None = None):
        self._registry = dict(registry)
        self._registered_adapters = set(registered_adapters or {"pi", "claude-code"})

    @staticmethod
    def _as_string_list(value: Any, field_name: str) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list) or any(
            not isinstance(x, str) or not x or any(
                ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF
                for char in x
            )
            for x in value
        ):
            raise ConfigError(f"E_CONFIG_FIELD: {field_name} must be a list of non-empty strings")
        return tuple(value)

    def resolve(self, profile_id: str, *, role: str | None = None, local_override: Mapping[str, Any] | None = None) -> EffectiveConfig:
        if not isinstance(profile_id, str) or not profile_id or any(
            ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF
            for char in profile_id
        ):
            raise ConfigError("E_PROFILE_UNKNOWN")
        if profile_id not in self._registry or not isinstance(self._registry[profile_id], Mapping):
            raise ConfigError(f"E_PROFILE_UNKNOWN: {profile_id}")
        raw = dict(self._registry[profile_id])
        allowed_profile_fields = {
            "profile_id", "adapter", "model", "role", "fixed_args", "allowed_tools",
            "allowed_paths", "shell", "timeout_seconds", "budget_tokens", "executable",
            "command_locator",
        }
        unknown_profile_fields = set(raw) - allowed_profile_fields
        if unknown_profile_fields:
            raise ConfigError(f"E_CONFIG_UNKNOWN_FIELD: {sorted(unknown_profile_fields)}")
        raw.setdefault("profile_id", profile_id)
        if role is not None and raw.get("role") != role:
            raise ConfigError("E_PROFILE_ROLE_MISMATCH")
        allowed_override = {"executable", "command_locator", "shell"}
        if local_override is not None and not isinstance(local_override, Mapping):
            raise ConfigError("E_CONFIG_OVERRIDE_SHAPE")
        override = dict(local_override or {})
        unknown = set(override) - allowed_override
        if unknown:
            raise ConfigError(f"E_CONFIG_OVERRIDE_FORBIDDEN: {sorted(unknown)}")
        # local override is recorded as source metadata; it cannot replace model, permissions or fixed args.
        for key in ("model", "adapter", "fixed_args", "allowed_tools", "allowed_paths", "budget_tokens"):
            if key in override:
                raise ConfigError(f"E_CONFIG_OVERRIDE_FORBIDDEN: {key}")
        adapter = raw.get("adapter")
        if not isinstance(adapter, str) or adapter not in self._registered_adapters:
            raise ConfigError("E_ADAPTER_UNREGISTERED")
        model = raw.get("model")
        role_value = raw.get("role")
        if (
            not isinstance(model, str) or not model or not isinstance(role_value, str) or not role_value
            or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF for char in model)
            or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF for char in role_value)
        ):
            raise ConfigError("E_CONFIG_REQUIRED")
        timeout = raw.get("timeout_seconds", 300.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ConfigError("E_CONFIG_TIMEOUT")
        allowed_tools = self._as_string_list(raw.get("allowed_tools"), "allowed_tools")
        allowed_paths = self._as_string_list(raw.get("allowed_paths"), "allowed_paths")
        fixed_args = self._as_string_list(raw.get("fixed_args"), "fixed_args")
        if not isinstance(raw.get("budget_tokens", None), (int, type(None))) or isinstance(raw.get("budget_tokens"), bool):
            raise ConfigError("E_CONFIG_BUDGET")
        if raw.get("budget_tokens") is not None and raw["budget_tokens"] < 0:
            raise ConfigError("E_CONFIG_BUDGET")
        if "shell" in override:
            shell = override["shell"]
            if shell is not None and (
                not isinstance(shell, str) or not shell.strip() or any(
                    ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF
                    for char in shell
                )
            ):
                raise ConfigError("E_CONFIG_SHELL")
            raw["shell"] = shell
        shell = raw.get("shell")
        if shell is not None and (
            not isinstance(shell, str) or not shell.strip() or any(
                ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or 0xD800 <= ord(char) <= 0xDFFF
                for char in shell
            )
        ):
            raise ConfigError("E_CONFIG_SHELL")
        for key in ("executable", "command_locator"):
            if key in raw and raw[key] is not None and (
                not isinstance(raw[key], str) or not raw[key].strip() or "\x00" in raw[key]
            ):
                raise ConfigError("E_CONFIG_LOCATOR")
        profile = Profile(
            profile_id=profile_id,
            adapter=adapter,
            model=model,
            role=role_value,
            fixed_args=fixed_args,
            allowed_tools=frozenset(allowed_tools),
            allowed_paths=allowed_paths,
            shell=raw.get("shell"),
            timeout_seconds=float(timeout),
            budget_tokens=raw.get("budget_tokens"),
        )
        source_refs = (f"profile:{profile_id}",) + tuple(f"local:{k}" for k in sorted(override))
        semantic_hash = sha256_bytes(canonical_json(profile.as_dict()))
        return EffectiveConfig(profile, source_refs, semantic_hash, {"override_keys": sorted(override)})


def load_profiles(raw: bytes | str) -> dict[str, dict[str, Any]]:
    if isinstance(raw, str):
        try:
            raw = raw.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise ConfigError("E_CONFIG_ENCODING") from exc
    value = strict_loads(raw)
    if not isinstance(value, dict) or "profiles" not in value or not isinstance(value["profiles"], dict):
        raise ConfigError("E_CONFIG_ROOT")
    return value["profiles"]
