# Review: task-done v1 · attempt-001

- Candidate: `d6119131054617b303128c61c9002beda23894ec` (worktree `KitExperiment-task-done-a001-mkyaz0eh`), base `7256c8b` (oracle on master `6433938`).
- Executor: step/step-5-preview · thinking high · Windows Server 2025 · 543.7 s / 1800 s · 1 tool error · 0 scope findings · changes A1 M3 D0 (scheduler.py +48, durable_ledger.py +28, ledger_cli.py +17/-1, tests/test_task_done.py 219 lines).
- Executor report: `completed`; no `contract_conflicts`, no `unresolved_questions`, no `limitations`; own V04 `988 passed, 281 skipped`.

## Independent checks (planner)

- `check --fresh`: 8/8 (V01 the two oracles, V02 executor tests 9 cases, V03 regression set, V04 full suite, V05–V08).
- Mutations on the fixed candidate (`.agent/runs/task-done/attempt-001/mutations/`), 29 deviations anchored on the candidate's own wording (the draft's 24 re-anchored on the candidate's four-line comment and split comprehension, plus five: an event emitted before the refusal, a fifth view key, the view counting every task's operations, `task_ids` listing every request's tasks, the proxy swapping its arguments):
  - round 01, oracle + executor tests: 27 caught, 2 missed — both equivalent: the rule placed after the active-task rule (an accepted task has no live operation) and `done` read off the last operation only (nothing is claimed after an acceptance);
  - round 02, executor tests alone: 17 caught; the 12 missed are the two equivalents plus the rule's place relative to the replay, the generation, the stop and the guard, the rule's request scope, the `str`-subclass and unknown-request codes of the views and the proxies' availability check — all left to the oracle by Step 5.
- Independent read-only review (subagent, brief `reviewer-brief-task-done.md`; the rule's place and the order of every check probed in memory, the views probed with `None`, `int`, `bytes`, a `str` subclass, unknown and foreign scopes, a two-request status probe, the base diff of `MUTATING_METHODS`/`LEDGER_FORMAT`, the executor tests re-run under an external basetemp): **PASS**, no high or medium finding.
  - F1 (low): the comment above the rule is four lines where the contract asked for one or two. Trimmed in the planner completion.
  - F2 (low): the executor report says V02 asserts the ledger's bytes unchanged; the executor tests assert `events`/`snapshot` in memory and the views after a reopen, the byte check is the oracle's. Noted.
  - F3 (low): the test helper `refuse_code` asserted the code itself and took arguments nobody passed, the callers asserting the code again. Simplified in the completion.
  - F4 (low): an ignored `.pytest_cache/` in the candidate worktree from the executor's own runs; the tests write only under the system temp. Nothing to do.
  - Clean: the rule byte-for-byte the contract's two lines after `E_GUARD_STALE` and before `E_ACTIVE_TASK`, the replay/generation/scope/stop/guard order untouched, nothing mutated on the refusal, the four view keys with the `E_ATTEMPTS` count parity, seeding order, the exact codes for every odd input, the proxies after `request_ids` with the availability check and equal signatures, `MUTATING_METHODS`/`LEDGER_FORMAT` byte-identical, `status.tasks` after `reviews` with `attention`/`reviews` unchanged, the four test groups with their legal baselines and no subprocess, the diffs limited to the named places, the report consistent with the diff.

## Decision

PASS. Integrate the candidate as is; the planner completion trims the comment (F1) and simplifies the helper (F3).

Verdict: PASS
