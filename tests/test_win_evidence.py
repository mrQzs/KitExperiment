"""Executor tests for the Windows evidence half of the supervisor.

`.agent/design/windows-supervisor.md` (section 3.4, W8) is what these cover: the
exclusive creation and pinning of the two evidence files, the rollback that closes
every descriptor before it removes anything, the stream facts read through the
pins, ``streams_match``, and the by-hand non-POSIX branches of the two record
modules.  The planner oracle already covers the rule boundaries; what is added
here is what can only be seen by really doing it on the platform: a file this
process holds open cannot be removed, a child that writes CR LF and 0x1A bytes
through an inherited write descriptor is read back byte for byte, a name that is
already taken starts nothing, and the two records on disk read back exactly what
was written.

No Job Object is involved and no ``supervise`` is defined: the child is a plain
``python -c`` started with the two write descriptors, and every file is only
created, never executed.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sentinelmesh.posix_supervisor as posix_supervisor
import sentinelmesh.supervised_record
from sentinelmesh.core import canonical_json
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.supervised_operation import evidence_digest
from sentinelmesh.supervised_record import HANDOFF_NAME, OBSERVATION_NAME, recorded_streams
from sentinelmesh.windows_supervisor import (
    _abandon_evidence,
    _fact_of,
    _open_evidence_files,
    _streams_of,
    streams_match,
)

PY = os.path.realpath(sys.executable)
NL = chr(10)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
# CR LF, a bare LF and a 0x1A byte: a write descriptor in text mode would turn the
# LF into CR LF, and a pin in text mode would fold the CR LF back and stop at the
# 0x1A, so the digest would describe a stream nobody wrote.
OUT_TAIL = bytes([13, 10, 10, 26, 13, 10])
OUT_BYTES = 2 * 4096 + 3 + len(OUT_TAIL)
ERR_BYTES = 5
WRITE_BOTH = ("import sys" + NL
              + "sys.stdout.buffer.write(b'o' * %d + %r)" % (2 * 4096 + 3, OUT_TAIL) + NL
              + "sys.stdout.flush()" + NL
              + "sys.stderr.buffer.write(b'e' * %d)" % ERR_BYTES + NL
              + "sys.stderr.flush()" + NL)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class EvidenceCase(unittest.TestCase):
    """A scratch directory, the lowest-free-descriptor probe and small helpers."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="win-evidence-exec-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.probe = self.work / "probe.bin"
        self.probe.write_bytes(b"probe")
        self.dirs = 0
        self.open_fds: list = []
        self.addCleanup(self.close_all)

    # helpers ---------------------------------------------------------------

    def evidence_dir(self) -> Path:
        self.dirs += 1
        path = self.work / ("evidence-%d" % self.dirs)
        path.mkdir()
        return path

    def close_all(self):
        for fd in self.open_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.open_fds = []

    def next_fd(self) -> int:
        """The lowest free descriptor number: unchanged by a call that leaks nothing."""
        fd = os.open(self.probe, os.O_RDONLY)
        os.close(fd)
        return fd

    def opened(self, evidence: Path):
        """Create and pin both evidence files; every descriptor is closed when the case ends."""
        result = _open_evidence_files(str(evidence))
        self.open_fds.extend(result[2:6])
        return result

    def code_of(self, func, *args, **kwargs):
        with self.assertRaises(RuntimeErrorCode) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def symlink_or_skip(self, target, link):
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symbolic links cannot be created here: %s" % exc)

    def snapshot(self, directory: Path) -> dict:
        """What each entry of ``directory`` is: its kind and, for a plain file, its bytes."""
        result = {}
        for name in sorted(os.listdir(directory)):
            path = directory / name
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                result[name] = ("symlink", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                result[name] = ("directory", None)
            else:
                result[name] = ("file", path.read_bytes())
        return result

    def run_child(self, code: str, stdout_fd: int, stderr_fd: int) -> int:
        completed = subprocess.run([PY, "-c", code], stdin=subprocess.DEVNULL, stdout=stdout_fd,
                                   stderr=stderr_fd, close_fds=True, timeout=120)
        return completed.returncode


@unittest.skipIf(os.name != "nt", "the measured refusal to remove an open file is native Windows")
class NativeWindowsEvidenceCase(EvidenceCase):
    def test_the_rollback_can_only_remove_the_files_after_it_closed_every_descriptor(self):
        evidence = self.evidence_dir()
        stdout_path, stderr_path, stdout_fd, stderr_fd, stdout_pin, stderr_pin, stdout_id, stderr_id = (
            self.opened(evidence))
        os.write(stdout_fd, b"written before the rollback")
        # Both a write descriptor and a read-only pin hold the file open, and
        # Windows refuses to unlink or replace a file this process holds open.
        with self.assertRaises(PermissionError):
            os.unlink(stdout_path)
        replacement = self.work / "replacement.log"
        replacement.write_bytes(b"replacement")
        with self.assertRaises(PermissionError):
            os.replace(replacement, stdout_path)
        self.assertEqual((evidence / "stdout.log").read_bytes(), b"written before the rollback")
        # The rollback closes first and removes by identity, so both files are gone
        # and not one descriptor of this call is left open.
        _abandon_evidence(((stdout_path, stdout_id), (stderr_path, stderr_id)),
                          (stderr_pin, stderr_fd, stdout_pin, stdout_fd))
        self.assertEqual(os.listdir(evidence), [])
        for descriptor in (stdout_fd, stderr_fd, stdout_pin, stderr_pin):
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.open_fds = []
        # A created file whose identity is unknown is closed but never removed.
        kept = evidence / "kept.log"
        descriptor = os.open(kept, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        _abandon_evidence(((str(kept), None),), (descriptor, None))
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        self.assertTrue(kept.exists())


class StreamEvidenceCase(EvidenceCase):
    def test_the_facts_of_a_real_child_describe_what_both_streams_hold(self):
        evidence = self.evidence_dir()
        stdout_path, stderr_path, stdout_fd, stderr_fd, stdout_pin, stderr_pin, _, _ = self.opened(evidence)
        self.assertEqual(self.run_child(WRITE_BOTH, stdout_fd, stderr_fd), 0)
        os.close(stdout_fd)
        os.close(stderr_fd)
        streams = _streams_of(stdout_pin, stderr_pin)
        self.assertEqual(sorted(streams), ["stderr", "stdout"])
        for name, expected_bytes in (("stdout", OUT_BYTES), ("stderr", ERR_BYTES)):
            with self.subTest(stream=name):
                path = evidence / (name + ".log")
                info = os.lstat(path)
                raw = path.read_bytes()
                self.assertEqual(streams[name], {"device": info.st_dev, "inode": info.st_ino,
                                                 "bytes": len(raw), "sha256": sha(raw)})
                self.assertEqual(streams[name]["bytes"], expected_bytes)
        self.assertEqual(streams["stdout"]["sha256"], sha(b"o" * (2 * 4096 + 3) + OUT_TAIL))
        self.assertIs(streams_match(str(evidence), streams), True)
        self.close_all()
        # A byte-identical copy at the same path is another file: the facts no
        # longer describe what is on disk, so nothing can be reviewed from it.
        copy = self.work / "identical.log"
        copy.write_bytes((evidence / "stdout.log").read_bytes())
        os.replace(copy, evidence / "stdout.log")
        self.assertIs(streams_match(str(evidence), streams), False)

    def test_a_taken_stream_name_refuses_the_start_without_touching_anything(self):
        # Legal baseline: an empty directory is created and pinned, both files empty.
        clean = self.evidence_dir()
        self.assertEqual(len(self.opened(clean)), 8)
        self.assertEqual(self.snapshot(clean), {"stdout.log": ("file", b""), "stderr.log": ("file", b"")})
        self.close_all()
        # One change: ``stderr.log`` is a plain file of an earlier run.
        evidence = self.evidence_dir()
        taken = evidence / "stderr.log"
        taken.write_bytes(b"evidence of an earlier run")
        before_entries = self.snapshot(evidence)
        before = self.next_fd()
        self.assertEqual(self.code_of(_open_evidence_files, str(evidence)), "E_RUNTIME_EVIDENCE")
        self.assertEqual(self.snapshot(evidence), before_entries)
        self.assertFalse((evidence / "stdout.log").exists(), "the file created before the refusal survived")
        self.assertEqual(taken.read_bytes(), b"evidence of an earlier run")
        self.assertEqual(self.next_fd(), before, "a descriptor leaked")

    def test_a_fact_describes_the_whole_pinned_stream_whatever_the_read_position(self):
        evidence = self.evidence_dir()
        _, _, stdout_fd, _, stdout_pin, stderr_pin, _, _ = self.opened(evidence)
        raw = b"abcdef"
        os.write(stdout_fd, raw)
        # The pin has already been read from: the facts are still the file's.
        self.assertEqual(os.read(stdout_pin, 3), b"abc")
        first = _fact_of(stdout_pin)
        self.assertEqual({key: first[key] for key in ("bytes", "sha256")},
                         {"bytes": len(raw), "sha256": sha(raw)})
        self.assertEqual(_fact_of(stdout_pin), first, "a second fact differs from the first")
        empty = _fact_of(stderr_pin)
        self.assertEqual({key: empty[key] for key in ("bytes", "sha256")}, {"bytes": 0, "sha256": sha(b"")})
        with mock.patch.object(posix_supervisor, "MAX_STREAM_DIGEST_BYTES", len(raw) - 1):
            self.assertIsNone(_fact_of(stdout_pin), "a stream beyond the digest limit has a fact")


class RecordCase(unittest.TestCase):
    """Hand-written records: the record readers need no supervisor to be exercised."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="win-evidence-records-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.spec = InvocationSpec("req-9", "run-9", (PY, "-c", "pass"), str(self.cwd),
                                   environment_digest(BASE_ENV), 5)
        self.streams = {"stdout": {"device": 7, "inode": 11, "bytes": 6, "sha256": sha(b"stdout")},
                        "stderr": {"device": 7, "inode": 13, "bytes": 0, "sha256": sha(b"")}}
        self.handoff = canonical_json({
            "schema": "sentinelmesh-handoff-v1",
            "request_id": "req-9",
            "run_id": "run-9",
            "environment_sha256": self.spec.environment_sha256,
            "spec_sha256": self.spec.digest(),
        })
        self.observation = canonical_json({
            "schema": "sentinelmesh-observation-v2",
            "spec_sha256": self.spec.digest(),
            "observation": {
                "run_id": "run-9",
                "environment_sha256": self.spec.environment_sha256,
                "state": "STOPPED",
                "process_identity": {"pid": 4321, "boot_id": "boot-1", "executable_sha256": sha(b"exe"),
                                     "worktree_id": "wt-1"},
                "cleanup_confirmed": True,
            },
            "streams": self.streams,
        })
        self.content = json.loads(self.handoff)

    def evidence_dir(self) -> Path:
        self.dirs = getattr(self, "dirs", 0) + 1
        path = self.work / ("evidence-%d" % self.dirs)
        path.mkdir()
        return path

    def recorded(self, observation: bool = True) -> Path:
        evidence = self.evidence_dir()
        (evidence / HANDOFF_NAME).write_bytes(self.handoff)
        if observation:
            (evidence / OBSERVATION_NAME).write_bytes(self.observation)
        return evidence

    def snapshot(self, directory: Path) -> dict:
        result = {}
        for name in sorted(os.listdir(directory)):
            path = directory / name
            info = os.lstat(path)
            result[name] = ("directory", None) if stat.S_ISDIR(info.st_mode) else ("file", path.read_bytes())
        return result

    def symlink_or_skip(self, target, link):
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symbolic links cannot be created here: %s" % exc)

    def code_of(self, func, *args, **kwargs):
        with self.assertRaises(RuntimeErrorCode) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code


class RecordReadCase(RecordCase):
    def test_the_recorded_streams_read_back_and_are_a_copy_every_time(self):
        evidence = self.recorded()
        first = recorded_streams(self.spec, str(evidence))
        self.assertEqual(first, self.streams)
        # What a caller does with the returned mapping cannot reach a later read.
        first["stdout"]["bytes"] = 99
        first["stderr"] = None
        self.assertEqual(recorded_streams(self.spec, str(evidence)), self.streams)
        self.assertEqual(self.snapshot(evidence)[HANDOFF_NAME], ("file", self.handoff))

    def test_an_observation_that_is_not_the_plain_file_it_was_is_refused(self):
        def a_symlink_to_the_same_bytes(path: Path) -> None:
            elsewhere = self.work / ("elsewhere-" + path.name)
            elsewhere.write_bytes(self.observation)
            path.unlink()
            self.symlink_or_skip(elsewhere, path)

        def a_directory(path: Path) -> None:
            path.unlink()
            path.mkdir()

        for label, change in (("a symlink to the same bytes", a_symlink_to_the_same_bytes),
                              ("a directory", a_directory)):
            with self.subTest(entry=label):
                evidence = self.recorded()
                self.assertEqual(recorded_streams(self.spec, str(evidence)), self.streams, "legal baseline")
                change(evidence / OBSERVATION_NAME)
                self.assertEqual(self.code_of(recorded_streams, self.spec, str(evidence)), "E_RUNTIME_EVIDENCE")
                self.assertEqual(self.code_of(recorded_streams, self.spec, str(evidence)), "E_RUNTIME_EVIDENCE")


class RecordWriteCase(RecordCase):
    def test_a_record_is_published_from_a_fresh_partial_or_not_at_all(self):
        # Legal baseline: a fresh directory holds exactly the canonical bytes.
        fresh = self.evidence_dir()
        sentinelmesh.supervised_record._write_record(str(fresh), HANDOFF_NAME, self.content)
        self.assertEqual(self.snapshot(fresh), {HANDOFF_NAME: ("file", canonical_json(self.content))})
        # One change: a plain file already sits where the partial would be created.
        evidence = self.evidence_dir()
        partial = evidence / (".handoff.json.partial")
        partial.write_bytes(b"a leftover partial of somebody else")
        before = self.snapshot(evidence)
        self.assertEqual(
            self.code_of(sentinelmesh.supervised_record._write_record, str(evidence), HANDOFF_NAME, self.content),
            "E_RUNTIME_EVIDENCE")
        self.assertEqual(self.snapshot(evidence), before)
        self.assertEqual(partial.read_bytes(), b"a leftover partial of somebody else")
        self.assertFalse((evidence / HANDOFF_NAME).exists(), "the record was published anyway")

    def test_the_evidence_digest_names_what_each_record_is(self):
        empty = self.evidence_dir()
        handoff_only = self.recorded(observation=False)
        handoff_as_a_directory = self.evidence_dir()
        (handoff_as_a_directory / HANDOFF_NAME).write_bytes(self.handoff)
        (handoff_as_a_directory / HANDOFF_NAME).unlink()
        (handoff_as_a_directory / HANDOFF_NAME).mkdir()
        values = [evidence_digest(str(path)) for path in (empty, handoff_only, handoff_as_a_directory)]
        self.assertEqual(len(set(values)), 3, "three different directory states gave the same digest")
        payload = json.dumps({"handoff": "unreadable", "observation": None},
                             sort_keys=True, separators=(",", ":"))
        self.assertEqual(values[2], hashlib.sha256(payload.encode("utf-8")).hexdigest())
        self.assertEqual(values[0], hashlib.sha256(json.dumps({"handoff": None, "observation": None},
                                                              sort_keys=True, separators=(",", ":"))
                                                   .encode("utf-8")).hexdigest())


if __name__ == "__main__":
    unittest.main()
