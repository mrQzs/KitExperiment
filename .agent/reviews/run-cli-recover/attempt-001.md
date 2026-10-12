# Review: run-cli-recover v1 · attempt-001

- Candidate: `b3f92ace4171ba9b9c7c17314af52c0c588d5f59` (worktree `KitExperiment-run-cli-recover-a001-nrmnrkwg`), base `7f4c00f` (oracle on master `68e1592`).
- Executor: step/step-5-preview · thinking high · pi 0.99.2 · Windows Server 2025 · 604.9 s / 1800 s · 1 tool error · 0 scope findings · changes A1 M1 D0.
- Executor report: `completed`; no `contract_conflicts`, no `unresolved_questions`, no limitations.

## Independent checks (planner)

- `check --fresh`: 8/8 (V01 oracle 6 cases, V02 executor tests 5 cases, V03 regression set, V04 full suite `943 passed, 281 skipped`, V05–V08).
- Mutations on the fixed candidate (`.agent/runs/run-cli-recover/attempt-001/mutations/`), 28 deviations anchored on the candidate's own wording (the planner draft and the candidate word `recover` almost identically, so no rehearsal round was needed):
  - round 01, oracle + executor tests: 27 caught, 1 missed — `except ValueError` in place of `except RuntimeErrorCode` is equivalent (`RuntimeErrorCode` subclasses `ValueError`);
  - round 02, executor tests alone: 24 caught; the 4 missed are the equivalent one and three rules the contract left to the oracle (a ledger that cannot be opened reported with its own code, a missing ledger not created, `--candidate` validated by argparse).
- Independent read-only review (subagent, brief `reviewer-brief-run-cli-recover.md`; parser introspection, probes with the driver patched to raise foreign exceptions, `KeyboardInterrupt` and `close()` spies, import side-effect probe): **PASS**, no high or medium finding.
  - F1 (low): `cmd_recover`'s docstring generalises "the ledger is left byte for byte as it was" to every refusal, while a `LedgerError` passed on after the rollover leaves the generation moved. Fixed in the planner completion (clause qualified).
  - F2 (low): the module docstring says `recover` "settles every handoff never decided ... settles nothing"; the second verb means usage. Fixed in the completion ("settles no usage").
  - F3 (low): the executor's default-candidate check compares one value on an empty directory rather than the function; the oracle pins the real default on a populated directory. Hardened in the completion (`is evidence_digest`).
  - F4 (low): the executor's refusal case has no legal baseline of its own and its "ledger unchanged" byte comparison is vacuous under the mock. Left as is; the oracle's refusal case pins the real ledger unchanged.
  - F5 (low): the executor report's JSON `changed_files` omits the new test file; the prose and `changes.json` are correct.
  - Informational: `cmd_recover` catches exactly `ContractError`/`LedgerError`; a foreign exception would be exit 2, and the reviewer confirmed none can reach it from the real driver (every per-operation call wrapped, every ledger read converted to `LedgerError`).
  - Clean: registration and shapes, the open → candidate → one call ordering with the report's own objects emitted unchanged and no ledger access afterwards, no claim/resume/settle/resolve/subprocess, `close()` exactly once on every path, the precheck placed and coded as item 3 with no other exception able to escape, `init`/`once` bodies byte-identical to the base, the five executor cases matching Step 5 one for one and leaving nothing in the repository, scope exact, report honest except F5.

## Decision

PASS. Integrate the candidate as is; the planner completion fixes F1–F3.

Verdict: PASS
