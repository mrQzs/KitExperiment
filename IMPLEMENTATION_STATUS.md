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
| Scheduler and budget | `scheduler.py`, `budget.py`, `planner_runtime.py` | DAG, lease, retry, fencing, reservation and unknown usage boundaries |
| Planning and contracts | `planner_protocol.py`, `planner.py`, `planner_projection.py` | PlanningBundle, Contract IR, same-source MD/JSON, draft CAS and publication idempotency |
| State and evidence | `state.py`, `verification.py`, `integration_records.py` | append/replay projection, coverage denominator, evidence manifests and delivery decisions |
| Integration gates | `integration_gates.py` | fixed contract/candidate identity, offline wiring and production fail-closed decisions |
| Persistence | `persistence.py` | SQLite WAL/FULL resources, idempotency, generation fencing and hash-chain journal |
| Observability/migration/observer | `observability.py`, `migration.py`, `observer.py` | redaction, CAS preview, source-bound read-only observer degradation |

The current KitExperiment check is:

```text
python -m pytest -q                 65 passed
python -m compileall -q sentinelmesh tests
git diff --check
```

Pi evidence is recorded in `.agent/reviews/pi-acceptance.v2-a002.md`. The
actual execution used `bb/step-5-preview` with requested thinking `high` and
added deterministic acceptance coverage. Its review records three tool errors
and a fixed `/tmp` scratch-file process deviation; the candidate was manually
reviewed and integrated as `3352627`, rather than being described as a
dispatcher `INTEGRATED` verdict.

## Explicitly not proven by this repository

The following remain `NOT_RUN` or `BLOCKED` until separately captured with
approved evidence: real Pi/Claude provider CLI calls, credentials and network
access, native Windows Job Objects, native macOS/Linux process supervision,
three-platform parity, distributed storage/locking, billing reconciliation,
production migration and rollback, CI artifact federation, and a real
heterogeneous provider chain. Offline fixtures never upgrade these rows to
`PASS`.
