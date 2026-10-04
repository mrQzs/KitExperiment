# KitExperiment implementation status

This repository is the dependency-free Python reference implementation built
from the Code-Kit v2 contracts. The status below separates deterministic
offline evidence from capabilities that require a real provider or platform.

## Implemented and tested offline

| Area | Implementation | Evidence |
| --- | --- | --- |
| Public protocol and immutable refs | `core.py`, `protocols.py`, `evidence.py` | strict JSON, duplicate/non-finite/cycle/path/hash tests |
| Baseline | `baseline.py` | read-only Git/worktree/toolchain capture, semantic digest, drift comparison |
| Config and approvals | `config.py`, `approvals.py` | profile precedence, CAS, input-lock and revocation checks |
| Plugins and workflows | `plugins.py`, `workflows.py` | seven trusted manifests; bugfix/documentation/regression DAG recipes |
| Runtime and adapters | `runtime.py`, `adapter.py` | bounded invocation, argv/env/input validation and normalized vendor events |
| POSIX runner cleanup (native macOS) | `runtime.py` `SubprocessRunner` POSIX branch | real process-tree tests on macOS 15.7.9: group SIGTERM, then group SIGKILL, bounded reads, and `cleanup="verified"` only once the pipes reached EOF and the group is empty; runs return within about timeout + 5 s; Windows branch unchanged |
| Process identity and explicit environment | `process_identity.py` | pinned environment digest (golden vectors) and verification against `InvocationSpec.environment_sha256` from one snapshot; macOS boot session UUID via `sysctlbyname` (native), Linux branch patched only; PATH resolution from the explicit environment with absolute entries only; file SHA-256; canonical `ProcessIdentity` |
| Native POSIX supervisor (macOS) | `posix_supervisor.py` | real process-tree tests on macOS 15.7.9: the child receives exactly the explicit environment (credential-shaped names only when allowlisted, names recorded but never values); exclusive 0600 evidence files; on timeout or cancellation, group SIGTERM then group SIGKILL; stragglers reaped until the group is confirmed empty; EPERM from zombie-only groups tolerated; interrupts during the stop and reap windows absorbed without moving their deadlines; a lifecycle record and a five-key observation that `reconcile` consumes. Not yet wired into adapters or the scheduler |
| Supervised start records and replay gate (macOS) | `supervised_record.py`, `posix_supervisor.py` `before_start` | real process-tree tests on macOS 15.7.9: a canonical 0600 handoff recorded before the start and the observation after it (staged with O_EXCL, published by hard link, never overwritten); `replay_decision` reads only those records: a timeout with confirmed cleanup may be replayed; an unconfirmed cleanup, a controller killed after the start (START_UNKNOWN) and records for another invocation may not; damaged, oversized, symlinked or FIFO records fail closed. Crash-consistent only; power-loss durability not verified |
| Runtime identity/lifecycle | `runtime.py` canonical layer | exact port of the frozen Code-Kit runtime (`.agent/design/runtime-reference/`, f8ef858): verbatim reference tests, fail-closed oracle and a differential oracle over a fixed input grid; legacy `Invocation`/`SubprocessRunner` API unchanged |
| Scheduler and budget | `scheduler.py`, `budget.py`, `planner_runtime.py` | DAG, lease, retry, fencing, reservation and unknown usage boundaries |
| Planning and contracts | `planner_protocol.py`, `planner.py`, `planner_projection.py` | PlanningBundle, Contract IR, same-source MD/JSON, draft CAS and publication idempotency |
| State and evidence | `state.py`, `verification.py`, `integration_records.py` | append/replay projection, coverage denominator, evidence manifests and delivery decisions |
| Integration gates | `integration_gates.py` | fixed contract/candidate identity, offline wiring and production fail-closed decisions |
| Persistence | `persistence.py` | SQLite WAL/FULL resources, idempotency, generation fencing and hash-chain journal |
| Observability/migration/observer | `observability.py`, `migration.py`, `observer.py` | redaction, CAS preview, source-bound read-only observer degradation |

The current KitExperiment check (macOS 15.7.9, Python 3.12 venv first on PATH)
is:

```text
python -m pytest -q                 316 passed
python -m compileall -q sentinelmesh tests
git diff --check
```

