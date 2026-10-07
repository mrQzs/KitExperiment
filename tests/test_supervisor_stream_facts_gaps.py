"""Planner follow-up for supervisor-stream-facts (review of attempt-001).

The oracle and the executor tests left three rules of the stream facts
unguarded, and one property was missing from the candidate:

* the facts are taken after the group is reaped, so a member of the group that
  writes after the leader exited (and before the reap kills it) is part of them;
  the member writes on a signal from the wrapped ``_reap_group``, so the order
  does not depend on how quickly ``Popen.wait`` notices the leader's exit;
* the digest stops at the size ``fstat`` reported before reading, so a stream
  that grows while it is digested cannot make the read go on;
* the byte count is what was read, so a stream that shrinks while it is
  digested is recorded at the size that was really digested;
* the digest is updated block by block, so the supervisor's memory does not
  grow with the child's output (the candidate joined every block first).

Version 2 of the contract adds what the independent review of attempt-001
found:

* a stream larger than ``MAX_STREAM_DIGEST_BYTES`` has no facts, and nothing of
  it is read: a child that truncates its own descriptor to a huge size (a sparse
  file, nothing written) must not keep the supervisor from reporting;
* ``streams_match`` refuses facts beyond that limit without reading, and a path
  it cannot even encode (a NUL, a lone surrogate) is a mismatch, not an error;
* a rollback or a failed hook removes only a file that is still the one this
  call created: a file another process put at the path is left alone, and the
  pin is compared with the write descriptor, not with whatever the path names.

The review of attempt-002 added three more (planner completion): a symlink put
at a created path is left alone (only ``lstat`` tells it from this call's own
file), facts over the limit are refused before any file is opened, and an
identity that cannot be read refuses the start with every descriptor closed.

Each case starts real local ``python`` processes.  The first one controls the
timing through ``posix_supervisor._reap_group``: the wrapper lets the member
write and waits for it before reaping, so a correct supervisor always passes,
and one that took the facts before the reap always misses the member's write.
"""
from __future__ import annotations

import errno
import hashlib
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sentinelmesh import posix_supervisor
from sentinelmesh.posix_supervisor import streams_match, supervise
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
BLOCK = 1 << 16
# The leader writes and exits at once.  A member of its group waits for the
# ``go`` file, writes, signals and sleeps until the reap kills it.
WRITES_AFTER_THE_LEADER = ("import os, sys, time\n"
                           "go, marker = sys.argv[1:3]\n"
                           "os.write(1, b'leader\\n')\n"
                           "if os.fork() == 0:\n"
                           "    deadline = time.monotonic() + 20\n"
                           "    while not os.path.exists(go) and time.monotonic() < deadline:\n"
                           "        time.sleep(0.005)\n"
                           "    os.write(1, b'member\\n')\n"
                           "    open(marker, 'w').close()\n"
                           "    time.sleep(30)\n"
                           "    os._exit(0)\n")
LARGE = "import os\nos.write(1, b'x' * 300000)\nos.write(2, b'e' * 70000)\n"
# The child makes its stdout look huge without writing anything (a sparse file).
TRUNCATE_STDOUT = "import os, sys\nos.ftruncate(1, int(sys.argv[1]))\nos.write(2, b'err\\n')\n"
WRITE_BYTES = "import os, sys\nos.write(1, b'x' * int(sys.argv[1]))\n"
MARK = "import sys\nopen(sys.argv[1], 'w').close()\n"
MiB = 1024 * 1024


