"""Executor tests for supervisor-stream-facts, covering what the oracle leaves out.

The oracle (`tests/test_supervisor_stream_facts_oracle.py`) starts real local
``python`` process trees and checks the contract of the stream facts end to end.
These cases fill the corners it does not reach, and follow the same rule for
every refusal: prove the legal baseline first, then change exactly one thing.

* a stream that spans more than one read block;
* a cancelled run, the second outcome in which facts are taken after the reap;
* descriptor hygiene when the ``before_start`` hook fails, now that the pins
  exist;
* that reading for `streams_match` is bounded by the recorded byte count, even
  when ``fstat`` understates what the file holds;
* that the new ``streams`` field of `SupervisedRun` stays optional, and that the
  facts a record hands back are a fresh value;
* version 2: that a rollback and a failing hook remove only the evidence file
  this call created, that a stream over ``MAX_STREAM_DIGEST_BYTES`` is not
  digested (and that a fact exactly at the limit still checks out), and that a
  stream whose ``fstat`` fails while the facts are taken is ``None`` while the
  run still reports.

Every case kills whatever it started, even when an assertion fails, and a
``KeyboardInterrupt`` a test raises itself is caught rather than allowed to end
the pytest session.
"""
from __future__ import annotations

import errno
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh import posix_supervisor
from sentinelmesh.posix_supervisor import SupervisedRun, streams_match, supervise
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode, reconcile
from sentinelmesh.supervised_record import (
    HANDOFF_NAME,
    OBSERVATION_NAME,
    recorded_streams,
    record_supervised_run,
    replay_decision,
)

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}

# Writes one stream larger than a read block (the module reads 64 KiB at a time),
# so a fact has to be built from more than one ``pread``.
BLOCK_SPANNING = ("import os\n"
                  "handle = os.fdopen(1, 'wb')\n"
                  "for index in range(20000):\n"
                  "    handle.write(b'0123456789abcdef')\n"
                  "handle.close()\n")
BLOCK_SPANNING_BYTES = 20000 * 16
BLOCK_SPANNING_TEXT = b"0123456789abcdef" * 20000
# Writes one line on each stream, then marks itself ready and sleeps.  SIGTERM is
# trapped so a cancellation has to escalate to SIGKILL for the run to finish.
WRITE_BOTH_THEN_SLEEP = ("import os, signal, sys, time\n"
                         "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                         "os.write(1, b'out-1\\nout-2\\n')\n"
                         "os.write(2, b'err-1\\n')\n"
                         "open(sys.argv[1], 'w').close()\n"
                         "time.sleep(30)\n")