Pi evidence is recorded in `.agent/reviews/pi-acceptance.v2-a002.md`. The
actual execution used `bb/step-5-preview` with requested thinking `high` and
added deterministic acceptance coverage. Its review records three tool errors
and a fixed `/tmp` scratch-file process deviation; the candidate was manually
reviewed and integrated as `3352627`, rather than being described as a
dispatcher `INTEGRATED` verdict.

The runtime port is recorded in `.agent/reviews/pi-runtime-contract-port/`.
Attempt-001 (`bb/step-5-preview`, Windows) was REVISE: its `reconcile`
allowed replay for missing, empty or malformed observations. Attempt-002
(`step/step-5-preview`, thinking `high`, macOS, pi 1.0.2) reworked it against
three planner oracles and was accepted with an exception: the executor again
wrote fixed `/tmp` scratch files. It was integrated as `2c3c115`; the planner
closed four test-coverage gaps in `8e09793`.

The POSIX runner fix is recorded in `.agent/reviews/runner-posix-cleanup/`.
Before it, on macOS, a 1 s timeout blocked for 20.1 s while a descendant held
the output pipe, `cleanup` was reported `verified` with descendants still
alive, and output was duplicated on the kill path. Attempt-001
(`step/step-5-preview`, thinking `high`) passed against a seven-case planner
oracle and was integrated as `136657c`. The planner closed four mutation gaps
in `fd57f49`. While reviewing the supervisor, the planner measured that macOS
answers EPERM, not ESRCH, when a group's only members are unreaped zombies,
and fixed the resulting crash in this runner in `74b4d62`.

Process identity is recorded in `.agent/reviews/posix-identity-env/`:
- Attempt-001 was REVISE. `file_sha256(True)` read and closed file
  descriptor 1, NUL paths leaked `ValueError`, and the executor ran its own
  mutation driver in a temp copy.
- Attempt-002 fixed the code, but three rejection tests were masked by other
  rules, so it was also REVISE. With the budget spent, the planner completed
  the work in `2191050`.

The native supervisor is recorded in `.agent/reviews/posix-supervisor/`:
- Attempt-001 was REVISE. The observation was rewritten to `START_UNKNOWN`
  instead of keeping the leader's outcome. A `PermissionError` from a group
  kill escaped `supervise()`. A second interrupt during the stop phase
  escaped as well.
- Attempt-002 fixed all three. It was REVISE because an interrupt at the
  grace deadline could report `timeout_killed` for a leader that had already
  exited, and because no test pinned the 0.05 s reap poll. With the budget
  spent, the planner completed the work in `cd7c918`. The completion added
  `tests/test_posix_supervisor_gaps.py`.
- Known limits:
  - a descendant that calls `setsid()` leaves the group and is not reaped;
  - an interrupt that lands in the few bytecodes outside the guarded waits
    still escapes;
  - a leader that survives SIGKILL for 2 s is reported with `exit_code`
    `None` and an unconfirmed cleanup, which blocks replay.

Supervised start records are recorded in
`.agent/reviews/posix-reconcile-integration/`. Attempt-001 was accepted with
two exceptions:
- it type-checks the inner observation values beyond the contract (a stricter,
  fail-closed reading);
- the dispatcher's attachment name exposed the main repository path, and the
  executor read files there (read-only, no change).

It was integrated as `a2c0e71`. The independent review then found contract
gaps, which the planner closed in `c12a88f` together with
`tests/test_supervised_record_gaps.py`:
- a stale `observation.json` could be read back as the outcome of a run that
  did start, allowing its replay;
- a FIFO named like a record blocked `replay_decision`;
- reads and writes had no byte ceiling;
- a close error after a write was ignored;
- a `worktree_id` with a C1 control left the child running unsupervised.

## Explicitly not proven by this repository

The following remain `NOT_RUN` or `BLOCKED` until separately captured with
approved evidence: real Pi/Claude provider CLI calls, credentials and network
access, native Windows Job Objects, native Linux process supervision,
wiring the macOS supervisor and its replay gate into adapters and the
scheduler (both have native macOS evidence on their own), power-loss
durability of the start records, three-platform parity, distributed storage/locking, billing reconciliation,
production migration and rollback, CI artifact federation, and a real
heterogeneous provider chain. Offline fixtures never upgrade these rows to
`PASS`.
