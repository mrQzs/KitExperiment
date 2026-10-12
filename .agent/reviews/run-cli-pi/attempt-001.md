# Review: run-cli-pi v1 · attempt-001

- Candidate: `1e2ce1816f0a01f400a347225320f1d960de6808` (worktree `KitExperiment-run-cli-pi-a001-z7t9ib1_`), base `5527374` (oracle on master `5732f63`).
- Executor: step/step-5-preview · thinking high · pi 0.99.2 · Windows Server 2025 · 483.9 s / 1800 s · 1 tool error · 0 scope findings · changes A1 M1 D0.
- Executor report: `completed`; no `contract_conflicts`, no `unresolved_questions`; one limitation (real pi NOT_RUN, captured streams replayed by `python -c` children), which is the contract's own non-goal.

## Independent checks (planner)

- `check --fresh`: 8/8 (V01 oracle 5 cases, V02 executor tests 5 cases, V03 regression set, V04 full suite `953 passed, 281 skipped`, V05–V08).
- Mutations on the fixed candidate (`.agent/runs/run-cli-pi/attempt-001/mutations/`), 15 deviations anchored on the candidate's own wording (the planner draft's 10 survived with two anchors re-wrapped, plus five new):
  - round 01, oracle + executor tests: 13 caught, 2 missed — M11 only inserts a dead module-level dict (no behaviour change, expected), M15 adds `total_tokens` to a pi job's dimensions when the job omits it: the oracle's subset case named `total_tokens` itself, so the mutant was invisible. Closed in the planner completion (the subset case now names two dimensions without `total_tokens` and asserts it is not settled), verified separately;
  - round 02, executor tests alone: the same 13 caught — the executor's five cases pin the registry, the instance per call and the invocation's adapter id on their own.
- Independent read-only review (subagent, brief `reviewer-brief-run-cli-pi.md`; fresh-interpreter registry probe, import side-effect probe with `open` and `Popen` spied, grep for any program-resolution code, the candidate's own test run and V05–V08): **PASS**, no high or medium finding.
  - F1 (low): the executor's four case/whitespace refusal subtests assert the detail but not the code; the oracle pins `E_RUN_JOB` on the same variants. Fixed in the completion.
  - F2 (low): one duplicated assertion on `total_tokens`. Dropped in the completion.
  - Clean: the registry exact (keys `offline`/`pi`, the classes themselves, keys equal to the ids, no alias or folding, the detail `adapter_id must be one of offline, pi`), `cmd_once` instantiating the job's adapter per call with every other argument byte-identical to the base and the invocation carrying the job's id, no `PATHEXT`/`os.environ`/`shutil.which`/`node.exe` code (prose only), import without side effect, the five executor cases matching Step 5 one for one and leaving nothing in the repository, docstrings with no stale "only offline" sentence, scope exact, report honest.

## Decision

PASS. Integrate the candidate as is; the planner completion closes the oracle gap M15 and fixes F1–F2.

Verdict: PASS
