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
| Runtime and adapters | `runtime.py`, `adapter.py`, `adapter_protocol.py` | bounded invocation, argv/env/input validation and normalized vendor events; `adapter_protocol` is a verbatim port of Code-Kit's adapter boundary (strict event sequence, single start and terminal, paired tools, unknown events leave a run unsettled), checked against the frozen reference |
| POSIX runner cleanup (native macOS) | `runtime.py` `SubprocessRunner` POSIX branch | real process-tree tests on macOS 15.7.9: group SIGTERM, then group SIGKILL, bounded reads, and `cleanup="verified"` only once the pipes reached EOF and the group is empty; runs return within about timeout + 5 s; Windows branch unchanged |
| Process identity and explicit environment | `process_identity.py` | pinned environment digest (golden vectors) and verification against `InvocationSpec.environment_sha256` from one snapshot; macOS boot session UUID via `sysctlbyname` (native), Linux branch patched only; PATH resolution from the explicit environment with absolute entries only; file SHA-256; canonical `ProcessIdentity` |
| Native POSIX supervisor (macOS) | `posix_supervisor.py` | real process-tree tests on macOS 15.7.9: the child receives exactly the explicit environment (credential-shaped names only when allowlisted, names recorded but never values); exclusive 0600 evidence files; on timeout or cancellation, group SIGTERM then group SIGKILL; stragglers reaped until the group is confirmed empty; EPERM from zombie-only groups tolerated; interrupts during the stop and reap windows absorbed without moving their deadlines; a lifecycle record and a five-key observation that `reconcile` consumes; both evidence files pinned with a read-only descriptor before the start, and after the reap the facts of each stream (device, inode, byte count, SHA-256) read through the pins block by block, so `streams_match` detects any later append, rewrite, truncation, rename, hard link, symlink or FIFO |
| Supervised start records and replay gate (macOS) | `supervised_record.py`, `posix_supervisor.py` `before_start` | real process-tree tests on macOS 15.7.9: a canonical 0600 handoff recorded before the start and the observation after it (staged with O_EXCL, published by hard link, never overwritten); `replay_decision` reads only those records: a timeout with confirmed cleanup may be replayed; an unconfirmed cleanup, a controller killed after the start (START_UNKNOWN) and records for another invocation may not; damaged, oversized, symlinked or FIFO records fail closed; observation schema v2 carries the stream facts in a closed `streams` object (v1 records are refused) and `recorded_streams` hands them to recovery. Crash-consistent only; power-loss durability not verified |
| Scheduler wiring for supervised runs (macOS) | `scheduler.py` (NOT_STARTED, `release_proven_unstarted`, `unknown` reason, `mark_running` identity, `recover`), `supervised_operation.py` | real process-tree tests on macOS 15.7.9 through the in-memory Scheduler: runs that ended with confirmed cleanup (completed, failed, timed out) go to REVIEW_PENDING with the process identity recorded; an unconfirmed cleanup is START_UNKNOWN (CLEANUP_UNCONFIRMED) with holds kept and the request stopped; a handoff proven not started releases its holds and reservations but keeps the dispatch and the attempt; on the live path what the supervisor returned comes first and forged or contradicting records never release or finish a run; after a rollover the new generation recovers fenced operations from the records on disk. A run reaches review only while both output streams are still exactly what the supervisor recorded in memory (CLEANUP_UNCONFIRMED when the pinned file changed after the reap, START_UNKNOWN when it was replaced or never digested), and recovery checks the recorded streams the same way; the specification must belong to the operation's request; an optional `before_finish` hook receives the in-memory run while the operation is still RUNNING. The Scheduler is the 07 in-memory prototype; `durable_ledger` makes it persistent and `restart_recovery` takes it over after a controller restart (rows below) |
| Adapter runs under the supervisor (macOS) | `adapter_run.py` | real-process tests on macOS 15.7.9: a normalized `adapter_protocol.Invocation` runs through the Scheduler bridge; the supervised stdout is read without following links (a regular file within a byte ceiling) as normalized-event JSONL and reduced by the adapter; usage is recorded only for a settled and complete result while the run is still RUNNING, just before `finish` (through the bridge's `before_finish` hook), only when the bytes reduced are exactly the ones the supervisor digested, with the supervisor's digest as the evidence; every usage dimension must be reserved by the operation before the handoff; every other stream or run state records nothing. The stream format is the normalized adapter protocol, not any vendor's native output |
| Pi native event translation (captured) | `pi_events.py` | `translate` turns Pi's `--mode json` session v3 frames into the normalized adapter protocol and `PiNativeAdapter` settles them with the reference reducer unchanged: usage is the sum of the assistant `message_end` counters (snapshots and mirrors never count; an illegal value leaves the whole stream unrecorded, never padded with zeros), only `agent_settled` ends a run (a non-`stop` final message ends in `error`), and unknown or malformed frames become bounded `unknown` events so the run never settles and usage stays unknown. Checked against the dispatcher's own sums on 31 real Step runs that pi_dispatch captured (pi 0.99.2 and 1.0.1 on Windows, 1.0.2 and 1.0.4 on macOS; two cut by the time limit): five reduced streams are committed under `tests/fixtures/pi_native/` with the dispatcher's summary as their expected values, the full streams are planner-only evidence (`tests/test_pi_events_captured.py`, skipped where `.agent/runs` is absent). A real pi running under the supervisor with this adapter is still NOT_RUN |
| Runtime identity/lifecycle | `runtime.py` canonical layer | exact port of the frozen Code-Kit runtime (`.agent/design/runtime-reference/`, f8ef858): verbatim reference tests, fail-closed oracle and a differential oracle over a fixed input grid; legacy `Invocation`/`SubprocessRunner` API unchanged |
| Scheduler and budget | `scheduler.py`, `budget.py`, `planner_runtime.py` | DAG, lease, retry, fencing, reservation and unknown usage boundaries; a rejected Scheduler call leaves the ledger unchanged (exact input types, event payloads encoded before any change, `settle` mappings recorded all or nothing); `operation()` is a read-only view of one operation (request, task, state, owner, generation, reservation); `rollover()` and the constructor refuse a generation beyond 2**53-1 before anything changes |
| Durable scheduling ledger (macOS) | `durable_ledger.py` | the in-memory Scheduler behind a SQLite command log (DELETE journal, `synchronous=FULL`, `fullfsync=ON`) linked by a SHA-256 chain: every successful mutating call, idempotent replays included, is appended before it returns, and opening replays the log to the same state, operation ids and events included. Real-process tests on macOS 15.7.9: every call that returned survives a SIGKILL of the writer; another process's rollover makes an older instance stale; damaged, re-chained, forged or foreign-format logs are refused with a ledger error and no leaked descriptor. An exception from the in-memory call other than a Scheduler refusal, a failed append and a stale head all invalidate the instance (fail closed); create is atomic (`O_EXCL`) and removes only the file it created; SQLite opens the file the kernel resolves. Power-loss durability not verified |
| Restart recovery (macOS) | `restart_recovery.py`, `scheduler.py` (run descriptors), `durable_ledger.py` | every supervised handoff records its run descriptor (the invocation's fields, the evidence directory and the worktree), checked before anything changes and kept as a private copy that the durable ledger replays; `recover_after_restart` rolls the ledger over once, cancels claims that never reached a handoff and settles the handoffs the old controller never decided (HANDOFF_ACCEPTED, RUNNING) from their records with `recover_supervised_operation`; operations already decided, START_UNKNOWN above all, are left alone; a failure is reported per operation, while a ledger error or an interrupt stops the recovery; requests stay stopped for an operator. Real-process tests on macOS 15.7.9 SIGKILL a controller after the records, before the review and mid-run, and recover from the ledger it left (REVIEW_PENDING, REVIEW_PENDING, START_UNKNOWN), the result surviving a reopen. Recovery from records alone keeps the trust limits of `recover_supervised_operation` |
| Operator resolution and request resume | `scheduler.py` (`resolve_unknown`, `resume`), `durable_ledger.py` | an operator resolves a START_UNKNOWN operation as NOT_STARTED, ABANDONED (process confirmed gone, result void, the task may be claimed again as a new attempt; its reservation stays held until its usage is complete) or FINISHED (into review with a candidate), and the operator's name, an evidence digest and a note are recorded, never verified; a stopped request resumes only for the current generation and every stop reason since the last resume, named in order (`stop_reasons` reads them, so a hold stacked on a stopped request cannot be cleared unnamed), with nothing START_UNKNOWN, nothing an older generation left undecided, no unknown usage and no exposure over the request's or any task's cap; attempts, usage and history are kept and no cap is raised. Real-process test on macOS 15.7.9: a controller killed mid-run, the restart recovery, the operator's usage, resolution and verdict, the resume, a reopen of the durable ledger, and the abandoned task run again to review. Every explicit `stop()` is a hold of its own (the same reason twice is named twice), while the automatic reasons count once. A run the ledger saw start, and any run handed to the supervisor (a run descriptor), can never be called unstarted by an operator, through `resolve_unknown` or `recover`, so a controller killed mid-run cannot have its run's usage taken as zero; an undecided supervised handoff is still decided from its records |
| Operator CLI (`smctl ledger`) | `ledger_cli.py`, `cli.py`, `scheduler.py` (`request_ids`, `operation_usage`) | `status`, `stop`, `settle`, `resolve` and `resume` on a durable ledger: each command replays the ledger, makes at most one change at its current generation through the ledger's own methods and closes it; success is one UTF-8 JSON line on stdout, a ledger refusal is exit 3 with the ledger's code on stderr and leaves the file unchanged, an argument error is exit 2. `status` lists the stop reasons and the operations that need an operator (start unknown, undecided, usage unknown) and never writes; `resume` takes every stop reason by hand. Tested in process and through `python -m sentinelmesh.cli`, an ascii terminal included. A write while a controller holds the ledger makes that controller stale (fail closed) |
| Crash-run usage reference (`smctl ledger reference`) | `ledger_cli.py`, `adapter_run.py` (`read_stream` without `O_NOFOLLOW`) | a read-only command that opens the ledger, reads the `stdout.log` named by a supervised run's descriptor (no stream fact survives a crash, so the file's own digest is the evidence) and prints a reference value next to the reservation and the usage already recorded: the dialect is named by hand (`--dialect pi-session-v3`, the only one), the Pi translation supplies the sums even when the stream is incomplete, marked with the reasons in order (no terminal, an illegal usage, unknown frames), `missing` lists the reserved dimensions without a known final amount in reservation order, a stream that is missing, unreadable, too large or invalid is an answer with no reference, and the command never writes; the operator still settles by hand with the stream digest. Checked on Windows against the five reduced real streams and, planner-only, the largest full captured stream (11 MB). `read_stream` now also reads without `O_NOFOLLOW` (Windows): `lstat` first, a link or reparse point refused before the open, a binary open, and the identity of what was opened compared with the `lstat` one; the POSIX branch is textually unchanged and keeps its macOS evidence |
| Ledger checkpoint and compaction (`smctl ledger compact`) | `durable_ledger.py`, `ledger_cli.py` | format `sentinelmesh-ledger-v2` adds a `checkpoint` table: `DurableScheduler.compact()` writes the whole in-memory state -- requests, tasks, operations with their usage and run descriptors, idempotency keys, resource holds and every event -- at the instance's sequence number, with a digest over the sequence number, the link to the last command and the state text, and removes the commands it covers, all in one `BEGIN IMMEDIATE` transaction checked against the log head; opening loads the checkpoint, verifies its digest and shape (exact field sets and types, references that hold, events numbered from 1), registers its operation ids as used and replays only the commands after it, so a checkpoint plus its tail rebuilds exactly what a full replay would, events and private state included. The command chain is never rewritten and the identity `(seq, sha)` of the log does not change, so a compaction by another process leaves a live controller current while an append still makes it stale; any failure of a compaction invalidates the instance; a damaged or forged checkpoint, a tail that does not continue it and a v1 file are refused. Compaction is explicit only (the CLI or a controller's own call); the file does not shrink (no `VACUUM`) and the state keeps growing with the events. Verified on Windows by compacting at every prefix of a 33-call scenario and in real child processes |
| Operator reads of the events (`smctl ledger events`) | `ledger_cli.py`, `durable_ledger.py` (`checkpoint_seq`) | a read-only command that prints the recorded events exactly as the Scheduler emitted them, filtered by request (an operation's events by the operation's request, request-level events by their payload, `controller.fenced` by none), by operation and by any of the sixteen event types, and paged by sequence number (`--after`, `--limit` of at most 10000, default 100, with `next`); the header reports the ledger's checkpoint, since the events are the only history left before it, and `status` keeps its shape. Decimal-digit arguments only; an unknown request, operation or type is refused before anything is read. Checked on Windows against the whole history of the 33-call scenario |
| Stream ceilings (`read_stream`, `run_adapter_operation`, `smctl ledger reference`) | `adapter_run.py`, `ledger_cli.py` | the largest stream a caller may ask `read_stream` to read is 64 MiB (was 16 MiB; the default stays 1 MiB), and the longest line the parser accepts is a parameter, `max_line_bytes`, 1 MiB by default (the protocol's own document limit) and 8 MiB at most, checked before the file is touched and, on the run path, before the handoff; `smctl ledger reference` reads with both maxima, so the controller and the operator get the same answer for the same file. Measured on the 34 captured Step streams: a run at the kit's own 1800 s limit wrote 11.3 MB with a 719 KB `agent_end` line, about 1.5 times under both old ceilings; the translators copy counts and identifiers out of a frame, never its content, so a longer line leaves the normalized payload limit untouched. Checked on Windows with byte-exact synthetic streams |
| Windows process identity (`boot_id`, `resolve_executable`) | `process_identity.py` | the first of the four Windows supervisor contracts (`.agent/design/windows-supervisor.md`): on `win32` the boot session id is the registry's `MachineGuid` and the prefetcher's `BootId` joined exactly as reported (`<guid>:<count>`, re-read on every call, any registry failure or wrong value kind refused), and an executable is resolved under the platform's own rules through the explicit environment alone: `;`-separated `PATH` entries each rooted on a drive or a share followed by a separator, a bare stem completed with `.exe` then `.com` only, `PATHEXT` never read, `.cmd`/`.bat` shims refused (the program that would run is `cmd.exe`), drive-relative and rootless paths refused before any join, the result a real-path `.exe`/`.com` file. The POSIX branches are textually unchanged. Native Windows evidence for the boot id, the interpreter's identity and the lookup rules; the registry rules also run on every platform through a scripted `winreg` |
| Windows evidence files (`windows_supervisor` without a Job Object; by-hand record reads) | `windows_supervisor.py`, `supervised_record.py`, `supervised_operation.py` | the second Windows supervisor contract: the evidence half of the Windows supervisor keeps the POSIX rules where Windows has no flag for them. Every path is `lstat`-ed before it is opened and refused unless it is a regular, non-reparse file (Windows creates *through* a dangling symlink on an exclusive open, so the creation is preceded by an `lstat` too); a pin must have the identity of both the write descriptor and the `lstat`; a rollback closes every descriptor before it removes by identity (an open file cannot be unlinked, renamed or replaced on Windows); stream facts are read from offset 0 through the pins in binary mode (no `pread`), bounded by the one `MAX_STREAM_DIGEST_BYTES` in `posix_supervisor`; `streams_match` proves what was opened is what was seen. The record reads, the partial creation and the record digest gained the same `lstat`-first branch where `O_NOFOLLOW` is missing; the POSIX branches are textually unchanged and the wiring still goes through the POSIX supervisor. No `supervise` yet: the Job Object is the next contract |
| Windows Job Object supervision (`windows_supervisor.supervise`) | `windows_supervisor.py` | the third Windows supervisor contract: the same signature, check order, hook, evidence files, observation and `SupervisedRun` as the POSIX supervisor, with a Job Object in place of the process group. The leader is created `CREATE_SUSPENDED`, assigned to a fresh job with `KILL_ON_JOB_CLOSE` and only then resumed, so every descendant is born inside the job; a timeout or a cancellation ends the whole job at once with `TerminateJobObject` (no grace, exit code `TERMINATED_EXIT_CODE` = 0xC000013A, reported as `timeout_killed` or `cancelled`); after the leader is gone the job's member count is polled, a survivor is a straggler, and a job not confirmed empty within the reap window (or whose count cannot be read) is `cleanup_confirmed=False` and `START_UNKNOWN`. `ctypes` is loaded on the first call, never at import. Native evidence on Windows Server 2025 with real `python` process trees: normal exit, nonzero exit, a three-level tree ended by a timeout, a straggler reaped after a normal exit, cancellation, start failure, a process refused by the job terminated while still suspended, unconfirmed cleanup, interrupt absorption in the reap poll, the handle census returning to its baseline |
| Windows wiring (`native_supervisor`, identities beyond the JSON profile) | `supervised_record.py`, `supervised_operation.py` | the last Windows supervisor contract: the record module chooses the supervisor by platform on every call (`os.name == "nt"` is the Job Object, `"posix"` the process group, anything else `E_RUNTIME_PLATFORM` before a file is written or a process started) and hands every argument over unchanged through a module-level `supervise`; the bridge asks the chosen module's `streams_match` through its own module name. A file identity (`st_dev`, `st_ino`) is 64 bits wide on Windows and does not fit the canonical JSON integer profile the records are written in, so an identity beyond `MAX_SAFE_INTEGER` is recorded as its shortest decimal text and read back as the number, with exactly one form per value (text for a number the profile could carry, a sign, a leading zero, a non-ASCII digit or more than 64 bits is damaged evidence); every record with identities inside the profile keeps its bytes. Native evidence on Windows Server 2025 with real Job Object process trees: `record_supervised_run`, `run_supervised_operation` (normal, nonzero exit, a timeout ending a three-level tree, cancellation, start failure, unconfirmed cleanup, a same-account write after the reap), `run_adapter_operation` with the stub adapter settling usage against the supervisor's digest and both stream ceilings reaching `read_stream` on the run path, `recover_after_restart` on a durable ledger surviving a reopen, the handle census returning to its baseline. The POSIX-guarded cases keep their guards: their fixtures are POSIX-shaped (non-`.exe` garbage programs, `killpg`, FIFOs, `setsid`); the two classes measured as platform-neutral run on both |
| Operator-driven supervised runs (`smctl run init`, `smctl run once`, `smctl run recover`) | `run_cli.py`, `cli.py` | the three controller-cli contracts (`.agent/design/controller-cli.md`): `init` creates a ledger and seeds one request with its tasks from a strictly checked specification file (exact key sets and types, a 64 KiB ceiling, a rehearsal on an in-memory scheduler before any file exists, nothing left behind on a refusal); `once` validates a job file before the ledger is opened, makes exactly one claim in the ledger's current generation, refuses a replayed claim without running it, and runs exactly one adapter invocation under the platform's supervisor with everything the child sees named in the file (the environment mapping, the allowed credential names, the command, the working directory, the timeout, both stream ceilings; `os.environ` never read), the candidate from `--candidate` or the digest of the two records. The conventions are `smctl ledger`'s: one JSON line on stdout, a refusal as one JSON line on stderr naming the claim that did happen, exit 3, argparse exit 2, the ledger closed in a `finally`; the outcome of a run that happened is the result with exit 0 whatever state it reached. Only the offline adapter is known; `pi-session-v3` and a real pi are the third contract's. Checked on Windows through real `smctl` processes and in-process calls with local `python -c` children. `recover` (the second contract) opens the ledger and runs `restart_recovery.recover_after_restart` exactly once with the owner from the command line and the same candidate rule as `once`, writing the driver's report as it is (the generation rolled over to and one entry per operation acted on, in claim order); it claims nothing, runs nothing, settles no usage and reopens no request, so every request stays held with `RECOVERY_REQUIRED` for `smctl ledger settle`/`resolve`/`resume`. A job whose environment names or values the invocation's digest would refuse is now refused by the job check before any claim (`E_RUN_JOB`) instead of stranding a RESERVED operation. The request-resume crash loop holds with product commands and real processes on Windows: a `smctl run once` controller killed with `TerminateProcess` mid-run (its child ends with the Job Object within 0.02 s, the handoff record on disk and no observation), `smctl run recover` from another process (`recovered`/`START_UNKNOWN`), the operator's `settle` one dimension at a time (which adds a `USAGE_UNKNOWN` hold the resume has to name), `resolve ABANDONED` (`NOT_STARTED` is `E_RESOLVE_CONFLICT` because the descriptor exists), `resume` naming both holds, and `smctl run once` again in the new generation. A job names its adapter from a registry keyed by the adapters' own ids (`offline`, `pi`; matched exactly), and `once` instantiates that adapter per call with nothing else depending on it: a pi job's child writes pi's `--mode json` session v3 frames, which `pi_events.PiNativeAdapter` translates and settles like the offline stub's events. Checked with the captured, reduced pi streams of `tests/fixtures/pi_native/` replayed by local `python -c` children through real `smctl` processes: the five token dimensions settle to the dispatcher's own numbers recorded next to each fixture and agree with `smctl ledger reference`, a truncated stream reviews with its usage unknown and holds the request, offline events through `pi` and pi frames through `offline` are the adapter's own `result_error`. The command starts no provider and resolves no program: the job's argv is the supervisor's to resolve, so on Windows the `pi.cmd` shim is `NOT_STARTED` with `E_RUNTIME_IDENTITY` and a real pi has to be named as `node.exe` with pi's `cli.js` |
| A real pi under the supervisor (live, once) | `run_cli.py`, `windows_supervisor.py`, `pi_events.py`, `tests/fixtures/pi_native/windows-pi110-live-smctl-a001.*`, `tests/test_live_pi_fixture.py` | the one provider call this repository has made, approved by the user on 2026-10-11 and run by the planner, not dispatched (`.agent/design/live-pi-run.md`): `smctl run init` and `smctl run once` with a job naming `C:\Program Files\nodejs\node.exe` and pi 1.1.0's `cli.js` (`--provider step --model step-5-preview --thinking off --no-session --no-context-files --no-extensions --no-skills --no-mcp --no-tools --mode json -p 'Reply with exactly: OK'`), an explicit environment of nine plain variables and no credential name (pi read its own `auth.json`). The Job Object supervisor recorded the run with `executable_sha256` equal to `node.exe`'s digest, the stream was 15 session v3 frames all known to the translator, the answer was `OK`, the outcome `REVIEW_PENDING` with `input_tokens` 536, `output_tokens` 3, `cache_read_tokens` 0, `cache_write_tokens` 0, `total_tokens` 539 settled in the ledger, and `smctl ledger reference` agreed frame for frame. The stream was reduced into the fixture with the settled numbers recorded next to it, checked for credential-shaped strings before commit, and the test replays it through the same command to the same settlement. One call, one prompt, no tools; everything else about providers stays NOT_RUN |
| Planning and contracts | `planner_protocol.py`, `planner.py`, `planner_projection.py` | PlanningBundle, Contract IR, same-source MD/JSON, draft CAS and publication idempotency |
| State and evidence | `state.py`, `verification.py`, `integration_records.py` | append/replay projection, coverage denominator, evidence manifests and delivery decisions |
| Integration gates | `integration_gates.py` | fixed contract/candidate identity, offline wiring and production fail-closed decisions |
| Persistence | `persistence.py` | SQLite WAL/FULL resources, idempotency, generation fencing and hash-chain journal |
| Observability/migration/observer | `observability.py`, `migration.py`, `observer.py` | redaction, CAS preview, source-bound read-only observer degradation |

The current KitExperiment check (Windows Server 2025, system Python 3.12.10 and
pytest 9.1.1; the native POSIX rows are skipped there and stay NOT_RUN for
Windows) is:

```text
python -m pytest -q                 955 passed, 281 skipped
python -m compileall -q sentinelmesh tests
git diff --check
```

With the captured Pi streams present under `.agent/runs` (planner evidence,
ignored by git) the same tree runs 688 passed / 286 skipped.

The last macOS count before the repositories moved back to Windows on
2026-10-08 was 837 passed at `bd9c029`; on macOS the same tree also runs the
POSIX rows. The 33 cases that failed on Windows after the move were all POSIX
facts exercised without a platform guard (PATH rules, boot id, `/dev/fd`,
`SIGKILL`, `link/..`, `O_NOFOLLOW`) and now carry
`@unittest.skipIf(os.name != "posix", ...)` (`62124c9`).

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

The Scheduler wiring is recorded in `.agent/reviews/scheduler-supervised-transitions/`
and `.agent/reviews/supervised-operation-bridge/`, following the user's decisions
in `.agent/design/scheduler-supervisor-wiring.md`:
- `scheduler-supervised-transitions` attempt-001 was REVISE: str subclasses
  (including `StrEnum`) passed the new checks, changed the ledger and only then
  failed while the event was encoded. Attempt-002 checked exact types and
  passed; the planner closed test gaps and made the tests run on Python 3.10
  in `68b6e05`.
- `supervised-operation-bridge` attempt-001 was accepted with an exception (it
  read two files through the main repository's path, read-only). The
  independent review then showed that the supervised child could forge a
  NOT_STARTED observation and get a run that did start released. The planner
  made the live path check the records against what the supervisor returned
  in `368b91a`. Recovery after a rollover still has only the records; in a
  same-account setting they are correlation, not authentication.
- `scheduler-exact-inputs` (`.agent/reviews/scheduler-exact-inputs/`) made the
  older Scheduler methods leave the ledger unchanged when they reject a call,
  and `scheduler.recovered` now names the new owner. Attempt-001 was REVISE:
  `observe` still let an unhashable dimension escape as `TypeError`. Attempt-002
  fixed it and passed. `rollover()` at generation 2**53-1 still changed the
  ledger before it failed; `scheduler-operation-view` fixed that (below).

The adapter wiring is recorded in `.agent/reviews/adapter-protocol-port/` and
`.agent/reviews/adapter-supervised-run/`, following the planner decisions in
`.agent/design/adapter-supervisor-wiring.md`:
- `adapter-protocol-port` ported Code-Kit's adapter boundary verbatim and passed
  against a differential oracle on the frozen reference.
- `adapter-supervised-run` passed. Recording usage after the run would have been
  too late: `finish` stops a request whose usage is not final, so the usage is
  recorded through the bridge's `candidate_hash` call, while the run is still
  RUNNING. The independent review found that a non-string `evidence_dir` left an
  operation HANDOFF_ACCEPTED; the planner fixed the bridge in `9726844`.

The output stream trust boundary is recorded in
`.agent/design/stream-trust-boundary.md` (planner decisions E1–E10) and in
`.agent/reviews/supervisor-stream-facts/` and
`.agent/reviews/scheduler-operation-view/`:
- `supervisor-stream-facts` pins both evidence files before the start and,
  after the reap, records the device, inode, byte count and SHA-256 of each
  stream in observation schema v2; `streams_match` tells whether the files are
  still exactly those facts.
  - Attempt-001 was REVISE. The independent review found three problems: one
    `ftruncate` on the child's own stdout made the supervisor read (and hold in
    memory) a sparse file of any size; `streams_match` raised on a path holding
    a NUL or a lone surrogate; and a rollback removed whatever file the path
    named.
  - Decision E10 caps the digest at 256 MiB; a larger stream has no facts.
  - Attempt-002 fixed all three and passed. The review found one nearly
    unreachable deviation (an identity `fstat` outside the protected region)
    and three test gaps; the planner completed them in `d69d45d`.
- `scheduler-operation-view` passed. `Scheduler.operation()` is a read-only
  view of one operation, and `rollover()` and the constructor refuse a
  generation beyond 2**53-1 before anything changes.
- `verified-review` passed. The bridge reviews a run, and recovery finishes
  one, only while the streams match; the adapter settles usage in the bridge's
  hook against the supervisor's digest and reconciles its dimensions with the
  reservation. The independent review found that an interrupt during the
  stream check left the operation RUNNING, which the contract had not asked to
  prevent; the planner completed it in `4fb0ceb`.

The durable scheduling ledger is recorded in `.agent/design/durable-ledger.md`
(the user's decisions in section 5, planner decisions L1–L7) and in
`.agent/reviews/durable-ledger/`:
- `durable-ledger` wraps the in-memory Scheduler in a SQLite command log with
  replay.
  - Attempt-001 was REVISE. The independent review found that an interrupt
    inside the in-memory call left the instance usable while the log missed
    the call, so later commands no longer replayed; a refused open leaked its
    connection, raised non-ledger errors and could create a file; and a failed
    create could remove another creator's ledger.
  - Attempt-002 fixed all of them and was REVISE again: the v2 connection
    collapsed `..` as text while `lstat`, `O_EXCL` and `unlink` follow symbolic
    links, so `a/link/../ledger.db` made create write the ledger tables into
    another program's database. The contract's wording invited that reading.
  - With the rework budget spent, the planner completed it in `bcf57d6`,
    together with four low deviations and two test gaps from the mutation
    check.
- `restart-recovery` passed. A handoff records its run descriptor, and
  `recover_after_restart` settles what a crashed controller left undecided.
  The independent review found one low copy-order deviation (the command list
  was checked before it was copied), two test gaps, and a bridge change the
  contract implied: an empty `evidence_dir` or `worktree_id`, or one with a
  control character, is now refused at handoff and the operation stays
  RESERVED. The planner completed them in `b9a68be`.
- `request-resume` passed (`.agent/design/request-resume.md`, the user's
  decisions in section 5, planner decisions P1–P7). The independent review
  found two contract-level gaps: the resume compares only the first stop
  reason, so a later explicit hold on an already stopped request is cleared
  with it, and an operation the ledger saw running can still be resolved
  NOT_STARTED with its unrecorded usage taken as zero. Both go to a follow-up
  contract; five check pairs whose order no test pinned were completed by the
  planner in `0419531`.
- `resume-holds` passed (section 9 of the same note, the user's decisions on
  those two gaps): every stop reason since the last resume is kept and has to
  be named, and a run the ledger saw start cannot be resolved or recovered
  NOT_STARTED. The review found that both decisions stop short of their
  intent: the bridge registers the start only after the run, so the commonest
  crash (mid-run) still allows NOT_STARTED, and two holds with the same reason
  text merge. Two small points were completed by the planner in `84c9a57`.
- `explicit-holds` passed (section 10, the user's decisions on those two
  gaps): every explicit stop is its own hold, and an operation handed to a
  supervised run can never be resolved NOT_STARTED. The review found that the
  planner's own decision P13 ("recover unchanged") left `recover(NOT_STARTED)`
  as a second way to do exactly that on a decided supervised run; the planner
  closed it in `d26c986`.
- `ledger-cli` passed (`.agent/design/ledger-cli.md`): the user kept Python
  over TypeScript, chose hand-typed resume reasons, the fail-closed ledger for a
  concurrent controller and five commands. The review found four low points --
  `resume` reading the reasons first changed the error order, an empty `settle`
  evidence was replaced by a derived digest, an output failure after a commit
  looked like an argument error, and no test could see a missing close -- which
  the planner completed in `2e303d0`.

The Pi native event translation is recorded in
`.agent/design/pi-native-events.md` (the user's decisions in section 5,
planner decisions N1–N16) and `.agent/reviews/pi-native-events/`:
- `pi-native-events` was ACCEPTED_WITH_EXCEPTION (Windows, pi 0.99.2). The
  candidate implements the contract clause by clause: `check --fresh` 7/7,
  35 of 36 mutations caught with the oracle and the executor tests together,
  and the translation agrees with the dispatcher's sums on all 31 captured
  streams and with the planner draft event for event. The one exception, found
  by the independent review, was the executor's test for "run hands the
  reducer copies of the events", written against a fresh `translate()` call so
  it could never fail; G5 was waived for that point and the planner completed
  the test (36 of 36 mutations caught afterwards), compared reasons whole
  instead of by prefix, and pinned three readings the contract had left open
  (a malformed first header still counts as the header, the object and type
  checks come before the terminal gate, an unencodable frame digests to no
  bytes). Integrated as `0ab0211` (oracle and fixtures), `301857c`
  (candidate) and `a97f3cd` (completion); full suite 603 passed on Windows.

The crash-run usage reference is recorded in
`.agent/design/ledger-reference.md` (the user's decisions in section 5,
planner decisions R1–R10) and `.agent/reviews/ledger-reference/`:
- `ledger-reference` passed (Windows, pi 0.99.2, 737 s, no scope finding).
  `check --fresh` 8/8; two mutation rounds caught 32 of 34 each, with the
  oracle and the executor tests together and with the executor tests alone
  (the two left are an amount of `None`, which the ledger never records as
  final, and the `lstat` regular-file check, shadowed by the reparse-point
  check and the open). The independent review recommended PASS and found one
  low point: an open that fails with `FileNotFoundError` after the `lstat`
  reads as `missing` where the contract had written `unreadable`; the planner
  ruled for the candidate (the POSIX branch reports `os.open` the same way),
  recorded it as R10, pinned both mappings in the oracle and tightened three
  of the executor's assertions. Integrated as `02b04ef` (oracle), `015fe05`
  (candidate) and `1e5832e` (completion); full suite 684 passed on Windows.

The ledger checkpoint and compaction is recorded in
`.agent/design/ledger-compaction.md` (the user's decisions in section 5,
planner decisions K1–K10) and `.agent/reviews/ledger-compaction/`:
- `ledger-compaction` attempt-001 timed out at the 1800 s limit with the
  candidate complete and no executor report: the implementation, the
  executor tests (30 cases) and the self-checks were done, and the time went
  into the long test list the contract asked for. Per the kit's rule for a
  timed-out but complete candidate it was REVISE (class TIMEOUT), the
  planner's itemized review replaced the report, and the rework commit was
  integrated directly. `check --fresh` 8/8; two mutation rounds caught
  46 of 46 with the oracle and the executor tests together and
  44 of 46 with the executor tests alone (two cases were shadowed
  by another rule; the completion edits an operation whose holds were released
  and a task nobody claimed, after which the executor tests catch both). The
  independent review found process deviations only -- three probes with
  `tempfile.mkdtemp()` left uncleaned, piped self-checks -- and low polish
  points the planner completed. Integrated as `dfb56a3` and `38f0c4e`
  (oracle), `76252ed` (candidate) and `c7c280d` (completion); full suite
  730 passed on Windows.

The operator reads of the events are recorded in
`.agent/design/ledger-events.md` (the user's decisions in section 5, planner
decisions E1–E7) and `.agent/reviews/ledger-events/`:
- `ledger-events` passed (Windows, pi 0.99.2, 571 s, no scope finding). The
  contract kept the executor's test list short after the compaction timeout:
  `check --fresh` 8/8, mutations 24/24 with the oracle and the executor tests
  together and 19/24 with the executor tests alone (the five left are rules
  the contract left to the oracle), independent review PASS with one low
  test point the planner completed. Integrated as `cdb1e62` (oracle),
  `054631a` (candidate) and `028d882` (completion); full suite 755 passed on
  Windows.

The stream ceilings are recorded in `.agent/design/stream-ceilings.md` (the
measurements in section 1, the user's decisions in section 5, planner
decisions S1–S8) and `.agent/reviews/stream-ceilings/`:
- `stream-ceilings` passed (Windows, pi 0.99.2, 467 s, no scope finding):
  `check --fresh` 8/8, mutations 28/28 with the oracle and the executor tests
  together and 15/28 with the executor tests alone (the thirteen left are
  rules the contract left to the oracle), independent review PASS with two
  low tidiness points the planner completed; the hand-through of the line
  ceiling to the two reductions on the run path is not reachable by any test
  on Windows and was confirmed by the reviewer's stub probe. Integrated as
  `53355a5` (oracle), `0c33ad4` (candidate) and `f60538e` (completion); full
  suite 781 passed on Windows.

The Windows supervisor direction is recorded in
`.agent/design/windows-supervisor.md` (the measurements and the local Win32
probe in section 1, the user's decisions in section 5, planner decisions
W1-W10, the four contracts in section 7) and `.agent/reviews/win-identity/`:
- `win-identity` passed (Windows, pi 0.99.2, 673 s, no scope finding):
  `check --fresh` 8/8, mutations 31/33 with the oracle and the executor tests
  together (the two left were oracle gaps the planner closed) and 23/33 with
  the executor tests alone, independent review PASS with three points the
  planner completed (the share-root rule follows the contract's wording; two
  executor cases strengthened). Integrated as `2570f59` (oracle), `4423c84`
  (candidate) and `048bf32` (completion); full suite 820 passed on Windows.
  The remaining three contracts (evidence files, Job Object supervision,
  wiring) are what turns the native Windows Job Object row below from
  NOT_RUN into evidence.
- `win-evidence` passed (Windows, pi 0.99.2, 602 s, no scope finding):
  `check --fresh` 8/8, mutations 30/31 with the oracle and the executor tests
  together (the one left is equivalent: the size check precedes the read
  count) and 10/31 with the executor tests alone (the 21 left are rules the
  contract left to the oracle), independent review PASS with two docstring
  completions by the planner and two facts recorded for the Job Object
  contract (descriptor numbers are reused after a rollback; a child still
  holding the write handle keeps the file from being removed). Integrated as
  `3c14b2a` (oracle), `bb0fecd` (candidate) and `776f3a4` (completion); full
  suite 846 passed on Windows.
- `win-supervise` passed (Windows, pi 0.99.2, 1134 s, no scope finding):
  `check --fresh` 8/8, mutations 31/32 with the oracle and the executor tests
  together (the one left was an oracle case that let the mutant's escaped
  interrupt abort the session; fixed in the planner completion and caught
  since) and 16/32 with the executor tests alone (the 16 left are rules the
  contract left to the oracle), independent review PASS with no high or
  medium finding (the handle census now pays CPython's one-time cost of a
  failed CreateProcess before its baseline). Integrated as `dc3179a`
  (oracle), `a387800` (candidate) and `0cf6f8f` (completion, oracle only);
  full suite 875 passed on Windows.
- `win-wiring` passed (Windows, pi 0.99.2, 1447 s, no scope finding):
  `check --fresh` 9/9, mutations 20/25 plus 6/7 once re-anchored on the candidate wording (the one left was an oracle gap, closed in the completion and caught since) with the oracle and the executor tests
  together and 6/20 with the executor tests alone (the ones left are rules the
  contract left to the oracle), independent review PASS with three comment-level findings fixed by the planner. The contract
  was preceded by a measurement the design had not foreseen: Windows file
  identities are 64-bit and the observation record could not be written at
  all (`E_INTEGER_RANGE`), which W11 settles. Integrated as `17503e9` (oracle),
  `2c5b4ae` (candidate) and `640aa4e` (completion); full suite
  909 passed on Windows. Native Windows Job Object supervision is
  evidence now, through the same wiring the POSIX supervisor uses.

The controller-cli direction is recorded in `.agent/design/controller-cli.md`
(user decisions in section 5, three contracts in section 7) and
`.agent/reviews/run-cli-once/`:
- `run-cli-once` passed (Windows, pi 0.99.2, 1067 s, no scope finding):
  `check --fresh` 8/8, mutations 36/37 once anchored on the candidate wording (the one left is equivalent: the file ceiling by truncation) with the oracle and the executor tests
  together and 11/37 with the executor tests alone, independent review PASS
  with one medium finding fixed by the planner (a RecursionError from a deeply nested file escaped the loaders as an argparse-style exit 2). Integrated as `f5f1de5` (oracle), `0106f32` (candidate)
  and `5dd47b7` (completion); full suite 932 passed on Windows.
- `run-cli-recover` passed (Windows, pi 0.99.2, 605 s, no scope finding):
  `check --fresh` 8/8, mutations 27/28 with the oracle and the executor tests
  together (the one left is equivalent: `RuntimeErrorCode` is a `ValueError`)
  and 24/28 with the executor tests alone, independent review PASS with five
  low findings (two docstring clauses and one test anchor fixed by the
  planner). Integrated as `4d5f484` (oracle), `25c3cd8` (candidate) and
  `43f4f5a` (completion); full suite 943 passed on Windows.
- `run-cli-pi` passed (Windows, pi 0.99.2, 484 s, no scope finding):
  `check --fresh` 8/8, mutations 13/15 with the oracle and the executor tests
  together and the same 13 with the executor tests alone (one insert-only
  mutant, one oracle gap -- a dimension added behind a pi job's back -- closed
  by the planner and verified caught), independent review PASS with two low
  findings fixed by the planner. Integrated as `757a7b4` (oracle), `c72ee3f`
  (candidate) and `5786d0b` (completion); full suite 953 passed on Windows.

The live pi run is recorded in `.agent/design/live-pi-run.md` (user approval
in section 5, the measured run in section 9): planner-run, one call, the
reduced stream and the settled numbers in
`tests/fixtures/pi_native/windows-pi110-live-smctl-a001.*`, replayed by
`tests/test_live_pi_fixture.py`; full suite 955 passed on Windows.

## Explicitly not proven by this repository

The following remain `NOT_RUN` or `BLOCKED` until separately captured with
approved evidence: real Pi/Claude provider CLI calls, credentials and network
access beyond the one live pi run recorded above (one prompt, no tools, the
credential read by pi itself; nothing else has called a provider), native
Linux process supervision, a real pi under the supervisor on any platform but
Windows and in any shape but that one run (frame types no captured run has
shown -- compaction, retry, queue, `entry_appended` -- still fail closed as
unknown until a later dialect),
the runtime behaviour of `read_stream`'s POSIX branch since the move to
Windows (its text is unchanged; the native cases that exercise it are
skipped here),
power-loss durability of the ledger and of the start records, operator
authority and the evidence behind an operator's resolution (recorded, never
verified),
records and output streams that a same-account process cannot forge (a
descendant that escapes the process group with `setsid` is still not detected:
what it writes after the reap no longer matches the recorded stream facts, but
what it writes before them cannot be told apart from the run's own output, and
on the recovery path the streams and the records can be forged together),
three-platform parity, distributed storage/locking, billing reconciliation,
production migration and rollback, CI artifact federation, and a real
heterogeneous provider chain. Offline fixtures never upgrade these rows to
`PASS`.
