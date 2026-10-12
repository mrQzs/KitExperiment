# Review: serve-loop v1 · attempt-001

- Candidate: `e1528cf0905bcf0a29b04fd29ef6902385b97755` (worktree `KitExperiment-serve-loop-a001-4xdt1p05`), base `88e7f14` (oracle on master, contract commit `db68db9`).
- Executor: step/step-5-preview · thinking high · Windows Server 2025 · 1265.4 s / 1800 s · 2 tool errors (misspelled tool names) · 0 scope findings · changes A1 M1 D0 (run_cli.py +279/−7, tests/test_run_cli_serve.py 216 lines, 11 cases).
- Executor report: `completed`; no `contract_conflicts`, no `unresolved_questions`; two `limitations` that are interpretations (the clock is `time.monotonic()` where the design prose said 墙钟; the module docstring was corrected from three to four commands and its "every command claims at most once" sentence rewritten); own V04 `1012 passed, 281 skipped`.
- Reviewer: planner session; independent read-only review by a subagent (tool: Claude Code general-purpose agent, same model family as the planner; not a second vendor).

## Independent checks (planner)

- `check --fresh`: 8/8 (V01 oracle 13 cases, V02 executor tests 11 cases, V03 regression set, V04 full suite, V05–V08).
- Mutations on the fixed candidate (`.agent/runs/serve-loop/attempt-001/mutations/`), 32 deviations anchored on the candidate's wording (the draft's 28 plus: the in-run interrupt swallowed, the file-name check removed, non-JSON files read, pre-ledger refusals raised instead of reported):
  - round 01, oracle + executor tests: 30 caught, 2 missed — M07 equivalent (the logical clock's increment is unobservable; known from the draft), M14 a real oracle gap: a `KeyboardInterrupt` inside a run swallowed as `interrupted` was not distinguished. Closed in the planner completion with two oracle cases (the interrupt raised by `run_adapter_operation`, and by `os.makedirs`), see F1.
  - round 02, executor tests alone: 12 caught; the 20 missed are the two above plus every rule Step 5 left to the oracle (the done/attempts skip reasons, the derived attempt number, the key separator, the evidence layout and its freshness check, `E_ACTIVE_TASK` skipping, the replay stop, `--max-seconds`, the interrupt between runs, jobs for unknown tasks, `--candidate`, the two argument types, the `attempt`/`state` fields of a run record, the post-claim refusal naming its operation, other codes skipping). Not executor omissions.
- Independent read-only review (subagent): **PASS**, no high or medium finding; the 24 tests re-run on the candidate worktree under an external basetemp.
  - F1 (low): `running = True` is set after `os.makedirs(evidence)`, so an interrupt landing between the successful claim and the directory creation is absorbed as `interrupted` with exit 0, while the ledger keeps a `RESERVED` operation the report's `runs` does not list. The contract puts `os.makedirs` inside step (g), whose interrupts must propagate. One-line move in the planner completion, pinned by the new oracle case.
  - F2 (low, report discipline): the two interpretations sit in `limitations` instead of `unresolved_questions`; the `register` docstring edit is not mentioned. Both interpretations judged reasonable: the contract fixed `_clock()` and the difference only, and a monotonic clock is the right one for an elapsed budget; the docstring corrections stay within 第 8 条's place and are true statements. Accepted, no rework.
  - F3 (low): the executor's V01–V04 self-checks ran through `| tail`, so the report's `reported_exit_code: 0` values are inferred; the fresh check is the evidence. Process deviation, noted.
  - F4 (info): two of the six `load_jobs` refusal cases do not verify the legal baseline first. Added in the planner completion.
  - Clean: every step of 第 1–8 条 matched line by line (skip versus stop, `E_RUN_EVIDENCE` before the claim, `E_RUN_CLAIM_REPLAYED` naming the replayed operation, post-claim refusals naming the claimed one, `now + claimed`, the pre-ledger and post-ledger refusal paths with the `finally` guard, the report keys, stderr only on `refused`, parser order and types); the executor tests assert codes exactly, the report object against the stderr line, and the ledger's operation count after `--max-runs`.

## DoD

| DoD | Result | Evidence |
|---|---|---|
| D01 command line, jobs directory | met | V01, V02 fresh |
| D02 report and exit codes | met | V01, V02 fresh; executor test asserts stdout `refusal` == stderr line |
| D03 pre-run checks and the single pass | met | V01, V02 fresh; mutations 30/32 |
| D04 interrupts and the ledger close | met with F1 | V01; the makedirs window fixed and pinned in the completion |
| D05 nothing else touched, docstrings, imports | met | V03, V04, V07, V08 fresh; review |
| D06 Step 5 tests, no self-mutation, scope | met (F4 info) | V05, V06, scope 0 |

## Decision

PASS. Integrate the candidate as is; the planner completion moves `running = True` before `os.makedirs` (F1), adds the two in-run interrupt oracle cases (M14), and gives the two executor refusal cases their baselines (F4).

Verdict: PASS
