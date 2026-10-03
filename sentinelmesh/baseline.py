"""Read-only repository baseline capture and drift checks.

The baseline is a deterministic description of repository inputs used by a
planning or execution request. It never mutates the worktree, fetches objects,
or interprets a successful command as proof that the input is complete.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
from typing import Any, Mapping, Sequence

from .core import ProtocolError, ResourceRef, sha256_bytes, stable_snapshot_bytes, validate_business_path


class BaselineError(ProtocolError):
    """Stable error raised when a baseline cannot be captured safely."""


@dataclass(frozen=True)
class BaselineSnapshot:
    """Immutable semantic repository snapshot.

    ``observed_at`` is diagnostic metadata and is excluded from the semantic
    digest. Repeated captures with unchanged inputs therefore compare equal.
    """

    repository_root: str
    git_dir: str
    object_format: str
    head: str | None
    status: tuple[Mapping[str, Any], ...]
    tracked: tuple[Mapping[str, Any], ...]
    untracked: tuple[Mapping[str, Any], ...]
    submodules: tuple[str, ...]
    toolchain: Mapping[str, Any]
    complete: bool = True
    issues: tuple[str, ...] = ()
    observed_at: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise BaselineError("E_BASELINE_SCHEMA")
        for field in ("repository_root", "git_dir", "object_format"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value or "\x00" in value:
                raise BaselineError("E_BASELINE_SHAPE")
        if self.object_format not in {"sha1", "sha256", "unknown"}:
            raise BaselineError("E_BASELINE_SHAPE")
        if self.head is not None and (not isinstance(self.head, str) or not self.head or "\x00" in self.head):
            raise BaselineError("E_BASELINE_SHAPE")
        if type(self.complete) is not bool or any(
            not isinstance(getattr(self, name), tuple)
            for name in ("status", "tracked", "untracked", "submodules", "issues")
        ):
            raise BaselineError("E_BASELINE_SHAPE")
        if not isinstance(self.toolchain, Mapping) or any(not isinstance(k, str) for k in self.toolchain):
            raise BaselineError("E_BASELINE_SHAPE")
        if not isinstance(self.observed_at, str):
            raise BaselineError("E_BASELINE_SHAPE")

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repository_root": self.repository_root,
            "git_dir": self.git_dir,
            "object_format": self.object_format,
            "head": self.head,
            "status": [dict(item) for item in self.status],
            "tracked": [dict(item) for item in self.tracked],
            "untracked": [dict(item) for item in self.untracked],
            "submodules": list(self.submodules),
            "toolchain": dict(self.toolchain),
            "complete": self.complete,
            "issues": list(self.issues),
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.semantic_dict()
        value["observed_at"] = self.observed_at
        return value

    def bytes(self) -> bytes:
        return stable_snapshot_bytes(self.semantic_dict())

    @property
    def digest(self) -> str:
        return sha256_bytes(self.bytes())

    @property
    def baseline_id(self) -> str:
        return "base-" + self.digest[:32]

    def resource_ref(self) -> ResourceRef:
        return ResourceRef("baseline", self.baseline_id, self.digest)

    def compare(self, other: "BaselineSnapshot") -> dict[str, Any]:
        if not isinstance(other, BaselineSnapshot):
            raise BaselineError("E_BASELINE_SHAPE")
        left, right = self.semantic_dict(), other.semantic_dict()
        changed = tuple(key for key in left if left[key] != right[key])
        return {"same": not changed, "changed": changed, "before": self.digest, "after": other.digest}


class BaselineCapture:
    """Capture a repository using fixed, read-only Git commands."""

    def __init__(self, *, timeout_seconds: float = 30.0, max_output_bytes: int = 8 * 1024 * 1024) -> None:
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise BaselineError("E_BASELINE_TIMEOUT")
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise BaselineError("E_BASELINE_LIMIT")
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = max_output_bytes

    def capture(self, repository_root: str | os.PathLike[str]) -> BaselineSnapshot:
        root = self._root(repository_root)
        issues: list[str] = []
        git_dir = self._git(root, ("rev-parse", "--path-format=absolute", "--git-dir"), issues)
        if git_dir is None:
            raise BaselineError("E_BASELINE_NOT_REPOSITORY")
        root_value = self._git(root, ("rev-parse", "--path-format=absolute", "--show-toplevel"), issues) or str(root)
        object_format = self._git(root, ("rev-parse", "--show-object-format=storage"), issues) or "unknown"
        if object_format not in {"sha1", "sha256"}:
            issues.append("object_format_unknown")
            object_format = "unknown"
        head = self._git(root, ("rev-parse", "--verify", "--quiet", "HEAD"), issues, allow_failure=True)
        if head is None:
            issues.append("head_unavailable")
        status_raw = self._git_bytes(root, ("status", "--porcelain=v1", "-z", "--untracked-files=all"), issues)
        status = self._parse_status(status_raw, issues)
        tracked_paths = self._parse_nul(self._git_bytes(root, ("ls-files", "-z"), issues), issues, "tracked_list")
        tracked = self._file_manifest(root, tracked_paths, issues)
        untracked_paths = [item["path"] for item in status if item["xy"].endswith("?") or item["xy"].startswith("?")]
        untracked = self._file_manifest(root, untracked_paths, issues)
        sub_raw = self._git_bytes(root, ("submodule", "status", "--recursive"), issues, allow_failure=True)
        submodules = tuple(sorted(line.decode("utf-8", "backslashreplace") for line in sub_raw.splitlines() if line.strip()))
        toolchain = {
            "python": platform.python_version(),
            "platform": platform.system().lower(),
            "git": self._tool_version(root),
            "executables": {name: shutil.which(name) or None for name in ("git", "python", "pi", "claude")},
        }
        return BaselineSnapshot(
            repository_root=root_value,
            git_dir=git_dir,
            object_format=object_format,
            head=head,
            status=tuple(status),
            tracked=tuple(tracked),
            untracked=tuple(untracked),
            submodules=submodules,
            toolchain=toolchain,
            complete=not issues,
            issues=tuple(sorted(set(issues))),
            observed_at=datetime.now(timezone.utc).isoformat(),
        )

    def verify_unchanged(self, baseline: BaselineSnapshot, repository_root: str | os.PathLike[str]) -> dict[str, Any]:
        if not isinstance(baseline, BaselineSnapshot):
            raise BaselineError("E_BASELINE_SHAPE")
        current = self.capture(repository_root)
        result = baseline.compare(current)
        result.update({"complete": current.complete, "issues": current.issues})
        return result

    @staticmethod
    def _root(value: str | os.PathLike[str]) -> Path:
        if not isinstance(value, (str, os.PathLike)):
            raise BaselineError("E_BASELINE_ROOT")
        try:
            path = Path(value).resolve(strict=True)
        except OSError as exc:
            raise BaselineError("E_BASELINE_ROOT") from exc
        if not path.is_dir():
            raise BaselineError("E_BASELINE_ROOT")
        return path

    @staticmethod
    def _env() -> dict[str, str]:
        env = dict(os.environ)
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            env.pop(key, None)
        env["LC_ALL"], env["LANG"] = "C", "C"
        return env

    def _run(self, root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[bytes] | None:
        try:
            return subprocess.run(("git", "-C", str(root), *args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self._env(), timeout=self.timeout_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _git(self, root: Path, args: Sequence[str], issues: list[str], *, allow_failure: bool = False) -> str | None:
        proc = self._run(root, args)
        if proc is None:
            issues.append("git_probe_failed")
            return None
        if len(proc.stdout) > self.max_output_bytes:
            issues.append("git_output_truncated")
            return None
        if proc.returncode != 0:
            if not allow_failure:
                issues.append("git_probe_nonzero")
            return None
        value = proc.stdout.decode("utf-8", "backslashreplace").strip()
        if not value or any(ord(c) < 32 and c not in "\t" for c in value):
            issues.append("git_output_malformed")
            return None
        return value

    def _git_bytes(self, root: Path, args: Sequence[str], issues: list[str], *, allow_failure: bool = False) -> bytes:
        proc = self._run(root, args)
        if proc is None:
            issues.append("git_probe_failed")
            return b""
        if len(proc.stdout) > self.max_output_bytes:
            issues.append("git_output_truncated")
            return b""
        if proc.returncode != 0 and not allow_failure:
            issues.append("git_probe_nonzero")
        return bytes(proc.stdout)

    @staticmethod
    def _parse_nul(raw: bytes, issues: list[str], reason: str) -> list[str]:
        if not raw:
            return []
        if not raw.endswith(b"\0"):
            issues.append(reason + "_truncated")
            return []
        return [item.decode("utf-8", "backslashreplace") for item in raw[:-1].split(b"\0") if item]

    def _parse_status(self, raw: bytes, issues: list[str]) -> list[dict[str, Any]]:
        if not raw:
            return []
        if not raw.endswith(b"\0"):
            issues.append("status_truncated")
            return []
        values: list[dict[str, Any]] = []
        items, index = raw[:-1].split(b"\0"), 0
        valid = set(b" M?DARCU!T")
        while index < len(items):
            record, index = items[index], index + 1
            if len(record) < 4 or record[2:3] != b" " or record[0] not in valid or record[1] not in valid:
                issues.append("status_malformed")
                continue
            path = record[3:].decode("utf-8", "backslashreplace")
            orig = None
            if chr(record[0]) in "RC" or chr(record[1]) in "RC":
                if index >= len(items):
                    issues.append("status_rename_truncated")
                    continue
                orig, index = items[index].decode("utf-8", "backslashreplace"), index + 1
            values.append({"xy": record[:2].decode("ascii"), "path": path, "orig_path": orig, "path_utf8": "\\ufffd" not in path and "\\ufffd" not in (orig or "")})
        return sorted(values, key=lambda item: (item["path"], item["xy"], item["orig_path"] or ""))

    @staticmethod
    def _file_manifest(root: Path, paths: Sequence[str], issues: list[str]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        root_real = root.resolve()
        for rel in sorted(set(paths)):
            try:
                validate_business_path(rel)
                target = root / Path(rel)
                if os.path.commonpath((str(root_real), str(target.parent.resolve(strict=True)))) != str(root_real):
                    raise OSError("path escapes repository")
                target.lstat()
                if target.is_symlink():
                    raw, kind = os.readlink(target).encode("utf-8", "backslashreplace"), "symlink"
                elif target.is_file():
                    raw, kind = target.read_bytes(), "file"
                else:
                    issues.append("input_not_regular:" + rel)
                    continue
                output.append({"path": rel, "kind": kind, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
            except (OSError, ValueError, ProtocolError):
                issues.append("input_unreadable:" + rel)
        return output

    def _tool_version(self, root: Path) -> str | None:
        proc = self._run(root, ("--version",))
        if proc is None or proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", "backslashreplace").strip() or None


def capture_baseline(repository_root: str | os.PathLike[str], **kwargs: Any) -> BaselineSnapshot:
    return BaselineCapture(**kwargs).capture(repository_root)


Baseline = BaselineSnapshot
BaselineRegistry = BaselineCapture


def compare_baselines(before: BaselineSnapshot, after: BaselineSnapshot) -> dict[str, Any]:
    """Compare two immutable captures without silently normalizing drift."""
    return before.compare(after)


__all__ = ["BaselineError", "BaselineSnapshot", "BaselineCapture", "Baseline", "BaselineRegistry",
           "capture_baseline", "compare_baselines"]
