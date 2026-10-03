import os
import subprocess

import pytest

from sentinelmesh.baseline import BaselineError, capture_baseline, compare_baselines
from sentinelmesh.workflows import builtin_workflow


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_baseline_captures_head_and_detects_worktree_drift(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "tracked.txt").write_text("one", encoding="utf-8")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-qm", "initial")
    before = capture_baseline(tmp_path)
    assert before.head and before.complete
    (tmp_path / "tracked.txt").write_text("two", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new", encoding="utf-8")
    after = capture_baseline(tmp_path)
    comparison = compare_baselines(before, after)
    assert not comparison["same"]
    assert "status" in comparison["changed"]
    assert "untracked" in comparison["changed"]


def test_baseline_rejects_non_repository(tmp_path):
    with pytest.raises(BaselineError, match="E_BASELINE_NOT_REPOSITORY"):
        capture_baseline(tmp_path)


def test_all_builtin_workflows_have_required_stage_order():
    for name in ("bugfix", "documentation", "regression"):
        definition = builtin_workflow(name)
        assert definition.validate()
        for node in definition.nodes:
            assert [stage["kind"] for stage in node["stages"]] == ["execute", "freeze", "check", "verdict"]
