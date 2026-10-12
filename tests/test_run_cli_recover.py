"""Executor tests for ``smctl run recover`` and the job environment pre-check.

These cover what the planner oracle (``tests/test_run_cli_recover_oracle.py``)
does not: the command's own wiring rather than the recovery driver's rules.
The driver is patched where the question is what the command does with its
report, its refusals and its ``candidate_hash``; the pre-check is exercised
through :func:`~sentinelmesh.run_cli.load_job` directly, because that is where
it lives; and one case drives a real ``smctl`` process so the exit codes and the
single stdout line are the product's own.  No provider CLI, no credentials, no
network: the only children are ``sys.executable -c`` and ``sys.executable -m
sentinelmesh.cli``.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from sentinelmesh import cli
from sentinelmesh.adapter_run import DEFAULT_MAX_LINE_BYTES, DEFAULT_MAX_STREAM_BYTES
from sentinelmesh.durable_ledger import LedgerError, open_ledger
from sentinelmesh.run_cli import RunSpecError, load_job
from sentinelmesh.scheduler import ContractError
from sentinelmesh.supervised_operation import evidence_digest

PY = os.path.realpath(sys.executable)
NL = chr(10)
REPO_ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {"LANG": "C", "PATH": os.path.dirname(PY)}
CAPS = {"dispatches": 9, "tokens": 500, "cost": 200}
RESERVATION = {"dispatches": 1, "tokens": 40, "cost": 20}
CANDIDATE = "a" * 64
REFUSAL_KEYS = {"error", "detail", "operation_id"}
REPORT_KEYS = {"command", "generation", "operations"}


def child_code() -> str:
    """Python code that reports one complete offline run, one JSON line each.

    The events carry the fields ``normalize_events`` requires -- a sequence, an
    event id and the ``offline`` source -- so the stream settles the run and its
    usage exactly as the oracle's own child does.
    """
    events = [{"seq": 0, "event_id": "e0", "type": "run_started", "source": "offline", "payload": {}},
              {"seq": 1, "event_id": "e1", "type": "assistant", "source": "offline", "payload": {}},
              {"seq": 2, "event_id": "e2", "type": "usage", "source": "offline",
               "payload": {"tokens": 12, "cost": 3}},
              {"seq": 3, "event_id": "e3", "type": "run_finished", "source": "offline", "payload": {}}]
    return ("import json, sys" + NL
            + "for event in json.loads(%r):" % json.dumps(events) + NL
            + "    sys.stdout.buffer.write((json.dumps(event) + chr(10)).encode())" + NL
            + "sys.stdout.buffer.flush()" + NL)

SPEC = {"request_id": "req-1", "repo_id": "repo-1", "caps": dict(CAPS), "concurrency": 4,
        "tasks": [{"task_id": "task-%d" % n, "caps": dict(CAPS), "max_attempts": 3} for n in range(1, 13)]}


# -- driving the CLI ------------------------------------------------------ #

def smctl(*argv):
    """Run ``smctl`` in this process: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main([str(a) for a in argv])
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def ok(*argv):
    code, out, err = smctl(*argv)
    assert (code, err) == (0, ""), "smctl %s failed: %s" % (argv, err)
    assert out.endswith(NL) and out.count(NL) == 1, "one JSON line expected: %r" % out
    return json.loads(out)


def refused(expected, *argv, operation_id=None):
    """A refusal: exit 3, nothing on stdout, one three-key JSON line on stderr."""
    code, out, err = smctl(*argv)
    assert (code, out) == (3, ""), "smctl %s should be refused: %s" % (argv, err)
    assert err.endswith(NL) and err.count(NL) == 1, "one JSON line expected on stderr: %r" % err
    error = json.loads(err)
    assert set(error) == REFUSAL_KEYS, error
    assert (error["error"], error["operation_id"]) == (expected, operation_id), error
    assert type(error["detail"]) in (str, type(None)), error
    return error


# -- fixtures ------------------------------------------------------------- #

