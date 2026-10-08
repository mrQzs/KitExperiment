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
``NOT_RUN``; no snapshots, no compaction, no multi-host locking and no ledger on
a network file system are claimed.  Run descriptors and the recovery driver are
a separate contract.

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
"""
from __future__ import annotations

from hashlib import sha256
import inspect
import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Mapping

from .scheduler import ContractError, Scheduler

LEDGER_FORMAT = "sentinelmesh-ledger-v1"

#: Every mutating method of :class:`Scheduler`, in the order the contract fixes.
MUTATING_METHODS = ("seed_request", "seed_task", "claim", "handoff", "mark_running", "renew", "observe", "settle",
                    "finish", "fixture_verdict", "unknown", "release_proven_unstarted", "recover",
                    "cancel_unstarted", "stop", "rollover")

#: A generation written as canonical decimal text: no sign, no leading zeros.
_CANONICAL_INTEGER = re.compile(r"\A(?:0|[1-9][0-9]*)\Z")

#: The longest canonical generation text there can be: 2^53-1 has sixteen digits.
_MAX_GENERATION_DIGITS = 16

#: The one shape ``Scheduler._new_operation_id`` produces: a recorded claim with
#: anything else is a forged log.
_OPERATION_ID = re.compile(r"\Aop-[0-9a-f]{32}\Z")

_SCHEMA = ("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
           "CREATE TABLE commands(seq INTEGER PRIMARY KEY, command TEXT NOT NULL, "
           "sha256 TEXT NOT NULL, previous TEXT NOT NULL)")


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


def _strict_loads(text: str) -> Any:
    """Strict JSON: duplicate keys, NaN and Infinity are refused, never repaired."""

    def object_hook(pairs):
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LedgerError("E_LEDGER_REPLAY", f"duplicate key {key!r}")
            result[key] = value
        return result

    def constant(name):
        raise LedgerError("E_LEDGER_REPLAY", name)

    try:
        return json.loads(text, object_pairs_hook=object_hook, parse_constant=constant)
    except LedgerError:
        raise
    except RecursionError as exc:
        # A command nested past what the parser can walk is a damaged ledger,
        # not a crash of the reader.
        raise LedgerError("E_LEDGER_REPLAY", "a command nested too deeply to parse") from exc
    except ValueError as exc:
        raise LedgerError("E_LEDGER_REPLAY", str(exc)) from exc
    except Exception as exc:
        # ``MemoryError`` above all: the same failure in the replayed call is
        # already ``E_LEDGER_REPLAY`` (review of attempt-002, F4).
        raise LedgerError("E_LEDGER_REPLAY", f"the command could not be parsed: {type(exc).__name__}") from exc


def _verify_chain(rows: list[tuple[Any, ...]]) -> tuple[list[str], str]:
    """Check the numbering and the hash chain; return the commands and the last digest."""
    commands: list[str] = []
    previous = ""
    for expected_seq, row in enumerate(rows, start=1):
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
    """

    def __init__(self, path: str, scheduler: Scheduler, db: sqlite3.Connection, seq: int, last_sha: str):
        self._path = path
        self._scheduler = scheduler
        self._db: sqlite3.Connection | None = db
        self._seq = seq
        self._last_sha = last_sha
        self._lock = threading.RLock()

    # -- read-only surface -------------------------------------------------- #
    @property
    def path(self) -> str:
        return self._path

    @property
    def seq(self) -> int:
        """How many commands this instance has applied."""
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
            head = db.execute("SELECT seq, sha256 FROM commands ORDER BY seq DESC LIMIT 1").fetchone()
            if self._seq == 0:
                if head is not None:
                    raise LedgerError("E_LEDGER_STALE", "the log already has entries")
            elif head is None or head[0] != self._seq or head[1] != self._last_sha:
                raise LedgerError("E_LEDGER_STALE", "the log moved on past this instance")
            db.execute("INSERT INTO commands(seq, command, sha256, previous) VALUES (?, ?, ?, ?)",
                       (self._seq + 1, text, digest, previous))
            db.execute("COMMIT")
        except BaseException:
            _rollback(db)
            raise
        self._seq += 1
        self._last_sha = digest


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
    the chain check, the replay Scheduler and the replay itself -- closes the
    connection before reporting, and reports a ledger error only.
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
            rows = db.execute("SELECT seq, command, sha256, previous FROM commands ORDER BY seq").fetchall()
        except sqlite3.Error as exc:
            if _missing_table(exc):
                raise LedgerError("E_LEDGER_FORMAT", "the commands table is missing") from exc
            raise LedgerError("E_LEDGER_OPEN", str(exc)) from exc
        finally:
            _rollback(db)
        generation = _stored_generation(meta)
        try:
            scheduler = _ReplayScheduler(generation=generation)
        except ContractError as exc:
            # A stored generation the Scheduler refuses is a damaged ledger.
            raise LedgerError("E_LEDGER_FORMAT", str(exc)) from exc
        commands, last_sha = _verify_chain(rows)
        used: dict[str, tuple[Any, Any]] = {}
        for command_text in commands:
            _replay(scheduler, command_text, used)
    except BaseException:
        try:
            db.close()
        except Exception:  # the ledger error at hand is the one to report
            pass
        raise
    return DurableScheduler(location, scheduler, db, len(commands), last_sha)


__all__ = ["LEDGER_FORMAT", "MUTATING_METHODS", "DurableScheduler", "LedgerError", "create_ledger", "open_ledger"]
