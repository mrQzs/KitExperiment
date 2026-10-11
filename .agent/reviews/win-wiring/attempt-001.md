# Review: win-wiring v1 · attempt-001

- Candidate: `c88554dc3be4812b00addd809148848213d23229` (worktree `KitExperiment-win-wiring-a001-vhpeab_q`), base `17503e9` (oracle on master `794db61`).
- Executor: step/step-5-preview · thinking high · pi 0.99.2 · Windows Server 2025 · 1446.6 s / 1800 s · 2 tool errors · 0 scope findings · changes A1 M4 D0.
- Executor report: `completed`; no `contract_conflicts`; one `unresolved_question` (the contract cites design W11–W12 that section 6 did not yet hold; the contract text defines the rules in full, the planner records W11–W12 in the wrap-up).

## Independent checks (planner)

- `check --fresh`: 9/9 (V01 oracle 20 cases, V02 executor tests 6 cases, V03 regression set, V04 full suite `909 passed, 281 skipped`, V05–V09 incl. the import-safety and the two-guard `--numstat` check).
- Mutations on the fixed candidate (`.agent/runs/win-wiring/attempt-001/mutations/`):
  - round 01, oracle + executor tests, 25 deviations: 20 caught, 0 missed, 5 skipped (anchors written for the planner draft's wording);
  - round 03, the 5 skipped re-anchored on the candidate's wording plus 2 more: 6 caught, 1 missed — "the length window of a text identity dropped" is an oracle gap (a 5000-digit text makes `int()` raise instead of being refused); the planner completion adds the case and re-runs the deviation;
  - round 02, executor tests alone: 6/20 applicable caught; the 14 missed are rules the contract left to the oracle (platform choice on every call, delegation by name, the identity encoding's edges, import safety).
- Independent read-only review (subagent, brief `reviewer-brief-win-wiring.md`): **PASS**, no high finding.
  - F1 (medium, comment only): `supervised_record.py` says the high 16 bits of an NTFS file reference number are the volume serial; they are a sequence number. Fixed in the planner completion.
  - F2 (low, comment only): the length window is applied to the digits (sign stripped) while the comment says "sign included"; behaviour identical in every probed case. Comment reworded in the completion.
  - F3 (low): `_recorded_identity` docstring says "no sign" next to "at most one leading `-`"; means "no `+`". Reworded in the completion.
  - Clean: platform selection on every call with no cache; delegates pass the very same objects with no checks; module-level names used by `record_supervised_run`, `_stream_reason`, `_recorded_streams_match`; no `ctypes` at import; identity encoding probed on every edge (empty, sign variants, spaces, leading zero, fullwidth and Arabic-Indic digits, 5000 digits, 2^53±1, 2^64±1) with the specified answers and no exception; no path hands a text identity on undecoded; only the two decorator lines removed; the six executor cases present, exact codes, cleanup registered, legal baseline before each refusal; docstrings no longer call Windows Job Object supervision NOT_RUN; scope exact; report honest.

## Decision

PASS. Integrate the candidate as is; the planner completion fixes F1–F3 (comments and docstring wording), closes the oracle gap found by the third mutation round, retires the stale "Windows Job Object stays NOT_RUN" guard reasons and the `adapter_run` first line, and records W11–W12 in the design note.

Verdict: PASS
