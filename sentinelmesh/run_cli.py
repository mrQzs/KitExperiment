"""Operator entry point for one-shot adapter runs: ``smctl run``.

`.agent/design/controller-cli.md` (sections 3, 5 and 6) turns the whole
``claim`` -> ``handoff`` -> ``supervise`` -> record -> ``_reduce`` -> ``settle``
-> ``finish`` chain into commands an operator can start from the command line,
one invocation at a time.  This module holds the three one-shot commands that
design names:

* ``smctl run init`` creates a ledger and seeds one request and its tasks from a
  strictly checked specification file;
* ``smctl run once`` claims exactly one operation and runs exactly one adapter
  invocation under the platform's own supervisor, reporting the outcome exactly
  as the bridge returned it;
* ``smctl run recover`` takes over what a controller that died left in the ledger:
  it rolls the ledger over exactly once (every request stops with
  ``RECOVERY_REQUIRED``), cancels every claim that never reached a handoff,
  settles every handoff never decided from its own records, and writes the
  driver's report exactly as it is -- it claims nothing, runs nothing, settles
  no usage and reopens nothing, so what the recovery could not decide is the
  operator's, through ``smctl ledger settle``/``resolve``/``resume``.

Every command claims at most once and runs at most once: there is no loop, no
retry and no daemon, so a command that is killed leaves at most one undecided
operation in the ledger for the recovery path to take over.  The conventions are
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
import re
import sys
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


def register(subparsers: Any) -> None:
    """Add the ``run`` command group to ``smctl``'s subparsers.

    ``init``, ``once`` and ``recover`` each take the ledger path as their first
    positional argument; an unknown combination is a usage error (exit 2), so the
    ledger is not even opened for one.
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

    p = commands.add_parser("recover", help="take over what a crashed controller left in the ledger, reporting it as it is")
    p.add_argument("ledger", help="path of the ledger file")
    p.add_argument("--owner", required=True, help="owner the recovered operations are handed to")
    p.add_argument("--candidate", type=_candidate,
                   help="sha256 of the candidate (default: the evidence records' own digest)")
    p.set_defaults(func=cmd_recover)
