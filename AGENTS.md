# KitExperiment agent rules

KitExperiment is a dependency-free Python reference implementation of the
Code-Kit/SentinelMesh control-plane contracts.

## Validation

- Use Python 3.10+ for the declared project target; the local compatibility
  runner may also be Python 3.9.
- Run `python -m pytest -q`, `python -m compileall -q sentinelmesh tests`, and
  `git diff --check` before reporting a change.
- Keep offline, synthetic, captured, and native evidence separate. Missing
  provider, platform, credentials, or production connector evidence remains
  `NOT_RUN` or `BLOCKED`.

## Boundaries

- Do not start a real provider CLI, read credentials, or modify a user
  repository from library imports.
- Do not let model text, process exit code, or observer output grant approval,
  publish a contract, or authorize integration.
- Preserve immutable resource references, canonical JSON, CAS/generation
  checks, and fail-closed validation.
- Changes to Code-Kit itself must be made in `D:\Code\Code-Kit` and validated
  with its existing test suite; KitExperiment changes must remain in this
  repository.

## Pi dispatch

Pi tasks use `step-5-preview` with `high` thinking through the repository
dispatcher. Contracts, `.agent/`, and AGENTS.md are control-plane inputs and
must not be changed by an executor.
