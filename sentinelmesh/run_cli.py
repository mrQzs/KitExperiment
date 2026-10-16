"""Operator entry point for one-shot adapter runs: ``smctl run``.

`.agent/design/controller-cli.md` (sections 3, 5 and 6) turns the whole
``claim`` -> ``handoff`` -> ``supervise`` -> record -> ``_reduce`` -> ``settle``
-> ``finish`` chain into commands an operator can start from the command line,
one invocation at a time.  This module holds the four one-shot commands that
design names:

* ``smctl run init`` creates a ledger and seeds one request and its tasks from a
  strictly checked specification file;
* ``smctl run once`` claims exactly one operation and runs exactly one adapter
  invocation under the platform's own supervisor, reporting the outcome exactly
  as the bridge returned it;
* ``smctl run serve`` drains one request: it walks the request's tasks in seeding
  order and, for each task that has a job file and can still be claimed, claims
  and runs exactly one operation through ``once``'s own chain -- no retry, no
  daemon -- reporting every run, every task it skipped and why, and why it
  stopped, as exactly one line; one run at a time, or with ``--parallel N`` the
  very same walk with at most N runs in flight, each claimed run handed to a
  daemon thread of its own while the main thread goes on claiming;
* ``smctl run recover`` takes over what a controller that died left in the ledger:
  it rolls the ledger over exactly once (every request stops with
  ``RECOVERY_REQUIRED``), cancels every claim that never reached a handoff,
  settles every handoff never decided from its own records, and writes the
  driver's report exactly as it is -- it claims nothing, runs nothing, settles
  no usage and reopens nothing, so what the recovery could not decide is the
  operator's, through ``smctl ledger settle``/``resolve``/``resume``.

``init``, ``once`` and ``recover`` each claim at most once and run at most once;
``serve`` walks a whole request in one pass, claiming and running each of its
tasks at most once.  There is no retry and no daemon in any of them, so a command
that is killed leaves at most one undecided operation in the ledger for the
recovery path to take over -- or, for ``serve --parallel N``, at most N, one per
run in flight.  The conventions are
``smctl ledger``'s exactly -- one canonical JSON line on stdout, a refusal as one
JSON line on stderr (the ledger's own code and detail, plus the operation this
call already claimed, or ``None`` when it claimed none) and exit 3, an argparse
usage error with exit 2, and the ledger closed in a ``finally`` whatever
happens.  This module widens no guard: the ledger, the scheduler, the bridge and
the adapter run refuse exactly what they refuse when they are called directly,
and every refusal is reported with its own code, never guessed at.

The child process sees exactly what the job file names and nothing else: its
environment is the job's explicit mapping (``os.environ`` is never read for it),
its working directory is the job's ``cwd`` and its argv is the job's
``command``.  Two adapters are accepted, each by its own ``adapter_id``:
``offline`` reduces normalized events, and ``pi`` translates pi's ``--mode
json`` session v3 frames (dialect ``pi-session-v3``) and reduces them the same
way.  This command starts no provider, resolves no program of its own and reads
no credentials: the program a job names is resolved by the supervisor from the
job's ``PATH``, so a real pi has to be named in the job as the real program
(``node.exe`` with pi's ``cli.js`` on Windows, where the ``pi.cmd`` shim is
refused), and credentials and the network stay out of scope.

Importing this module has no side effect: no file is read, no environment is
read, no ledger is opened and no process is started.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from typing import Any

from .adapter_protocol import AdapterError, Invocation, OfflineAdapter
from .adapter_run import (DEFAULT_MAX_LINE_BYTES, DEFAULT_MAX_STREAM_BYTES, MAX_LINE_BYTES, MAX_STREAM_BYTES,
                          run_adapter_operation)
from .durable_ledger import LedgerError, create_ledger, open_ledger
from .ledger_cli import _write_line
from .pi_events import PiNativeAdapter
from .process_identity import environment_digest
from .runtime import RuntimeErrorCode
from .restart_recovery import recover_after_restart
from .scheduler import ContractError, Scheduler
from .supervised_operation import evidence_digest

#: The largest a specification or job file may be, read in one bounded pass.
_MAX_FILE_BYTES = 65536

#: The exact keys of a request specification file, and of one of its tasks.
_SPEC_KEYS = frozenset({"request_id", "repo_id", "caps", "concurrency", "tasks"})
_TASK_KEYS = frozenset({"task_id", "caps", "max_attempts"})

#: The exact keys of a job file: the required ones, and the two optional ceilings
#: that default to the adapter run's own.
_JOB_REQUIRED = frozenset({"request_id", "task_id", "plan", "idempotency_key", "guard_version", "reservation",
                           "resources", "adapter_id", "role", "profile_id", "command", "capabilities", "cwd",
                           "environment", "allowed_secret_names", "timeout_seconds", "usage_dimensions"})
_JOB_OPTIONAL = frozenset({"max_stream_bytes", "max_line_bytes"})
#: The job keys that must be exact, non-empty text.
_JOB_TEXT = ("request_id", "task_id", "idempotency_key", "adapter_id", "role", "profile_id", "cwd")
#: The job keys that must be exact integers of at least one.
_JOB_POSITIVE = ("plan", "guard_version", "timeout_seconds")
#: The job keys that must be arrays of distinct strings (order kept as written).
_JOB_STRING_LISTS = ("resources", "capabilities", "allowed_secret_names", "usage_dimensions")

#: The adapters this contract accepts, keyed by each adapter's own ``adapter_id``:
#: ``offline`` is the offline stub and ``pi`` the translator of pi's ``--mode
#: json`` session v3 frames.  There is no alias and no case or whitespace folding:
#: a job names one of these keys exactly.  Neither adapter starts a provider, so
#: the program the job names is still the supervisor's to resolve.
_ADAPTERS = {OfflineAdapter.adapter_id: OfflineAdapter, PiNativeAdapter.adapter_id: PiNativeAdapter}

#: The claim-refusal codes ``serve`` reads as "this task cannot be claimed right
#: now" and skips, as opposed to the ones that mean the ledger must not move and
#: stop the whole loop: an active or review-pending operation of the task, a task
#: already accepted, and a task out of attempts.  Every other refusal code --
#: ``E_STOPPED``, ``E_GUARD_STALE``, ``E_IDEMPOTENCY_CONFLICT`` and any ledger
#: error -- stops the loop instead.
_SERVE_SKIP = frozenset({"E_ACTIVE_TASK", "E_TASK_DONE", "E_ATTEMPTS"})

#: The claim-refusal codes ``serve --parallel`` reads as "one of this call's own
#: runs in flight holds the slot, the resource key or the budget", as opposed to
#: the ones that mean somebody else does and the walk must stop: the request's
#: concurrency, a resource key another operation holds, and a budget the
#: reservations of the runs in flight already use up.  With none of this call's
#: runs in flight there is nothing of ours to wait for, and the code is an
#: ordinary stop instead.
_SERVE_WAIT = frozenset({"E_CONCURRENCY", "E_RESOURCE_BUSY", "E_BUDGET_INSUFFICIENT"})

#: The longest the main thread of ``serve --parallel`` blocks in one wait -- for
#: a free slot, for a run that holds a slot, a resource or the budget, or for the
#: runs still in flight when the walk stops.  A wait never blocks without a
#: timeout: on Windows a lock wait without one can keep a ``KeyboardInterrupt``
#: from ever reaching the main thread, and the first Ctrl-C has to be seen.
_POLL_SECONDS = 0.1


class RunSpecError(Exception):
    """A specification or job file the command refuses before touching a ledger.

    ``code`` is the stable refusal code (``E_RUN_SPEC`` for a request
    specification, ``E_RUN_JOB`` for a job) and ``detail`` names the item that
    failed, so the one JSON line on stderr reads like any ledger refusal.  It is
    never a scheduler or ledger error: those keep their own codes.
    """

    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")


def _is_int(value: Any) -> bool:
    """An exact integer: a ``bool`` is not one, and neither is a ``float``."""
    return type(value) is int


def _nonempty_str(value: Any) -> bool:
    return type(value) is str and bool(value)


def _caps_ok(caps: Any) -> bool:
    """A non-empty object whose every value is an exact integer of at least zero.

    The dimension *names* are the scheduler's to accept or refuse (``_caps``,
    ``E_BUDGET_DIMENSION``); a name that reads oddly passes this file check and
    is refused by the rehearsal instead, before any file exists.
    """
    return type(caps) is dict and bool(caps) and all(_is_int(value) and value >= 0 for value in caps.values())


def _string_list(value: Any, name: str, code: str) -> None:
    """Require an array of distinct strings, raising ``RunSpecError`` otherwise."""
    if type(value) is not list or any(type(item) is not str for item in value):
        raise RunSpecError(code, f"{name} must be an array of strings")
    if len(set(value)) != len(value):
        raise RunSpecError(code, f"{name} contains a duplicate entry")


def _read_json(path: str, code: str, label: str) -> Any:
    """Read one bounded file and parse it as UTF-8 JSON, or refuse it.

    At most :data:`_MAX_FILE_BYTES` bytes are read: a file one byte larger is
    refused rather than parsed, and a file exactly at the limit is read.  A file
    that cannot be opened or read, or that is not UTF-8 JSON, is refused with the
    caller's own code (``E_RUN_SPEC`` or ``E_RUN_JOB``).
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise RunSpecError(code, f"the {label} could not be read: {exc}") from exc
    if len(raw) > _MAX_FILE_BYTES:
        raise RunSpecError(code, f"the {label} is larger than {_MAX_FILE_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # ``RecursionError`` is what ``json`` raises for nesting deeper than
        # the interpreter allows, and a file nested that deep fits the byte
        # ceiling with room to spare (review of attempt-001, F1).
        raise RunSpecError(code, f"the {label} is not UTF-8 JSON: {exc}") from exc


def load_spec(path: str) -> dict[str, Any]:
    """Read and strictly check a request specification file, or refuse it.

    The file is one object with exactly ``request_id``, ``repo_id``, ``caps``,
    ``concurrency`` and ``tasks``; a non-empty ``request_id``/``repo_id``; a
    non-empty ``caps`` object of non-negative integers; a ``concurrency`` of at
    least one; and a non-empty ``tasks`` array whose items each carry exactly
    ``task_id``, ``caps`` and ``max_attempts`` (``max_attempts`` at least one)
    with no ``task_id`` repeated.  Every violation -- a missing or extra key, a
    wrong type, an empty text field, a negative or non-integer amount -- is an
    ``E_RUN_SPEC`` whose detail names the item.  What the scheduler itself
    refuses (a dimension name it does not accept, a task subbudget it rejects) is
    left for the rehearsal, not decided here.
    """
    value = _read_json(path, "E_RUN_SPEC", "specification")
    if type(value) is not dict or set(value) != _SPEC_KEYS:
        raise RunSpecError("E_RUN_SPEC",
                           "the specification must be an object with exactly %s" % ", ".join(sorted(_SPEC_KEYS)))
    if not _nonempty_str(value["request_id"]):
        raise RunSpecError("E_RUN_SPEC", "request_id must be a non-empty string")
    if not _nonempty_str(value["repo_id"]):
        raise RunSpecError("E_RUN_SPEC", "repo_id must be a non-empty string")
    if not _caps_ok(value["caps"]):
        raise RunSpecError("E_RUN_SPEC", "caps must be a non-empty object of non-negative integers")
    if not _is_int(value["concurrency"]) or value["concurrency"] < 1:
        raise RunSpecError("E_RUN_SPEC", "concurrency must be an integer of at least 1")
    tasks = value["tasks"]
    if type(tasks) is not list or not tasks:
        raise RunSpecError("E_RUN_SPEC", "tasks must be a non-empty array")
    seen: set[str] = set()
    for task in tasks:
        if type(task) is not dict or set(task) != _TASK_KEYS:
            raise RunSpecError("E_RUN_SPEC",
                               "each task must be an object with exactly %s" % ", ".join(sorted(_TASK_KEYS)))
        if not _nonempty_str(task["task_id"]):
            raise RunSpecError("E_RUN_SPEC", "task_id must be a non-empty string")
        if not _caps_ok(task["caps"]):
            raise RunSpecError("E_RUN_SPEC", "task caps must be a non-empty object of non-negative integers")
        if not _is_int(task["max_attempts"]) or task["max_attempts"] < 1:
            raise RunSpecError("E_RUN_SPEC", "max_attempts must be an integer of at least 1")
        if task["task_id"] in seen:
            raise RunSpecError("E_RUN_SPEC", f"task_id {task['task_id']!r} is repeated")
        seen.add(task["task_id"])
    return value


def load_job(path: str) -> dict[str, Any]:
    """Read and strictly check a job file, or refuse it.

    The file is one object with exactly the required keys and, optionally,
    ``max_stream_bytes`` and ``max_line_bytes`` (defaulting to the adapter run's
    own ceilings).  The text keys are non-empty strings; ``plan``,
    ``guard_version`` and ``timeout_seconds`` are integers of at least one;
    ``reservation`` is a non-empty object of non-negative integers; ``resources``,
    ``capabilities``, ``allowed_secret_names`` and ``usage_dimensions`` are arrays
    of distinct strings; ``command`` is a non-empty array of strings (repeats
    allowed); ``environment`` is a string-to-string object (possibly empty)
    whose names and values the invocation's own ``environment_digest`` accepts;
    ``adapter_id`` is one of :data:`_ADAPTERS` (matched exactly, no alias, no case
    or whitespace folding); and the two ceilings are integers within their own
    bounds.  Every violation is an ``E_RUN_JOB`` whose detail
    names the item.  The returned mapping is the file's own content with the two
    ceilings filled in.
    """
    value = _read_json(path, "E_RUN_JOB", "job")
    if type(value) is not dict:
        raise RunSpecError("E_RUN_JOB", "the job must be an object")
    keys = set(value)
    if not _JOB_REQUIRED <= keys or not keys <= (_JOB_REQUIRED | _JOB_OPTIONAL):
        raise RunSpecError("E_RUN_JOB", "the job must have exactly the required keys and, at most, "
                                        "%s" % ", ".join(sorted(_JOB_OPTIONAL)))
    for name in _JOB_TEXT:
        if not _nonempty_str(value[name]):
            raise RunSpecError("E_RUN_JOB", f"{name} must be a non-empty string")
    for name in _JOB_POSITIVE:
        if not _is_int(value[name]) or value[name] < 1:
            raise RunSpecError("E_RUN_JOB", f"{name} must be an integer of at least 1")
    reservation = value["reservation"]
    if type(reservation) is not dict or not reservation or not all(_is_int(item) and item >= 0
                                                                   for item in reservation.values()):
        raise RunSpecError("E_RUN_JOB", "reservation must be a non-empty object of non-negative integers")
    for name in _JOB_STRING_LISTS:
        _string_list(value[name], name, "E_RUN_JOB")
    command = value["command"]
    if type(command) is not list or not command or any(type(part) is not str for part in command):
        raise RunSpecError("E_RUN_JOB", "command must be a non-empty array of strings")
    environment = value["environment"]
    if type(environment) is not dict or any(type(key) is not str or type(item) is not str
                                            for key, item in environment.items()):
        raise RunSpecError("E_RUN_JOB", "environment must be an object of strings")
    try:
        # The environment the child will run with, checked here rather than when
        # the invocation is built after the claim: a name or value the digest
        # refuses would otherwise strand a RESERVED operation (review of
        # run-cli-once attempt-001, F4).
        environment_digest(environment)
    except RuntimeErrorCode as exc:
        raise RunSpecError("E_RUN_JOB", "environment: " + str(exc)) from exc
    if value["adapter_id"] not in _ADAPTERS:
        raise RunSpecError("E_RUN_JOB", "adapter_id must be one of %s" % ", ".join(sorted(_ADAPTERS)))
    max_stream_bytes = value.get("max_stream_bytes", DEFAULT_MAX_STREAM_BYTES)
    if not _is_int(max_stream_bytes) or not 1 <= max_stream_bytes <= MAX_STREAM_BYTES:
        raise RunSpecError("E_RUN_JOB", "max_stream_bytes must be an integer in 1..%d" % MAX_STREAM_BYTES)
    max_line_bytes = value.get("max_line_bytes", DEFAULT_MAX_LINE_BYTES)
    if not _is_int(max_line_bytes) or not 1 <= max_line_bytes <= MAX_LINE_BYTES:
        raise RunSpecError("E_RUN_JOB", "max_line_bytes must be an integer in 1..%d" % MAX_LINE_BYTES)
    result = dict(value)
    result["max_stream_bytes"] = max_stream_bytes
    result["max_line_bytes"] = max_line_bytes
    return result


def load_jobs(directory: str, request_id: str) -> dict[str, Any]:
    """Read every per-task job file of one request from a directory, or refuse it.

    ``serve`` keeps one job file per task, named ``<task_id>.json``.  Every
    ``*.json`` file in ``directory`` (read in file-name order, every other file
    ignored) is checked by :func:`load_job` exactly as ``once`` checks a job; a
    file ``load_job`` refuses (its own ``E_RUN_JOB``) is re-raised as
    ``E_RUN_JOBS`` with the file name in front of the original detail.  A file
    whose ``request_id`` is not ``request_id``, or whose ``task_id`` is not the
    file name without its ``.json``, is ``E_RUN_JOBS`` naming the file.  A
    ``directory`` that is not a directory, or that holds no ``*.json`` file at
    all, is ``E_RUN_JOBS``.  The first file that fails refuses the whole
    directory: nothing is returned, so the caller runs nothing.  The result maps
    each task id to its job, exactly as :func:`load_job` returned it.
    """
    if not os.path.isdir(directory):
        raise RunSpecError("E_RUN_JOBS", "%s is not a directory" % directory)
    names = sorted(name for name in os.listdir(directory) if name.endswith(".json"))
    if not names:
        raise RunSpecError("E_RUN_JOBS", "there is no job file (*.json) in %s" % directory)
    jobs: dict[str, Any] = {}
    for name in names:
        try:
            job = load_job(os.path.join(directory, name))
        except RunSpecError as exc:
            raise RunSpecError("E_RUN_JOBS", "%s: %s" % (name, exc.detail)) from exc
        if job["request_id"] != request_id:
            raise RunSpecError("E_RUN_JOBS", "%s: request_id is %r, not %r" % (name, job["request_id"], request_id))
        task_id = job["task_id"]
        if task_id != name[:-len(".json")]:
            raise RunSpecError("E_RUN_JOBS", "%s: task_id is %r, not the file name" % (name, task_id))
        jobs[task_id] = job
    return jobs


def _emit(payload: Any) -> None:
    """One line of canonical JSON on stdout, non-ASCII left as it was written."""
    _write_line(sys.stdout, payload)


def _refuse(exc: Exception, operation_id: str | None) -> int:
    """One line of JSON on stderr with the error's code, detail and operation.

    A ledger or scheduler error -- and the command's own :class:`RunSpecError` --
    carries a ``detail`` that is written as it is (``None`` when there is none);
    an adapter or runtime error carries no detail, so its textual form is used.
    The ``operation_id`` is the operation this call already claimed, or ``None``
    when it claimed none, which is what lets an operator tell a refusal before
    the claim from one that left a reserved operation behind.
    """
    if isinstance(exc, (ContractError, LedgerError, RunSpecError)):
        code, detail = exc.code, exc.detail
    else:  # AdapterError, RuntimeErrorCode: no ``detail`` field of their own.
        code, detail = exc.code, str(exc)
    _write_line(sys.stderr, {"error": code, "detail": detail, "operation_id": operation_id})
    return 3


def _refusal(exc: Exception, operation_id: str | None) -> dict[str, Any]:
    """The one refusal mapping ``serve`` reports on stdout and repeats on stderr.

    Same code/detail split as :func:`_refuse` -- a scheduler, ledger or
    ``RunSpecError`` carries its own ``code`` and ``detail``, an adapter or
    runtime error carries only a code so its textual form is the detail -- wrapped
    as the ``{"error", "detail", "operation_id"}`` object the report and the
    stderr line share, ``operation_id`` being the operation this call already
    claimed and left in the ledger, or ``None`` when it claimed none.
    """
    if isinstance(exc, (ContractError, LedgerError, RunSpecError)):
        code, detail = exc.code, exc.detail
    else:  # AdapterError, RuntimeErrorCode: no ``detail`` field of their own.
        code, detail = exc.code, str(exc)
    return {"error": code, "detail": detail, "operation_id": operation_id}


def _seed_into(scheduler: Any, spec: dict[str, Any]) -> None:
    """Seed the request and every task of ``spec`` onto one scheduler."""
    scheduler.seed_request(spec["request_id"], spec["repo_id"], spec["caps"], spec["concurrency"])
    for task in spec["tasks"]:
        scheduler.seed_task(spec["request_id"], task["task_id"], task["caps"], task["max_attempts"])


def cmd_init(args: argparse.Namespace) -> int:
    """Create a ledger and seed one request and its tasks from a spec file.

    The specification is checked in full first; then it is seeded once on a fresh
    in-memory ``Scheduler`` so a refusal the scheduler would make (a dimension
    name it does not accept, a subbudget it rejects) is reported with its own
    code *before any file exists*; only then is the ledger created (an existing
    path is ``E_LEDGER_EXISTS``) and seeded for real.  A refusal before the
    ledger is created leaves nothing behind; a ledger error while the seeds are
    appended (an I/O failure, the only error the rehearsal cannot rule out) is
    reported the same way and the file is left as it is, for the operator to
    inspect.  The ledger is closed in the ``finally``.
    """
    try:
        spec = load_spec(args.spec)
    except RunSpecError as exc:
        return _refuse(exc, None)
    try:
        # The rehearsal: the scheduler's own guards, on a throwaway instance, so
        # a spec it refuses never reaches ``create_ledger``.
        _seed_into(Scheduler(), spec)
    except ContractError as exc:
        return _refuse(exc, None)
    try:
        ledger = create_ledger(args.ledger)
    except (ContractError, LedgerError) as exc:
        return _refuse(exc, None)
    try:
        try:
            _seed_into(ledger, spec)
            result = {"command": "init", "ledger": args.ledger, "request_id": spec["request_id"],
                      "tasks": [task["task_id"] for task in spec["tasks"]],
                      "generation": ledger.generation, "seq": ledger.seq}
        except (ContractError, LedgerError) as exc:
            return _refuse(exc, None)
    finally:
        ledger.close()
    _emit(result)
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    """Claim exactly one operation and run exactly one adapter invocation.

    The job file is checked in full first (any ``E_RUN_JOB`` happens before the
    ledger is opened); then the ledger is opened (a path that cannot be opened is
    the ledger's own code); then exactly one ``claim`` is made in the ledger's
    current generation.  A claim the ledger refuses is reported with its own code
    and no operation; a claim that replays an earlier one is
    ``E_RUN_CLAIM_REPLAYED`` and names the replayed operation without running
    anything.  Otherwise the invocation is built from the job and handed to
    :func:`~sentinelmesh.adapter_run.run_adapter_operation` exactly once, with the
    child's environment, working directory and argv all taken from the job file.
    The adapter is the one the job's ``adapter_id`` names, taken from
    :data:`_ADAPTERS` and instantiated fresh for this call; nothing else -- the
    dimensions, the ceilings, the timeout, the environment, the working
    directory, the argv -- depends on which adapter it is.

    What the bridge raises before the outcome -- an ``AdapterError``, a scheduler
    ``ContractError``, a ``LedgerError`` or a ``RuntimeErrorCode`` -- is a refusal
    that names the operation already claimed (which stays ``RESERVED``); what it
    returns is the outcome, reported verbatim and exiting 0 even when the state is
    ``NOT_STARTED`` or ``START_UNKNOWN``.  ``KeyboardInterrupt`` is not caught,
    and the ledger is closed in the ``finally`` whatever happens.
    """
    try:
        job = load_job(args.job)
    except RunSpecError as exc:
        return _refuse(exc, None)
    try:
        ledger = open_ledger(args.ledger)
    except (ContractError, LedgerError) as exc:
        return _refuse(exc, None)
    operation_id: str | None = None
    try:
        try:
            claim = ledger.claim(job["request_id"], job["task_id"], job["plan"], job["idempotency_key"],
                                 args.owner, ledger.generation, job["guard_version"], job["reservation"],
                                 list(job["resources"]), args.now)
        except (ContractError, LedgerError) as exc:
            # A claim the ledger refused: nothing was claimed, nothing runs.
            return _refuse(exc, None)
        if claim["replayed"]:
            # The same request and idempotency key already produced an operation:
            # report it and its state, and start nothing.
            _write_line(sys.stderr, {"error": "E_RUN_CLAIM_REPLAYED",
                                     "detail": "operation %s is already %s" % (claim["operation_id"], claim["state"]),
                                     "operation_id": claim["operation_id"]})
            return 3
        operation_id = claim["operation_id"]
        # The candidate is the operator's, or the evidence records' own digest.
        candidate_hash = evidence_digest if args.candidate is None else _constant(args.candidate)
        try:
            invocation = Invocation(job["request_id"], operation_id, job["role"], job["profile_id"],
                                    tuple(job["command"]), tuple(job["capabilities"]),
                                    environment_digest(job["environment"]), job["adapter_id"])
            outcome = run_adapter_operation(
                ledger, _ADAPTERS[job["adapter_id"]](), invocation, cwd=job["cwd"],
                timeout_seconds=job["timeout_seconds"],
                environment=job["environment"], owner=args.owner, generation=ledger.generation,
                evidence_dir=args.evidence, worktree_id=args.worktree_id, candidate_hash=candidate_hash,
                usage_dimensions=tuple(job["usage_dimensions"]),
                allowed_secret_names=frozenset(job["allowed_secret_names"]), now=args.now,
                max_stream_bytes=job["max_stream_bytes"], max_line_bytes=job["max_line_bytes"])
            result = {"command": "once", "operation_id": operation_id, "outcome": outcome,
                      "operation": ledger.operation(operation_id)}
        except (AdapterError, ContractError, LedgerError, RuntimeErrorCode) as exc:
            # A refusal after the claim: the operation stays in the ledger.
            return _refuse(exc, operation_id)
    finally:
        ledger.close()
    _emit(result)
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    """Take over what a controller that died left in the ledger, reporting it.

    The ledger is opened first (a path that cannot be opened is the ledger's own
    code, e.g. ``E_LEDGER_MISSING``); the candidate is the operator's
    ``--candidate``, or the evidence records' own digest exactly as ``once``
    uses; then :func:`~sentinelmesh.restart_recovery.recover_after_restart` is
    called on the ledger exactly once.  It rolls the ledger over exactly once,
    cancels every claim that never reached a handoff, settles every handoff never
    decided from its records and reports every operation it acted on, in claim
    order.  Its report is written exactly as it came back -- no entry is added,
    dropped, reordered or re-decided here -- and the ledger is not read or
    written again afterwards.

    A ``ContractError`` or ``LedgerError`` the driver raises is a refusal with
    its own code and detail.  A refusal before the rollover -- a ledger that
    cannot be opened, ``E_RECOVERY_INPUT`` -- leaves the ledger byte for byte as
    it was; a ``LedgerError`` the driver passes on after the rollover is
    reported the same way, and the ledger is then as the driver left it: the
    generation moved and every request held (review of attempt-001, F1).  This
    command claims nothing, runs nothing, settles no usage and reopens no
    request: every request stays stopped with ``RECOVERY_REQUIRED``, a
    ``failed`` or ``skipped`` entry is left for the next call or for the
    operator, and what a recovered review still needs -- usage, resolution, the
    resume -- is the operator's, through ``smctl ledger settle``/``resolve``/
    ``resume``.  ``KeyboardInterrupt`` is not caught, and the ledger is closed in
    the ``finally`` whatever happens.
    """
    try:
        ledger = open_ledger(args.ledger)
    except (ContractError, LedgerError) as exc:
        return _refuse(exc, None)
    try:
        try:
            candidate_hash = evidence_digest if args.candidate is None else _constant(args.candidate)
            report = recover_after_restart(ledger, owner=args.owner, candidate_hash=candidate_hash)
            result = {"command": "recover", "generation": report["generation"],
                      "operations": report["operations"]}
        except (ContractError, LedgerError) as exc:
            # Nothing was claimed by this command, so there is no operation to
            # name; a refusal before the rollover left the ledger untouched, one
            # after it left the generation moved.
            return _refuse(exc, None)
    finally:
        ledger.close()
    _emit(result)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Drain one request: claim and run each of its tasks once, reporting it all.

    ``serve`` walks one request from end to end in a single call.  The per-task
    job files are read from ``--jobs`` first (any ``E_RUN_JOBS`` happens before
    the ledger is opened, so nothing runs and the ledger is untouched); then the
    ledger is opened; then the request's tasks are read in seeding order and any
    task the directory has a job for but the request does not have is ``E_RUN_JOBS``
    before a single claim.  A refusal in any of those checks -- and any refusal
    during the walk that is not one of :data:`_SERVE_SKIP` -- stops the command as
    ``refused``: the report is still written on stdout, the refusal is repeated on
    stderr exactly as ``once`` writes it, and the exit code is 3.

    The walk considers each task exactly once, in seeding order, and runs each
    task at most once however its run ends -- there is no second pass, no retry
    and no concurrency.  For each task: a task already accepted or out of
    attempts is skipped (``done``/``attempts``); a task with no job file is
    skipped (``no_job``); a claim the ledger refuses with one of
    :data:`_SERVE_SKIP` is skipped with that code; and ``--max-runs`` or
    ``--max-seconds``, checked only before a claim and never inside a run, ends
    the walk as ``max_runs`` or ``max_seconds``.  A task that survives all of that
    is claimed with the derived key ``<job key>#<attempt>`` (``attempt`` being the
    task's used attempts plus one) in a fresh evidence directory
    ``<evidence>/<task_id>/<attempt>`` that must not already exist
    (``E_RUN_EVIDENCE``), and run through ``once``'s own chain exactly once; a
    replayed claim is ``E_RUN_CLAIM_REPLAYED`` and a refusal the run raises names
    the operation just claimed.  A walk that reaches the end stops as ``drained``.

    stdout is always exactly one report line -- ``command``, ``request``, the
    ``runs`` (task, attempt, operation, state and evidence directory of each), the
    ``skipped`` (task and reason, in order), the ``stopped`` reason and the
    ``refusal`` (or ``null``).  ``drained``/``max_runs``/``max_seconds``/
    ``interrupted`` exit 0; ``refused`` exits 3.  A ``KeyboardInterrupt`` between
    runs -- before a claim, including the clock read, the task view and the claim
    itself -- is absorbed as ``interrupted``; one raised inside a run is not
    caught, exactly as ``once`` leaves it.  The ledger is closed in the ``finally``
    whatever happens.

    ``--parallel N`` asks for the same walk with at most N runs in flight at a
    time and is dispatched to :func:`_serve_parallel`; without it nothing above
    changes.
    """
    if getattr(args, "parallel", None) is not None:
        return _serve_parallel(args)
    runs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    refusal: dict[str, Any] | None = None
    stopped: str | None = None
    ledger = None
    try:
        # The checks before any claim: the job files (before the ledger), the
        # ledger itself, the request's tasks, and any job with no task.
        jobs = load_jobs(args.jobs, args.request)
        ledger = open_ledger(args.ledger)
        task_ids = ledger.task_ids(args.request)
        known = set(task_ids)
        extra = sorted(task for task in jobs if task not in known)
        if extra:
            raise RunSpecError("E_RUN_JOBS", "the request has no task for %s" % ", ".join(extra))
        # The candidate is the operator's, or the evidence records' own digest,
        # exactly as ``once`` decides it.
        candidate_hash = evidence_digest if args.candidate is None else _constant(args.candidate)
        claimed = 0
        running = False
        try:
            started = _clock()
            for task in task_ids:
                # (a) --max-runs, checked before a claim and never inside a run.
                if args.max_runs is not None and len(runs) >= args.max_runs:
                    stopped = "max_runs"
                    break
                # (b) --max-seconds, the same: the budget never reaches into a run.
                if args.max_seconds is not None and _clock() - started >= args.max_seconds:
                    stopped = "max_seconds"
                    break
                # (c) a task the request has but the directory has no job for.
                if task not in jobs:
                    skipped.append({"task_id": task, "reason": "no_job"})
                    continue
                job = jobs[task]
                # (d) a task already accepted, or one that used all its attempts.
                view = ledger.task(args.request, task)
                if view["done"]:
                    skipped.append({"task_id": task, "reason": "done"})
                    continue
                if view["attempts"] >= view["max_attempts"]:
                    skipped.append({"task_id": task, "reason": "attempts"})
                    continue
                # (e) the attempt's evidence directory, which must be fresh.
                attempt = view["attempts"] + 1
                evidence = os.path.join(args.evidence, task, str(attempt))
                if os.path.exists(evidence):
                    refusal = _refusal(RunSpecError("E_RUN_EVIDENCE",
                                                    "the evidence directory %s already exists" % evidence), None)
                    stopped = "refused"
                    break
                # (f) the claim, under the derived key, in the current generation.
                key = "%s#%d" % (job["idempotency_key"], attempt)
                claim_now = args.now + claimed
                try:
                    claim = ledger.claim(args.request, task, job["plan"], key, args.owner, ledger.generation,
                                         job["guard_version"], job["reservation"], list(job["resources"]),
                                         claim_now)
                except (ContractError, LedgerError) as exc:
                    if exc.code in _SERVE_SKIP:
                        skipped.append({"task_id": task, "reason": exc.code})
                        continue
                    refusal = _refusal(exc, None)
                    stopped = "refused"
                    break
                if claim["replayed"]:
                    # Someone already used this derived key for this task: report
                    # the operation it produced and start nothing.
                    refusal = _refusal(RunSpecError("E_RUN_CLAIM_REPLAYED",
                                                    "operation %s is already %s"
                                                    % (claim["operation_id"], claim["state"])),
                                       claim["operation_id"])
                    stopped = "refused"
                    break
                operation_id = claim["operation_id"]
                claimed += 1
                # (g) ``once``'s own chain, exactly once, in the fresh directory.  From
                # here on an interrupt is inside the run: the directory creation is
                # part of (g), so it is not absorbed either (review of attempt-001, F1).
                running = True
                os.makedirs(evidence)
                try:
                    invocation = Invocation(args.request, operation_id, job["role"], job["profile_id"],
                                            tuple(job["command"]), tuple(job["capabilities"]),
                                            environment_digest(job["environment"]), job["adapter_id"])
                    outcome = run_adapter_operation(
                        ledger, _ADAPTERS[job["adapter_id"]](), invocation, cwd=job["cwd"],
                        timeout_seconds=job["timeout_seconds"], environment=job["environment"],
                        owner=args.owner, generation=ledger.generation, evidence_dir=evidence,
                        worktree_id=args.worktree_id, candidate_hash=candidate_hash,
                        usage_dimensions=tuple(job["usage_dimensions"]),
                        allowed_secret_names=frozenset(job["allowed_secret_names"]), now=claim_now,
                        max_stream_bytes=job["max_stream_bytes"], max_line_bytes=job["max_line_bytes"])
                except (AdapterError, ContractError, LedgerError, RuntimeErrorCode) as exc:
                    # A refusal after the claim: the operation stays in the ledger.
                    refusal = _refusal(exc, operation_id)
                    stopped = "refused"
                    break
                runs.append({"task_id": task, "attempt": attempt, "operation_id": operation_id,
                             "state": outcome["state"], "evidence_dir": evidence})
                running = False
        except KeyboardInterrupt:
            # Between runs: absorbed as ``interrupted``.  A ``running`` flag still
            # set means the interrupt landed inside a run, which is not ours to
            # catch -- re-raise it, exactly as ``once`` leaves it.
            if running:
                raise
            stopped = "interrupted"
    except (ContractError, LedgerError, RunSpecError) as exc:
        # A refusal from the checks before the walk: nothing was claimed, so
        # there is no operation to name.
        refusal = _refusal(exc, None)
        stopped = "refused"
    finally:
        if ledger is not None:
            ledger.close()
    if stopped is None:
        stopped = "drained"
    report = {"command": "serve", "request": args.request, "runs": runs, "skipped": skipped,
              "stopped": stopped, "refusal": refusal}
    _emit(report)
    if refusal is not None:
        _write_line(sys.stderr, refusal)
        return 3
    return 0


def _same_directory(one: dict[str, Any], other: dict[str, Any]) -> bool:
    """Two jobs in one working directory whose resource keys do not meet.

    The directories are compared as
    ``os.path.normcase(os.path.realpath(job["cwd"]))``, so another spelling of
    the same tree -- a trailing separator, a ``..`` segment, another case where
    the file system folds it -- is the same directory.  Sharing a directory is
    only allowed when the two jobs name a resource key in common, because that
    key is what keeps the second claim from starting while the first run is
    still LIVE.
    """
    if os.path.normcase(os.path.realpath(one["cwd"])) != os.path.normcase(os.path.realpath(other["cwd"])):
        return False
    return not set(one["resources"]) & set(other["resources"])


def _shared_directories(jobs: dict[str, Any]) -> None:
    """Refuse two jobs of one request that share a working directory, or nothing.

    Two runs in flight write files at the same time, and nothing between the
    scheduler's resource keys and the adapter run keeps two children out of one
    tree: a directory two jobs share therefore has to be paid for with a resource
    key they have in common, which is what makes the second claim wait for the
    first run to leave LIVE.  The task ids are walked in sorted order and the
    *first* pair that shares a directory without a common key is the one
    reported -- ``E_RUN_JOBS`` naming both tasks and the first job's own ``cwd``
    -- so a directory with more than one offending pair still refuses the whole
    call with one deterministic line.  Nothing is returned when no pair offends.
    """
    order = sorted(jobs)
    for index, first in enumerate(order):
        for second in order[index + 1:]:
            if _same_directory(jobs[first], jobs[second]):
                raise RunSpecError("E_RUN_JOBS", "tasks %s and %s share the working directory %s "
                                                 "without a common resource key"
                                   % (first, second, jobs[first]["cwd"]))


def _runs_in_flight(threads: list[threading.Thread]) -> int:
    """How many of this call's runs are still in flight: their threads are alive.

    A worker thread writes its own result before it ends, so a thread that is no
    longer alive has a result the main thread can read; the main thread never
    learns an outcome from anywhere else.
    """
    return sum(1 for thread in threads if thread.is_alive())


def _await_run(threads: list[threading.Thread]) -> None:
    """Block at most :data:`_POLL_SECONDS` waiting for one of the live runs.

    A plain sleep of the poll interval while any run is in flight, so the main
    thread comes back to its own checks -- the slot count, the run that just
    ended, the ceilings -- at least that often and a ``KeyboardInterrupt``
    always has a chance to land.  With no run in flight there is nothing to
    wait for and this returns at once.

    Never ``Thread.join``: on Python 3.12 a ``KeyboardInterrupt`` that lands
    inside ``join(timeout)`` releases the thread's state lock and marks a thread
    that is still running as stopped, so ``is_alive()`` turns false, the drain
    stops waiting for that run and the report leaves it out while the ledger is
    closed under it (found when a fresh check of ledger-catch-up hit it,
    2026-10-16).
    """
    if any(thread.is_alive() for thread in threads):
        time.sleep(_POLL_SECONDS)


def _ended_badly(records: list[dict[str, Any]]) -> bool:
    """Whether one of this call's ended runs was refused or raised something.

    A run that ended in a refusal, or in an error the four refusal classes do not
    cover, ends the claims: nothing new is started behind it.
    """
    return any(record["refusal"] is not None or "error" in record for record in records)


def _serve_run(record: dict[str, Any], ledger: Any, job: dict[str, Any], args: argparse.Namespace,
               candidate_hash: Any) -> None:
    """One run of ``serve --parallel``: ``once``'s own chain, in its own thread.

    The thread is a daemon and runs exactly one adapter invocation: it creates
    the attempt's evidence directory, builds the invocation from the job exactly
    as ``once`` builds it and calls
    :func:`~sentinelmesh.adapter_run.run_adapter_operation` through this module's
    own name exactly once, with this claim's own logical clock.  Whatever it
    learns is written back into ``record`` -- the claim's own row -- before the
    thread ends: the outcome's ``state``, a refusal as
    :func:`_refusal` names it, or an error none of the four refusal classes
    covers, kept as it is for the main thread to raise.  It writes nothing to
    stdout or stderr of its own: the report is the main thread's to write.
    """
    operation_id = record["operation_id"]
    try:
        os.makedirs(record["evidence_dir"])
        invocation = Invocation(args.request, operation_id, job["role"], job["profile_id"],
                                tuple(job["command"]), tuple(job["capabilities"]),
                                environment_digest(job["environment"]), job["adapter_id"])
        outcome = run_adapter_operation(
            ledger, _ADAPTERS[job["adapter_id"]](), invocation, cwd=job["cwd"],
            timeout_seconds=job["timeout_seconds"], environment=job["environment"],
            owner=args.owner, generation=ledger.generation, evidence_dir=record["evidence_dir"],
            worktree_id=args.worktree_id, candidate_hash=candidate_hash,
            usage_dimensions=tuple(job["usage_dimensions"]),
            allowed_secret_names=frozenset(job["allowed_secret_names"]), now=record["now"],
            max_stream_bytes=job["max_stream_bytes"], max_line_bytes=job["max_line_bytes"])
        record["state"] = outcome["state"]
    except (AdapterError, ContractError, LedgerError, RuntimeErrorCode) as exc:
        # A refusal after the claim: the operation stays in the ledger.
        record["refusal"] = _refusal(exc, operation_id)
    except BaseException as exc:  # a program error: the main thread raises it
        record["error"] = exc


def _serve_parallel(args: argparse.Namespace) -> int:
    """Drain one request with at most ``--parallel`` runs in flight at a time.

    The walk is the one ``serve`` makes without ``--parallel`` -- the same checks
    before any claim (the job files, the shared-directory rule, the ledger, the
    request's tasks, any job with no task), the same per-task steps in seeding
    order, the same derived keys, the same evidence directories, the same skips
    and the same ``max_runs``/``max_seconds`` stops -- with one difference: every
    claim is still made by the main thread, in seeding order, but the run it
    claimed is handed to a daemon thread of its own and the main thread goes on
    claiming while fewer than ``--parallel`` of this call's runs are in flight.

    A claim refused with one of :data:`_SERVE_WAIT` means the slot, the resource
    key or the budget is held by one of this call's own runs in flight, so the
    main thread waits for one of them to end and claims the very same task
    again; with none of this call's runs in flight the code means what it means
    without ``--parallel`` -- somebody else holds it -- and stops the walk as
    ``refused``.  A run of this call that ends in a refusal or in an error stops
    the claims too.  Every stop -- a refusal, a ceiling, the first Ctrl-C, the
    end of the request -- waits for this call's own runs before anything is
    reported, and the report is only written once the ledger is closed.

    The first ``KeyboardInterrupt`` the main thread sees, in the walk or while it
    waits, is absorbed as ``interrupted``: the claims stop, the runs in flight
    are still waited out and the report is written (exit 0 unless something was
    refused, which still exits 3).  A second
    one is the operator giving up on them: it is not absorbed, it leaves
    ``_serve_parallel`` with the ledger closed in the ``finally``, nothing is
    written on stdout or stderr, and the runs still in flight are left to
    ``smctl run recover``.  A ``KeyboardInterrupt`` in the checks before the walk
    is not absorbed either, exactly as it is not in the walk without
    ``--parallel``.

    The report is one line on stdout with ``parallel`` added to ``serve``'s own
    keys: the ``runs`` in claim order -- each with its task, attempt, operation,
    state and evidence directory -- the ``skipped`` as without ``--parallel``,
    the ``stopped`` reason and the ``refusal``.  The refusals are the runs' own,
    in claim order, followed by the walk's own (the one that stopped it), the
    first of them being the report's ``refusal`` and each of them one line on
    stderr; a non-empty refusal list exits 3, anything else 0.  A run that ended
    in an error the four refusal classes do not cover is raised, in claim order,
    after the runs are over and the ledger is closed, and no report is written
    for it.
    """
    skipped: list[dict[str, Any]] = []
    stopped: str | None = None
    interrupted = False
    refusal: dict[str, Any] | None = None
    ledger = None
    records: list[dict[str, Any]] = []
    threads: list[threading.Thread] = []
    try:
        # The checks before any claim: the job files (before the ledger), the
        # shared directories, the ledger itself, the request's tasks, and any job
        # with no task.  A ``KeyboardInterrupt`` here is not absorbed.
        jobs = load_jobs(args.jobs, args.request)
        _shared_directories(jobs)
        ledger = open_ledger(args.ledger)
        task_ids = ledger.task_ids(args.request)
        known = set(task_ids)
        extra = sorted(task for task in jobs if task not in known)
        if extra:
            raise RunSpecError("E_RUN_JOBS", "the request has no task for %s" % ", ".join(extra))
        # The candidate is the operator's, or the evidence records' own digest,
        # exactly as the walk without ``--parallel`` decides it.
        candidate_hash = evidence_digest if args.candidate is None else _constant(args.candidate)
        claimed = 0
        try:
            started = _clock()
            index = 0
            while index < len(task_ids):
                # (s) a free slot: at most ``--parallel`` of this call's runs in
                # flight, so wait for one of them to end before claiming again.
                while _runs_in_flight(threads) >= args.parallel:
                    _await_run(threads)
                # (h) a run of this call that ended in a refusal or in an error
                # stops the claims: nothing new is started behind it.
                if _ended_badly(records):
                    stopped = "refused"
                    break
                # (a) --max-runs, counting this call's claims, checked before a
                # claim and never inside a run.
                if args.max_runs is not None and claimed >= args.max_runs:
                    stopped = "max_runs"
                    break
                # (b) --max-seconds, the same: the budget never reaches into a run.
                if args.max_seconds is not None and _clock() - started >= args.max_seconds:
                    stopped = "max_seconds"
                    break
                task = task_ids[index]
                # (c) a task the request has but the directory has no job for.
                if task not in jobs:
                    skipped.append({"task_id": task, "reason": "no_job"})
                    index += 1
                    continue
                job = jobs[task]
                # (d) a task already accepted, or one that used all its attempts.
                view = ledger.task(args.request, task)
                if view["done"]:
                    skipped.append({"task_id": task, "reason": "done"})
                    index += 1
                    continue
                if view["attempts"] >= view["max_attempts"]:
                    skipped.append({"task_id": task, "reason": "attempts"})
                    index += 1
                    continue
                # (e) the attempt's evidence directory, which must be fresh.
                attempt = view["attempts"] + 1
                evidence = os.path.join(args.evidence, task, str(attempt))
                if os.path.exists(evidence):
                    refusal = _refusal(RunSpecError("E_RUN_EVIDENCE",
                                                    "the evidence directory %s already exists" % evidence), None)
                    stopped = "refused"
                    break
                # (f) the claim, under the derived key, in the current generation.
                key = "%s#%d" % (job["idempotency_key"], attempt)
                claim_now = args.now + claimed
                try:
                    claim = ledger.claim(args.request, task, job["plan"], key, args.owner, ledger.generation,
                                         job["guard_version"], job["reservation"], list(job["resources"]),
                                         claim_now)
                except (ContractError, LedgerError) as exc:
                    if exc.code in _SERVE_SKIP:
                        skipped.append({"task_id": task, "reason": exc.code})
                        index += 1
                        continue
                    # The runs in flight are counted once: counted twice, the last
                    # one could end in between and leave a wait for fewer than
                    # zero runs that never ends (review of attempt-001, F1).
                    flight = _runs_in_flight(threads)
                    if exc.code in _SERVE_WAIT and flight:
                        # One of this call's own runs holds the slot, the resource
                        # key or the budget: wait for one of them to end, then
                        # claim this very task again from (s).
                        while _runs_in_flight(threads) >= flight:
                            _await_run(threads)
                        continue
                    refusal = _refusal(exc, None)
                    stopped = "refused"
                    break
                if claim["replayed"]:
                    # Someone already used this derived key for this task: report
                    # the operation it produced and start nothing.
                    refusal = _refusal(RunSpecError("E_RUN_CLAIM_REPLAYED",
                                                    "operation %s is already %s"
                                                    % (claim["operation_id"], claim["state"])),
                                       claim["operation_id"])
                    stopped = "refused"
                    break
                # (g) the run is registered before its thread starts, so an
                # interrupt between the two leaves the operation RESERVED and
                # unreported -- exactly as a claim interrupted in the walk
                # without ``--parallel`` leaves it.
                claimed += 1
                record = {"task_id": task, "attempt": attempt, "operation_id": claim["operation_id"],
                          "evidence_dir": evidence, "now": claim_now, "state": None, "refusal": None}
                records.append(record)
                threads.append(threading.Thread(target=_serve_run, daemon=True,
                                                args=(record, ledger, job, args, candidate_hash)))
                threads[-1].start()
                index += 1
        except (ContractError, LedgerError, RunSpecError) as exc:
            # A refusal from a ledger call of the walk other than the claim (the
            # task view of a ledger a run has made unusable): it stops the walk
            # like the claim's own refusals and, like them, waits for the runs in
            # flight before anything is reported (review of attempt-001, F2).
            refusal = _refusal(exc, None)
            stopped = "refused"
        except KeyboardInterrupt:
            # The first Ctrl-C, wherever in the walk it landed: the claims stop
            # here, and the runs in flight are waited out below.
            interrupted = True
        try:
            # Every stop waits for this call's own runs before anything is
            # reported: a run in flight is a run the report owes a result to.
            while _runs_in_flight(threads):
                _await_run(threads)
        except KeyboardInterrupt:
            # The first Ctrl-C that landed while the runs in flight were already
            # being waited out is absorbed and the wait simply continues; the
            # second is the operator giving up on them, so it is not absorbed --
            # the runs are left to ``smctl run recover``.
            if interrupted:
                raise
            interrupted = True
            while _runs_in_flight(threads):
                _await_run(threads)
    except (ContractError, LedgerError, RunSpecError) as exc:
        # A refusal from the checks before the walk: nothing was claimed, so
        # there is no operation to name.
        refusal = _refusal(exc, None)
        stopped = "refused"
    finally:
        if ledger is not None:
            ledger.close()
    # A run that ended in an error the four refusal classes do not cover: the
    # runs are all over and the ledger is closed, so the first one in claim
    # order is raised as it is and no report is written for it.
    for record in records:
        if "error" in record:
            raise record["error"]
    refusals = [record["refusal"] for record in records if record["refusal"] is not None]
    if refusal is not None:
        refusals.append(refusal)
    runs = [{"task_id": record["task_id"], "attempt": record["attempt"],
             "operation_id": record["operation_id"], "state": record["state"],
             "evidence_dir": record["evidence_dir"]}
            for record in records if record["state"] is not None]
    if refusals:
        stopped = "refused"
    elif interrupted:
        stopped = "interrupted"
    elif stopped is None:
        stopped = "drained"
    report = {"command": "serve", "request": args.request, "runs": runs, "skipped": skipped,
              "stopped": stopped, "refusal": refusals[0] if refusals else None, "parallel": args.parallel}
    _emit(report)
    for item in refusals:
        _write_line(sys.stderr, item)
    return 3 if refusals else 0


def _constant(value: str) -> Any:
    """A ``candidate_hash`` that returns the operator's value, ignoring the dir."""
    return lambda _evidence_dir: value


def _digits(text: str) -> int:
    """One non-negative integer written in decimal digits, and nothing else.

    ``--now`` is a sequence number, not an arithmetic value: a sign, a decimal
    point or surrounding whitespace is a typo, refused by argparse before the
    ledger is opened.
    """
    if re.fullmatch("[0-9]+", text) is None:
        raise argparse.ArgumentTypeError("%r is not a number written in decimal digits" % text)
    return int(text)


def _candidate(text: str) -> str:
    """The ``--candidate`` value: exactly 64 lower-case hexadecimal digits."""
    if re.fullmatch("[0-9a-f]{64}", text) is None:
        raise argparse.ArgumentTypeError("the candidate must be 64 lower-case hexadecimal digits")
    return text


def _count(text: str) -> int:
    """One positive integer in decimal digits: ``_digits``, and at least one.

    ``--max-runs`` counts runs, so zero is a usage error rather than a budget
    that stops before the first claim -- that is ``--max-seconds 0``'s job.
    """
    value = _digits(text)
    if value < 1:
        raise argparse.ArgumentTypeError("%r is not at least 1" % text)
    return value


def _clock() -> float:
    """The clock ``serve`` measures ``--max-seconds`` against, in seconds.

    Read through this module-level name so a test can substitute its own clock;
    the value is only ever differenced against the start, so it is a monotonic
    reading of elapsed time.
    """
    return time.monotonic()


def register(subparsers: Any) -> None:
    """Add the ``run`` command group to ``smctl``'s subparsers.

    ``init``, ``once``, ``serve`` and ``recover`` each take the ledger path as
    their first positional argument; an unknown combination is a usage error
    (exit 2), so the ledger is not even opened for one.
    """
    group = subparsers.add_parser("run", help="one-shot adapter runs: create a ledger and run once under the supervisor")
    commands = group.add_subparsers(dest="run_command", required=True)

    p = commands.add_parser("init", help="create a ledger and seed one request and its tasks from a spec file")
    p.add_argument("ledger", help="path of the ledger file to create")
    p.add_argument("--spec", required=True, help="path of the strictly checked request specification file")
    p.set_defaults(func=cmd_init)

    p = commands.add_parser("once", help="claim one operation and run one adapter invocation, reporting the outcome")
    p.add_argument("ledger", help="path of the ledger file")
    p.add_argument("--job", required=True, help="path of the strictly checked job file")
    p.add_argument("--evidence", required=True, help="evidence directory for the run, used exactly as given")
    p.add_argument("--worktree-id", required=True, help="worktree id recorded with the run")
    p.add_argument("--owner", required=True, help="owner recorded on the operation and its events")
    p.add_argument("--candidate", type=_candidate,
                   help="sha256 of the candidate (default: the evidence records' own digest)")
    p.add_argument("--now", type=_digits, default=0, help="logical clock for the claim and the run (default 0)")
    p.set_defaults(func=cmd_once)

    p = commands.add_parser("serve", help="drain one request task by task, running each task once and reporting it all")
    p.add_argument("ledger", help="path of the ledger file")
    p.add_argument("--request", required=True, help="request id whose tasks are drained in seeding order")
    p.add_argument("--jobs", required=True, help="directory of per-task job files, each named <task_id>.json")
    p.add_argument("--evidence", required=True, help="evidence root; each run uses <root>/<task_id>/<attempt>")
    p.add_argument("--worktree-id", required=True, help="worktree id recorded with each run")
    p.add_argument("--owner", required=True, help="owner recorded on each operation and its events")
    p.add_argument("--max-runs", type=_count,
                   help="stop before a claim once this many runs are done (a positive integer)")
    p.add_argument("--max-seconds", type=_digits,
                   help="stop before a claim once this many seconds have passed (0 stops at once)")
    p.add_argument("--candidate", type=_candidate,
                   help="sha256 of the candidate (default: the evidence records' own digest)")
    p.add_argument("--now", type=_digits, default=0,
                   help="base logical clock; each claim uses it plus the claims before it (default 0)")
    p.add_argument("--parallel", type=_count,
                   help="run at most this many of the request's tasks at a time, each in a thread of its own")
    p.set_defaults(func=cmd_serve)

    p = commands.add_parser("recover", help="take over what a crashed controller left in the ledger, reporting it as it is")
    p.add_argument("ledger", help="path of the ledger file")
    p.add_argument("--owner", required=True, help="owner the recovered operations are handed to")
    p.add_argument("--candidate", type=_candidate,
                   help="sha256 of the candidate (default: the evidence records' own digest)")
    p.set_defaults(func=cmd_recover)