def make_ledger(work: Path) -> str:
    """A fresh ledger seeded with one request, created through the command."""
    ledger = str(work / "ledger.db")
    (work / "spec.json").write_text(json.dumps(SPEC), encoding="utf-8")
    ok("run", "init", ledger, "--spec", str(work / "spec.json"))
    return ledger


def job_file(work: Path, count: int, **overrides) -> str:
    job = {"request_id": "req-1", "task_id": "task-%d" % count, "plan": 1, "idempotency_key": "idem-%d" % count,
           "guard_version": 1, "reservation": dict(RESERVATION), "resources": ["src/task-%d.py" % count],
           "adapter_id": "offline", "role": "executor", "profile_id": "stub-profile",
           "command": [PY, "-c", child_code()], "capabilities": [], "cwd": str(work),
           "environment": dict(BASE_ENV), "allowed_secret_names": [], "timeout_seconds": 20,
           "usage_dimensions": ["tokens", "cost"]}
    job.update(overrides)
    path = work / ("job-%d.json" % count)
    path.write_text(json.dumps(job), encoding="utf-8")
    return str(path)


def run_once(work: Path, ledger: str, count: int, evidence: Path):
    """``smctl run once`` on one job file, reporting its parsed stdout."""
    return ok("run", "once", ledger, "--job", job_file(work, count), "--evidence", str(evidence),
              "--worktree-id", "wt-1", "--owner", "owner-1")


def evidence_dir(work: Path, name: str) -> Path:
    path = work / name
    path.mkdir()
    return path


def operation(ledger: str, operation_id):
    handle = open_ledger(ledger)
    try:
        return handle.operation(operation_id)
    finally:
        handle.close()


def status(ledger: str):
    result = ok("ledger", "status", ledger)
    assert [r["request_id"] for r in result["requests"]] == ["req-1"]
    return result["generation"], result["requests"][0]


# -- the recovery report -------------------------------------------------- #

def test_recover_cancels_a_stranded_claim_and_holds_the_request(tmp_path):
    ledger = make_ledger(tmp_path)
    reviewed = run_once(tmp_path, ledger, 1, evidence_dir(tmp_path, "ev-1"))["operation_id"]
    # A claim the bridge refused after the claim: the operation stays RESERVED.
    code, out, err = smctl("run", "once", ledger,
                           "--job", job_file(tmp_path, 2, usage_dimensions=["tokens", "gpu"]),
                           "--evidence", str(evidence_dir(tmp_path, "ev-2")), "--worktree-id", "wt-1",
                           "--owner", "owner-1")
    assert (code, out) == (3, "")
    assert json.loads(err)["error"] == "E_ADAPTER_SCHEMA"
    stranded = json.loads(err)["operation_id"]
    assert operation(ledger, stranded)["state"] == "RESERVED"

    report = ok("run", "recover", ledger, "--owner", "owner-2")
    assert set(report) == REPORT_KEYS and report["command"] == "recover"
    # Exactly the one stranded claim is reported: the run the dead controller
    # settled itself is neither cancelled nor mentioned.
    assert report["operations"] == [{"operation_id": stranded, "action": "cancelled",
                                     "state": "CANCELLED_NOT_STARTED"}]
    assert operation(ledger, stranded)["state"] == "CANCELLED_NOT_STARTED"
    assert operation(ledger, reviewed)["state"] == "REVIEW_PENDING"
    generation, request = status(ledger)
    assert generation == 2
    assert request["stop_reasons"] == ["RECOVERY_REQUIRED"]
    assert request["snapshot"]["tokens"]["held"] == 0
    assert request["snapshot"]["tokens"]["known"] == 12
    fenced = ok("ledger", "events", ledger, "--type", "controller.fenced")
    assert fenced["matched"] == 1
    assert [e["type"] for e in fenced["events"]] == ["controller.fenced"]


