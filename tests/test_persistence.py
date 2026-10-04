import hashlib
import sqlite3

import pytest

from sentinelmesh.persistence import DurableStore, PersistenceError


def ref(raw=b"payload"):
    return {"kind": "fixture", "id": "one", "path": "inputs/one.bin",
            "sha256": hashlib.sha256(raw).hexdigest()}


def test_resources_idempotency_journal_and_restart(tmp_path):
    db = tmp_path / "state.sqlite"
    with DurableStore(db) as store:
        assert store.put_resource(ref(), b"payload")["id"] == "one"
        assert store.put_resource(ref(), b"payload")["sha256"] == ref()["sha256"]
        assert store.get_resource(ref()) == b"payload"
        assert store.idempotent("op-1", "fingerprint-1", {"state": "PUBLISHED"}) == {"state": "PUBLISHED"}
        assert store.idempotent("op-1", "fingerprint-1", {"state": "different"}) == {"state": "PUBLISHED"}
        with pytest.raises(PersistenceError, match="E_IDEMPOTENCY_CONFLICT"):
            store.idempotent("op-1", "fingerprint-2", {"state": "CONFLICT"})
        first = store.append({"record_type": "event", "value": 1})
        second = store.append({"record_type": "event", "value": 2})
        assert (first.seq, second.seq) == (1, 2)
        assert [x.record["value"] for x in store.read_journal()] == [1, 2]
        assert store.fence(1) == 2
    with DurableStore(db) as reopened:
        assert reopened.generation == 2
        assert reopened.get_resource(ref()) == b"payload"
        assert [x.seq for x in reopened.read_journal()] == [1, 2]


def test_resource_hash_conflict_generation_and_chain_fail_closed(tmp_path):
    with DurableStore(tmp_path / "state.sqlite") as store:
        with pytest.raises(PersistenceError, match="E_RESOURCE_HASH_MISMATCH"):
            store.put_resource(ref(), b"changed")
        store.put_resource(ref(), b"payload")
        with pytest.raises(PersistenceError, match="E_RESOURCE_CONFLICT"):
            store.put_resource({**ref(b"other"), "id": "one"}, b"other")
        with pytest.raises(PersistenceError, match="E_GENERATION_CONFLICT"):
            store.fence(99)
        store.append({"record_type": "event", "value": 1})
        store._db.execute("UPDATE journal SET record_sha256=? WHERE seq=1", ("0" * 64,))
        with pytest.raises(PersistenceError, match="E_JOURNAL_HASH"):
            store.read_journal()


def test_invalid_resource_json_and_closed_database_are_rejected(tmp_path):
    with DurableStore(tmp_path / "state.sqlite") as store:
        with pytest.raises(PersistenceError, match="E_RESOURCE_REF"):
            store.get_resource({"kind": "fixture", "id": "bad", "sha256": "not-a-hash"})
        store.close()
        with pytest.raises((sqlite3.ProgrammingError, PersistenceError)):
            _ = store.generation