APPENDED = b"appended after the reap"
# Writes one line on each stream and exits, for the legal baselines below.
WRITE_BOTH = "import os\nos.write(1, b'out-1\\nout-2\\n')\nos.write(2, b'err-1\\n')\n"
# Writes exactly ``argv[1]`` bytes on stdout, so a stream of a chosen size exists.
WRITE_N = "import os, sys\nos.write(1, b'x' * int(sys.argv[1]))\n"
# Marks itself ready by writing its pid, then sleeps.  ``start_new_session``
# makes it a group leader, so a run that must not start can be cleaned up by
# killing that group.
MARK_THEN_SLEEP = ("import os, sys, time\n"
                   "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                   "time.sleep(30)\n")
# Moves its own stdout file aside, puts a forged one at the path and marks the
# rename, so a later ``fstat`` of the pinned descriptor can be told apart from
# the ``fstat`` of the file the path names.
RENAME_STDOUT_ASIDE = ("import os, sys\n"
                       "d, marker = sys.argv[1], sys.argv[2]\n"
                       "os.write(1, b'out\\n')\n"
                       "os.rename(os.path.join(d, 'stdout.log'), os.path.join(d, 'moved.log'))\n"
                       "with open(os.path.join(d, 'stdout.log'), 'wb') as forged:\n"
                       "    forged.write(b'forged\\n')\n"
                       "open(marker, 'w').close()\n")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def facts_of(path: Path) -> dict:
    """The facts of the file at ``path``, computed independently of the module."""
    info = os.lstat(path)
    data = path.read_bytes()
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": len(data), "sha256": sha(data)}


def fd_count() -> int:
    return len(os.listdir("/dev/fd"))


class LyingSize:
    """A stat result that is faithful except that it understates ``st_size``."""

    def __init__(self, info, size):
        self.st_dev = info.st_dev
        self.st_ino = info.st_ino
        self.st_mode = info.st_mode
        self.st_size = size


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class StreamFactsExecutor(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="stream-facts-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.cwd = self.work / "tree"
        self.cwd.mkdir()
        self.dirs = 0

    # helpers ---------------------------------------------------------------

    def evidence_dir(self) -> Path:
        self.dirs += 1
        path = self.work / ("evidence-%d" % self.dirs)
        path.mkdir()
        return path

    def spec(self, code, *args, timeout=10, run_id="run-1"):
        return InvocationSpec("req-1", run_id, (PY, "-c", code, *args), str(self.cwd),
                              environment_digest(BASE_ENV), timeout)

    def supervise(self, spec, evidence, **kwargs):
        return supervise(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1", **kwargs)

    def record(self, spec, evidence, **kwargs):
        return record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence),
                                     worktree_id="wt-1", **kwargs)

    def code_of(self, func, *args, **kwargs):
        with self.assertRaises(RuntimeErrorCode) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    # 1. a stream that spans more than one read block ------------------------

    def test_a_stream_larger_than_one_read_block_is_digested_whole(self):
        evidence = self.evidence_dir()
        run = self.supervise(self.spec(BLOCK_SPANNING), evidence)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertIs(type(run.streams), dict)
        self.assertEqual(run.streams["stdout"], facts_of(evidence / "stdout.log"))
        self.assertEqual(run.streams["stdout"]["bytes"], BLOCK_SPANNING_BYTES)
        self.assertEqual(run.streams["stdout"]["sha256"], sha(BLOCK_SPANNING_TEXT))
        self.assertEqual(run.streams["stderr"], facts_of(evidence / "stderr.log"))
        self.assertEqual((run.streams["stderr"]["bytes"], run.streams["stderr"]["sha256"]), (0, sha(b"")))
        self.assertIs(streams_match(str(evidence), run.streams), True)

    # 2. a cancelled run still reports its streams ---------------------------

    def test_a_cancelled_run_records_the_streams_of_both_streams(self):
        ready = self.work / "ready"
        real_wait = subprocess.Popen.wait
        calls = []

        def interrupt_first(proc, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                raise KeyboardInterrupt
            return real_wait(proc, *args, **kwargs)

        evidence = self.evidence_dir()
        started_at = time.monotonic()
        before = fd_count()
        with mock.patch.object(subprocess.Popen, "wait", interrupt_first):
            run = self.supervise(self.spec(WRITE_BOTH_THEN_SLEEP, str(ready), timeout=30), evidence)
        self.assertTrue(ready.exists(), "the leader was not ready before the interrupt")
        self.assertLess(time.monotonic() - started_at, 30.0, "the cancellation did not stop the group")
        self.assertEqual(run.stop_reason, "cancelled")
        self.assertEqual(run.exit_code, -signal.SIGKILL)
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(run.streams["stdout"], facts_of(evidence / "stdout.log"))
        self.assertEqual(run.streams["stdout"]["sha256"], sha(b"out-1\nout-2\n"))
        self.assertEqual(run.streams["stderr"], facts_of(evidence / "stderr.log"))
        self.assertEqual((run.streams["stderr"]["bytes"], run.streams["stderr"]["sha256"]), (6, sha(b"err-1\n")))
        self.assertIs(streams_match(str(evidence), run.streams), True)
        if os.path.isdir("/dev/fd"):
            self.assertEqual(fd_count(), before, "the pinned descriptors outlived the run")

    # 3. the pins are closed when the hook fails -----------------------------

    def test_a_completed_run_closes_the_two_pins(self):
        self.skip_without_dev_fd()
        before = fd_count()
        evidence = self.evidence_dir()
        run = self.supervise(self.spec(BLOCK_SPANNING), evidence)
        self.assertEqual(run.stop_reason, "completed")
        self.assertIs(streams_match(str(evidence), run.streams), True, "the facts still describe the files")
        self.assertEqual(fd_count(), before, "the pinned descriptors outlived the run")

    def skip_without_dev_fd(self):
        if not os.path.isdir("/dev/fd"):
            self.skipTest("/dev/fd is the POSIX descriptor directory")
        self.assertEqual(fd_count(), fd_count(), "/dev/fd is not a stable measurement on this platform")

    def test_a_failing_hook_leaves_no_descriptor_and_no_evidence(self):
        # ``/dev/fd`` only changes size when a descriptor is opened or closed, so
        # the count around a refused start is what proves the pins went with it.
        self.skip_without_dev_fd()
        before = fd_count()

        def failing_hook():
            raise RuntimeError("the handoff could not be recorded")

        evidence = self.evidence_dir()
        with self.assertRaises(RuntimeError):
            self.supervise(self.spec(WRITE_BOTH_THEN_SLEEP), evidence, before_start=failing_hook)
        self.assertEqual(fd_count(), before, "the pinned descriptors leaked when the hook failed")
        self.assertEqual(os.listdir(evidence), [], "evidence was left behind")

    def test_an_interrupting_hook_leaves_no_descriptor_and_no_evidence(self):
        self.skip_without_dev_fd()
        before = fd_count()

        def interrupting_hook():
            raise KeyboardInterrupt

        evidence = self.evidence_dir()
        with self.assertRaises(KeyboardInterrupt):
            self.supervise(self.spec(WRITE_BOTH_THEN_SLEEP), evidence, before_start=interrupting_hook)
        self.assertEqual(fd_count(), before, "the pinned descriptors leaked when the hook was interrupted")
        self.assertEqual(os.listdir(evidence), [], "evidence was left behind")

    def test_an_uncallable_hook_leaves_no_descriptor_at_all(self):
        # The hook is checked before the evidence files are created, so there is
        # no descriptor and no file to clean up in the first place.
        self.skip_without_dev_fd()
        before = fd_count()
        evidence = self.evidence_dir()
        code = self.code_of(self.supervise, self.spec(WRITE_BOTH_THEN_SLEEP), evidence, before_start=object())
        self.assertEqual(code, "E_RUNTIME_INVOCATION")
        self.assertEqual(fd_count(), before, "a rejected call leaked descriptors")
        self.assertEqual(os.listdir(evidence), [], "evidence was left behind")

    # 4. reading for the check is bounded by the recorded byte count ---------

    def test_the_check_reads_no_further_than_the_recorded_bytes(self):
        evidence = self.evidence_dir()
        run = self.supervise(self.spec(BLOCK_SPANNING), evidence)
        self.assertIs(streams_match(str(evidence), run.streams), True, "legal baseline")
        recorded = run.streams["stdout"]["bytes"]
        self.assertGreater(recorded, 0)
        # The file is genuinely longer than the recorded facts say, and the size
        # check is made to understate it: the digest has to refuse the mismatch
        # without reading the whole grown file.
        with open(evidence / "stdout.log", "ab") as handle:
            handle.write(APPENDED)
        real_fstat, real_read = os.fstat, os.read
        requests = []

        def understating_fstat(descriptor):
            return LyingSize(real_fstat(descriptor), recorded)

        def counting_read(descriptor, count):
            requests.append(count)
            return real_read(descriptor, count)

        with mock.patch.object(os, "fstat", understating_fstat):
            with mock.patch.object(os, "read", counting_read):
                self.assertIs(streams_match(str(evidence), run.streams), False)
        self.assertTrue(requests, "the check read nothing")
        self.assertLessEqual(sum(requests), recorded + 1, "the check read past the recorded byte count")

    def test_the_check_accepts_the_grown_file_once_the_facts_follow_it(self):
        evidence = self.evidence_dir()
        run = self.supervise(self.spec(BLOCK_SPANNING), evidence)
        self.assertIs(streams_match(str(evidence), run.streams), True, "legal baseline")
        with open(evidence / "stdout.log", "ab") as handle:
            handle.write(b"x")
        grown = dict(run.streams, stdout=dict(run.streams["stdout"],
                                              bytes=run.streams["stdout"]["bytes"] + 1,
                                              sha256=sha(BLOCK_SPANNING_TEXT + b"x")))
        self.assertIs(streams_match(str(evidence), run.streams), False, "the change alone was refused")
        self.assertIs(streams_match(str(evidence), grown), True, "the changed file itself is readable")

    # 5. the new field stays optional ----------------------------------------

    def test_a_supervised_run_needs_only_the_ten_positional_fields(self):
        fields = ("completed", 0, None, {"run_id": "run-1"}, None, "out.log", "err.log", False, ("LANG",), 0.5)
        self.assertIsNone(SupervisedRun(*fields).streams)
        self.assertIsNone(SupervisedRun(*fields, None).streams)
        self.assertEqual(SupervisedRun(*fields, {"stdout": None, "stderr": None}).streams,
                         {"stdout": None, "stderr": None})

    def test_a_run_that_never_started_carries_no_streams(self):
        self.skip_without_dev_fd()
        before = fd_count()
        garbage = self.cwd / "not-a-program"
        garbage.write_bytes(b"\x00\x01\x02 not an executable image\n")
        garbage.chmod(0o755)
        spec = InvocationSpec("req-1", "run-1", (str(garbage),), str(self.cwd),
                              environment_digest(BASE_ENV), 10)
        evidence = self.evidence_dir()
        run = self.record(spec, evidence)
        self.assertEqual(run.stop_reason, "start_failed")
        self.assertIsNone(run.streams)
        self.assertEqual(fd_count(), before, "the pinned descriptors leaked when the exec failed")
        self.assertEqual(sorted(os.listdir(evidence)),
                         [HANDOFF_NAME, OBSERVATION_NAME, "stderr.log", "stdout.log"])
        self.assertEqual(replay_decision(spec, str(evidence))["status"], "NOT_STARTED")
        self.assertIsNone(recorded_streams(spec, str(evidence)))

    def test_the_recorded_facts_come_back_as_a_fresh_value(self):
        spec = self.spec(BLOCK_SPANNING)
        evidence = self.evidence_dir()
        run = self.record(spec, evidence)
        first = recorded_streams(spec, str(evidence))
        self.assertEqual(first, run.streams)
        # A caller mutating what it was handed must not be able to change what a
        # later read sees.
        first["stdout"]["bytes"] = 0
        first["stderr"] = None
        self.assertEqual(recorded_streams(spec, str(evidence)), run.streams)
        self.assertEqual(replay_decision(spec, str(evidence)), dict(reconcile(spec, run.observation)))

    # version 2: only one's own evidence file is ever removed ----------------

    def assert_nothing_started(self, marker: Path):
        """Wait, then assert the marker is absent and kill anything that made it."""
        self.addCleanup(self.kill_group_of, marker)
        time.sleep(0.3)
        self.assertFalse(marker.exists(), "a process started")

    def kill_group_of(self, marker: Path):
        # Nothing is expected to have started; if a refusal were broken, the
        # child of a ``MARK_THEN_SLEEP`` run would otherwise outlive the test.
        try:
            pid = int(marker.read_text().strip())
        except (OSError, ValueError):
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def test_a_pin_on_a_substituted_stderr_keeps_the_substitute(self):
        real_open = os.open
        baseline = self.evidence_dir()
        run = self.supervise(self.spec(WRITE_BOTH), baseline)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0), "legal baseline")
        self.assertEqual(sorted(os.listdir(baseline)), ["stderr.log", "stdout.log"], "legal baseline")

        evidence = self.evidence_dir()
        moved = evidence / "moved.log"
        stderr = evidence / "stderr.log"
        marker = self.work / "started-stderr-pin"

        def substituting_open(path, flags, *args, **kwargs):
            # Just before the stderr pin is opened, another process moves the
            # created file away and puts its own at the path.
            if ((flags & os.O_ACCMODE) == os.O_RDONLY
                    and os.fsdecode(path) == str(stderr) and not moved.exists()):
                os.rename(stderr, moved)
                descriptor = real_open(str(stderr), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(descriptor, b"foreign\n")
                os.close(descriptor)
            return real_open(path, flags, *args, **kwargs)

        before = fd_count()
        with mock.patch.object(os, "open", substituting_open):
            code = self.code_of(self.supervise, self.spec(MARK_THEN_SLEEP, str(marker)), evidence)
        self.assertEqual(code, "E_RUNTIME_EVIDENCE")
        self.assert_nothing_started(marker)
        self.assertEqual(stderr.read_bytes(), b"foreign\n", "another process's file was removed")
        # The stdout this call created is still on its path and goes with the
        # refused start; the one moved away stays where it was moved to.
        self.assertEqual(sorted(os.listdir(evidence)), ["moved.log", "stderr.log"])
        self.assertEqual(moved.read_bytes(), b"", "the created file was not left where it was moved")
        self.assertEqual(fd_count(), before, "the refused pin leaked descriptors")

    def test_a_failing_hook_keeps_a_substituted_stdout(self):
        # The baseline: a hook that raises without touching anything leaves no
        # evidence at all, because both files are still this call's own.
        baseline = self.evidence_dir()
        with self.assertRaises(ValueError):
            self.supervise(self.spec(WRITE_BOTH), baseline, before_start=self.failing_hook)
        self.assertEqual(os.listdir(baseline), [], "the legal baseline left evidence behind")

        evidence = self.evidence_dir()
        stdout = evidence / "stdout.log"
        marker = self.work / "started-hook-substitution"

        def substitute_then_fail():
            os.rename(stdout, evidence / "moved.log")
            stdout.write_bytes(b"foreign\n")
            raise ValueError("hook failed")

        before = fd_count()
        with self.assertRaises(ValueError):
            self.supervise(self.spec(MARK_THEN_SLEEP, str(marker)), evidence,
                           before_start=substitute_then_fail)
        self.assert_nothing_started(marker)
        self.assertEqual(stdout.read_bytes(), b"foreign\n", "another process's file was removed")
        # The stderr this call created is still on its path and goes with the
        # start that never happened; the stdout moved away stays where it was
        # moved, and the file that took its place is left alone.
        self.assertEqual(sorted(os.listdir(evidence)), ["moved.log", "stdout.log"])
        self.assertEqual((evidence / "moved.log").read_bytes(), b"",
                         "the created file was not left where it was moved")
        self.assertEqual(fd_count(), before, "the failing hook leaked descriptors")

    def failing_hook(self):
        raise ValueError("hook failed")

    # version 2: the digest limit is read from the module when it is needed ---

    def test_a_fact_exactly_at_the_digest_limit_still_checks_out(self):
        evidence = self.evidence_dir()
        with mock.patch.object(posix_supervisor, "MAX_STREAM_DIGEST_BYTES", 1000):
            run = self.supervise(self.spec(WRITE_N, "1000"), evidence)
        self.assertEqual(run.streams["stdout"]["bytes"], 1000, "legal baseline")
        with mock.patch.object(posix_supervisor, "MAX_STREAM_DIGEST_BYTES", 1000):
            self.assertIs(streams_match(str(evidence), run.streams), True, "legal baseline")

        # One byte more is beyond the limit the module is made to have.  The file
        # is made to agree with the claim, so only the limit can refuse it, and
        # it has to be refused without a single read.
        grown = b"x" * 1001
        with open(evidence / "stdout.log", "ab") as handle:
            handle.write(b"x")
        inode = os.lstat(evidence / "stdout.log").st_ino
        claimed = dict(run.streams, stdout=dict(run.streams["stdout"], bytes=1001, sha256=sha(grown)))
        real_read = os.read
        asked = []

        def counting_read(descriptor, count):
            if os.fstat(descriptor).st_ino == inode:
                asked.append(count)
            return real_read(descriptor, count)

        with mock.patch.object(os, "read", counting_read):
            with mock.patch.object(posix_supervisor, "MAX_STREAM_DIGEST_BYTES", 1000):
                self.assertIs(streams_match(str(evidence), claimed), False)
        self.assertEqual(asked, [], "a fact over the limit was checked by reading")
        self.assertIs(streams_match(str(evidence), run.streams), False,
                      "the file grew, so the recorded facts no longer describe it")

    def test_a_stream_that_cannot_be_stated_while_digesting_is_null(self):
        evidence = self.evidence_dir()
        renamed = evidence / "moved.log"
        marker = self.work / "renamed"
        # The legal baseline: without the failing ``fstat`` the fact describes the
        # file the child moved away, because it is read through the pin.
        baseline = self.evidence_dir()
        baseline_marker = self.work / "renamed-baseline"
        run = self.supervise(self.spec(RENAME_STDOUT_ASIDE, str(baseline), str(baseline_marker)), baseline)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0), "legal baseline")
        self.assertEqual(run.streams["stdout"], facts_of(baseline / "moved.log"), "legal baseline")
        self.assertIs(streams_match(str(baseline), run.streams), False, "legal baseline")

        real_fstat, real_lstat = os.fstat, os.lstat

        def failing_fstat(descriptor):
            info = real_fstat(descriptor)
            # The pinned descriptor names the file the child moved away, which is
            # not the file the path names any more: that is the digest's own
            # ``fstat``, never the one the pin was proved with.
            if marker.exists() and info.st_ino == real_lstat(renamed).st_ino:
                raise OSError(errno.EIO, "Input/output error")
            return info

        with mock.patch.object(os, "fstat", failing_fstat):
            run = self.supervise(self.spec(RENAME_STDOUT_ASIDE, str(evidence), str(marker)), evidence)
        self.assertTrue(marker.exists(), "the child never renamed its stdout")
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertIsNone(run.streams["stdout"], "a stream whose fstat failed was not recorded as null")
        self.assertEqual(run.streams["stderr"], facts_of(evidence / "stderr.log"))
        self.assertEqual(renamed.read_bytes(), b"out\n", "the facts were not read through the pin")
        self.assertIs(streams_match(str(evidence), run.streams), False)


if __name__ == "__main__":
    unittest.main()
