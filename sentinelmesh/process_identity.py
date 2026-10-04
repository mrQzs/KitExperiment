"""Explicit-environment digest/verification and POSIX process identity capture.

This module carries the two prerequisites of the native POSIX supervisor
described in `.agent/design/posix-runtime-runner.md` (section 2, items "输入"
and "身份"):

* the canonical SHA-256 of an *explicit* environment mapping plus its
  verification against `InvocationSpec.environment_sha256`.  The supervisor
  never inherits `os.environ` (user decision 2026-10-04, section 5), so a run
  only starts after the caller's mapping has been proven to be exactly the one
  the invocation was locked to;
* native identity facts: the boot session id, the realpath-resolved executable
  and its SHA-256, assembled into the canonical `ProcessIdentity`.

Everything here is fail-closed: every rejection raises
`sentinelmesh.runtime.RuntimeErrorCode` with a stable code, and nothing is
inferred from `os.environ`, from process output, or from a partially readable
platform source.  Importing the module has no side effect: no `ctypes` import,
no file read, no environment read and no process start.
"""
from __future__ import annotations

import collections.abc
import hashlib
import json
import os
import re
import shutil
import sys
from typing import Any, Mapping

from .runtime import InvocationSpec, ProcessIdentity, RuntimeErrorCode

# Schema tag of the canonical environment document.  Bumping it changes every
# digest, which is the point: an old mapping can never verify against a new
# supervisor.
_ENVIRONMENT_SCHEMA = "sentinelmesh-environment-v1"

# A boot session id is a UUID in one of the three textual UUID versions; the
# textual form is kept exactly as the platform reports it (macOS reports an
# upper-case UUID) so the captured identity never silently normalises evidence.
_BOOT_ID_PATTERN = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)

# `sysctlbyname` is one of the few platform queries that cannot be satisfied
# with pure Python, so it is the only reason `ctypes` exists in this module.
_DARWIN_BOOT_ID_SYSCTL = b"kern.bootsessionuuid"
_LINUX_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

# Chunked hashing keeps `file_sha256` bounded in memory for large binaries.
_SHA256_CHUNK_BYTES = 1 << 20


def _scalar_text(value: str) -> bool:
    """Return whether every character is a printable scalar value.

    NUL and the other C0 controls, DEL and the surrogate range are rejected;
    they cannot be transported through a POSIX `execve` environment block or
    through canonical JSON, so accepting them would create a digest that no
    supervisor could ever reproduce.
    """
    for char in value:
        code = ord(char)
        if code < 0x20 or code == 0x7F or 0xD800 <= code <= 0xDFFF:
            return False
    return True


def _validated_environment(environment: Any) -> Mapping[str, str]:
    """Validate an explicit environment mapping or raise ``E_RUNTIME_ENVIRONMENT``.

    Names must be non-empty text without ``=`` (a ``name=value`` block has no
    other legal syntax); values may be empty.  Both are ordinary scalar text.
    """
    if not isinstance(environment, collections.abc.Mapping):
        raise RuntimeErrorCode("E_RUNTIME_ENVIRONMENT", "environment must be a mapping")
    for name, value in environment.items():
        if type(name) is not str or not name:
            raise RuntimeErrorCode("E_RUNTIME_ENVIRONMENT", "environment name must be non-empty text")
        if "=" in name:
            raise RuntimeErrorCode("E_RUNTIME_ENVIRONMENT", "environment name must not contain '='")
        if type(value) is not str:
            raise RuntimeErrorCode("E_RUNTIME_ENVIRONMENT", "environment value must be text")
        if not _scalar_text(name) or not _scalar_text(value):
            raise RuntimeErrorCode(
                "E_RUNTIME_ENVIRONMENT",
                "environment entry contains a control or non-scalar character",
            )
    return environment


def environment_digest(environment: Mapping[str, str]) -> str:
    """Return the canonical SHA-256 of an explicit environment mapping.

    The document is ``{"schema": ..., "variables": {name: value}}`` encoded as
    canonical JSON (`ensure_ascii=False`, sorted keys, no whitespace) and hashed
    as UTF-8, so the digest depends on the mapping's content only and never on
    its iteration order.
    """
    variables = _validated_environment(environment)
    payload = {"schema": _ENVIRONMENT_SCHEMA, "variables": dict(variables)}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def verified_environment(spec: InvocationSpec, environment: Mapping[str, str]) -> dict[str, str]:
    """Return a private ``dict`` copy of ``environment`` once it matches ``spec``.

    The spec is the locked identity of the invocation: a mapping that digests to
    anything else is a different environment and must fail closed before any
    process is started.  The returned mapping is a plain ``dict`` copy, so a
    caller mutating its original mapping afterwards cannot change the verified
    environment handed to the supervisor.
    """
    if type(spec) is not InvocationSpec:
        raise RuntimeErrorCode("E_RUNTIME_INVOCATION", "verified_environment requires an InvocationSpec")
    if environment_digest(environment) != spec.environment_sha256:
        raise RuntimeErrorCode("E_RUNTIME_ENVIRONMENT", "environment digest does not match the invocation spec")
    return dict(environment)


