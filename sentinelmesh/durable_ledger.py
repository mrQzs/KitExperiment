"""Durable command log for the in-memory :class:`~sentinelmesh.scheduler.Scheduler`.

The in-memory ``Scheduler`` stays the only implementation of the ledger's
semantics.  This module wraps it in a single-host, append-only command log:

* every successful mutating call is executed in memory first and only then
  appended, as one command, to a SQLite command log that is hash chained (each
  entry carries the digest of the entry before it).  The call returns once the
  append is committed, so a call that returned survived the process;
* opening a ledger replays the log entry by entry on a fresh in-memory
  ``Scheduler`` and rebuilds exactly the state the writer had -- the same
  operation ids and the same event sequence numbers;
* a failed append, or a log another instance already moved on, fails closed:
  the instance refuses every further call, reads included, and the ledger has to
  be opened (replayed) again.

The log is deliberately conservative: a rollback journal (``DELETE``),
``synchronous=FULL`` and ``fullfsync=ON``, which is what 07 appendix B.4 chose
for the same job.  Durability across a power loss was not measured and stays
``NOT_RUN``; no multi-host locking and no ledger on a network file system are
claimed.  Run descriptors and the recovery driver are a separate contract.

Version 2 of the contract tightened three of those rules, and the module follows
it:

* an exception from the in-memory call other than the Scheduler's own
  ``ContractError`` -- an interrupt above all -- makes the instance unusable,
  because the in-memory state may already have changed while the log has
  nothing: appending later commands on top of it would write a log that no
  longer replays.  A wrong signature is refused before anything runs and leaves
  the instance usable;
* opening never creates a file (the connection is a ``mode=rw`` file URI), and
  every step after a successful connection -- pragmas, reading, the chain
  check, the replay Scheduler, the replay itself -- closes that connection
  before reporting.  Opening reports ledger errors only: a stored generation
  the Scheduler refuses is a damaged ledger (``E_LEDGER_FORMAT``), and so is a
  command that breaks the parser or the replay in any way (``E_LEDGER_REPLAY``).
  A recorded claim must carry an operation id of the shape the Scheduler
  generates, and no two claims may share one unless the second is the
  idempotent replay of the first;
* creating makes its file atomically with ``O_CREAT | O_EXCL`` and, on any later
  failure, removes the path only while it still is the file this call created
  (same device and inode) -- never a file somebody else put there.

The planner completed the module after the review of attempt-002: the
connection path keeps every ``..`` so SQLite opens the file the kernel resolves
(a path collapsed as text could name another one), the connection is closed on
every way out of the pragmas, a path the file system cannot encode is
``E_LEDGER_PATH``, any failure while parsing a command is ``E_LEDGER_REPLAY``,
and a failure to close the creation descriptor removes the created file.  A
process killed between creating the file and the first commit still leaves an
empty file behind: ``create_ledger`` then reports ``E_LEDGER_EXISTS`` and
``open_ledger`` ``E_LEDGER_FORMAT`` until an operator removes it.

Checkpoints and compaction arrived with the second version of the *format*,
because replaying a log grew faster than the log itself -- the replay of a
``claim`` walks every operation -- while loading the same state from JSON did
not.  A checkpoint row holds the whole in-memory state, events included, at one
sequence number, and :meth:`DurableScheduler.compact` writes it and removes the
commands it covers inside one ``BEGIN IMMEDIATE`` transaction.  Opening then
loads the checkpoint and replays only the commands after it, which rebuilds
exactly what a full replay does -- every read-only view, every event and the
private state of the in-memory ``Scheduler``.  The command chain is never
rewritten: the entry after a checkpoint links to the checkpoint's ``last_sha``
with the digest it always had, so the tail of a compacted ledger is byte for
byte the tail of the ledger it came from.  Compaction is explicit -- an operator's command or a
controller's own call -- and never automatic: the append path does not look at
the length of the log, because a threshold is policy and not ledger.

The checkpoint is verified, not re-derived: the digest covers its sequence
number, its link and its state text, and the state is checked for shape -- exact
field sets, exact types, references that exist and hold together -- and nothing
more.  Budgets and transitions are not proven again, because the checkpoint was
written by this module from a ``Scheduler`` that had already passed every guard;
what the checks refuse is a damaged or forged file.  The format moved to
``sentinelmesh-ledger-v2`` for the same fail-closed reason: a format 1 file has
no ``checkpoint`` table, and a binary that does not know about checkpoints would
read a compacted ledger as an empty one.  There is no migration -- a format 1
file is refused.
"""
from __future__ import annotations

import copy
import dataclasses
from hashlib import sha256
import inspect
import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping

from .scheduler import ContractError, Scheduler, _Operation, _Request, _Task, _Usage

LEDGER_FORMAT = "sentinelmesh-ledger-v2"

#: Every mutating method of :class:`Scheduler`, in the order the contract fixes.
MUTATING_METHODS = ("seed_request", "seed_task", "claim", "handoff", "mark_running", "renew", "observe", "settle",
                    "finish", "fixture_verdict", "verdict", "unknown", "release_proven_unstarted", "recover",
                    "cancel_unstarted", "stop", "rollover", "resolve_unknown", "resume")

#: A generation written as canonical decimal text: no sign, no leading zeros.
_CANONICAL_INTEGER = re.compile(r"\A(?:0|[1-9][0-9]*)\Z")

#: The longest canonical generation text there can be: 2^53-1 has sixteen digits.
_MAX_GENERATION_DIGITS = 16

#: The one shape ``Scheduler._new_operation_id`` produces: a recorded claim with
#: anything else is a forged log.
_OPERATION_ID = re.compile(r"\Aop-[0-9a-f]{32}\Z")

#: A checkpoint link: the digest of the command it follows, or "" at sequence 0.
_LINK = re.compile(r"\A[0-9a-f]{64}\Z")

#: The states an operation can be in.  The Scheduler owns them; a checkpoint that
#: records another one describes a ledger that never existed.
_OPERATION_STATES = frozenset({"RESERVED", "HANDOFF_ACCEPTED", "RUNNING", "START_UNKNOWN", "REVIEW_PENDING",
                               "ACCEPTED", "REJECTED", "ABANDONED", "NOT_STARTED", "CANCELLED_NOT_STARTED"})

#: The keys of the checkpoint state, in the order they are written.
_STATE_KEYS = ("generation", "requests", "tasks", "operations", "idempotency", "resource_holds", "events")

