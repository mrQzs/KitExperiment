from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from sentinelmesh.baseline import BaselineCapture, BaselineError, compare_baselines


class BaselineCaptureTests(unittest.TestCase):
    def make_repo(self) -> Path:
        directory = Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(directory)], check=True)
        subprocess.run(["git", "-C", str(directory), "config", "user.email", "tests@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(directory), "config", "user.name", "tests"], check=True)
        (directory / "README.md").write_bytes(b"hello\n")
        subprocess.run(["git", "-C", str(directory), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(directory), "commit", "-qm", "initial"], check=True)
        return directory

    def test_capture_is_deterministic_and_tracks_inputs(self):
        directory = self.make_repo()
        try:
            capture = BaselineCapture()
            first = capture.capture(directory)
            second = capture.capture(directory)
            self.assertTrue(first.complete, first.issues)
            self.assertEqual(first.digest, second.digest)
            self.assertEqual(first.observed_at != second.observed_at, True)
            self.assertEqual(compare_baselines(first, second)["same"], True)
            self.assertEqual(first.object_format, "sha1")
            self.assertEqual(first.resource_ref().sha256, first.digest)
            self.assertIn("README.md", {item["path"] for item in first.tracked})
        finally:
            import shutil
            shutil.rmtree(directory, ignore_errors=True)

    def test_untracked_and_content_drift_are_visible(self):
        directory = self.make_repo()
        try:
            capture = BaselineCapture()
            before = capture.capture(directory)
            (directory / "new.txt").write_text("new", encoding="utf-8")
            after = capture.capture(directory)
            self.assertFalse(compare_baselines(before, after)["same"])
            self.assertIn("status", compare_baselines(before, after)["changed"])
            self.assertIn("new.txt", {item["path"] for item in after.untracked})
            (directory / "README.md").write_bytes(b"changed\n")
            changed = capture.verify_unchanged(before, directory)
            self.assertFalse(changed["same"])
            self.assertIn("tracked", changed["changed"])
        finally:
            import shutil
            shutil.rmtree(directory, ignore_errors=True)

    def test_git_environment_redirect_is_ignored_and_non_repo_fails(self):
        directory = self.make_repo()
        try:
            old = os.environ.get("GIT_DIR")
            os.environ["GIT_DIR"] = str(directory / "missing")
            try:
                self.assertTrue(BaselineCapture().capture(directory).head)
            finally:
                if old is None:
                    os.environ.pop("GIT_DIR", None)
                else:
                    os.environ["GIT_DIR"] = old
            with tempfile.TemporaryDirectory() as empty:
                with self.assertRaises(BaselineError) as context:
                    BaselineCapture().capture(empty)
                self.assertEqual(context.exception.code, "E_BASELINE_NOT_REPOSITORY")
        finally:
            import shutil
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
