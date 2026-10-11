# Review: run-cli-once v1 · attempt-001

- Candidate: `e8e72445f2f313af61e0ed3119ff4ba558c2e3c7` (worktree `KitExperiment-run-cli-once-a001-ujix5uab`), base `f5f1de5` (oracle on master `c07cce4`).
- Executor: step/step-5-preview · thinking high · pi 0.99.2 · Windows Server 2025 · 1066.7 s / 1800 s · 0 tool errors · 0 scope findings · changes A2 M1 D0.
- Executor report: `completed`; no `contract_conflicts`, no `unresolved_questions`, no limitations.

## Independent checks (planner)

- `check --fresh`: 8/8 (V01 oracle 17 cases, V02 executor tests 6 cases, V03 regression set, V04 full suite `932 passed, 281 skipped`, V05–V08).
- Mutations on the fixed candidate (`.agent/runs/run-cli-once/attempt-001/mutations/`):
  - rounds 01/02, 29 deviations anchored on the planner draft's wording: 20 skipped (the candidate words the same rules differently), 8/9 and 3/9 caught — a rehearsal only;
  - round 03, 37 deviations re-anchored on the candidate's own wording, oracle + executor tests: 36 caught, 1 missed — reading 65536 instead of 65537 bytes is equivalent in outcome (an oversize file is still refused with the same code, by truncation);
  - round 04, the same 37 with the executor tests alone: 11 caught; the 26 missed are rules the contract left to the oracle (file-shape edges, replay, the ceilings and the timeout passed on, the generation, the candidate).
- Independent read-only review (subagent, brief `reviewer-brief-run-cli-once.md`, about 85 adversarial inputs probed): **PASS**, no high finding.
  - F1 (medium): `_read_json` lets `RecursionError` escape — a 3000-level nested JSON file inside the 64 KiB ceiling makes `cli.main` answer exit 2 with an `error:` line instead of `E_RUN_SPEC`/`E_RUN_JOB` exit 3 (nothing is created or opened either way). Fixed in the planner completion (`RecursionError` added to the `except`), with an oracle case.
  - F2 (low): a `LedgerError` while seeding after `create_ledger` would leave a half-seeded file although the docstring promises "nothing left behind"; unreachable without I/O failure. Docstring narrowed in the completion.
  - F3 (low): `ledger.operation(operation_id)` after the run sits outside the post-claim `except`; practically unreachable. Moved inside in the completion.
  - F4 (low, informational): a malformed environment name passes `load_job`, is refused by `environment_digest` after the claim and strands a `RESERVED` operation — contract-conformant; noted for `run-cli-recover`.
  - F5 (cosmetic): `candidate_hash` chosen after the claim rather than before; unobservable.
  - Clean: conventions and shapes (eight usage-error shapes exit 2 with nothing created; one JSON line each way; the three refusal keys), both loaders exact on every probed type (bool/float, unhashable list items, top-level non-objects, BOM, UTF-16, two documents, directories), the 64 KiB bound at exactly 65536/65537, `init` ordering with the rehearsal, `once` ordering with one claim in the current generation and the replay refusal, every job field reaching `run_adapter_operation`, `os.environ` never referenced, `KeyboardInterrupt` uncaught, the six executor cases pinned on real values, scope exact, report honest except "no undefined behaviour" versus F1.

## Decision

PASS. Integrate the candidate as is; the planner completion fixes F1–F3 and adds the oracle case for F1.

Verdict: PASS