def fd_count() -> int:
    return len(os.listdir("/dev/fd"))


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def facts_of(path: Path) -> dict:
    info = os.lstat(path)
    data = path.read_bytes()
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": len(data), "sha256": sha(data)}


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class StreamFactsGaps(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="stream-facts-gaps-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.evidence = self.work / "evidence"
        self.evidence.mkdir()

    def spec(self, code, *args):
        return InvocationSpec("req-1", "run-1", (PY, "-c", code, *args), str(self.cwd),
                              environment_digest(BASE_ENV), 10)

    def supervise(self, spec):
        return supervise(spec, environment=BASE_ENV, evidence_dir=str(self.evidence), worktree_id="wt-1")

    def is_stdout(self, descriptor):
        info, path = os.fstat(descriptor), os.lstat(self.evidence / "stdout.log")
        return (info.st_dev, info.st_ino) == (path.st_dev, path.st_ino)

    def test_the_facts_are_taken_after_the_reap(self):
        go, marker = self.work / "go", self.work / "member-wrote"
        real_reap = posix_supervisor._reap_group

        def reap_once_the_member_wrote(pgid):
            # The member writes only now: after the leader was waited for and
            # before the group is reaped.  A supervisor that took the facts
            # before the reap has already taken them, so it misses this write
            # every time (planner completion of attempt-002: waiting for the
            # leader to be reparented left the order to the scheduler).
            go.touch()
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            return real_reap(pgid)

        with mock.patch.object(posix_supervisor, "_reap_group", reap_once_the_member_wrote):
            run = self.supervise(self.spec(WRITES_AFTER_THE_LEADER, str(go), str(marker)))
        self.assertTrue(marker.exists(), "the member never wrote")
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertEqual((run.stragglers_killed, run.observation["cleanup_confirmed"]), (True, True))
        self.assertEqual((self.evidence / "stdout.log").read_bytes(), b"leader\nmember\n")
        self.assertEqual(run.streams["stdout"], facts_of(self.evidence / "stdout.log"),
                         "the facts were taken before the reap")
        self.assertIs(streams_match(str(self.evidence), run.streams), True)

    def test_the_digest_stops_at_the_size_fstat_reported(self):
        real_pread = os.pread
        grown = []

        def growing_pread(descriptor, count, offset):
            # The first read of stdout finds the file already longer than the
            # size taken before reading, as if a writer were still at work.
            if not grown and self.is_stdout(descriptor):
                with open(self.evidence / "stdout.log", "ab") as handle:
                    handle.write(b"grown\n")
                grown.append(True)
            return real_pread(descriptor, count, offset)

        with mock.patch.object(os, "pread", growing_pread):
            run = self.supervise(self.spec("import os\nos.write(1, b'leader\\n')\n"))
        self.assertEqual(grown, [True], "stdout was never digested")
        self.assertEqual((self.evidence / "stdout.log").read_bytes(), b"leader\ngrown\n")
        self.assertEqual((run.streams["stdout"]["bytes"], run.streams["stdout"]["sha256"]), (7, sha(b"leader\n")))
        self.assertIs(streams_match(str(self.evidence), run.streams), False)

    def test_the_byte_count_is_what_was_read(self):
        real_pread = os.pread
        shrunk = []

        def shrinking_pread(descriptor, count, offset):
            if not shrunk and self.is_stdout(descriptor):
                os.truncate(self.evidence / "stdout.log", 3)
                shrunk.append(True)
            return real_pread(descriptor, count, offset)

        with mock.patch.object(os, "pread", shrinking_pread):
            run = self.supervise(self.spec("import os\nos.write(1, b'leader\\n')\n"))
        self.assertEqual(shrunk, [True], "stdout was never digested")
        self.assertEqual((run.streams["stdout"]["bytes"], run.streams["stdout"]["sha256"]), (3, sha(b"lea")))
        self.assertEqual(run.streams["stdout"], facts_of(self.evidence / "stdout.log"))
        self.assertIs(streams_match(str(self.evidence), run.streams), True)

    def test_streams_are_hashed_one_block_at_a_time(self):
        sizes = []

        class Recording:
            def __init__(self, data=b""):
                sizes.append(len(data))
                self.inner = hashlib.sha256(data)

            def update(self, data):
                sizes.append(len(data))
                self.inner.update(data)

            def hexdigest(self):
                return self.inner.hexdigest()

        recording = SimpleNamespace(sha256=Recording)
        with mock.patch.object(posix_supervisor, "hashlib", recording):
            run = self.supervise(self.spec(LARGE))
            digested = list(sizes)
            sizes.clear()
            self.assertIs(streams_match(str(self.evidence), run.streams), True)
        self.assertEqual(run.streams["stdout"], facts_of(self.evidence / "stdout.log"))
        self.assertEqual(run.streams["stdout"]["bytes"], 300000)
        self.assertEqual(sum(digested), 300000 + 70000)
        self.assertLessEqual(max(digested), BLOCK, "a stream was held in memory whole while digesting")
        self.assertEqual(sum(sizes), 300000 + 70000)
        self.assertLessEqual(max(sizes), BLOCK, "a stream was held in memory whole while checking")


    # version 2 -------------------------------------------------------------

    def reads_of_stdout(self):
        """Wrap ``os.pread`` and ``os.read``; record how many bytes were asked of stdout."""
        real_pread, real_read = os.pread, os.read
        asked = []

        def pread(descriptor, count, offset):
            if self.is_stdout(descriptor):
                asked.append(count)
            return real_pread(descriptor, count, offset)

        def read(descriptor, count):
            if self.is_stdout(descriptor):
                asked.append(count)
            return real_read(descriptor, count)

        return asked, mock.patch.multiple(os, pread=pread, read=read)

    def test_a_stream_over_the_digest_limit_has_no_facts(self):
        self.assertEqual(posix_supervisor.MAX_STREAM_DIGEST_BYTES, 256 * MiB)
        asked, patched = self.reads_of_stdout()
        started = time.monotonic()
        with patched:
            run = self.supervise(self.spec(TRUNCATE_STDOUT, str(256 * MiB + 1)))
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertEqual(os.stat(self.evidence / "stdout.log").st_size, 256 * MiB + 1)
        self.assertIsNone(run.streams["stdout"])
        self.assertEqual(asked, [], "a stream over the limit was read")
        self.assertEqual(run.streams["stderr"], facts_of(self.evidence / "stderr.log"))
        self.assertIs(streams_match(str(self.evidence), run.streams), False)

    def test_the_digest_limit_is_inclusive(self):
        # The limit is read from the module when the facts are taken.
        for size, digested in ((1000, True), (1001, False)):
            with self.subTest(size=size):
                evidence = self.work / ("limit-%d" % size)
                evidence.mkdir()
                with mock.patch.object(posix_supervisor, "MAX_STREAM_DIGEST_BYTES", 1000):
                    run = supervise(self.spec(WRITE_BYTES, str(size)), environment=BASE_ENV,
                                    evidence_dir=str(evidence), worktree_id="wt-1")
                    if digested:
                        self.assertEqual(run.streams["stdout"], facts_of(evidence / "stdout.log"))
                        self.assertIs(streams_match(str(evidence), run.streams), True)
                    else:
                        self.assertIsNone(run.streams["stdout"])

    def test_streams_match_refuses_facts_over_the_limit_without_reading(self):
        run = self.supervise(self.spec("import os\nos.write(1, b'leader\\n')\n"))
        self.assertIs(streams_match(str(self.evidence), run.streams), True, "legal baseline")
        # A record can claim any size; the file is made to agree with it, sparse.
        huge = 256 * MiB + 1
        os.truncate(self.evidence / "stdout.log", huge)
        claimed = dict(run.streams, stdout=dict(run.streams["stdout"], bytes=huge))
        asked, patched = self.reads_of_stdout()
        real_open = os.open
        opened = []

        def counting_open(path, flags, *args, **kwargs):
            if os.path.dirname(os.fsdecode(path)) == str(self.evidence):
                opened.append(os.fsdecode(path))
            return real_open(path, flags, *args, **kwargs)

        with patched, mock.patch.object(os, "open", counting_open):
            self.assertIs(streams_match(str(self.evidence), claimed), False)
        self.assertEqual(asked, [], "facts over the limit were checked by reading")
        # Refused before any file is opened, not after an open and an fstat
        # (planner completion of attempt-002).
        self.assertEqual(opened, [], "facts over the limit were checked by opening the files")

    def test_streams_match_refuses_a_path_it_cannot_encode(self):
        run = self.supervise(self.spec("import os\nos.write(1, b'leader\\n')\n"))
        self.assertIs(streams_match(str(self.evidence), run.streams), True, "legal baseline")
        for label, directory in (("a NUL", str(self.evidence) + "\x00x"),
                                 ("a lone surrogate", str(self.evidence) + "/\ud800")):
            with self.subTest(case=label):
                self.assertIs(streams_match(directory, run.streams), False)

    def test_a_pin_on_a_substituted_file_keeps_the_substitute(self):
        real_open = os.open
        marker = self.work / "started"
        moved = self.evidence / "moved.log"
        stdout = self.evidence / "stdout.log"

        def substituting_open(path, flags, *args, **kwargs):
            # Just before the pin is opened, another process moves the created
            # file away and puts its own at the path.
            if (flags & os.O_ACCMODE) == os.O_RDONLY and os.fsdecode(path) == str(stdout) and not moved.exists():
                os.rename(stdout, moved)
                descriptor = real_open(str(stdout), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(descriptor, b"foreign\n")
                os.close(descriptor)
            return real_open(path, flags, *args, **kwargs)

        before = fd_count()
        with mock.patch.object(os, "open", substituting_open):
            with self.assertRaises(RuntimeErrorCode) as ctx:
                self.supervise(self.spec(MARK, str(marker)))
        self.assertEqual(ctx.exception.code, "E_RUNTIME_EVIDENCE")
        time.sleep(0.3)
        self.assertFalse(marker.exists(), "a process started")
        self.assertEqual(stdout.read_bytes(), b"foreign\n", "another process's file was removed")
        self.assertEqual(sorted(os.listdir(self.evidence)), ["moved.log", "stdout.log"])
        self.assertEqual(fd_count(), before)

    def test_a_failing_hook_keeps_a_substituted_evidence_file(self):
        marker = self.work / "started"
        stderr = self.evidence / "stderr.log"

        def substitute_then_fail():
            os.rename(stderr, self.evidence / "moved.log")
            stderr.write_bytes(b"foreign\n")
            raise ValueError("hook failed")

        with self.assertRaises(ValueError):
            supervise(self.spec(MARK, str(marker)), environment=BASE_ENV, evidence_dir=str(self.evidence),
                      worktree_id="wt-1", before_start=substitute_then_fail)
        time.sleep(0.3)
        self.assertFalse(marker.exists(), "a process started")
        self.assertEqual(stderr.read_bytes(), b"foreign\n", "another process's file was removed")
        # The file this call created and still owned is gone; the one moved away stays.
        self.assertEqual(sorted(os.listdir(self.evidence)), ["moved.log", "stderr.log"])


    # planner completion of attempt-002 -----------------------------------------

    def test_a_symlink_put_at_a_created_path_is_left_alone(self):
        # The created file is moved away and a link to it takes its place before
        # the pin: following the link (``stat``) would see this call's own inode
        # and remove the link; only ``lstat`` sees a link that is not this file.
        real_open = os.open
        marker = self.work / "started"
        moved = self.evidence / "moved.log"
        stdout = self.evidence / "stdout.log"

        def linking_open(path, flags, *args, **kwargs):
            if (flags & os.O_ACCMODE) == os.O_RDONLY and os.fsdecode(path) == str(stdout) and not moved.exists():
                os.rename(stdout, moved)
                os.symlink(moved, stdout)
            return real_open(path, flags, *args, **kwargs)

        before = fd_count()
        with mock.patch.object(os, "open", linking_open):
            with self.assertRaises(RuntimeErrorCode) as ctx:
                self.supervise(self.spec(MARK, str(marker)))
        self.assertEqual(ctx.exception.code, "E_RUNTIME_EVIDENCE")
        time.sleep(0.3)
        self.assertFalse(marker.exists(), "a process started")
        self.assertTrue(stdout.is_symlink(), "the link another process put there was removed")
        self.assertEqual(os.readlink(stdout), str(moved))
        self.assertEqual(sorted(os.listdir(self.evidence)), ["moved.log", "stdout.log"])
        self.assertEqual(fd_count(), before)

    def test_an_evidence_file_that_cannot_be_identified_starts_nothing(self):
        # The fstat that records which file was created fails: the start is
        # refused with the evidence code, every descriptor is closed, and the
        # file - which nothing now proves is this call's - is not removed.
        real_open, real_fstat = os.open, os.fstat
        created = []
        failed = []

        def recording_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if flags & os.O_EXCL and os.fsdecode(path) == str(self.evidence / "stdout.log"):
                created.append(descriptor)
            return descriptor

        def failing_fstat(descriptor):
            if created and descriptor == created[0] and not failed:
                failed.append(descriptor)
                raise OSError(errno.EIO, "Input/output error")
            return real_fstat(descriptor)

        marker = self.work / "started"
        before = fd_count()
        with mock.patch.object(os, "open", recording_open), mock.patch.object(os, "fstat", failing_fstat):
            with self.assertRaises(RuntimeErrorCode) as ctx:
                self.supervise(self.spec(MARK, str(marker)))
        self.assertEqual(ctx.exception.code, "E_RUNTIME_EVIDENCE")
        self.assertEqual(failed, created, "the identity of the created file was never taken")
        time.sleep(0.3)
        self.assertFalse(marker.exists(), "a process started")
        self.assertEqual(fd_count(), before, "a descriptor leaked")
        self.assertEqual(os.listdir(self.evidence), ["stdout.log"])


if __name__ == "__main__":
    unittest.main()
