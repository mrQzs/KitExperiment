"""Planner oracle for win-evidence (`.agent/design/windows-supervisor.md`, section 3.4).

The second of the four Windows supervisor contracts: the evidence half of
``sentinelmesh.windows_supervisor`` (exclusive creation, pins, rollback by
identity with every descriptor closed first, stream facts read through the pins,
``streams_match``) and the non-POSIX branches of the three record reads and the
partial-file creation in ``supervised_record`` / ``supervised_operation``.

Windows has no ``O_NOFOLLOW``, no ``O_NONBLOCK`` and no ``os.pread``; it creates
*through* a dangling symbolic link on an exclusive open, and it refuses to unlink,
rename or replace a file this process holds open.  Every rule here is therefore
kept by hand (``lstat`` first, reparse points refused, ``O_BINARY``, identity of
what was opened proved equal to what was seen), and the helpers are plain
``os`` calls, so these cases run on every platform; only the cases that stand
in for ``O_NOFOLLOW`` on the two record modules are limited to a platform
without the flag.  No Job Object is involved: the child that writes the streams
is a plain ``python -c`` started with the write descriptors.
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
import types
import unittest
from pathlib import Path
from unittest import mock

import sentinelmesh.posix_supervisor as posix_supervisor
import sentinelmesh.supervised_operation as supervised_operation
import sentinelmesh.supervised_record as supervised_record
import sentinelmesh.windows_supervisor as windows_supervisor
from sentinelmesh.core import canonical_json
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode
from sentinelmesh.supervised_operation import evidence_digest
from sentinelmesh.supervised_record import HANDOFF_NAME, OBSERVATION_NAME, recorded_streams, replay_decision
from sentinelmesh.windows_supervisor import (
    _abandon_evidence,
    _fact_of,
    _open_evidence_files,
    _remove_own_evidence,
    _streams_of,
    streams_match,
)

PY = os.path.realpath(sys.executable)
NL = chr(10)
O_BIN = getattr(os, "O_BINARY", 0)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
FACT_KEYS = {"device", "inode", "bytes", "sha256"}
# The stdout bytes end in CR LF and a 0x1A byte: read in text mode (no
# ``O_BINARY``) the C runtime would fold the CR LF and stop at the 0x1A.
OUT_TAIL = bytes([13, 10, 26]) + b"tail"
OUT_BYTES = 2 * 65536 + 7 + len(OUT_TAIL)
ERR_BYTES = 11
WRITE_BOTH = ("import sys" + NL
              + "sys.stdout.buffer.write(b'o' * %d + %r)" % (2 * 65536 + 7, OUT_TAIL) + NL
              + "sys.stderr.buffer.write(b'e' * %d)" % ERR_BYTES + NL)
IMPORT_CHECK = ("import sys, sentinelmesh.windows_supervisor, sentinelmesh.supervised_record, "
                "sentinelmesh.supervised_operation" + NL
                + "assert 'ctypes' not in sys.modules, 'ctypes'" + NL
                + "print('IMPORT_SAFE_OK')" + NL)


class DictSubclass(dict):
    pass


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def facts_of(path: Path) -> dict:
    """The facts of the file at ``path``, computed independently of the module."""
    info = os.lstat(path)
    data = path.read_bytes()
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": len(data), "sha256": sha(data)}


def stale_for(path, seen):
    """An ``lstat`` that answers ``seen`` for ``path`` and the truth for every other path."""
    real_lstat = os.lstat

    def lstat(target, *args, **kwargs):
        if os.fspath(target) == os.fspath(path):
            return seen
        return real_lstat(target, *args, **kwargs)
    return lstat


def with_reparse_flag(info: os.stat_result) -> types.SimpleNamespace:
    """A stat result that looks like ``info`` with the reparse-point attribute set."""
    return types.SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino,
                                 st_size=info.st_size,
                                 st_file_attributes=getattr(info, "st_file_attributes", 0)
                                 | stat.FILE_ATTRIBUTE_REPARSE_POINT)


class EvidenceCase(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="win-evidence-")).resolve()
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
        """The lowest free descriptor number: equal before and after a call that leaks nothing."""
        fd = os.open(self.probe, os.O_RDONLY | O_BIN)
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

    def symlink_or_skip(self, target, link, **kwargs):
        try:
            os.symlink(target, link, **kwargs)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("symbolic links cannot be created here: %s" % exc)

    def link_or_skip(self, target, link):
        try:
            os.link(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("hard links cannot be created here: %s" % exc)

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

    def written_streams(self, evidence: Path):
        """Both files written by a real child; returns ``(paths, streams)`` with everything closed."""
        stdout_path, stderr_path, stdout_fd, stderr_fd, stdout_pin, stderr_pin, _, _ = self.opened(evidence)
        self.assertEqual(self.run_child(WRITE_BOTH, stdout_fd, stderr_fd), 0)
        os.close(stdout_fd)
        os.close(stderr_fd)
        streams = _streams_of(stdout_pin, stderr_pin)
        self.close_all()
        return (Path(stdout_path), Path(stderr_path)), streams


class EvidenceFilesOracle(EvidenceCase):
    """Exclusive creation, pins and the rollback of ``_open_evidence_files``."""

    def test_both_files_are_created_pinned_and_identified(self):
        evidence = self.evidence_dir()
        before = self.next_fd()
        result = self.opened(evidence)
        stdout_path, stderr_path, stdout_fd, stderr_fd, stdout_pin, stderr_pin, stdout_id, stderr_id = result
        self.assertEqual(sorted(os.listdir(evidence)), ["stderr.log", "stdout.log"])
        self.assertEqual((stdout_path, stderr_path),
                         (os.path.join(str(evidence), "stdout.log"), os.path.join(str(evidence), "stderr.log")))
        for path, identity, write_fd, pin in ((stdout_path, stdout_id, stdout_fd, stdout_pin),
                                              (stderr_path, stderr_id, stderr_fd, stderr_pin)):
            with self.subTest(path=os.path.basename(path)):
                seen = os.lstat(path)
                self.assertEqual(identity, (seen.st_dev, seen.st_ino))
                self.assertEqual(seen.st_size, 0)
                self.assertTrue(stat.S_ISREG(seen.st_mode))
                for descriptor in (write_fd, pin):
                    info = os.fstat(descriptor)
                    self.assertEqual((info.st_dev, info.st_ino), identity)
                # The write descriptor writes and cannot read; the pin reads and
                # cannot write, and it sees what the write descriptor wrote.
                self.assertEqual(os.write(write_fd, b"abc"), 3)
                with self.assertRaises(OSError):
                    os.read(write_fd, 1)
                with self.assertRaises(OSError):
                    os.write(pin, b"x")
                self.assertEqual(os.read(pin, 10), b"abc")
        self.close_all()
        self.assertEqual(self.next_fd(), before, "a descriptor leaked")

    def test_a_name_that_is_taken_starts_nothing_and_leaks_nothing(self):
        def plain(path):
            path.write_bytes(b"old evidence")

        def directory(path):
            path.mkdir()

        def dangling_link(path):
            self.symlink_or_skip(path.with_name("nowhere-" + path.name), path)

        for taken in ("stdout.log", "stderr.log"):
            for label, plant in (("a file", plain), ("a directory", directory), ("a dangling link", dangling_link)):
                with self.subTest(taken=taken, entry=label):
                    evidence = self.evidence_dir()
                    plant(evidence / taken)
                    before_entries = self.snapshot(evidence)
                    before = self.next_fd()
                    self.assertEqual(self.code_of(_open_evidence_files, str(evidence)), "E_RUNTIME_EVIDENCE")
                    # The planted entry is untouched, the other file this call may
                    # have created is gone, and no file was created *through* the
                    # dangling link (Windows would, on a bare exclusive open).
                    self.assertEqual(self.snapshot(evidence), before_entries)
                    self.assertFalse((evidence / ("nowhere-" + taken)).exists())
                    self.assertEqual(self.next_fd(), before, "a descriptor leaked")

    def test_a_reparse_point_seen_at_the_pin_is_refused_and_rolled_back(self):
        real_lstat = os.lstat
        evidence = self.evidence_dir()
        target = os.path.join(str(evidence), "stderr.log")
        faked = []

        def lstat_with_a_link_once(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if os.fspath(path) == target and not faked:
                faked.append(path)
                return with_reparse_flag(info)
            return info

        before = self.next_fd()
        with mock.patch.object(windows_supervisor.os, "lstat", side_effect=lstat_with_a_link_once):
            self.assertEqual(self.code_of(_open_evidence_files, str(evidence)), "E_RUNTIME_EVIDENCE")
        self.assertEqual(faked, [target], "the pin's lstat of stderr.log was not consulted")
        self.assertEqual(os.listdir(evidence), [], "the rollback left a created file behind")
        self.assertEqual(self.next_fd(), before, "a descriptor leaked")

    def test_a_pin_that_is_not_the_created_file_is_refused_and_rolled_back(self):
        real_open, real_lstat = os.open, os.lstat
        other = self.work / "other.bin"
        other.write_bytes(b"another file")

        def open_another_file_for_the_pin(path, flags, *args, **kwargs):
            if os.path.basename(os.fspath(path)) == "stderr.log" and not flags & os.O_CREAT:
                return real_open(other, flags, *args, **kwargs)
            return real_open(path, flags, *args, **kwargs)

        def lstat_another_file_for_the_pin():
            # Only the first lstat that finds stderr.log answers with the other
            # file: the lstat before the creation must still find nothing (so
            # the creation is not what refuses), and the rollback's lstat must
            # see the truth (so the created file is removed).
            served = []

            def lstat(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                if os.path.basename(os.fspath(path)) == "stderr.log" and not served:
                    served.append(path)
                    return real_lstat(other)
                return info
            return lstat

        cases = {
            # The pin opened a file that is neither the created one nor the seen one.
            "the open yields another file": lambda: {"open": open_another_file_for_the_pin},
            # The pin is the created file, but not the one the lstat saw.
            "the lstat saw another file": lambda: {"lstat": lstat_another_file_for_the_pin()},
            # The lstat and the pin agree on a file that the write descriptor did not create.
            "the lstat and the open agree on another file": lambda: {"open": open_another_file_for_the_pin,
                                                                    "lstat": lstat_another_file_for_the_pin()},
        }
        for label, make_fakes in cases.items():
            with self.subTest(case=label):
                evidence = self.evidence_dir()
                before = self.next_fd()
                with mock.patch.multiple(windows_supervisor.os, **{name: mock.Mock(side_effect=fake)
                                                                   for name, fake in make_fakes().items()}):
                    self.assertEqual(self.code_of(_open_evidence_files, str(evidence)), "E_RUNTIME_EVIDENCE")
                self.assertEqual(os.listdir(evidence), [], "the rollback left a created file behind")
                self.assertEqual(other.read_bytes(), b"another file")
                self.assertEqual(self.next_fd(), before, "a descriptor leaked")

    def test_the_rollback_closes_every_descriptor_before_it_removes(self):
        evidence = self.evidence_dir()
        stdout_path, stderr_path, stdout_fd, stderr_fd, stdout_pin, stderr_pin, stdout_id, stderr_id = (
            self.opened(evidence))
        os.write(stdout_fd, b"written before the rollback")
        _abandon_evidence(((stdout_path, stdout_id), (stderr_path, stderr_id)),
                          (stderr_pin, stderr_fd, stdout_pin, stdout_fd))
        # On Windows an open file cannot be unlinked, so both files gone proves
        # the descriptors were closed first; they are closed on every platform.
        self.assertEqual(os.listdir(evidence), [])
        for descriptor in (stdout_fd, stderr_fd, stdout_pin, stderr_pin):
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.open_fds = []
        # A created file whose identity is unknown is closed but never removed.
        kept = evidence / "stdout.log"
        descriptor = os.open(kept, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BIN, 0o600)
        _abandon_evidence(((str(kept), None),), (descriptor, None))
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        self.assertTrue(kept.exists())


class RemoveOwnEvidenceOracle(EvidenceCase):
    def test_only_the_created_file_still_at_its_path_is_removed(self):
        evidence = self.evidence_dir()
        path = evidence / "stdout.log"

        def created():
            path.write_bytes(b"created")
            info = os.lstat(path)
            return (info.st_dev, info.st_ino)

        with self.subTest(case="the created file"):
            identity = created()
            _remove_own_evidence(str(path), identity)
            self.assertFalse(path.exists())
        with self.subTest(case="another file at the path"):
            identity = created()
            moved = evidence / "moved.log"
            os.rename(path, moved)
            path.write_bytes(b"substitute")
            _remove_own_evidence(str(path), identity)
            self.assertEqual(path.read_bytes(), b"substitute")
            self.assertEqual(moved.read_bytes(), b"created")
            path.unlink()
            moved.unlink()
        with self.subTest(case="nothing at the path"):
            identity = created()
            path.unlink()
            _remove_own_evidence(str(path), identity)
        with self.subTest(case="no identity"):
            created()
            _remove_own_evidence(str(path), None)
            self.assertEqual(path.read_bytes(), b"created")
            path.unlink()
        with self.subTest(case="a directory at the path"):
            identity = created()
            path.unlink()
            path.mkdir()
            _remove_own_evidence(str(path), identity)
            self.assertTrue(path.is_dir())
            path.rmdir()
        with self.subTest(case="a reparse point at the path"):
            identity = created()
            real = os.lstat(path)
            with mock.patch.object(windows_supervisor.os, "lstat", return_value=with_reparse_flag(real)):
                _remove_own_evidence(str(path), identity)
            self.assertEqual(path.read_bytes(), b"created")
            path.unlink()
        with self.subTest(case="a link to the created file at the path"):
            identity = created()
            original = evidence / "original.log"
            os.rename(path, original)
            self.symlink_or_skip(original, path)
            _remove_own_evidence(str(path), identity)
            self.assertTrue(path.is_symlink())
            self.assertEqual(original.read_bytes(), b"created")


class StreamFactsOracle(EvidenceCase):
    """What the pins report once the child is gone, and ``streams_match`` afterwards."""

    def test_the_facts_describe_what_a_real_child_wrote_to_both_streams(self):
        evidence = self.evidence_dir()
        (stdout_path, stderr_path), streams = self.written_streams(evidence)
        self.assertEqual(frozenset(streams), {"stdout", "stderr"})
        self.assertEqual(streams["stdout"], facts_of(stdout_path))
        self.assertEqual(streams["stderr"], facts_of(stderr_path))
        self.assertEqual((streams["stdout"]["bytes"], streams["stderr"]["bytes"]), (OUT_BYTES, ERR_BYTES))
        self.assertEqual(streams["stdout"]["sha256"], sha(b"o" * (2 * 65536 + 7) + OUT_TAIL))
        for fact in streams.values():
            self.assertEqual(frozenset(fact), FACT_KEYS)
            self.assertTrue(all(type(fact[key]) is int for key in ("device", "inode", "bytes")))
            self.assertIs(type(fact["sha256"]), str)
        self.assertIs(streams_match(str(evidence), streams), True)

    def test_a_fact_is_read_from_offset_zero_whatever_the_pin_position(self):
        evidence = self.evidence_dir()
        _, _, stdout_fd, _, stdout_pin, stderr_pin, _, _ = self.opened(evidence)
        # A LF among the bytes: a write descriptor in text mode would turn it
        # into CR LF, and a pin in text mode would fold a CR LF back.
        raw = b"abc" + bytes([10, 13, 10]) + b"def"
        os.write(stdout_fd, raw)
        self.assertEqual(os.read(stdout_pin, 3), b"abc")
        expected = {"bytes": len(raw), "sha256": sha(raw)}
        first = _fact_of(stdout_pin)
        self.assertEqual({key: first[key] for key in expected}, expected)
        self.assertEqual(_fact_of(stdout_pin), first, "a second fact differs from the first")
        self.assertEqual({key: _fact_of(stderr_pin)[key] for key in ("bytes", "sha256")},
                         {"bytes": 0, "sha256": sha(b"")})

    def test_a_stream_beyond_the_digest_limit_has_no_fact_and_is_not_read(self):
        evidence = self.evidence_dir()
        _, _, stdout_fd, _, stdout_pin, stderr_pin, _, _ = self.opened(evidence)
        os.write(stdout_fd, b"sixbyt")
        with mock.patch.object(posix_supervisor, "MAX_STREAM_DIGEST_BYTES", 6):
            self.assertEqual(_fact_of(stdout_pin)["bytes"], 6, "legal baseline at the limit")
        with mock.patch.object(posix_supervisor, "MAX_STREAM_DIGEST_BYTES", 5):
            with mock.patch.object(windows_supervisor.os, "read", side_effect=AssertionError("a byte was read")):
                self.assertIsNone(_fact_of(stdout_pin))
            self.assertEqual(_streams_of(stdout_pin, stderr_pin)["stderr"]["bytes"], 0)
        # A read that fails is a missing fact, never an exception; so is a
        # descriptor that is no longer open.
        with mock.patch.object(windows_supervisor.os, "read", side_effect=OSError(5, "read failed")):
            self.assertIsNone(_fact_of(stdout_pin))
        self.close_all()
        self.assertIsNone(_fact_of(stdout_pin))

    def test_streams_match_refuses_every_change_to_the_files(self):
        def appended(path):
            with open(path, "ab") as handle:
                handle.write(b"x")

        def truncated(path):
            os.truncate(path, path.stat().st_size - 1)

        def rewritten_in_place(path):
            with open(path, "r+b") as handle:
                handle.write(b"O")

        def replaced_by_an_identical_copy(path):
            copy = path.with_name("copy.tmp")
            copy.write_bytes(path.read_bytes())
            os.replace(copy, path)

        def replaced_by_an_identical_hard_link(path):
            other, link = path.with_name("other.log"), path.with_name("link.tmp")
            other.write_bytes(path.read_bytes())
            self.link_or_skip(other, link)
            os.replace(link, path)

        def a_symlink_to_the_original_file(path):
            original = path.with_name("original.log")
            os.rename(path, original)
            self.symlink_or_skip(original, path)

        def a_directory(path):
            os.unlink(path)
            os.mkdir(path)

        def removed(path):
            os.unlink(path)

        changes = {"stdout appended": ("stdout.log", appended),
                   "stdout truncated": ("stdout.log", truncated),
                   "stdout rewritten in place": ("stdout.log", rewritten_in_place),
                   "stdout replaced by an identical copy": ("stdout.log", replaced_by_an_identical_copy),
                   "stdout replaced by an identical hard link": ("stdout.log", replaced_by_an_identical_hard_link),
                   "stdout reached through a symlink": ("stdout.log", a_symlink_to_the_original_file),
                   "stdout replaced by a directory": ("stdout.log", a_directory),
                   "stdout removed": ("stdout.log", removed),
                   "stderr appended": ("stderr.log", appended),
                   "stderr removed": ("stderr.log", removed)}
        for label, (name, change) in changes.items():
            with self.subTest(case=label):
                evidence = self.evidence_dir()
                _, streams = self.written_streams(evidence)
                self.assertIs(streams_match(str(evidence), streams), True, "legal baseline")
                change(evidence / name)
                self.assertIs(streams_match(str(evidence), streams), False)

    def test_a_hard_link_to_the_same_file_still_matches(self):
        evidence = self.evidence_dir()
        (stdout_path, _), streams = self.written_streams(evidence)
        aside = stdout_path.with_name("aside.log")
        self.link_or_skip(stdout_path, aside)
        os.unlink(stdout_path)
        os.rename(aside, stdout_path)
        self.assertIs(streams_match(str(evidence), streams), True)

    def test_what_is_opened_must_be_what_the_lstat_saw(self):
        evidence = self.evidence_dir()
        (stdout_path, _), streams = self.written_streams(evidence)
        seen = os.lstat(stdout_path)
        elsewhere = self.work / "elsewhere.log"
        elsewhere.write_bytes(stdout_path.read_bytes())
        self.assertIs(streams_match(str(evidence), streams), True, "legal baseline")
        with self.subTest(case="a reparse point at the path"):
            real_lstat = os.lstat

            def lstat_as_a_link(path, *args, **kwargs):
                info = real_lstat(path, *args, **kwargs)
                return with_reparse_flag(info) if os.fspath(path) == str(stdout_path) else info

            with mock.patch.object(windows_supervisor.os, "lstat", side_effect=lstat_as_a_link):
                self.assertIs(streams_match(str(evidence), streams), False)
        with self.subTest(case="a link planted after the lstat"):
            os.unlink(stdout_path)
            self.symlink_or_skip(elsewhere, stdout_path)
            # ``lstat`` saw the original regular file; the open then follows the
            # link to a byte-identical file with another identity.  Only this
            # stream's lstat is stale: the other file must match on its own.
            with mock.patch.object(windows_supervisor.os, "lstat", side_effect=stale_for(stdout_path, seen)):
                self.assertIs(streams_match(str(evidence), streams), False)

    def test_streams_match_refuses_malformed_arguments_without_raising(self):
        evidence = self.evidence_dir()
        _, good = self.written_streams(evidence)
        out, err = good["stdout"], good["stderr"]
        self.assertIs(streams_match(str(evidence), good), True, "legal baseline")
        cases = {
            "streams None": None,
            "streams a list": [out, err],
            "streams a dict subclass": DictSubclass(good),
            "streams without stderr": {"stdout": out},
            "streams with an extra key": {"stdout": out, "stderr": err, "stdin": err},
            "stdout facts None": {"stdout": None, "stderr": err},
            "stdout facts a list": {"stdout": list(out.values()), "stderr": err},
            "stdout facts a dict subclass": {"stdout": DictSubclass(out), "stderr": err},
            "stdout facts without sha256": {"stdout": {k: v for k, v in out.items() if k != "sha256"},
                                            "stderr": err},
            "stdout facts with an extra key": {"stdout": dict(out, mtime=0), "stderr": err},
            "stdout bytes as a bool": {"stdout": dict(out, bytes=True), "stderr": err},
            "stderr bytes False for an empty stream": {"stdout": out, "stderr": dict(err, bytes=False)},
            "stdout sha256 in upper case": {"stdout": dict(out, sha256=out["sha256"].upper()), "stderr": err},
            "stdout sha256 as bytes": {"stdout": dict(out, sha256=out["sha256"].encode()), "stderr": err},
            "stdout inode negative": {"stdout": dict(out, inode=-1), "stderr": err},
        }
        for label, streams in cases.items():
            with self.subTest(case=label):
                self.assertIs(streams_match(str(evidence), streams), False)
        with self.subTest(case="stdout bytes beyond the digest limit"):
            with mock.patch.object(posix_supervisor, "MAX_STREAM_DIGEST_BYTES", OUT_BYTES - 1):
                self.assertIs(streams_match(str(evidence), good), False)
        for label, directory in {"evidence_dir None": None,
                                 "evidence_dir as bytes": os.fsencode(str(evidence)),
                                 "evidence_dir with a NUL": str(evidence) + chr(0),
                                 "evidence_dir missing": str(evidence) + "-missing"}.items():
            with self.subTest(case=label):
                self.assertIs(streams_match(directory, good), False)
        self.assertIs(streams_match(str(evidence), good), True, "a case above changed the files")


class RecordCase(EvidenceCase):
    """Hand-written records: the record readers need no supervisor to be exercised."""

    def setUp(self):
        super().setUp()
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.spec = InvocationSpec("req-1", "run-1", (PY, "-c", "pass"), str(self.cwd),
                                   environment_digest(BASE_ENV), 5)
        self.handoff = canonical_json({
            "schema": "sentinelmesh-handoff-v1",
            "request_id": "req-1",
            "run_id": "run-1",
            "environment_sha256": self.spec.environment_sha256,
            "spec_sha256": self.spec.digest(),
        })
        self.streams = {"stdout": {"device": 1, "inode": 2, "bytes": 3, "sha256": sha(b"abc")},
                        "stderr": {"device": 1, "inode": 4, "bytes": 0, "sha256": sha(b"")}}
        self.observation = canonical_json({
            "schema": "sentinelmesh-observation-v2",
            "spec_sha256": self.spec.digest(),
            "observation": {
                "run_id": "run-1",
                "environment_sha256": self.spec.environment_sha256,
                "state": "STOPPED",
                "process_identity": {"pid": 4321, "boot_id": "boot-1", "executable_sha256": sha(b"exe"),
                                     "worktree_id": "wt-1"},
                "cleanup_confirmed": True,
            },
            "streams": self.streams,
        })

    def recorded(self, handoff=True, observation=False) -> Path:
        evidence = self.evidence_dir()
        if handoff:
            (evidence / HANDOFF_NAME).write_bytes(self.handoff)
        if observation:
            (evidence / OBSERVATION_NAME).write_bytes(self.observation)
        return evidence


class RecordReadOracle(RecordCase):
    def test_a_record_is_read_only_as_a_plain_regular_file(self):
        baseline = replay_decision(self.spec, str(self.recorded()))
        self.assertEqual(baseline["status"], "START_UNKNOWN", "a handoff without an observation")
        absent = replay_decision(self.spec, str(self.evidence_dir()))
        self.assertEqual(absent["status"], "NOT_STARTED")
        self.assertEqual(recorded_streams(self.spec, str(self.recorded(observation=True))), self.streams)

        def a_symlink(path):
            elsewhere = self.work / ("elsewhere-" + path.name)
            elsewhere.write_bytes(path.read_bytes())
            path.unlink()
            self.symlink_or_skip(elsewhere, path)

        def a_directory(path):
            path.unlink()
            path.mkdir()

        for name in (HANDOFF_NAME, OBSERVATION_NAME):
            for label, change in (("a symlink", a_symlink), ("a directory", a_directory)):
                with self.subTest(record=name, entry=label):
                    evidence = self.recorded(observation=True)
                    change(evidence / name)
                    self.assertEqual(self.code_of(replay_decision, self.spec, str(evidence)), "E_RUNTIME_EVIDENCE")
                    self.assertEqual(self.code_of(recorded_streams, self.spec, str(evidence)), "E_RUNTIME_EVIDENCE")
        with self.subTest(case="a hard link to the handoff reads"):
            evidence = self.recorded()
            aside = evidence / "aside.json"
            self.link_or_skip(evidence / HANDOFF_NAME, aside)
            (evidence / HANDOFF_NAME).unlink()
            os.rename(aside, evidence / HANDOFF_NAME)
            self.assertEqual(replay_decision(self.spec, str(evidence)), baseline)

    def test_the_evidence_digest_reports_links_and_substitutes_as_unreadable(self):
        def digest_of(handoff, observation):
            payload = json.dumps({"handoff": handoff, "observation": observation},
                                 sort_keys=True, separators=(",", ":")).encode("utf-8")
            return sha(payload)

        evidence = self.recorded(observation=True)
        both = digest_of(sha(self.handoff), sha(self.observation))
        self.assertEqual(evidence_digest(str(evidence)), both, "legal baseline")
        self.assertEqual(evidence_digest(str(self.evidence_dir())), digest_of(None, None))
        with self.subTest(case="a directory in place of the observation"):
            (evidence / OBSERVATION_NAME).unlink()
            (evidence / OBSERVATION_NAME).mkdir()
            self.assertEqual(evidence_digest(str(evidence)), digest_of(sha(self.handoff), "unreadable"))
            (evidence / OBSERVATION_NAME).rmdir()
        with self.subTest(case="a symlink in place of the observation"):
            elsewhere = self.work / "elsewhere.json"
            elsewhere.write_bytes(self.observation)
            self.symlink_or_skip(elsewhere, evidence / OBSERVATION_NAME)
            self.assertEqual(evidence_digest(str(evidence)), digest_of(sha(self.handoff), "unreadable"))


@unittest.skipIf(hasattr(os, "O_NOFOLLOW"), "the lstat/fstat identity check replaces O_NOFOLLOW only where it is missing")
class RecordReadWithoutNofollowOracle(RecordCase):
    """The by-hand branch of the record reads: what the lstat saw must be what was opened."""

    def test_the_record_reads_refuse_what_the_lstat_did_not_see(self):
        evidence = self.recorded(observation=True)
        path = evidence / HANDOFF_NAME
        seen = os.lstat(path)
        baseline = replay_decision(self.spec, str(evidence))
        self.assertEqual(evidence_digest(str(evidence)), evidence_digest(str(evidence)), "legal baseline")
        with self.subTest(case="a reparse point at the handoff"):
            real_lstat = os.lstat

            def lstat_as_a_link(target, *args, **kwargs):
                info = real_lstat(target, *args, **kwargs)
                return with_reparse_flag(info) if os.fspath(target) == str(path) else info

            with mock.patch.object(supervised_record.os, "lstat", side_effect=lstat_as_a_link):
                self.assertEqual(self.code_of(replay_decision, self.spec, str(evidence)), "E_RUNTIME_EVIDENCE")
                self.assertEqual(self.code_of(recorded_streams, self.spec, str(evidence)), "E_RUNTIME_EVIDENCE")
            with mock.patch.object(supervised_operation.os, "lstat", side_effect=lstat_as_a_link):
                self.assertIn("unreadable", json.dumps(self.digest_values(evidence)))
        with self.subTest(case="a file that vanished after the lstat is absent"):
            with mock.patch.object(supervised_record.os, "open", side_effect=FileNotFoundError(2, "gone")):
                self.assertEqual(replay_decision(self.spec, str(evidence))["status"], "NOT_STARTED")
            with mock.patch.object(supervised_operation.os, "open", side_effect=FileNotFoundError(2, "gone")):
                self.assertEqual(evidence_digest(str(evidence)), evidence_digest(str(self.evidence_dir())))
        with self.subTest(case="an open that fails after the lstat"):
            with mock.patch.object(supervised_record.os, "open", side_effect=PermissionError(13, "denied")):
                self.assertEqual(self.code_of(replay_decision, self.spec, str(evidence)), "E_RUNTIME_EVIDENCE")
            with mock.patch.object(supervised_operation.os, "open", side_effect=PermissionError(13, "denied")):
                self.assertIn("unreadable", json.dumps(self.digest_values(evidence)))
        with self.subTest(case="a link planted after the lstat"):
            elsewhere = self.work / "elsewhere.json"
            elsewhere.write_bytes(self.handoff)
            path.unlink()
            self.symlink_or_skip(elsewhere, path)
            # ``lstat`` saw the original regular file; the open follows the link
            # to a byte-identical file with another identity.
            with mock.patch.object(supervised_record.os, "lstat", side_effect=stale_for(path, seen)):
                self.assertEqual(self.code_of(replay_decision, self.spec, str(evidence)), "E_RUNTIME_EVIDENCE")
            with mock.patch.object(supervised_operation.os, "lstat", side_effect=stale_for(path, seen)):
                self.assertEqual(self.digest_values(evidence), {HANDOFF_NAME: "unreadable",
                                                               OBSERVATION_NAME: sha(self.observation)})
        self.assertEqual(replay_decision(self.spec, str(self.recorded(observation=True))), baseline)

    def digest_values(self, evidence: Path) -> dict:
        """The two per-record values ``evidence_digest`` summarizes, read the same way."""
        return {name: supervised_operation._record_digest(os.path.join(str(evidence), name))
                for name in (HANDOFF_NAME, OBSERVATION_NAME)}


class RecordWriteOracle(RecordCase):
    def test_a_record_is_published_only_through_a_fresh_partial(self):
        content = json.loads(self.handoff)
        partial_name = "." + HANDOFF_NAME + ".partial"
        with self.subTest(case="a fresh directory"):
            evidence = self.evidence_dir()
            supervised_record._write_record(str(evidence), HANDOFF_NAME, content)
            self.assertEqual(self.snapshot(evidence), {HANDOFF_NAME: ("file", self.handoff)})
            self.assertEqual(replay_decision(self.spec, str(evidence))["status"], "START_UNKNOWN")
        with self.subTest(case="an existing record is kept"):
            (evidence / HANDOFF_NAME).write_bytes(b"{}" + NL.encode())
            self.assertEqual(self.code_of(supervised_record._write_record, str(evidence), HANDOFF_NAME, content),
                             "E_RUNTIME_EVIDENCE")
            self.assertEqual(self.snapshot(evidence), {HANDOFF_NAME: ("file", b"{}" + NL.encode())})
        plants = {
            "a leftover partial file": lambda path: path.write_bytes(b"leftover"),
            "a directory at the partial": lambda path: path.mkdir(),
            "a dangling link at the partial": lambda path: self.symlink_or_skip(path.with_name("nowhere"), path),
        }
        for label, plant in plants.items():
            with self.subTest(case=label):
                evidence = self.evidence_dir()
                plant(evidence / partial_name)
                before = self.snapshot(evidence)
                self.assertEqual(self.code_of(supervised_record._write_record, str(evidence), HANDOFF_NAME, content),
                                 "E_RUNTIME_EVIDENCE")
                self.assertEqual(self.snapshot(evidence), before)
                self.assertFalse((evidence / "nowhere").exists(), "a file was created through the link")
        with self.subTest(case="a link at the record path"):
            evidence = self.evidence_dir()
            elsewhere = self.work / "elsewhere-record.json"
            elsewhere.write_bytes(b"elsewhere")
            self.symlink_or_skip(elsewhere, evidence / HANDOFF_NAME)
            before = self.snapshot(evidence)
            self.assertEqual(self.code_of(supervised_record._write_record, str(evidence), HANDOFF_NAME, content),
                             "E_RUNTIME_EVIDENCE")
            self.assertEqual(self.snapshot(evidence), before, "the partial was not discarded or the link changed")
            self.assertEqual(elsewhere.read_bytes(), b"elsewhere")


class ModuleShapeOracle(unittest.TestCase):
    def test_the_module_imports_without_side_effects_and_shares_the_run_type(self):
        completed = subprocess.run([PY, "-c", IMPORT_CHECK], capture_output=True, text=True, timeout=120,
                                   cwd=str(Path(__file__).resolve().parent.parent))
        self.assertEqual((completed.returncode, completed.stdout.strip()), (0, "IMPORT_SAFE_OK"), completed.stderr)
        self.assertIs(windows_supervisor.SupervisedRun, posix_supervisor.SupervisedRun)
        self.assertEqual(windows_supervisor.__all__, ["SupervisedRun", "streams_match"])
        # The wiring is unchanged until the last contract: the operation module
        # still asks the POSIX supervisor, and the record module still starts
        # through it.
        self.assertIs(supervised_operation.streams_match, posix_supervisor.streams_match)
        self.assertIs(supervised_record.supervise, posix_supervisor.supervise)


if __name__ == "__main__":
    unittest.main()
