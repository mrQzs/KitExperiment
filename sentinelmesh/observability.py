"""Structured logging, metrics and read-only debug bundles."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from threading import Lock
from typing import Any, Mapping
from uuid import uuid4

from .core import canonical_json, sha256_bytes

_SECRET = re.compile(r"(?i)(authorization|api[_-]?key|token|password|secret)\s*[:=]\s*([^\s,;]+)")


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): ("[REDACTED]" if re.search(r"(?i)(token|secret|password|authorization|api[_-]?key)", str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET.sub(lambda m: f"{m.group(1)}=[REDACTED]", value)
    return value


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StructuredLogger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def write(self, record_type: str, *, level: str = "INFO", code: str | None = None, **fields: Any) -> dict[str, Any]:
        if not isinstance(record_type, str) or not record_type.strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in record_type):
            raise ValueError("E_LOG_RECORD_TYPE")
        if not isinstance(level, str) or not level.strip():
            raise ValueError("E_LOG_LEVEL")
        if code is not None and (not isinstance(code, str) or not code.strip()):
            raise ValueError("E_LOG_CODE")
        record = {"record_type": record_type, "observed_at": now(), "level": level, "code": code, **redact(fields)}
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
                fh.flush()
        return record


class Metrics:
    def __init__(self):
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._lock = Lock()

    def inc(self, name: str, value: float = 1, **labels: str) -> float:
        if not isinstance(name, str) or not name.strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
            raise ValueError("E_METRIC_NAME")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("E_METRIC_VALUE")
        if any(not isinstance(key, str) or not key.strip() for key in labels):
            raise ValueError("E_METRIC_LABEL")
        key = (name, tuple(sorted((str(k), str(v)) for k, v in labels.items())))
        with self._lock:
            self._values[key] = self._values.get(key, 0) + value
            return self._values[key]

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"name": n, "labels": dict(labels), "value": v} for (n, labels), v in sorted(self._values.items())]


@dataclass(frozen=True)
class DebugBundle:
    bundle_version: int
    bundle_id: str
    incident_id: str
    scope: Mapping[str, Any]
    evidence: Mapping[str, Any]
    reproduction: str
    sha256: str


class DebugBundleBuilder:
    def build(self, *, incident_id: str, scope: Mapping[str, Any], evidence: Mapping[str, Any], reproduction: str = "") -> DebugBundle:
        if not isinstance(incident_id, str) or not incident_id.strip() or not isinstance(scope, Mapping) or not isinstance(evidence, Mapping) or not isinstance(reproduction, str):
            raise ValueError("E_BUNDLE_SHAPE")
        safe_scope = redact(dict(scope))
        safe_evidence = redact(dict(evidence))
        payload = {"bundle_version": 1, "incident_id": incident_id, "scope": safe_scope, "evidence": safe_evidence, "reproduction": reproduction}
        digest = sha256_bytes(canonical_json(payload))
        return DebugBundle(1, "bundle-" + uuid4().hex, incident_id, safe_scope, safe_evidence, reproduction, digest)

    def write(self, bundle: DebugBundle, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(bundle)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path
