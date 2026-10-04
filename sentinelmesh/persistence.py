"""Small durable SQLite seams for the SentinelMesh reference controller.

The in-memory stores remain useful for pure tests, while this module gives a
single-host controller restart-safe resources, idempotency records, fenced
generations and an append-only hash chained journal.  It deliberately does
not claim distributed locking or production object storage.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Mapping

from .core import canonical_json, sha256_bytes, validate_resource_ref


class PersistenceError(ValueError):
    def __init__(self, code: str, detail: str | None = None):
        self.code, self.detail = code, detail
        super().__init__(code if detail is None else f"{code}: {detail}")


def _text(value: Any, code: str = "E_PERSISTENCE_TEXT") -> str:
    if type(value) is not str or not value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise PersistenceError(code)
    return value


def _strict_json(value: Any) -> bytes:
    try:
        raw = canonical_json(value)
    except Exception as exc:
        raise PersistenceError("E_PERSISTENCE_JSON") from exc
    if len(raw) > 1_048_576:
        raise PersistenceError("E_PERSISTENCE_LIMIT")
    return raw


@dataclass(frozen=True)
class JournalEntry:
    seq: int
    record: Mapping[str, Any]
    record_sha256: str
    previous_sha256: str | None


class DurableStore:
    """Thread-safe, short-transaction SQLite store suitable for one host."""

    def __init__(self, path: str | Path):
        if not isinstance(path, (str, Path)):
            raise PersistenceError("E_PERSISTENCE_PATH")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._db = sqlite3.connect(str(self.path), timeout=10, isolation_level=None,
                                       check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
        except sqlite3.Error as exc:
            raise PersistenceError("E_PERSISTENCE_OPEN") from exc

    def _init_schema(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS resources (
                kind TEXT NOT NULL, ident TEXT NOT NULL, path TEXT NOT NULL,
                sha256 TEXT NOT NULL, content BLOB NOT NULL,
                PRIMARY KEY(kind, ident, path)
            );
            CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS journal (
                seq INTEGER PRIMARY KEY, record TEXT NOT NULL, record_sha256 TEXT NOT NULL,
                previous_sha256 TEXT
            );
            INSERT OR IGNORE INTO meta(key, value) VALUES ('generation', '1');
        """)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "DurableStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @property
    def generation(self) -> int:
        row = self._db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        return int(row[0])

    def fence(self, expected_generation: int) -> int:
        if type(expected_generation) is not int or expected_generation < 1:
            raise PersistenceError("E_GENERATION")
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                current = self.generation
                if current != expected_generation:
                    raise PersistenceError("E_GENERATION_CONFLICT")
                value = current + 1
                self._db.execute("UPDATE meta SET value=? WHERE key='generation'", (str(value),))
                self._db.execute("COMMIT")
                return value
            except PersistenceError:
                self._db.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                self._db.execute("ROLLBACK")
                raise PersistenceError("E_PERSISTENCE_TX") from exc

    def put_resource(self, ref: Mapping[str, Any], raw: bytes) -> dict[str, Any]:
        if type(raw) is not bytes:
            raise PersistenceError("E_RESOURCE_BYTES")
        try:
            normalized = dict(ref)
            validate_resource_ref(normalized)
        except Exception as exc:
            raise PersistenceError("E_RESOURCE_REF") from exc
        actual = sha256_bytes(raw)
        if actual != normalized["sha256"]:
            raise PersistenceError("E_RESOURCE_HASH_MISMATCH")
        key = (normalized["kind"], normalized["id"], normalized.get("path", ""))
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT sha256, content FROM resources WHERE kind=? AND ident=? AND path=?", key).fetchone()
                if row is not None:
                    if row[0] != actual or bytes(row[1]) != raw:
                        raise PersistenceError("E_RESOURCE_CONFLICT")
                else:
                    self._db.execute("INSERT INTO resources(kind,ident,path,sha256,content) VALUES (?,?,?,?,?)",
                                     (*key, actual, sqlite3.Binary(raw)))
                self._db.execute("COMMIT")
            except PersistenceError:
                self._db.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                self._db.execute("ROLLBACK")
                raise PersistenceError("E_PERSISTENCE_TX") from exc
        return dict(normalized)

    def get_resource(self, ref: Mapping[str, Any]) -> bytes:
        try:
            normalized = dict(ref)
            validate_resource_ref(normalized)
        except Exception as exc:
            raise PersistenceError("E_RESOURCE_REF") from exc
        row = self._db.execute("SELECT sha256, content FROM resources WHERE kind=? AND ident=? AND path=?",
                               (normalized["kind"], normalized["id"], normalized.get("path", ""))).fetchone()
        if row is None:
            raise PersistenceError("E_RESOURCE_MISSING")
        raw = bytes(row[1])
        if row[0] != normalized["sha256"] or sha256_bytes(raw) != normalized["sha256"]:
            raise PersistenceError("E_RESOURCE_HASH_MISMATCH")
        return raw

    def idempotent(self, operation_id: str, fingerprint: str, result: Mapping[str, Any]) -> dict[str, Any]:
        _text(operation_id, "E_OPERATION_ID")
        _text(fingerprint, "E_OPERATION_FINGERPRINT")
        raw = _strict_json(dict(result))
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT fingerprint,result FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
                if row is not None:
                    if row[0] != fingerprint:
                        raise PersistenceError("E_IDEMPOTENCY_CONFLICT")
                    value = json.loads(row[1])
                    self._db.execute("COMMIT")
                    return value
                self._db.execute("INSERT INTO operations(operation_id,fingerprint,result) VALUES (?,?,?)",
                                 (operation_id, fingerprint, raw.decode("utf-8")))
                self._db.execute("COMMIT")
                return json.loads(raw)
            except PersistenceError:
                self._db.execute("ROLLBACK")
                raise
            except (sqlite3.Error, ValueError) as exc:
                self._db.execute("ROLLBACK")
                raise PersistenceError("E_PERSISTENCE_TX") from exc

    def append(self, record: Mapping[str, Any], *, generation: int | None = None) -> JournalEntry:
        if not isinstance(record, Mapping):
            raise PersistenceError("E_JOURNAL_RECORD")
        raw = _strict_json(dict(record))
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                if generation is not None and generation != self.generation:
                    raise PersistenceError("E_GENERATION_CONFLICT")
                row = self._db.execute("SELECT seq, record_sha256 FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
                seq = (int(row[0]) + 1) if row else 1
                previous = row[1] if row else None
                digest = hashlib.sha256(raw).hexdigest()
                self._db.execute("INSERT INTO journal(seq,record,record_sha256,previous_sha256) VALUES (?,?,?,?)",
                                 (seq, raw.decode("utf-8"), digest, previous))
                self._db.execute("COMMIT")
                return JournalEntry(seq, dict(record), digest, previous)
            except PersistenceError:
                self._db.execute("ROLLBACK")
                raise
            except sqlite3.Error as exc:
                self._db.execute("ROLLBACK")
                raise PersistenceError("E_PERSISTENCE_TX") from exc

    def read_journal(self, after: int = 0) -> tuple[JournalEntry, ...]:
        if type(after) is not int or after < 0:
            raise PersistenceError("E_JOURNAL_CURSOR")
        rows = self._db.execute("SELECT seq,record,record_sha256,previous_sha256 FROM journal WHERE seq>? ORDER BY seq", (after,)).fetchall()
        result: list[JournalEntry] = []
        previous = None
        expected = after + 1
        if after:
            prior = self._db.execute("SELECT record_sha256 FROM journal WHERE seq=?", (after,)).fetchone()
            previous = prior[0] if prior else None
        for row in rows:
            if int(row[0]) != expected or row[3] != previous:
                raise PersistenceError("E_JOURNAL_CHAIN")
            raw = row[1].encode("utf-8")
            if hashlib.sha256(raw).hexdigest() != row[2]:
                raise PersistenceError("E_JOURNAL_HASH")
            result.append(JournalEntry(int(row[0]), json.loads(row[1]), row[2], row[3]))
            previous, expected = row[2], expected + 1
        return tuple(result)


__all__ = ["PersistenceError", "JournalEntry", "DurableStore"]