def _validated_boot_id(text: str, source: str) -> str:
    if not isinstance(text, str) or not _BOOT_ID_PATTERN.fullmatch(text):
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", f"{source} is not a boot session id")
    return text


def _darwin_boot_id() -> str:
    """Read ``kern.bootsessionuuid`` through `sysctlbyname` in libSystem."""
    import ctypes

    # ``CDLL(None)`` is the main program handle, which on macOS resolves the
    # libSystem symbols the interpreter is already linked against.  It needs no
    # library-name lookup, so no helper subprocess is ever started.
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except (OSError, ValueError) as exc:  # pragma: no cover - platform failure
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", f"libSystem is unavailable: {exc}") from exc
    if not hasattr(libc, "sysctlbyname"):
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "sysctlbyname is unavailable")
    length = ctypes.c_size_t(0)
    if libc.sysctlbyname(_DARWIN_BOOT_ID_SYSCTL, None, ctypes.byref(length), None, 0) != 0:
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "sysctlbyname(kern.bootsessionuuid) length query failed")
    if length.value == 0:
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "kern.bootsessionuuid is empty")
    buffer = ctypes.create_string_buffer(length.value)
    if libc.sysctlbyname(_DARWIN_BOOT_ID_SYSCTL, buffer, ctypes.byref(length), None, 0) != 0:
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "sysctlbyname(kern.bootsessionuuid) read failed")
    # ``sysctlbyname`` reports the true length, NUL terminator included, so strip
    # exactly one terminator instead of truncating at the first NUL byte: an
    # embedded NUL then stays visible to the format check below.
    raw = buffer.raw[: length.value]
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "kern.bootsessionuuid is not UTF-8 text") from exc
    return _validated_boot_id(text, "kern.bootsessionuuid")


def _linux_boot_id() -> str:
    """Read ``/proc/sys/kernel/random/boot_id`` (native evidence stays NOT_RUN)."""
    try:
        with open(_LINUX_BOOT_ID_PATH, "r", encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, ValueError) as exc:
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", f"{_LINUX_BOOT_ID_PATH} is unreadable") from exc
    return _validated_boot_id(text.strip(), _LINUX_BOOT_ID_PATH)


def boot_id() -> str:
    """Return this machine's boot session id, re-read on every call.

    Never cached: a cached boot id would keep "verifying" a process after a
    reboot, which is exactly when a stale pid can be recycled by another
    process.  Unsupported platforms fail closed.
    """
    if sys.platform == "darwin":
        return _darwin_boot_id()
    if sys.platform.startswith("linux"):
        return _linux_boot_id()
    raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "boot id is not available on this platform")


def resolve_executable(argv0: str, *, cwd: str, environment: Mapping[str, str]) -> str:
    """Resolve ``argv0`` against an explicit ``environment`` and ``cwd``.

    A name containing ``/`` is a path: absolute paths are used as given and
    relative ones are joined with ``cwd``.  A bare name is only looked up in
    ``environment["PATH"]`` through `shutil.which`; `os.environ` is never read,
    so a supervisor that refuses to inherit the parent environment cannot
    resolve to a binary the caller did not name.  The result is the real path of
    an executable regular file, or ``E_RUNTIME_IDENTITY``.
    """
    if type(argv0) is not str or not argv0:
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "argv0 must be non-empty text")
    if type(cwd) is not str or not cwd:
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "cwd must be non-empty text")
    _validated_environment(environment)
    if "/" in argv0:
        candidate = argv0 if os.path.isabs(argv0) else os.path.join(cwd, argv0)
    else:
        if "PATH" not in environment or not environment["PATH"]:
            raise RuntimeErrorCode("E_RUNTIME_IDENTITY", "environment PATH is required to resolve a bare argv0")
        candidate = shutil.which(argv0, path=environment["PATH"])
        if candidate is None:
            raise RuntimeErrorCode("E_RUNTIME_IDENTITY", f"argv0 {argv0!r} was not found on environment PATH")
    resolved = os.path.realpath(candidate)
    if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", f"argv0 {argv0!r} does not resolve to an executable regular file")
    return resolved


def file_sha256(path: str) -> str:
    """Return the lowercase SHA-256 of a regular file, read in bounded chunks."""
    try:
        if not os.path.isfile(path):
            raise RuntimeErrorCode("E_RUNTIME_IDENTITY", f"{path!r} is not a regular file")
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_SHA256_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except RuntimeErrorCode:
        raise
    except (OSError, TypeError, ValueError) as exc:
        # Missing files, directories, unreadable files and non-text paths all
        # fail closed rather than falling back to a weaker identity.
        raise RuntimeErrorCode("E_RUNTIME_IDENTITY", f"{path!r} could not be hashed") from exc


def capture_process_identity(pid: int, *, executable: str, worktree_id: str) -> ProcessIdentity:
    """Assemble the canonical `ProcessIdentity` for one about-to-start process.

    ``pid`` and ``worktree_id`` are validated by `ProcessIdentity` itself, so
    its ``RuntimeErrorCode`` reaches the caller unchanged.
    """
    return ProcessIdentity(pid, boot_id(), file_sha256(executable), worktree_id)