def test_recover_writes_the_driver_report_verbatim(tmp_path):
    ledger = make_ledger(tmp_path)
    operations = [{"operation_id": "op-failed", "action": "failed", "state": "RUNNING",
                   "error": "E_RUNTIME_ENVIRONMENT"},
                  {"operation_id": "op-skipped", "action": "skipped", "state": "HANDOFF_ACCEPTED",
                   "reason": "no run descriptor"}]
    with mock.patch("sentinelmesh.run_cli.recover_after_restart",
                    return_value={"generation": 7, "operations": operations}) as driver:
        # With --candidate the callable hands the operator's value to the driver.
        result = ok("run", "recover", ledger, "--owner", "ops-1", "--candidate", CANDIDATE)
        assert result == {"command": "recover", "generation": 7, "operations": operations}
        assert driver.call_count == 1
        assert driver.call_args.kwargs["owner"] == "ops-1"
        assert driver.call_args.kwargs["candidate_hash"](str(tmp_path)) == CANDIDATE
    with mock.patch("sentinelmesh.run_cli.recover_after_restart",
                    return_value={"generation": 7, "operations": operations}) as driver:
        # Without it the callable is the evidence records' own digest, as for
        # ``once``: a real empty directory gives the missing-record digest.
        empty = evidence_dir(tmp_path, "empty")
        ok("run", "recover", ledger, "--owner", "ops-1")
        assert driver.call_count == 1
        assert driver.call_args.kwargs["candidate_hash"] is evidence_digest
        assert driver.call_args.kwargs["candidate_hash"](str(empty)) == evidence_digest(str(empty))


def test_recover_refuses_with_the_driver_code_and_names_no_operation(tmp_path):
    ledger = make_ledger(tmp_path)
    before = Path(ledger).read_bytes()
    for exc, code, detail in ((LedgerError("E_LEDGER_CLOSED", "x"), "E_LEDGER_CLOSED", "x"),
                              (ContractError("E_RECOVERY_INPUT"), "E_RECOVERY_INPUT", None)):
        with mock.patch("sentinelmesh.run_cli.recover_after_restart", side_effect=exc):
            error = refused(code, "run", "recover", ledger, "--owner", "owner-2")
        assert error["detail"] == detail, error
        # This command claims nothing, so a refusal names no operation; the
        # ledger itself is whatever the driver left it as.
        assert Path(ledger).read_bytes() == before


# -- the job environment pre-check ---------------------------------------- #

def test_load_job_checks_the_environment_before_any_claim(tmp_path):
    path = job_file(tmp_path, 1)
    written = json.loads(Path(path).read_text(encoding="utf-8"))
    loaded = load_job(path)
    # The pre-check changes nothing about what a legal job loads: the same
    # mapping back, with the two ceilings filled in.
    assert loaded == dict(written, max_stream_bytes=DEFAULT_MAX_STREAM_BYTES, max_line_bytes=DEFAULT_MAX_LINE_BYTES)
    assert loaded["environment"] == written["environment"]
    for environment, fragment in (({"A=B": "x"}, "must not contain '='"),
                                  ({"": "x"}, "non-empty"),
                                  ({"A": "x" + chr(0) + "y"}, "control")):
        bad = job_file(tmp_path, 2, environment=environment)
        with pytest.raises(RunSpecError) as caught:
            load_job(bad)
        assert caught.value.code == "E_RUN_JOB"
        assert caught.value.detail.startswith("environment: ")
        assert "E_RUNTIME_ENVIRONMENT" in caught.value.detail
        assert fragment in caught.value.detail, caught.value.detail


# -- the real command ----------------------------------------------------- #

def test_real_smctl_recovers_a_fresh_ledger(tmp_path):
    ledger = make_ledger(tmp_path)
    completed = subprocess.run([PY, "-m", "sentinelmesh.cli", "run", "recover", ledger, "--owner", "owner-2"],
                               cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout.count(NL) == 1
    assert json.loads(completed.stdout) == {"command": "recover", "generation": 2, "operations": []}
    # A missing --owner is argparse's own usage error: the ledger is not opened.
    usage = subprocess.run([PY, "-m", "sentinelmesh.cli", "run", "recover", ledger],
                           cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
    assert usage.returncode == 2
    assert usage.stdout == ""
    assert usage.stderr != ""