#: The field sets of the four private dataclasses, taken from the dataclasses
#: themselves: a field the Scheduler gains refuses every older checkpoint.
_REQUEST_FIELDS = frozenset(field.name for field in dataclasses.fields(_Request))
_TASK_FIELDS = frozenset(field.name for field in dataclasses.fields(_Task))
_OPERATION_FIELDS = frozenset(field.name for field in dataclasses.fields(_Operation))
_USAGE_FIELDS = frozenset(field.name for field in dataclasses.fields(_Usage))

#: The keys of one event, exactly.
_EVENT_KEYS = frozenset({"seq", "operation_id", "type", "generation", "payload"})

_SCHEMA = ("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
           "CREATE TABLE commands(seq INTEGER PRIMARY KEY, command TEXT NOT NULL, "
           "sha256 TEXT NOT NULL, previous TEXT NOT NULL)",
           "CREATE TABLE checkpoint(seq INTEGER PRIMARY KEY, last_sha TEXT NOT NULL, "
           "state TEXT NOT NULL, sha256 TEXT NOT NULL)")


class LedgerError(Exception):
    """Fail-closed error of the durable ledger, with a stable code."""

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _check_path(path: Any) -> str:
    """``path`` must be an exact, non-empty ``str`` without NUL that the file system can encode."""
    if type(path) is not str or not path or "\0" in path:
        raise LedgerError("E_LEDGER_PATH", "the ledger path must be a non-empty str without NUL")
    try:
        os.fsencode(path)
    except UnicodeError as exc:
        # A lone surrogate outside the escape range would otherwise surface as
        # ``UnicodeEncodeError`` from ``os.lstat`` (review of attempt-002, F3).
        raise LedgerError("E_LEDGER_PATH", "the ledger path cannot be encoded for the file system") from exc
    return path


def entry_sha256(previous: str, command: str) -> str:
    """Digest of one log entry: the previous digest, a newline, the command."""
    return sha256(previous.encode("ascii") + b"\n" + command.encode("utf-8")).hexdigest()


def checkpoint_sha256(seq: int, last_sha: str, state: str) -> str:
    """Digest of one checkpoint row: the sequence, a newline, the link, a newline, the state.

    Every column of the row but the digest itself is covered, so touching the
    sequence number, the link to the command chain or the state text is visible.
    The link is what ties the checkpoint to the chain: it is the digest of the
    command the state covers, which is also the ``previous`` of the command after
    it, so a checkpoint cannot be moved along the log without breaking one of the
    two.
    """
    return sha256(("%d\n%s\n" % (seq, last_sha)).encode("ascii") + state.encode("utf-8")).hexdigest()


def _rollback(db: sqlite3.Connection) -> None:
    try:
        db.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _connect(path: str) -> sqlite3.Connection:
    """Connect to the database at ``path``, read-write, never creating it.

    ``mode=rw`` makes SQLite refuse a path that is not there, so neither opening
    nor creating can leave a new file behind.  The path is made absolute and
    then percent-encoded by ``Path.as_uri`` -- it may contain spaces.

    ``Path.absolute`` only puts the working directory in front: unlike
    ``os.path.abspath`` it keeps every ``..``, so SQLite resolves the path one
    component at a time and follows a symbolic link before the ``..`` after it,
    as the kernel does for ``os.lstat``, ``os.open`` and ``os.unlink``.  A path
    collapsed as text could name another file than the one this module checked,
    created or removes (review of attempt-002, F1).
    """
    try:
        uri = Path(path).absolute().as_uri() + "?mode=rw"
        # ``check_same_thread=False``: one instance is written from several
        # threads, and every access to the connection happens under the
        # instance's own lock, which serializes them.
        db = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise LedgerError("E_LEDGER_OPEN", str(exc)) from exc
    try:
        mode = db.execute("PRAGMA journal_mode=DELETE").fetchone()
        if not mode or mode[0] != "delete":
            raise LedgerError("E_LEDGER_OPEN", f"journal_mode={mode!r}")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA fullfsync=ON")
    except BaseException as exc:
        # Every way out closes the connection first, an interrupt included
        # (review of attempt-002, F2).
        try:
            db.close()
        except Exception:  # the failure at hand is the one to report
            pass
        if isinstance(exc, sqlite3.Error):
            raise LedgerError("E_LEDGER_OPEN", str(exc)) from exc
        raise
    return db


def _missing_table(exc: sqlite3.Error) -> bool:
    return "no such table" in str(exc)


def _remove_created(path: str, identity: tuple[int, int]) -> None:
    """Remove the file this call created -- only while it still is that file.

    The size is not consulted: a file this call created is empty, but so is a
    file somebody else put on the path in the meantime, and only the device and
    inode tell the two apart.
    """
    try:
        current = os.lstat(path)
    except OSError:
        return
    if (current.st_dev, current.st_ino) == identity:
        try:
            os.unlink(path)
        except OSError:
            pass


def _jsonable(value: Any) -> Any:
    """A JSON-encodable copy that keeps a mapping's own iteration order."""
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if value is None or type(value) in (str, bool, int):
        return value
    raise TypeError(f"cannot record a {type(value).__name__} argument")


