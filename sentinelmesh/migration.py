"""Fail-closed migration preview and compare-and-swap helper."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from typing import Any, Mapping
from uuid import uuid4

from .core import validate_business_path


class MigrationError(ValueError):
    pass


class MigrationStage(str, Enum):
    DISCOVERED = "DISCOVERED"
    PREFLIGHTED = "PREFLIGHTED"
    BACKUP_VERIFIED = "BACKUP_VERIFIED"
    STAGED = "STAGED"
    SWITCHED = "SWITCHED"
    VERIFIED = "VERIFIED"
    ACCEPTED = "ACCEPTED"
    HOLD = "HOLD"
    ROLLED_BACK = "ROLLED_BACK"


def digest(data: bytes | None) -> str | None:
    if data is None:
        return None
    if type(data) is not bytes:
        raise MigrationError("E_MIGRATION_BYTES")
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class FileChange:
    path: str
    before_sha256: str | None
    after_sha256: str | None
    before_exists: bool
    after_exists: bool

    def __post_init__(self) -> None:
        try:
            validate_business_path(self.path)
        except Exception as exc:
            raise MigrationError("E_MIGRATION_PATH") from exc
        for value in (self.before_exists, self.after_exists):
            if type(value) is not bool:
                raise MigrationError("E_MIGRATION_SHAPE")
        for value in (self.before_sha256, self.after_sha256):
            if value is not None and (not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)):
                raise MigrationError("E_MIGRATION_DIGEST")
        if self.before_exists != (self.before_sha256 is not None) or self.after_exists != (self.after_sha256 is not None):
            raise MigrationError("E_MIGRATION_SHAPE")


@dataclass(frozen=True)
class MigrationPlan:
    operation_id: str
    expected_generation: int
    changes: tuple[FileChange, ...]
    stage: MigrationStage = MigrationStage.DISCOVERED

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise MigrationError("E_MIGRATION_OPERATION")
        if type(self.expected_generation) is not int or self.expected_generation < 1:
            raise MigrationError("E_MIGRATION_GENERATION")
        if not isinstance(self.changes, tuple):
            try:
                object.__setattr__(self, "changes", tuple(self.changes))
            except TypeError as exc:
                raise MigrationError("E_MIGRATION_SHAPE") from exc
        if any(not isinstance(change, FileChange) for change in self.changes):
            raise MigrationError("E_MIGRATION_SHAPE")
        if not isinstance(self.stage, MigrationStage):
            try:
                object.__setattr__(self, "stage", MigrationStage(self.stage))
            except (TypeError, ValueError) as exc:
                raise MigrationError("E_MIGRATION_STAGE") from exc


class MigrationController:
    def __init__(self):
        self.generation = 1
        self.history: list[MigrationPlan] = []

    def preview(self, before: Mapping[str, bytes], after: Mapping[str, bytes]) -> MigrationPlan:
        before = self._files(before)
        after = self._files(after)
        paths = sorted(set(before) | set(after))
        changes = tuple(FileChange(p, digest(before.get(p)), digest(after.get(p)), p in before, p in after) for p in paths if before.get(p) != after.get(p))
        plan = MigrationPlan("mig-" + uuid4().hex, self.generation, changes, MigrationStage.PREFLIGHTED)
        self.history.append(plan)
        return plan

    def verify_backup(self, plan: MigrationPlan, backup: Mapping[str, bytes]) -> MigrationPlan:
        if not isinstance(plan, MigrationPlan) or plan.operation_id not in {x.operation_id for x in self.history}:
            raise MigrationError("E_MIGRATION_UNKNOWN")
        backup = self._files(backup)
        for change in plan.changes:
            if digest(backup.get(change.path)) != change.before_sha256:
                raise MigrationError("E_BACKUP_MISMATCH")
        updated = MigrationPlan(plan.operation_id, plan.expected_generation, plan.changes, MigrationStage.BACKUP_VERIFIED)
        self._replace(updated)
        return updated

    def apply(self, plan: MigrationPlan, current: Mapping[str, bytes], after: Mapping[str, bytes]) -> dict[str, bytes]:
        if not isinstance(plan, MigrationPlan) or plan.operation_id not in {x.operation_id for x in self.history}:
            raise MigrationError("E_MIGRATION_UNKNOWN")
        current = self._files(current)
        after = self._files(after)
        if plan.expected_generation != self.generation:
            raise MigrationError("E_MIGRATION_GENERATION")
        if plan.stage not in (MigrationStage.BACKUP_VERIFIED, MigrationStage.PREFLIGHTED):
            raise MigrationError("E_MIGRATION_STAGE")
        for change in plan.changes:
            if digest(current.get(change.path)) != change.before_sha256:
                self._replace(MigrationPlan(plan.operation_id, plan.expected_generation, plan.changes, MigrationStage.HOLD))
                raise MigrationError("E_USER_MODIFIED")
        result = dict(current)
        for change in plan.changes:
            if change.after_exists:
                if change.path not in after or digest(after[change.path]) != change.after_sha256:
                    raise MigrationError("E_AFTER_MISMATCH")
                result[change.path] = after[change.path]
            else:
                result.pop(change.path, None)
        self._replace(MigrationPlan(plan.operation_id, plan.expected_generation, plan.changes, MigrationStage.SWITCHED))
        return result

    def accept(self, plan: MigrationPlan) -> MigrationPlan:
        if not isinstance(plan, MigrationPlan) or plan.operation_id not in {x.operation_id for x in self.history}:
            raise MigrationError("E_MIGRATION_UNKNOWN")
        updated = MigrationPlan(plan.operation_id, plan.expected_generation, plan.changes, MigrationStage.ACCEPTED)
        self._replace(updated)
        return updated

    def _replace(self, plan: MigrationPlan) -> None:
        self.history = [x for x in self.history if x.operation_id != plan.operation_id] + [plan]

    @staticmethod
    def _files(value: Mapping[str, bytes]) -> dict[str, bytes]:
        if not isinstance(value, Mapping):
            raise MigrationError("E_MIGRATION_FILES")
        result: dict[str, bytes] = {}
        for path, content in value.items():
            try:
                validate_business_path(path)
            except Exception as exc:
                raise MigrationError("E_MIGRATION_PATH") from exc
            if type(content) is not bytes:
                raise MigrationError("E_MIGRATION_BYTES")
            result[path] = bytes(content)
        return result


__all__ = ["MigrationError", "MigrationStage", "FileChange", "MigrationPlan", "MigrationController"]
