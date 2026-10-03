"""Append-only project events, deterministic projection and recovery briefs.

The state layer is intentionally pull based: it indexes immutable source records,
replays them into a read model and renders a bounded brief.  A brief is an
explanation aid and never grants approval or dispatch permission.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
import unicodedata
import uuid
from typing import Any, Iterable, Mapping

from .core import MAX_SAFE_INTEGER, validate_business_path


class StateError(ValueError):
    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")


ContractError = StateError


def _check_json(value: Any, *, _ancestors: set[int] | None = None, _depth: int = 0) -> None:
    if _depth > 64:
        raise StateError("E_JSON_LIMIT")
    if value is None or type(value) in (str, bool, int):
        if type(value) is int and abs(value) > MAX_SAFE_INTEGER:
            raise StateError("E_JSON_INTEGER")
        return
    if type(value) is float:
        raise StateError("E_JSON_FLOAT")
    if _ancestors is None:
        _ancestors = set()
    if isinstance(value, (list, dict)):
        identity = id(value)
        if identity in _ancestors:
            raise StateError("E_JSON_CYCLE")
        _ancestors.add(identity)
    if isinstance(value, list):
        for item in value: _check_json(item, _ancestors=_ancestors, _depth=_depth + 1)
        _ancestors.remove(identity)
    elif isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str: raise StateError("E_JSON_KEY")
            _check_json(item, _ancestors=_ancestors, _depth=_depth + 1)
        _ancestors.remove(identity)
    else:
        raise StateError("E_JSON_TYPE")


def canonical(value: Any) -> bytes:
    _check_json(value)
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise StateError("E_JSON_OUTPUT") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_load(raw: bytes) -> Any:
    if not isinstance(raw, bytes): raise StateError("E_JSON_LIMIT")
    if raw.startswith(b"\xef\xbb\xbf"): raise StateError("E_JSON_BOM")
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out: raise StateError("E_JSON_DUPLICATE_KEY")
            out[key] = value
        return out
    def forbidden(_): raise StateError("E_JSON_NUMBER")
    try:
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=pairs,
                           parse_float=forbidden, parse_constant=forbidden)
    except StateError:
        raise
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise StateError("E_JSON_SYNTAX") from exc
    _check_json(value)
    return value


def source_key(record: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return tuple(record.get(k) for k in ("repository_namespace", "project_id", "producer", "source_record_id"))


def entity_key(record: Mapping[str, Any]) -> tuple[str, str, str]:
    return tuple(record.get(k) for k in ("request_id", "stream", "subject_id"))


def event_identity(record: Mapping[str, Any]) -> str:
    return digest(["project-source-key-v1", *source_key(record)])


def safe_text(text: str) -> str:
    if not isinstance(text, str): text = str(text)
    out = []
    for ch in text:
        if unicodedata.category(ch) in ("Cc", "Cf"):
            out.append(f"\\u{ord(ch):04x}")
        elif ch in r"\`*_{}[]()<>#!|":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _validate_source(src: Mapping[str, Any], repo: str, project: str) -> None:
    required = ("repository_namespace", "project_id", "producer", "source_record_id",
                "request_id", "stream", "subject_id", "revision", "value")
    if any(k not in src for k in required): raise StateError("E_SCHEMA")
    for key in ("repository_namespace", "project_id", "producer", "source_record_id", "request_id", "stream", "subject_id"):
        if not isinstance(src[key], str) or not src[key].strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in src[key]):
            raise StateError("E_SCHEMA")
    if src["repository_namespace"] != repo or src["project_id"] != project: raise StateError("E_SOURCE_SCOPE")
    if type(src["revision"]) is not int or src["revision"] <= 0: raise StateError("E_SCHEMA")
    if not isinstance(src["value"], Mapping): raise StateError("E_SCHEMA")
    _check_json(src)


def _resource_bytes(root: Path, ref: Mapping[str, Any]) -> bytes:
    path = ref.get("path")
    if not isinstance(path, str) or not path:
        raise StateError("E_PATH")
    try:
        validate_business_path(path)
    except Exception as exc:
        raise StateError("E_PATH") from exc
    target = (root / path).resolve()
    try: target.relative_to(root.resolve())
    except ValueError as exc: raise StateError("E_PATH") from exc
    try: raw = target.read_bytes()
    except OSError as exc: raise StateError("E_SOURCE_MISSING") from exc
    if ref.get("sha256") and hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise StateError("E_SOURCE_HASH")
    return raw


@dataclass(frozen=True)
class Cursor:
    generation: str
    seq: int

    def as_dict(self) -> dict[str, Any]: return {"generation": self.generation, "seq": self.seq}


@dataclass(frozen=True)
class ProjectEvent:
    """Typed source fact accepted by :class:`ProjectEventStore`.

    The object is intentionally a thin value wrapper; source bytes and their
    digest remain the immutable evidence, while ``to_dict`` is the stable wire
    representation consumed by the projector.
    """

    repository_namespace: str
    project_id: str
    producer: str
    source_record_id: str
    request_id: str
    stream: str
    subject_id: str
    revision: int
    value: dict[str, Any]
    previous_record_id: str | None = None

    def __post_init__(self) -> None:
        record = self.to_dict()
        _validate_source(record, self.repository_namespace, self.project_id)
        if self.previous_record_id is not None and (
            not isinstance(self.previous_record_id, str) or not self.previous_record_id.strip()
        ):
            raise StateError("E_SCHEMA")

    def to_dict(self) -> dict[str, Any]:
        return {"repository_namespace": self.repository_namespace, "project_id": self.project_id,
                "producer": self.producer, "source_record_id": self.source_record_id,
                "request_id": self.request_id, "stream": self.stream, "subject_id": self.subject_id,
                "revision": self.revision, "previous_record_id": self.previous_record_id,
                "value": deepcopy(self.value)}

    as_dict = to_dict

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProjectEvent":
        if not isinstance(value, Mapping):
            raise StateError("E_SCHEMA")
        required = ("repository_namespace", "project_id", "producer", "source_record_id",
                    "request_id", "stream", "subject_id", "revision", "value")
        if any(k not in value for k in required): raise StateError("E_SCHEMA")
        return cls(*(value[k] for k in ("repository_namespace", "project_id", "producer", "source_record_id",
                                        "request_id", "stream", "subject_id", "revision", "value")),
                   value.get("previous_record_id"))


@dataclass(frozen=True)
class ProjectView:
    """Immutable projection result with its cursor and rendered brief."""

    state: dict[str, Any]
    brief_bytes: bytes
    render_meta: dict[str, Any]

    @property
    def cursor(self) -> dict[str, Any]: return dict(self.state.get("cursor", {}))

    @property
    def brief(self) -> str: return self.brief_bytes.decode("utf-8")


class EventIndex:
    """Single-process append-only JSONL index with source-key idempotency.

    ``root`` is optional.  With a path supplied, each append is fsynced and the
    file can be reopened by another ``EventIndex`` instance.  In-memory mode is
    convenient for deterministic unit tests.
    """

    def __init__(self, root: str | Path | None = None, generation: str | int = "index-1",
                 repository_namespace: str = "repo", project_id: str = "project"):
        self.root = Path(root) if root is not None else None
        if type(generation) not in (str, int) or isinstance(generation, bool):
            raise StateError("E_SCHEMA")
        self.generation = str(generation)
        self.repo, self.project = repository_namespace, project_id
        for value in (self.generation, self.repo, self.project):
            if not isinstance(value, str) or not value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
                raise StateError("E_SCHEMA")
        self.path = self.root / "events.jsonl" if self.root else None
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        if self.root:
            self.root.mkdir(parents=True, exist_ok=True)
            if self.path.exists(): self._load_file()

    @contextmanager
    def writer(self, timeout: float = 2.0):
        # RLock supplies atomicity for threads.  The JSONL file remains a
        # single-writer resource; callers using multiple processes should add a
        # platform file lock around this context.
        acquired = self._lock.acquire(timeout=timeout)
        if not acquired: raise StateError("E_LOCK_BUSY")
        try: yield
        finally: self._lock.release()

    def _load_file(self) -> None:
        assert self.path is not None
        raw = self.path.read_bytes()
        if not raw: return
        if not raw.endswith(b"\n"): raise StateError("E_INDEX_TAIL")
        for seq, line in enumerate(raw.splitlines(), 1):
            event = strict_load(line)
            if not isinstance(event, Mapping) or event.get("record_type") != "project-event": raise StateError("E_SCHEMA")
            expected_fields = {"schema_version", "record_type", "repository_namespace", "project_id", "producer", "source_record_id", "request_id", "stream", "subject_id", "revision", "previous_record_id", "value", "cursor", "event_id", "recorded_at", "source_ref"}
            if set(event) != expected_fields:
                raise StateError("E_SCHEMA")
            _validate_source(event, self.repo, self.project)
            cursor = event.get("cursor", {})
            if cursor.get("generation") != self.generation or cursor.get("seq") != seq:
                raise StateError("E_INDEX_CURSOR")
            if event.get("event_id") != event_identity(event): raise StateError("E_EVENT_ID")
            if any(source_key(event) == source_key(old) for old in self._events): raise StateError("E_INDEX_DUPLICATE")
            self._events.append(dict(event))

    def read(self) -> tuple[list[dict[str, Any]], bytes, bytes]:
        with self._lock:
            if self.path is None: return deepcopy(self._events), canonical(self._events) if False else b"", b""
            raw = self.path.read_bytes() if self.path.exists() else b""
            boundary = raw.rfind(b"\n") + 1
            prefix, tail = raw[:boundary], raw[boundary:]
            if tail: return deepcopy(self._events), prefix, tail
            # Reload catches tampering after construction.
            old = self._events; self._events = []
            try: self._load_file()
            except Exception:
                self._events = old
                raise
            return deepcopy(self._events), prefix, tail

    @property
    def cursor(self) -> dict[str, Any]:
        with self._lock: return Cursor(self.generation, len(self._events)).as_dict()

    def append(self, resource: Mapping[str, Any], recorded_at: str | None = None,
               crash_at: str | None = None) -> tuple[dict[str, Any], bool]:
        """Append a source record or a ResourceRef containing JSON source bytes."""
        if not isinstance(resource, Mapping):
            raise StateError("E_SCHEMA")
        if "path" in resource and "value" not in resource:
            if self.root is None: raise StateError("E_SOURCE_MISSING")
            src = strict_load(_resource_bytes(self.root, resource))
            if not isinstance(src, Mapping): raise StateError("E_SCHEMA")
            if resource.get("id") != src.get("source_record_id"): raise StateError("E_SOURCE_BINDING")
            source_ref = dict(resource)
        else:
            src = dict(resource)
            _check_json(src)
            source_ref = {"kind": "inline-source", "id": src.get("source_record_id", ""),
                          "sha256": digest(src)}
        _validate_source(src, self.repo, self.project)
        with self.writer():
            if self.path is not None and self.path.exists():
                # Reconcile an append response lost after fsync before making a
                # new cursor allocation.  A partial tail is never silently
                # skipped or overwritten.
                current, _, tail = self.read()
                if tail:
                    raise StateError("E_INDEX_TAIL")
            for old in self._events:
                if source_key(old) == source_key(src):
                    # Compare copied source fields and value, not receipt time.
                    same = all(old.get(k) == src.get(k) for k in ("repository_namespace", "project_id", "producer",
                              "source_record_id", "request_id", "stream", "subject_id", "revision",
                              "previous_record_id", "value"))
                    if same: return deepcopy(old), True
                    raise StateError("E_SOURCE_CONFLICT")
            seq = len(self._events) + 1
            event = {"schema_version": 1, "record_type": "project-event",
                     "repository_namespace": src["repository_namespace"], "project_id": src["project_id"],
                     "producer": src["producer"], "source_record_id": src["source_record_id"],
                     "request_id": src["request_id"], "stream": src["stream"], "subject_id": src["subject_id"],
                     "revision": src["revision"], "previous_record_id": src.get("previous_record_id"),
                     "value": deepcopy(src["value"]), "cursor": {"generation": self.generation, "seq": seq},
                     "event_id": event_identity(src),
                     "recorded_at": recorded_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                     "source_ref": source_ref}
            raw = canonical(event)
            if crash_at == "before_append": raise StateError("E_INJECTED_CRASH")
            if self.path is not None:
                assert self.root is not None
                with self.path.open("ab") as fh:
                    fh.write(raw if crash_at != "partial_append" else raw[:len(raw)//2]); fh.flush()
                    import os; os.fsync(fh.fileno())
            if crash_at == "partial_append":
                raise StateError("E_INJECTED_CRASH")
            # The bytes are durable even if the response is lost.  Retaining
            # the in-memory copy models a process that reconnects to the same
            # store; a fresh instance reloads it from disk.
            self._events.append(event)
            if crash_at == "after_append": raise StateError("E_INJECTED_CRASH")
            return deepcopy(event), False

    def reconcile(self, catalog: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        added = replayed = 0; errors = []
        for item in sorted(catalog, key=lambda x: (x.get("source_record_id", x.get("id", "")), x.get("revision", 0))):
            try:
                _, replay = self.append(item); replayed += int(replay); added += int(not replay)
            except StateError as exc:
                errors.append({"source_id": item.get("source_record_id", item.get("id")), "code": exc.code})
        return {"added": added, "replayed": replayed, "errors": errors}

    def rollover(self, generation: str | int | None = None) -> dict[str, Any]:
        """Start a new cursor generation while retaining old events in memory.

        The JSONL form is rotated to ``events.<old-generation>.jsonl`` so each
        active log has a contiguous cursor prefix.
        """
        with self.writer():
            old = self.generation
            if self.path and self.path.exists():
                archive = self.root / f"events.{old}.jsonl"
                if not archive.exists(): self.path.replace(archive)
            self.generation = str(generation) if generation is not None else str(int(old) + 1) if str(old).isdigit() else old + "-next"
            self._events = []
            return self.cursor


class ProjectEventStore(EventIndex):
    """Public typed facade over :class:`EventIndex`.

    ``append`` accepts ``ProjectEvent``, source dictionaries, or a sequence of
    already enveloped project-event dictionaries.  The return value is the
    immutable event dictionary, with ``replayed`` available through
    ``append_result`` when callers need to distinguish idempotent writes.
    """

    def append(self, event: ProjectEvent | Mapping[str, Any], recorded_at: str | None = None,
               crash_at: str | None = None) -> dict[str, Any]:  # type: ignore[override]
        source = event.to_dict() if isinstance(event, ProjectEvent) else dict(event)
        result, _ = super().append(source, recorded_at=recorded_at, crash_at=crash_at)
        return result

    def append_result(self, event: ProjectEvent | Mapping[str, Any], recorded_at: str | None = None,
                      crash_at: str | None = None) -> tuple[dict[str, Any], bool]:
        source = event.to_dict() if isinstance(event, ProjectEvent) else dict(event)
        return super().append(source, recorded_at=recorded_at, crash_at=crash_at)

    def events(self) -> tuple[ProjectEvent, ...]:
        values, _, _ = super().read()
        return tuple(ProjectEvent.from_dict(value) for value in values)

    def project(self, catalog: Iterable[Mapping[str, Any]] | None = None,
                as_of: str = "2026-01-01T00:00:00Z", catalog_complete: bool = True) -> dict[str, Any]:
        return project(self, catalog, as_of, catalog_complete)


def _records(index: EventIndex | Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], str, str]:
    if isinstance(index, EventIndex):
        events, _, tail = index.read()
        return events, index.repo, index.project
    events = [dict(x) for x in index]
    repo = events[0].get("repository_namespace", "repo") if events else "repo"
    project = events[0].get("project_id", "project") if events else "project"
    return events, repo, project


def _task_view(ent: Mapping[str, Any]) -> tuple[dict[str, Any], bool, bool, list[dict[str, Any]]]:
    value = ent.get("value", {})
    issues: list[dict[str, Any]] = []
    valid = True
    if value.get("verdict") == "PASS" and (value.get("candidate_id") is None or value.get("candidate_id") != value.get("checks_candidate_id")):
        valid = False; issues.append({"code": "E_TASK_BINDING", "scope": f"{ent.get('request_id')}/task/{ent.get('subject_id')}"})
    if value.get("acceptance") == "ACCEPTED" and (value.get("verdict") not in ("PASS", "EXCEPTION_ACCEPTED") or not value.get("acceptance_record_id")):
        valid = False; issues.append({"code": "E_TASK_BINDING", "scope": f"{ent.get('request_id')}/task/{ent.get('subject_id')}"})
    integrated = value.get("integration") not in (None, "NONE") and bool(value.get("acceptance_record_id"))
    accepted = valid and value.get("acceptance") == "ACCEPTED"
    if not valid: display = "unverified"
    elif value.get("integration") == "SUPERSEDED": display = "superseded"
    elif value.get("integration") == "INTEGRATED": display = "integrated" if accepted else "integrated-requires-review"
    elif accepted: display = "accepted" if value.get("verdict") == "PASS" else "exception-accepted"
    elif value.get("attempt_status") == "START_UNKNOWN": display = "recovery-required"
    elif value.get("attempt_status") == "TERMINAL": display = "needs-acceptance" if value.get("verdict") == "PASS" else "needs-review"
    else: display = str(value.get("attempt_status", "unknown")).lower()
    return ({"request_id": ent.get("request_id"), "task_id": ent.get("subject_id"),
             "display_state": display, "effective_acceptance": accepted and value.get("integration") != "SUPERSEDED",
             "historically_integrated": integrated, "source_ref": ent.get("source_ref")}, accepted and value.get("integration") != "SUPERSEDED", integrated, issues)


def project(index: EventIndex | Iterable[Mapping[str, Any]], catalog: Iterable[Mapping[str, Any]] | None = None,
            as_of: str = "2026-01-01T00:00:00Z", catalog_complete: bool = True) -> dict[str, Any]:
    """Replay the current event prefix into a deterministic project state."""
    try: datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    except ValueError as exc: raise StateError("E_AS_OF") from exc
    events, repo, project_id = _records(index)
    catalog_values = [dict(x) for x in catalog] if catalog is not None else [dict(x) for x in events]
    expected = {x.get("source_record_id", x.get("id")): x for x in catalog_values}
    indexed = {e.get("source_record_id"): e for e in events}
    issues: list[dict[str, Any]] = []
    missing, extra = sorted(set(expected) - set(indexed)), sorted(set(indexed) - set(expected))
    if missing or extra or not catalog_complete:
        issues.append({"code": "E_SCAN_INCOMPLETE", "scope": "project", "source_ref": None})
    catalog_conflicts: set[str] = set()
    for identity in sorted(set(expected) & set(indexed)):
        advertised = expected[identity]
        event = indexed[identity]
        # A catalog may contain a ResourceRef (id/path/hash) or the complete
        # source record.  In either form, a changed byte reference is a conflict
        # and must not silently select the latest event.
        if isinstance(advertised, Mapping) and {"kind", "id", "sha256"}.issubset(advertised):
            if event.get("source_ref") != advertised:
                catalog_conflicts.add(identity)
                issues.append({"code": "E_CATALOG_CONFLICT", "scope": "/".join(entity_key(event)), "source_ref": advertised})
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for event in events:
        try:
            key = entity_key(event)
            if any(x is None for x in key): raise StateError("E_SCHEMA")
            groups.setdefault(key, []).append(event)
        except StateError as exc:
            issues.append({"code": exc.code, "scope": "project", "source_ref": event.get("source_ref")})
    entities: list[dict[str, Any]] = []
    blocked: set[tuple[str, str, str]] = set()
    for key, chain in sorted(groups.items()):
        chain.sort(key=lambda x: (x.get("revision", 0), x.get("source_record_id", "")))
        prev = None
        for expected_rev, event in enumerate(chain, 1):
            if event.get("revision") != expected_rev or event.get("previous_record_id") != prev:
                blocked.add(key); issues.append({"code": "E_REVISION_CHAIN", "scope": "/".join(key), "source_ref": event.get("source_ref")}); break
            prev = event.get("source_record_id")
        if key in blocked: continue
        event = chain[-1]
        if event.get("source_record_id") in catalog_conflicts:
            blocked.add(key)
            continue
        value = deepcopy(event.get("value", {}))
        if event.get("stream") == "approval":
            expiry = value.get("valid_until")
            if expiry and datetime.fromisoformat(expiry.replace("Z", "+00:00")) <= datetime.fromisoformat(as_of.replace("Z", "+00:00")):
                issues.append({"code": "E_APPROVAL_STALE", "scope": "/".join(key), "source_ref": event.get("source_ref")})
        if event.get("stream") == "budget":
            currencies = value.get("currencies", [])
            if len({x.get("currency") for x in currencies}) != len(currencies) or value.get("ledger_revision", event.get("revision")) != event.get("revision"):
                issues.append({"code": "E_BUDGET_SNAPSHOT", "scope": "/".join(key), "source_ref": event.get("source_ref")}); continue
        entities.append({"request_id": event.get("request_id"), "stream": event.get("stream"), "subject_id": event.get("subject_id"),
                         "revision": event.get("revision"), "source_ref": event.get("source_ref"), "value": value})
    task_views: list[dict[str, Any]] = []; task_entities: dict[tuple[str, str], dict[str, Any]] = {}
    for ent in entities:
        if ent["stream"] == "task":
            view, _, _, local = _task_view(ent); task_views.append(view); issues.extend(local)
            task_entities[(ent["request_id"], ent["subject_id"])] = ent
    present = {(v["request_id"], v["task_id"]): v for v in task_views}
    remaining = set(task_entities)
    while remaining:
        ready = {key for key in remaining if all((key[0], dep) not in remaining for dep in task_entities[key]["value"].get("depends_on", []))}
        if not ready:
            for key in sorted(remaining):
                view = present[key]; view["effective_acceptance"] = False; view["display_state"] = "dependency-conflict"
                issues.append({"code": "E_DEPENDENCY_CYCLE", "scope": "/".join(key), "source_ref": view.get("source_ref")})
            break
        for key in sorted(ready):
            ent = task_entities[key]; view = present[key]
            for dep in ent["value"].get("depends_on", []):
                up = present.get((key[0], dep))
                if not up or not up.get("effective_acceptance"):
                    view["effective_acceptance"] = False
                    view["display_state"] = "integrated-requires-review" if view.get("historically_integrated") else "dependency-wait"
                    issues.append({"code": "E_DEPENDENCY_WAIT", "scope": f"{key[0]}/task/{key[1]}", "source_ref": view.get("source_ref")})
        remaining -= ready
    issues = sorted({canonical(x): x for x in issues}.values(), key=lambda x: (x.get("scope", ""), x.get("code", ""), canonical(x)))
    cursor = index.cursor if isinstance(index, EventIndex) else {"generation": "memory", "seq": len(events)}
    coverage = {"catalog_id": "catalog", "expected_records": len(expected), "indexed_records": len(indexed),
                "unindexed_source_ids": missing, "unexpected_source_ids": extra,
                "complete": catalog_complete and not missing and not extra}
    state = {"schema_version": 1, "record_type": "project-state", "repository_namespace": repo,
             "project_id": project_id, "cursor": cursor, "as_of": as_of,
             "reducer_profile": "sentinelmesh-projector-v1", "health": "degraded" if issues else "complete",
             "coverage": coverage, "entities": sorted(entities, key=lambda x: (x["request_id"], x["stream"], x["subject_id"])),
             "task_views": sorted(task_views, key=lambda x: (x["request_id"], x["task_id"])), "issues": issues,
             "known_accepted_tasks": sum(bool(x.get("effective_acceptance")) for x in task_views),
             "known_integrated_tasks": sum(bool(x.get("historically_integrated")) for x in task_views)}
    state["input_fingerprint"] = digest({"events": events, "catalog": sorted(catalog_values, key=lambda x: canonical(x)),
                                          "as_of": as_of, "catalog_complete": catalog_complete,
                                          "issues": issues, "reducer": state["reducer_profile"]})
    return state


def brief_items(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    def add(identity: str, text: str, critical: bool, rank: int, resource: Any = None):
        items.append({"id": identity, "text": text, "critical": critical, "rank": rank,
                      "refs": [] if resource is None else [resource]})
    for i, issue in enumerate(state.get("issues", [])):
        add(f"issue-{i}", f"[未核实/阻塞] {issue.get('code')} {issue.get('scope')}", True, 0, issue.get("source_ref"))
    views = {(x["request_id"], x["task_id"]): x for x in state.get("task_views", [])}
    for ent in state.get("entities", []):
        ident = "item-" + digest([ent.get("request_id"), ent.get("stream"), ent.get("subject_id")])[:24]
        key = (ent.get("request_id"), ent.get("subject_id")); stream = ent.get("stream"); value = ent.get("value", {})
        critical, rank = False, 50
        if stream == "task" and key in views:
            display = views[key]["display_state"]; text = f"[任务事实] {key[0]}/{key[1]} {display}；{value.get('summary', '')}"
            critical, rank = display in ("running", "recovery-required", "integrated-requires-review", "unverified"), 30
        elif stream == "budget":
            text = f"[预算快照] {key[0]}/{key[1]} {canonical(value).decode().strip()}"; critical, rank = True, 10
        elif stream in ("decision", "goal"):
            label = "已确认" if value.get("status") == "CONFIRMED" else "未确认/撤销"
            text = f"[{label}] {key[0]}/{key[1]} {value.get('text', '')}"; critical, rank = stream == "goal" or bool(value.get("safety_critical")), 15
        else:
            text = f"[来源观察] {key[0]}/{key[1]} {canonical(value).decode().strip()}"; rank = 40
        add(ident, text, critical, rank, ent.get("source_ref"))
    if not any(x.get("stream") == "budget" for x in state.get("entities", [])):
        add("budget-unknown", "[未知] 无可核验预算快照；不推定免费或有剩余额度。", True, 10)
    add("next-action", "[下一动作建议] 按来源核对阻塞/待审对象；任何执行仍由 03/07 实时准入。", True, 90)
    return sorted(items, key=lambda x: (not x["critical"], x["rank"], x["id"]))


def render_brief(state: Mapping[str, Any], max_bytes: int = 16384) -> tuple[bytes, dict[str, Any]]:
    if type(max_bytes) is not int or not 256 <= max_bytes <= 262144: raise StateError("E_BRIEF_LIMIT")
    items = brief_items(state)
    cursor = state.get("cursor", {})
    head = ("# 项目恢复简报\n\n通知模式：pull；这是阅读辅助，不构成验收、授权或派遣许可。\n"
            f"观察时点：{state.get('as_of', '')}；游标：{cursor.get('generation')}:{cursor.get('seq')}\n"
            f"状态：{state.get('health', 'unknown')}；输入版本：{state.get('input_fingerprint', '')}\n\n")
    def encode(selected):
        body = []
        for item in selected:
            refs = " ".join(f"[来源={r.get('kind')}:{r.get('id')};sha256={r.get('sha256')}]" for r in item["refs"] if isinstance(r, Mapping))
            body.append(safe_text(item["text"]) + (" " + refs if refs else "") + "\n")
        return (head + "\n".join(body) + f"\n省略条目：{len(items)-len(selected)}；完整清单见同版本 manifest。读取不证明理解，生成不证明送达。\n").encode()
    selected = [x for x in items if x["critical"]]
    if len(encode(selected)) > max_bytes: raise StateError("E_BRIEF_BUDGET_TOO_SMALL")
    for item in (x for x in items if not x["critical"]):
        candidate = selected + [item]
        if len(encode(candidate)) > max_bytes: break
        selected = candidate
    raw = encode(selected); included = [x["id"] for x in selected]; omitted = [x["id"] for x in items if x["id"] not in included]
    refs = {canonical(r): r for x in selected for r in x["refs"] if isinstance(r, Mapping)}
    meta = {"renderer_profile": "sentinelmesh-brief-v1", "max_bytes": max_bytes, "rendered_bytes": len(raw),
            "included_item_ids": included, "omitted_item_ids": omitted, "truncated": bool(omitted),
            "source_refs": [refs[k] for k in sorted(refs)]}
    return raw, meta


def brief(state: Mapping[str, Any], max_bytes: int = 16384) -> str:
    return render_brief(state, max_bytes)[0].decode("utf-8")


class Projector:
    def __init__(self, index: EventIndex, reducer_profile: str = "sentinelmesh-projector-v1"):
        self.index, self.reducer_profile = index, reducer_profile

    @property
    def cursor(self) -> dict[str, Any]: return self.index.cursor

    def project(self, catalog: Iterable[Mapping[str, Any]] | None = None, as_of: str = "2026-01-01T00:00:00Z",
                catalog_complete: bool = True) -> dict[str, Any]:
        state = project(self.index, catalog, as_of, catalog_complete)
        state["reducer_profile"] = self.reducer_profile
        state["input_fingerprint"] = digest({"state": {k: v for k, v in state.items() if k != "input_fingerprint"}, "reducer": self.reducer_profile})
        return state

    def brief(self, state: Mapping[str, Any], max_bytes: int = 16384) -> tuple[bytes, dict[str, Any]]:
        return render_brief(state, max_bytes)

    def rebuild(self, catalog: Iterable[Mapping[str, Any]] | None = None,
                as_of: str = "2026-01-01T00:00:00Z", catalog_complete: bool = True,
                max_bytes: int = 16384) -> ProjectView:
        """Recreate state and brief from the event prefix, with no side effects."""
        state = self.project(catalog, as_of, catalog_complete)
        raw, meta = render_brief(state, max_bytes)
        return ProjectView(deepcopy(state), raw, meta)


__all__ = ["StateError", "ContractError", "ProjectEvent", "ProjectEventStore", "ProjectView",
           "EventIndex", "Projector", "Cursor", "canonical", "digest",
           "strict_load", "source_key", "entity_key", "event_identity", "project", "brief_items",
           "render_brief", "brief", "safe_text"]