def _encode_command(name: str, arguments: Mapping[str, Any], result: Any) -> bytes:
    """The command of one successful call: method, arguments and, for a claim, the id."""
    command: dict[str, Any] = {"method": name, "arguments": {key: _jsonable(value)
                                                             for key, value in arguments.items()}}
    if name == "claim":
        command["operation_id"] = result["operation_id"]
    try:
        # Keys are *not* sorted: a mapping argument keeps the order the caller
        # used, and ``_settle_mapping`` records a mapping entry by entry in that
        # order, so the replayed events must keep it too.
        return json.dumps(command, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LedgerError("E_LEDGER_WRITE", str(exc)) from exc


def _strict_loads(text: str, code: str = "E_LEDGER_REPLAY") -> Any:
    """Strict JSON: duplicate keys, NaN and Infinity are refused, never repaired.

    ``code`` is what a refusal reports: a command that cannot be parsed is
    ``E_LEDGER_REPLAY``, a checkpoint state that cannot be is
    ``E_LEDGER_CHECKPOINT``.
    """

    def object_hook(pairs):
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LedgerError(code, f"duplicate key {key!r}")
            result[key] = value
        return result

    def constant(name):
        raise LedgerError(code, name)

    try:
        return json.loads(text, object_pairs_hook=object_hook, parse_constant=constant)
    except LedgerError:
        raise
    except RecursionError as exc:
        # A command nested past what the parser can walk is a damaged ledger,
        # not a crash of the reader.
        raise LedgerError(code, "a command nested too deeply to parse") from exc
    except ValueError as exc:
        raise LedgerError(code, str(exc)) from exc
    except Exception as exc:
        # ``MemoryError`` above all: the same failure in the replayed call is
        # already ``E_LEDGER_REPLAY`` (review of attempt-002, F4).
        raise LedgerError(code, f"the command could not be parsed: {type(exc).__name__}") from exc


def _encode_state(scheduler: Scheduler) -> str:
    """The whole in-memory state of one ``Scheduler`` as one JSON object.

    Seven keys, and the mappings keep their own iteration order -- requests in
    seeding order, operations in claim order, usage in recording order -- because
    that order is what the read-only views of the writer showed and what the
    reopened ledger has to show again.  Keys are therefore *not* sorted.
    ``resource_keys`` becomes a list (a tuple is not a JSON type) and is restored
    as a tuple when the state is decoded again.
    """
    state: dict[str, Any] = {
        "generation": scheduler._generation,
        "requests": {request_id: dataclasses.asdict(request)
                     for request_id, request in scheduler._requests.items()},
        "tasks": [[request_id, task_id, dataclasses.asdict(task)]
                  for (request_id, task_id), task in scheduler._tasks.items()],
        "operations": {operation_id: dataclasses.asdict(operation)
                       for operation_id, operation in scheduler._operations.items()},
        "idempotency": [[request_id, key, operation_id]
                        for (request_id, key), operation_id in scheduler._idempotency.items()],
        "resource_holds": [[repo_id, resource, operation_id]
                           for (repo_id, resource), operation_id in scheduler._resource_holds.items()],
        "events": scheduler._events,
    }
    try:
        return json.dumps(_jsonable(state), separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise LedgerError("E_LEDGER_WRITE", str(exc)) from exc


def _fields_of(record: Any, fields: frozenset, label: str) -> dict[str, Any]:
    """``record`` as a mapping with exactly ``fields`` -- or a damaged checkpoint."""
    if type(record) is not dict or set(record) != fields:
        raise LedgerError("E_LEDGER_CHECKPOINT", f"a {label} without exactly its own fields")
    return record


def _exact_int(value: Any) -> bool:
    """An exact integer: a ``bool`` is not one, and neither is a float."""
    return type(value) is int


def _int_map(record: Mapping[str, Any]) -> dict[str, int]:
    """A mapping of dimensions to exact integers -- a capacity, a reservation."""
    if type(record) is not dict or not all(type(item) is int for item in record.values()):
        raise LedgerError("E_LEDGER_CHECKPOINT", "a mapping of dimensions that is not one of integers")
    return dict(record)


def _decode_state(text: str, stored_generation: int) -> _ReplayScheduler:
    """Rebuild a ``Scheduler`` from a checkpoint state, or refuse the file.

    The checks are the shape of the state and nothing else: exact field sets,
    exact types (a ``bool`` is not an ``int``), references that exist and hold
    together, events numbered from one.  Budgets, transitions and idempotency are
    *not* proven again -- the checkpoint was written from a ``Scheduler`` that had
    already passed every guard, and what is being refused here is a damaged or
    forged file.  Anything unexpected is ``E_LEDGER_CHECKPOINT``.

    The mappings keep the order the JSON had, so a reopened ledger shows the same
    order the writer did.
    """
    state = _strict_loads(text, "E_LEDGER_CHECKPOINT")
    if type(state) is not dict or set(state) != set(_STATE_KEYS):
        raise LedgerError("E_LEDGER_CHECKPOINT", "a state without exactly its own keys")
    generation = state["generation"]
    if not _exact_int(generation) or generation < stored_generation:
        raise LedgerError("E_LEDGER_CHECKPOINT", "a generation that is not an integer at or above the stored one")
    try:
        scheduler = _ReplayScheduler(generation=generation)
    except ContractError as exc:
        # A generation the Scheduler refuses describes a ledger that never ran.
        raise LedgerError("E_LEDGER_CHECKPOINT", str(exc)) from exc

    requests_raw = state["requests"]
    if type(requests_raw) is not dict:
        raise LedgerError("E_LEDGER_CHECKPOINT", "requests that are not an object")
    requests: dict[str, _Request] = {}
    for request_id, record in requests_raw.items():
        fields = _fields_of(record, _REQUEST_FIELDS, "request")
        stopped = fields["stopped"]
        stop_reasons = fields["stop_reasons"]
        if (type(fields["repo_id"]) is not str or type(fields["max_concurrency"]) is not int
                or (stopped is not None and type(stopped) is not str)
                or type(stop_reasons) is not list or not all(type(reason) is str for reason in stop_reasons)):
            raise LedgerError("E_LEDGER_CHECKPOINT", "a request field of the wrong type")
        requests[request_id] = _Request(repo_id=fields["repo_id"], caps=_int_map(fields["caps"]),
                                        max_concurrency=fields["max_concurrency"], stopped=stopped,
                                        stop_reasons=list(stop_reasons))

    tasks_raw = state["tasks"]
    if type(tasks_raw) is not list:
        raise LedgerError("E_LEDGER_CHECKPOINT", "tasks that are not an array")
    tasks: dict[tuple[str, str], _Task] = {}
    for entry in tasks_raw:
        if type(entry) is not list or len(entry) != 3:
            raise LedgerError("E_LEDGER_CHECKPOINT", "a task entry that is not a triple")
        request_id, task_id, fields = entry
        if (type(request_id) is not str or type(task_id) is not str or request_id not in requests
                or (request_id, task_id) in tasks):
            raise LedgerError("E_LEDGER_CHECKPOINT", "a task naming an unknown request, or recorded twice")
        record = _fields_of(fields, _TASK_FIELDS, "task")
        caps = _int_map(record["caps"])
        if (type(record["max_attempts"]) is not int or type(record["guard_version"]) is not int
                or type(record["fixture_eligible"]) is not bool):
            raise LedgerError("E_LEDGER_CHECKPOINT", "a task field of the wrong type")
        tasks[(request_id, task_id)] = _Task(caps=caps, max_attempts=record["max_attempts"],
                                            guard_version=record["guard_version"],
                                            fixture_eligible=record["fixture_eligible"])

    operations_raw = state["operations"]
    if type(operations_raw) is not dict:
        raise LedgerError("E_LEDGER_CHECKPOINT", "operations that are not an object")
    operations: dict[str, _Operation] = {}
    for operation_id, record in operations_raw.items():
        if _OPERATION_ID.fullmatch(operation_id) is None:
            raise LedgerError("E_LEDGER_CHECKPOINT", "an operation id the Scheduler would not generate")
        fields = _fields_of(record, _OPERATION_FIELDS, "operation")
        if fields["operation_id"] != operation_id:
            raise LedgerError("E_LEDGER_CHECKPOINT", "an operation recorded under another id")
        if (type(fields["request_id"]) is not str or type(fields["task_id"]) is not str
                or (fields["request_id"], fields["task_id"]) not in tasks):
            raise LedgerError("E_LEDGER_CHECKPOINT", "an operation naming an unknown task")
        if type(fields["state"]) is not str or fields["state"] not in _OPERATION_STATES:
            raise LedgerError("E_LEDGER_CHECKPOINT", "a state the Scheduler never records")
        for name in ("plan_version", "guard_version", "generation", "lease_until"):
            if not _exact_int(fields[name]):
                raise LedgerError("E_LEDGER_CHECKPOINT", f"an operation {name} that is not an integer")
        for name in ("idempotency_key", "request_digest", "owner"):
            if type(fields[name]) is not str:
                raise LedgerError("E_LEDGER_CHECKPOINT", f"an operation {name} that is not text")
        candidate_hash = fields["candidate_hash"]
        if candidate_hash is not None and type(candidate_hash) is not str:
            raise LedgerError("E_LEDGER_CHECKPOINT", "a candidate hash that is neither text nor null")
        resource_keys = fields["resource_keys"]
        if type(resource_keys) is not list or not all(type(item) is str for item in resource_keys):
            raise LedgerError("E_LEDGER_CHECKPOINT", "a resource key that is not text")
        if type(fields["started"]) is not bool:
            raise LedgerError("E_LEDGER_CHECKPOINT", "a started that is not a boolean")
        run = fields["run"]
        if run is not None and type(run) is not dict:
            raise LedgerError("E_LEDGER_CHECKPOINT", "a run descriptor that is neither an object nor null")
        usage_raw = fields["usage"]
        if type(usage_raw) is not dict:
            raise LedgerError("E_LEDGER_CHECKPOINT", "usage that is not an object")
        usage: dict[str, _Usage] = {}
        for dimension, item in usage_raw.items():
            observed = _fields_of(item, _USAGE_FIELDS, "observation")
            amount = observed["amount"]
            if (type(observed["call_key"]) is not str or not _exact_int(observed["revision"])
                    or (amount is not None and not _exact_int(amount))
                    or type(observed["final"]) is not bool
                    or type(observed["evidence_hash"]) is not str):
                raise LedgerError("E_LEDGER_CHECKPOINT", "an observation field of the wrong type")
            usage[dimension] = _Usage(call_key=observed["call_key"], revision=observed["revision"],
                                      amount=amount, final=observed["final"],
                                      evidence_hash=observed["evidence_hash"])
        operations[operation_id] = _Operation(
            operation_id=operation_id, request_id=fields["request_id"], task_id=fields["task_id"],
            plan_version=fields["plan_version"], guard_version=fields["guard_version"],
            idempotency_key=fields["idempotency_key"], request_digest=fields["request_digest"],
            owner=fields["owner"], generation=fields["generation"], lease_until=fields["lease_until"],
            state=fields["state"], reservation=_int_map(fields["reservation"]),
            resource_keys=tuple(resource_keys), candidate_hash=candidate_hash, usage=usage,
            run=copy.deepcopy(run), started=fields["started"])

    idempotency_raw = state["idempotency"]
    if type(idempotency_raw) is not list:
        raise LedgerError("E_LEDGER_CHECKPOINT", "idempotency entries that are not an array")
    idempotency: dict[tuple[str, str], str] = {}
    for entry in idempotency_raw:
        if type(entry) is not list or len(entry) != 3:
            raise LedgerError("E_LEDGER_CHECKPOINT", "an idempotency entry that is not a triple")
        request_id, key, operation_id = entry
        if type(request_id) is not str or type(key) is not str or type(operation_id) is not str:
            raise LedgerError("E_LEDGER_CHECKPOINT", "an idempotency entry that is not text")
        operation = operations.get(operation_id)
        if operation is None or operation.request_id != request_id or operation.idempotency_key != key:
            raise LedgerError("E_LEDGER_CHECKPOINT", "an idempotency entry that does not name its own operation")
        if (request_id, key) in idempotency:
            raise LedgerError("E_LEDGER_CHECKPOINT", "an idempotency key recorded twice")
        idempotency[(request_id, key)] = operation_id
    # Every operation is reached by exactly one key: a claim without one, or a
    # second one, describes a ledger the Scheduler never held.
    if len(idempotency) != len(operations):
        raise LedgerError("E_LEDGER_CHECKPOINT", "an operation without exactly one idempotency key")

    holds_raw = state["resource_holds"]
    if type(holds_raw) is not list:
        raise LedgerError("E_LEDGER_CHECKPOINT", "resource holds that are not an array")
    resource_holds: dict[tuple[str, str], str] = {}
    for entry in holds_raw:
        if type(entry) is not list or len(entry) != 3:
            raise LedgerError("E_LEDGER_CHECKPOINT", "a resource hold that is not a triple")
        repo_id, resource, operation_id = entry
        if type(repo_id) is not str or type(resource) is not str or type(operation_id) is not str:
            raise LedgerError("E_LEDGER_CHECKPOINT", "a resource hold that is not text")
        operation = operations.get(operation_id)
        if operation is None or requests[operation.request_id].repo_id != repo_id:
            raise LedgerError("E_LEDGER_CHECKPOINT", "a resource hold of another repository")
        if resource not in operation.resource_keys:
            raise LedgerError("E_LEDGER_CHECKPOINT", "a resource hold its operation does not hold")
        if (repo_id, resource) in resource_holds:
            raise LedgerError("E_LEDGER_CHECKPOINT", "a resource held twice")
        resource_holds[(repo_id, resource)] = operation_id

    events_raw = state["events"]
    if type(events_raw) is not list:
        raise LedgerError("E_LEDGER_CHECKPOINT", "events that are not an array")
    for index, event in enumerate(events_raw, start=1):
        if type(event) is not dict or set(event) != _EVENT_KEYS:
            raise LedgerError("E_LEDGER_CHECKPOINT", "an event without exactly its own keys")
        if not _exact_int(event["seq"]) or event["seq"] != index:
            raise LedgerError("E_LEDGER_CHECKPOINT", f"an event numbered {event['seq']!r} where {index} was expected")
        if (event["operation_id"] is not None and type(event["operation_id"]) is not str
                or type(event["type"]) is not str or not _exact_int(event["generation"])):
            raise LedgerError("E_LEDGER_CHECKPOINT", "an event field of the wrong type")

    scheduler._requests = requests
    scheduler._tasks = tasks
    scheduler._operations = operations
    scheduler._idempotency = idempotency
    scheduler._resource_holds = resource_holds
    scheduler._events = list(events_raw)
    return scheduler


def _read_checkpoint(db: sqlite3.Connection) -> tuple[int, str, str] | None:
    """The one checkpoint row, verified -- or ``None`` when there is none.

    At most one row: the table's key is the sequence number, so a second row is
    a file two writers disagreed about and is refused.  The row's own digest
    covers every other column of it.
    """
    try:
        rows = db.execute("SELECT seq, last_sha, state, sha256 FROM checkpoint").fetchall()
    except sqlite3.Error as exc:
        if _missing_table(exc):
            raise LedgerError("E_LEDGER_FORMAT", "the checkpoint table is missing") from exc
        raise LedgerError("E_LEDGER_OPEN", str(exc)) from exc
    if not rows:
        return None
    if len(rows) > 1:
        raise LedgerError("E_LEDGER_CHECKPOINT", "more than one checkpoint row")
    row = rows[0]
    if len(row) != 4:
        raise LedgerError("E_LEDGER_CHECKPOINT", "a checkpoint row without four columns")
    seq, last_sha, state, digest = row
    if not _exact_int(seq) or seq < 0:
        raise LedgerError("E_LEDGER_CHECKPOINT", f"a checkpoint sequence {seq!r}")
    if type(last_sha) is not str or type(state) is not str or type(digest) is not str:
        raise LedgerError("E_LEDGER_CHECKPOINT", "a checkpoint row with a non-text column")
    if seq == 0:
        # The empty state has no command to link to.
        if last_sha != "":
            raise LedgerError("E_LEDGER_CHECKPOINT", "a link above sequence 0")
    elif _LINK.fullmatch(last_sha) is None:
        raise LedgerError("E_LEDGER_CHECKPOINT", "a link that is not a command digest")
    if digest != checkpoint_sha256(seq, last_sha, state):
        raise LedgerError("E_LEDGER_CHECKPOINT", "a checkpoint digest that does not verify")
    return seq, last_sha, state


def _verify_chain(rows: list[tuple[Any, ...]], start_seq: int = 0, previous: str = "") -> tuple[list[str], str]:
    """Check the numbering and the hash chain; return the commands and the last digest.

    ``start_seq`` and ``previous`` are where the rows begin: the sequence numbers
    run from ``start_seq + 1`` and the first row has to link to ``previous``,
    which is the digest of the command before them -- the empty string when the
    rows are the whole log, a checkpoint's link when they are the tail after one.
    """
    commands: list[str] = []
    for offset, row in enumerate(rows, start=1):
        expected_seq = start_seq + offset
        if len(row) != 4:
            raise LedgerError("E_LEDGER_CHAIN", "a command row without four columns")
        seq, command, digest, row_previous = row
        if type(seq) is not int or seq != expected_seq:
            raise LedgerError("E_LEDGER_CHAIN", f"sequence {seq!r} where {expected_seq} was expected")
        if (type(command) is not str or type(digest) is not str or type(row_previous) is not str):
            raise LedgerError("E_LEDGER_CHAIN", "a command row with a non-text column")
        if row_previous != previous or digest != entry_sha256(previous, command):
            raise LedgerError("E_LEDGER_CHAIN", f"the chain breaks at sequence {seq}")
        previous = digest
        commands.append(command)
    return commands, previous


def _head(db: sqlite3.Connection) -> tuple[int, str]:
    """The identity of the state the log holds: ``(seq, sha)`` of its last command.

    The last command of the ``commands`` table when there is one; otherwise the
    checkpoint's own ``(seq, last_sha)``; otherwise the empty log.  This is what
    an instance compares itself against before it appends or compacts: a
    compaction by another instance leaves it unchanged -- the state the log holds
    did not move -- while an append by one does not.
    """
    row = db.execute("SELECT seq, sha256 FROM commands ORDER BY seq DESC LIMIT 1").fetchone()
    if row is not None:
        return row[0], row[1]
    row = db.execute("SELECT seq, last_sha FROM checkpoint").fetchone()
    if row is not None:
        return row[0], row[1]
    return 0, ""


def _stored_generation(meta: Mapping[str, str]) -> int:
    """The stored generation as an ``int``, or a damaged ledger."""
    text = meta["generation"]
    if _CANONICAL_INTEGER.fullmatch(text) is None or len(text) > _MAX_GENERATION_DIGITS:
        raise LedgerError("E_LEDGER_FORMAT", f"generation {text!r} is not a decimal integer")
    return int(text)


def _read_meta(db: sqlite3.Connection) -> dict[str, str]:
    """The two meta rows, checked for exactly this format and generation."""
    try:
        rows = db.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.Error as exc:
        if _missing_table(exc):
            raise LedgerError("E_LEDGER_FORMAT", "the meta table is missing") from exc
        raise LedgerError("E_LEDGER_OPEN", str(exc)) from exc
    meta: dict[str, str] = {}
    for key, value in rows:
        if type(key) is not str or type(value) is not str or key in meta:
            raise LedgerError("E_LEDGER_FORMAT", "a meta row that is not unique text")
        meta[key] = value
    if set(meta) != {"format", "generation"} or meta["format"] != LEDGER_FORMAT:
        raise LedgerError("E_LEDGER_FORMAT", "unexpected meta keys or format")
    if _CANONICAL_INTEGER.fullmatch(meta["generation"]) is None:
        raise LedgerError("E_LEDGER_FORMAT", f"generation {meta['generation']!r} is not a decimal integer")
    return meta


class _ReplayScheduler(Scheduler):
    """In-memory ``Scheduler`` that reuses the operation id a claim recorded.

    Replaying a log has to rebuild the state of the process that wrote it, ids
    included.  ``Scheduler._new_operation_id`` is the single place that decides
    them, so the replay injects the recorded id there and then checks that the
    claim really returned it.
    """

    def __init__(self, *, generation: int):
        super().__init__(generation=generation)
        self._recorded_operation_id: str | None = None

    def _new_operation_id(self) -> str:
        recorded = self._recorded_operation_id
        if recorded is None:
            return super()._new_operation_id()
        self._recorded_operation_id = None
        return recorded


def _check_claim(result: Any, operation_id: str, arguments: Mapping[str, Any],
                  used: dict[str, tuple[Any, Any]]) -> None:
    """A recorded claim must have replayed to exactly that operation."""
    if type(result) is not dict or result.get("operation_id") != operation_id:
        raise LedgerError("E_LEDGER_REPLAY", "the claim replayed to another operation")
    # Two claims may carry the same operation id only when the second one is the
    # idempotent replay of the first: the same request and the same key.
    identity = (arguments.get("request"), arguments.get("key"))
    previous = used.get(operation_id)
    if previous is None:
        used[operation_id] = identity
    elif previous != identity:
        raise LedgerError("E_LEDGER_REPLAY", f"two claims recorded with the operation id {operation_id}")


def _replay(scheduler: _ReplayScheduler, command_text: str,
            used: dict[str, tuple[Any, Any]]) -> None:
    """Apply one logged command; anything unexpected refuses the whole ledger."""
    command = _strict_loads(command_text)
    if type(command) is not dict:
        raise LedgerError("E_LEDGER_REPLAY", "a command that is not an object")
    method = command.get("method")
    if method == "claim":
        expected = {"method", "arguments", "operation_id"}
    else:
        expected = {"method", "arguments"}
    if set(command) != expected:
        raise LedgerError("E_LEDGER_REPLAY", "a command without exactly its own keys")
    if type(method) is not str or method not in MUTATING_METHODS:
        raise LedgerError("E_LEDGER_REPLAY", f"unknown method {method!r}")
    arguments = command["arguments"]
    if type(arguments) is not dict:
        raise LedgerError("E_LEDGER_REPLAY", "arguments that are not an object")
    operation_id = command.get("operation_id")
    if method == "claim" and (type(operation_id) is not str or _OPERATION_ID.fullmatch(operation_id) is None):
        raise LedgerError("E_LEDGER_REPLAY", "a recorded operation id the Scheduler would not generate")
    scheduler._recorded_operation_id = operation_id
    try:
        result = getattr(scheduler, method)(**arguments)
        if method == "claim":
            _check_claim(result, operation_id, arguments, used)
    except LedgerError:
        raise
    except Exception as exc:  # a refusal, a broken parser, anything else
        raise LedgerError("E_LEDGER_REPLAY", str(exc)) from exc
    finally:
        scheduler._recorded_operation_id = None


# --------------------------------------------------------------------------- #
# the durable ledger
# --------------------------------------------------------------------------- #
class DurableScheduler:
    """A :class:`Scheduler` whose successful mutations live in a command log.

    One instance is the only writer of its log: every mutation is applied in
    memory, then appended inside one ``BEGIN IMMEDIATE`` transaction that first
    checks the log head against what this instance already applied.  A different
    head means another instance moved the log on and this one is stale.  Any
    failure of the append invalidates the instance -- it refuses mutations and
    reads alike -- so the caller has to reopen (replay) the ledger.

    An exception from the in-memory call invalidates the instance the same way,
    unless it is the Scheduler's own ``ContractError``: a refusal leaves the
    ledger alone, while anything else (an interrupt above all) may have changed
    the in-memory state already, and a log that does not know about it would no
    longer replay.  A wrong signature is refused before the call and leaves the
    instance usable.

    :meth:`compact` writes a checkpoint of the whole state and removes the
    commands it covers, in one transaction, and leaves the instance's identity
    ``(seq, sha)`` -- and with it the instance -- where it was.
    """

    def __init__(self, path: str, scheduler: Scheduler, db: sqlite3.Connection, seq: int, last_sha: str,
                 checkpoint_seq: int | None = None):
        self._path = path
        self._scheduler = scheduler
        self._db: sqlite3.Connection | None = db
        self._seq = seq
        self._last_sha = last_sha
        #: The sequence number of the checkpoint this instance's state starts
        #: from, or ``None`` when the ledger holds none.  It is read the way
        #: every other read is and never touches the log.
        self._checkpoint_seq = checkpoint_seq
        self._lock = threading.RLock()

    # -- read-only surface -------------------------------------------------- #
    @property
    def path(self) -> str:
        return self._path

    @property
    def seq(self) -> int:
        """The sequence number of the last command this instance's state covers.

        Not how many commands the log holds: after a compaction the commands it
        covered are gone, and the number is the checkpoint's sequence plus the
        commands replayed after it.  An uncompacted ledger reads exactly as it
        always did.
        """
        with self._lock:
            self._require_available()
            return self._seq

    @property
    def generation(self) -> int:
        with self._lock:
            self._require_available()
            return self._scheduler.generation

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            self._require_available()
            return self._scheduler.events

    @property
    def checkpoint_seq(self) -> int | None:
        """The sequence number of the checkpoint this instance's state starts from.

        ``None`` when the ledger holds no checkpoint: the whole history is then
        the commands the log still holds.  A compaction writes one covering
        everything this instance has applied, so it moves to this instance's own
        sequence number -- and stays there for the commands appended after it.

        Read like every other read: an unavailable instance refuses it with
        ``E_LEDGER_UNAVAILABLE`` and the log is never touched.
        """
        with self._lock:
            self._require_available()
            return self._checkpoint_seq

    def snapshot(self, request: str, task: str | None = None) -> dict[str, dict[str, int]]:
        with self._lock:
            self._require_available()
            return self._scheduler.snapshot(request, task)

    def operation(self, operation: str) -> dict[str, Any]:
        with self._lock:
            self._require_available()
            return self._scheduler.operation(operation)

    def operation_ids(self) -> tuple[str, ...]:
        """Every operation id the replayed ledger knows, in claim order.

        The read is served from the in-memory ``Scheduler`` exactly as
        :meth:`operation` is: an unavailable instance refuses it with
        ``E_LEDGER_UNAVAILABLE``, and the log is never touched.  This is the
        list a restarted controller walks to find the operations it has to
        decide, and it is the order the log replays in, not a sorted one.
        """
        with self._lock:
            self._require_available()
            return self._scheduler.operation_ids()

    def request_ids(self) -> tuple[str, ...]:
        """Every request id the replayed ledger knows, in seeding order.

        Served from the in-memory ``Scheduler`` exactly as :meth:`operation` is:
        an unavailable instance refuses it with ``E_LEDGER_UNAVAILABLE`` and the
        log is never touched.  A command that has to name a request -- a stop, a
        resume, a budget snapshot -- needs the ids, and they were only derivable
        from the event log before.
        """
        with self._lock:
            self._require_available()
            return self._scheduler.request_ids()

    def operation_usage(self, operation: str) -> dict[str, dict[str, Any]]:
        """The usage one operation has recorded, dimension by dimension.

        The observations travel with the commands that recorded them (``observe``,
        ``settle``, the dispatch a ``handoff`` records on its own), so a reopened
        ledger reads back exactly what the writer saw.  It is what tells an
        operator which dimension of an operation is still unknown, which
        :meth:`snapshot` only counts.  Like every other read it is served from
        memory and an unavailable instance refuses it with
        ``E_LEDGER_UNAVAILABLE``; the log is never touched.
        """
        with self._lock:
            self._require_available()
            return self._scheduler.operation_usage(operation)

    def run_descriptor(self, operation: str) -> dict[str, Any] | None:
        """The run descriptor the ledger recorded for one operation, or ``None``.

        The descriptor travels with the ``handoff`` command, so it is replayed
        with everything else and read back unchanged after a reopen.  Like every
        other read it is served from memory and an unavailable instance refuses
        it with ``E_LEDGER_UNAVAILABLE``.
        """
        with self._lock:
            self._require_available()
            return self._scheduler.run_descriptor(operation)

    def stop_reasons(self, request: str) -> tuple[str, ...]:
        """Every stop reason recorded for a request since its last resume.

        The reasons travel with the commands that recorded them (``stop``,
        ``rollover``, ``unknown``, the usage observations that trip the budget
        guard) and are replayed with them, so a reopened ledger reads back
        exactly what the writer saw.  It is the list an operator has to name back
        in ``resume``, and it is read from memory exactly as :meth:`operation`
        is: an unavailable instance refuses it with ``E_LEDGER_UNAVAILABLE`` and
        the log is never touched.
        """
        with self._lock:
            self._require_available()
            return self._scheduler.stop_reasons(request)

    # -- lifecycle ---------------------------------------------------------- #
    def close(self) -> None:
        """Close the log; the instance refuses every call from here on."""
        with self._lock:
            self._invalidate()

    def __enter__(self) -> "DurableScheduler":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self.close()
        return False

    # -- internals ---------------------------------------------------------- #
    def _require_available(self) -> None:
        if self._db is None:
            raise LedgerError("E_LEDGER_UNAVAILABLE", "the ledger instance is closed or has failed")

    def _invalidate(self) -> None:
        db, self._db = self._db, None
        if db is not None:
            try:
                db.close()
            except Exception:  # the instance is gone either way
                pass

    def _apply(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        with self._lock:
            self._require_available()
            target = getattr(self._scheduler, name)
            # Bound before anything runs: a wrong signature raises ``TypeError``
            # here and leaves the ledger alone.  ``apply_defaults`` records every
            # parameter, defaults included, by name.
            bound = inspect.signature(target).bind(*args, **kwargs)
            bound.apply_defaults()
            # In-memory first: a rejection by the Scheduler raises here, records
            # nothing and leaves the instance usable.  Anything else -- an
            # interrupt above all -- may already have changed the in-memory state
            # while the log has nothing, so the instance is invalidated and the
            # exception is passed on as it is.
            try:
                result = target(*args, **kwargs)
            except ContractError:
                raise
            except BaseException:
                self._invalidate()
                raise
            try:
                raw = _encode_command(name, bound.arguments, result)
                self._append(raw)
            except LedgerError:
                self._invalidate()
                raise
            except Exception as exc:
                self._invalidate()
                raise LedgerError("E_LEDGER_WRITE", str(exc)) from exc
            except BaseException:
                self._invalidate()
                raise
            return result

    def _append(self, raw: bytes) -> None:
        db = self._db
        if db is None:
            raise LedgerError("E_LEDGER_UNAVAILABLE", "the ledger instance is closed or has failed")
        text = raw.decode("utf-8")
        previous = self._last_sha
        digest = entry_sha256(previous, text)
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise LedgerError("E_LEDGER_WRITE", str(exc)) from exc
        try:
            if _head(db) != (self._seq, self._last_sha):
                raise LedgerError("E_LEDGER_STALE", "the log moved on past this instance")
            db.execute("INSERT INTO commands(seq, command, sha256, previous) VALUES (?, ?, ?, ?)",
                       (self._seq + 1, text, digest, previous))
            db.execute("COMMIT")
        except BaseException:
            _rollback(db)
            raise
        self._seq += 1
        self._last_sha = digest

    def compact(self) -> dict[str, Any]:
        """Write a checkpoint of the whole state and remove the commands it covers.

        One ``BEGIN IMMEDIATE`` transaction does all of it: the log head is
        checked against this instance's own ``(seq, sha)`` -- a different one
        means another instance appended and this one is stale -- the checkpoint
        row is replaced, and every command at or below this instance's sequence
        number is deleted.  The instance's identity does not change, so it stays
        usable and its next append is the command after the checkpoint, linked to
        it exactly as it would have been without the compaction.

        This is not a mutating method: no command is recorded, no event is
        emitted and the generation does not move.  Compaction is explicit -- an
        operator's ``smctl ledger compact`` or a controller's own call -- because
        when to drop the commands an audit needs is policy, not ledger.

        The failures follow the append's rules: a ledger error is passed on, any
        other exception becomes ``E_LEDGER_WRITE``, and the instance is
        invalidated either way -- a half-written checkpoint is not a state this
        instance can keep appending to.  A stale instance is refused before
        anything is written.
        """
        with self._lock:
            self._require_available()
            try:
                state = _encode_state(self._scheduler)
                digest = checkpoint_sha256(self._seq, self._last_sha, state)
                db = self._db
                if db is None:  # pragma: no cover -- ``_require_available`` just checked
                    raise LedgerError("E_LEDGER_UNAVAILABLE", "the ledger instance is closed or has failed")
                try:
                    db.execute("BEGIN IMMEDIATE")
                except sqlite3.Error as exc:
                    raise LedgerError("E_LEDGER_WRITE", str(exc)) from exc
                try:
                    if _head(db) != (self._seq, self._last_sha):
                        raise LedgerError("E_LEDGER_STALE", "the log moved on past this instance")
                    db.execute("DELETE FROM checkpoint")
                    db.execute("INSERT INTO checkpoint(seq, last_sha, state, sha256) VALUES (?, ?, ?, ?)",
                               (self._seq, self._last_sha, state, digest))
                    cursor = db.execute("DELETE FROM commands WHERE seq <= ?", (self._seq,))
                    removed = cursor.rowcount
                    db.execute("COMMIT")
                except BaseException:
                    _rollback(db)
                    raise
            except LedgerError:
                self._invalidate()
                raise
            except Exception as exc:
                self._invalidate()
                raise LedgerError("E_LEDGER_WRITE", str(exc)) from exc
            except BaseException:
                self._invalidate()
                raise
            # The checkpoint is written and committed: the instance's state now
            # starts from it.  A failure above left the ledger -- and this
            # number -- where it was.
            self._checkpoint_seq = self._seq
            return {"seq": self._seq, "last_sha": self._last_sha, "sha256": digest, "removed": removed}


def _make_method(name: str):
    """A ``DurableScheduler`` method with the signature of the ``Scheduler`` one."""
    target = getattr(Scheduler, name)

    def method(self: DurableScheduler, *args: Any, **kwargs: Any) -> Any:
        return self._apply(name, args, kwargs)

    method.__name__ = name
    method.__qualname__ = f"{DurableScheduler.__name__}.{name}"
    method.__doc__ = target.__doc__
    method.__signature__ = inspect.signature(target)  # type: ignore[attr-defined]
    return method


for _name in MUTATING_METHODS:
    setattr(DurableScheduler, _name, _make_method(_name))
# ``reserve`` is the same call as ``claim``, exactly as on ``Scheduler``.
DurableScheduler.reserve = DurableScheduler.claim  # type: ignore[attr-defined]
del _name


# --------------------------------------------------------------------------- #
# opening and creating
# --------------------------------------------------------------------------- #
def create_ledger(path: str, *, generation: int = 1) -> DurableScheduler:
    """Create a new ledger at ``path``; refuse a path that already has anything.

    The file itself is created atomically with ``O_CREAT | O_EXCL``, so a path
    another process got to first is refused instead of taken over.  Should
    anything fail after that, the path is removed only while it still is the file
    this call created -- same device and inode -- and the error is
    ``E_LEDGER_OPEN``.
    """
    location = _check_path(path)
    # The in-memory ledger comes first: a refused generation raises its own
    # ``ContractError`` and leaves no file behind.
    scheduler = Scheduler(generation=generation)
    try:
        os.lstat(location)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise LedgerError("E_LEDGER_OPEN", str(exc)) from exc
    else:
        raise LedgerError("E_LEDGER_EXISTS", location)
    # The atomic creation: this call either makes the file itself or refuses.
    try:
        handle = os.open(location, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise LedgerError("E_LEDGER_EXISTS", location) from exc
    except OSError as exc:
        raise LedgerError("E_LEDGER_OPEN", str(exc)) from exc
    try:
        status = os.fstat(handle)
    except BaseException as exc:
        # No identity to prove the file is ours, so it is left where it is.
        try:
            os.close(handle)
        except OSError:
            pass
        if isinstance(exc, Exception):
            raise LedgerError("E_LEDGER_OPEN", str(exc)) from exc
        raise
    identity = (status.st_dev, status.st_ino)
    db: sqlite3.Connection | None = None
    try:
        # From here on the identity is known, so every failure -- closing the
        # creation descriptor included -- removes the file this call made
        # (review of attempt-002, F5).
        os.close(handle)
        db = _connect(location)
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in _SCHEMA:
                db.execute(statement)
            db.execute("INSERT INTO meta(key, value) VALUES ('format', ?)", (LEDGER_FORMAT,))
            db.execute("INSERT INTO meta(key, value) VALUES ('generation', ?)", (str(generation),))
            db.execute("COMMIT")
        except BaseException:
            _rollback(db)
            raise
    except BaseException as exc:
        if db is not None:
            try:
                db.close()
            except Exception:  # the failure at hand is the one to report
                pass
        _remove_created(location, identity)
        if isinstance(exc, LedgerError):
            raise
        if isinstance(exc, Exception):
            raise LedgerError("E_LEDGER_OPEN", str(exc)) from exc
        raise
    return DurableScheduler(location, scheduler, db, 0, "")


def open_ledger(path: str) -> DurableScheduler:
    """Replay the ledger at ``path``; never create one, never repair one.

    The connection is a read-write file URI, so a path that is not there is
    refused instead of created.  Every step after it -- the pragmas, the read,
    the chain check, the checkpoint, the replay Scheduler and the replay itself
    -- closes the connection before reporting, and reports a ledger error only.

    A ledger with a checkpoint is loaded from it and only the commands after it
    are replayed: the tail has to start at the checkpoint's sequence plus one and
    link to its digest, and the state has to verify.  What comes out is the state
    a full replay of the whole log would have produced.
    """
    location = _check_path(path)
    try:
        os.lstat(location)
    except FileNotFoundError:
        raise LedgerError("E_LEDGER_MISSING", location)
    except OSError as exc:
        raise LedgerError("E_LEDGER_OPEN", str(exc)) from exc
    db = _connect(location)
    try:
        try:
            db.execute("BEGIN")
            meta = _read_meta(db)
            checkpoint = _read_checkpoint(db)
            rows = db.execute("SELECT seq, command, sha256, previous FROM commands ORDER BY seq").fetchall()
        except sqlite3.Error as exc:
            if _missing_table(exc):
                raise LedgerError("E_LEDGER_FORMAT", "the commands table is missing") from exc
            raise LedgerError("E_LEDGER_OPEN", str(exc)) from exc
        finally:
            _rollback(db)
        generation = _stored_generation(meta)
        try:
            # Built from the stored generation first, so a meta generation the
            # Scheduler refuses is a damaged ledger whatever the checkpoint says.
            scheduler = _ReplayScheduler(generation=generation)
        except ContractError as exc:
            raise LedgerError("E_LEDGER_FORMAT", str(exc)) from exc
        used: dict[str, tuple[Any, Any]] = {}
        if checkpoint is None:
            start_seq, previous = 0, ""
        else:
            start_seq, previous, state = checkpoint
            scheduler = _decode_state(state, generation)
            # The operations the checkpoint holds are already claimed: a tail
            # claim that reuses one of their ids with another request or key is a
            # forgery, exactly as two claims sharing an id are.
            for operation_id, operation in scheduler._operations.items():
                used[operation_id] = (operation.request_id, operation.idempotency_key)
        commands, last_sha = _verify_chain(rows, start_seq, previous)
        for command_text in commands:
            _replay(scheduler, command_text, used)
    except BaseException:
        try:
            db.close()
        except Exception:  # the ledger error at hand is the one to report
            pass
        raise
    return DurableScheduler(location, scheduler, db, start_seq + len(commands), last_sha,
                           None if checkpoint is None else checkpoint[0])


__all__ = ["LEDGER_FORMAT", "MUTATING_METHODS", "DurableScheduler", "LedgerError", "create_ledger", "open_ledger",
           "entry_sha256", "checkpoint_sha256"]
