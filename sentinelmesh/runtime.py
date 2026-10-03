"""Adapter and subprocess runtime primitives for SentinelMesh."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import signal
import subprocess
import time
import math
from typing import Any, Mapping, Protocol

from .config import Profile


class RuntimeErrorSM(ValueError):
    pass


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    timeout_seconds: float
    shell: bool = False
    script: str | None = None

    def validate(self) -> None:
        if type(self.shell) is not bool:
            raise RuntimeErrorSM("E_INVOCATION_SHELL")
        if self.shell and (not isinstance(self.script, str) or not self.script.strip()):
            raise RuntimeErrorSM("E_INVOCATION_SHELL_SCRIPT_REQUIRED")
        if self.shell and self.argv:
            raise RuntimeErrorSM("E_INVOCATION_CARRIERS_CONFLICT")
        if self.shell and "\x00" in self.script:
            raise RuntimeErrorSM("E_INVOCATION_SCRIPT")
        if not isinstance(self.argv, tuple):
            raise RuntimeErrorSM("E_INVOCATION_ARGV")
        if not self.shell and (
            not self.argv
            or any(not isinstance(x, str) or not x or "\x00" in x for x in self.argv)
        ):
            raise RuntimeErrorSM("E_INVOCATION_ARGV")
        if not isinstance(self.cwd, str) or not self.cwd or "\x00" in self.cwd:
            raise RuntimeErrorSM("E_INVOCATION_CWD")
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(float(self.timeout_seconds)) or self.timeout_seconds <= 0:
            raise RuntimeErrorSM("E_INVOCATION_TIMEOUT")
        if not os.path.isdir(self.cwd):
            raise RuntimeErrorSM("E_INVOCATION_CWD")
        if not isinstance(self.env, Mapping):
            raise RuntimeErrorSM("E_INVOCATION_ENV")
        for key, value in self.env.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or "\x00" in key
                or "\x00" in value
            ):
                raise RuntimeErrorSM("E_INVOCATION_ENV")


@dataclass(frozen=True)
class ProcessResult:
    started: bool
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stop_reason: str
    duration_seconds: float
    cleanup: str
    pid: int | None = None

    def __post_init__(self) -> None:
        if type(self.started) is not bool or (self.exit_code is not None and type(self.exit_code) is not int):
            raise RuntimeErrorSM("E_PROCESS_RESULT")
        if type(self.stdout) is not bytes or type(self.stderr) is not bytes:
            raise RuntimeErrorSM("E_PROCESS_RESULT")
        if not isinstance(self.stop_reason, str) or not self.stop_reason.strip() or not isinstance(self.cleanup, str) or not self.cleanup.strip():
            raise RuntimeErrorSM("E_PROCESS_RESULT")
        if not isinstance(self.duration_seconds, (int, float)) or isinstance(self.duration_seconds, bool) or not math.isfinite(float(self.duration_seconds)) or self.duration_seconds < 0:
            raise RuntimeErrorSM("E_PROCESS_RESULT")
        if self.pid is not None and (type(self.pid) is not int or self.pid <= 0):
            raise RuntimeErrorSM("E_PROCESS_RESULT")


class Adapter(Protocol):
    adapter_id: str
    def build_invocation(self, profile: Profile, request: Mapping[str, Any]) -> Invocation: ...


@dataclass(frozen=True)
class StaticAdapter:
    adapter_id: str = "static"
    executable: str = "python"

    def build_invocation(self, profile: Profile, request: Mapping[str, Any]) -> Invocation:
        if not isinstance(request, Mapping):
            raise RuntimeErrorSM("E_REQUEST_SHAPE")
        cwd = str(request.get("cwd", os.getcwd()))
        raw_args = request.get("args", ())
        if not isinstance(raw_args, (tuple, list)):
            raise RuntimeErrorSM("E_REQUEST_ARGS")
        if any(not isinstance(x, str) for x in raw_args):
            raise RuntimeErrorSM("E_REQUEST_ARGS")
        args = tuple(raw_args)
        argv = (self.executable,) + profile.fixed_args + args
        raw_env = request.get("env", {})
        if not isinstance(raw_env, Mapping):
            raise RuntimeErrorSM("E_REQUEST_ENV")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in raw_env.items()):
            raise RuntimeErrorSM("E_REQUEST_ENV")
        env = dict(raw_env)
        return Invocation(argv, cwd, env, profile.timeout_seconds)


class SubprocessRunner:
    """Supervise an owned process. Real platform-specific Job Object support remains an adapter extension."""

    def run(self, invocation: Invocation, *, input_bytes: bytes | None = None) -> ProcessResult:
        invocation.validate()
        if input_bytes is not None and type(input_bytes) is not bytes:
            raise RuntimeErrorSM("E_INVOCATION_INPUT")
        started_at = time.monotonic()
        creationflags = 0
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(
                list(invocation.argv) if not invocation.shell else invocation.script,
                cwd=invocation.cwd,
                env={**os.environ, **dict(invocation.env)},
                shell=invocation.shell,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
                **kwargs,
            )
        except OSError as exc:
            return ProcessResult(False, None, b"", str(exc).encode("utf-8", "replace"), "start_failed", time.monotonic() - started_at, "not_started")
        try:
            stdout, stderr = proc.communicate(input=input_bytes, timeout=invocation.timeout_seconds)
            reason = "completed"
        except subprocess.TimeoutExpired as exc:
            reason = "timeout"
            stdout = exc.output or b""
            stderr = exc.stderr or b""
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(proc.pid, signal.SIGTERM)
            except (OSError, AttributeError):
                proc.terminate()
            try:
                tail_out, tail_err = proc.communicate(timeout=1.0)
                stdout += tail_out or b""
                stderr += tail_err or b""
            except subprocess.TimeoutExpired:
                proc.kill()
                tail_out, tail_err = proc.communicate()
                stdout += tail_out or b""
                stderr += tail_err or b""
                reason = "timeout_killed"
        except KeyboardInterrupt:
            reason = "cancelled"
            proc.terminate()
            stdout, stderr = proc.communicate()
        return ProcessResult(True, proc.returncode, stdout, stderr, reason, time.monotonic() - started_at, "verified", proc.pid)
