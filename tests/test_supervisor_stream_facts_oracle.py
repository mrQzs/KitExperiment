"""Planner oracle for supervisor-stream-facts (`.agent/design/stream-trust-boundary.md`).

Before the start the supervisor pins both evidence files with a read-only
descriptor.  Once the process group is reaped it records what each stream holds
-- device, inode, byte count and SHA-256 -- read through those descriptors,
never through the path.  The observation record (schema v2) carries the same
facts next to the unchanged observation, and ``streams_match`` tells whether the
files on disk are still exactly the ones recorded.

Every case starts real local ``python`` processes.  An escaped descendant is
still invisible to the group check (its cleanup stays confirmed); what this
contract adds is that anything it writes after the reap can be detected.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sentinelmesh.core import canonical_json
from sentinelmesh.posix_supervisor import streams_match, supervise
from sentinelmesh.process_identity import environment_digest
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode, reconcile
from sentinelmesh.supervised_record import (
    HANDOFF_NAME,
    OBSERVATION_NAME,
    record_supervised_run,
    recorded_streams,
    replay_decision,
)

PY = os.path.realpath(sys.executable)
BASE_ENV = {"LANG": "C", "PATH": "/usr/bin:/bin"}
SCHEMA = "sentinelmesh-observation-v2"
GARBAGE = b"\x00\x01\x02 not an executable image\n"
STARTED_KEYS = {"run_id", "environment_sha256", "state", "process_identity", "cleanup_confirmed"}
FACT_KEYS = {"device", "inode", "bytes", "sha256"}

WRITE_BOTH = "import os\nos.write(1, b'out-1\\nout-2\\n')\nos.write(2, b'err-1\\n')\n"
ONE_BYTE = "import os\nos.write(1, b'x')\n"
WRITE_THEN_SLEEP = "import os, time\nos.write(1, b'partial\\n')\ntime.sleep(30)\n"
MARK = "import sys\nopen(sys.argv[1], 'w').close()\n"
FD_MODES = ("import fcntl, os, sys\n"
            "modes = [fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE for fd in (1, 2)]\n"
            "open(sys.argv[1], 'w').write(' '.join(map(str, modes)))\n")
# The child moves its own stdout file aside, puts a forged one at the path and
# keeps writing to the descriptor it was given.
RENAME_DURING_RUN = ("import os, sys\n"
                     "d = sys.argv[1]\n"
                     "os.write(1, b'first\\n')\n"
                     "os.rename(os.path.join(d, 'stdout.log'), os.path.join(d, 'moved.log'))\n"
                     "with open(os.path.join(d, 'stdout.log'), 'wb') as forged:\n"
                     "    forged.write(b'forged\\n')\n"
                     "os.write(1, b'second\\n')\n")
# A clean escape: the middle child is reaped, the grandchild leaves the group
# with setsid() before the leader exits, so no zombie keeps the group alive.  It
# writes only once the test opens the gate, i.e. after the supervisor returned.
CLEAN_ESCAPE = ("import os, sys, time\n"
                "pidfile, gate, done, mode = sys.argv[1:5]\n"
                "os.write(1, b'leader\\n')\n"
                "middle = os.fork()\n"
                "if middle == 0:\n"
                "    if os.fork() == 0:\n"
                "        os.setsid()\n"
                "        open(pidfile + '.tmp', 'w').write(str(os.getpid()))\n"
                "        os.rename(pidfile + '.tmp', pidfile)\n"
                "        deadline = time.monotonic() + 30\n"
                "        while not os.path.exists(gate) and time.monotonic() < deadline:\n"
                "            time.sleep(0.01)\n"
                "        if mode == 'stdout-append':\n"
                "            os.write(1, b'late\\n')\n"
                "        elif mode == 'stderr-append':\n"
                "            os.write(2, b'late\\n')\n"
                "        else:\n"
                "            os.pwrite(1, b'L', 0)\n"
                "        open(done, 'w').close()\n"
                "        os._exit(0)\n"
                "    os._exit(0)\n"
                "os.waitpid(middle, 0)\n"
                "while not os.path.exists(pidfile):\n"
                "    time.sleep(0.01)\n")
# A parent that leaves the supervised group after its child has exited but before
# reaping it: the group keeps only that zombie, so the cleanup is never confirmed.
ESCAPED_PARENT = ("import os, sys, time\n"
                  "r, w = os.pipe()\n"
                  "if os.fork() == 0:\n"
                  "    os._exit(0)\n"
                  "os.close(w)\n"
                  "os.read(r, 1)\n"
                  "time.sleep(0.2)\n"
                  "os.setsid()\n"
                  "open(sys.argv[1] + '.tmp', 'w').write(str(os.getpid()))\n"
                  "os.rename(sys.argv[1] + '.tmp', sys.argv[1])\n"
                  "time.sleep(30)\n")
ZOMBIE_GROUP_LEADER = ("import os, subprocess, sys, time\n"
                       "os.write(1, b'leader\\n')\n"
                       "subprocess.Popen([sys.executable, '-c', %r, sys.argv[1]])\n"
                       "while not os.path.exists(sys.argv[1]):\n"
                       "    time.sleep(0.02)\n" % ESCAPED_PARENT)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def facts_of(path: Path) -> dict:
    """The facts of the file at ``path``, computed independently of the module."""
    info = os.lstat(path)
    data = path.read_bytes()
    return {"device": info.st_dev, "inode": info.st_ino, "bytes": len(data), "sha256": sha(data)}


def fd_count() -> int:
    return len(os.listdir("/dev/fd"))


class DictSubclass(dict):
    pass


@unittest.skipIf(os.name != "posix", "native POSIX supervisor; Windows Job Object stays NOT_RUN")
class StreamFactsOracle(unittest.TestCase):
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

    def spec(self, code, *args, timeout=10, command=None, run_id="run-1"):
        argv = command if command is not None else (PY, "-c", code, *args)
        return InvocationSpec("req-1", run_id, argv, str(self.cwd), environment_digest(BASE_ENV), timeout)

    def garbage_spec(self):
        garbage = self.cwd / "not-a-program"
        garbage.write_bytes(GARBAGE)
        garbage.chmod(0o755)
        return self.spec(None, command=(str(garbage),))

    def supervise(self, spec, evidence):
        return supervise(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")

    def record(self, spec, evidence):
        return record_supervised_run(spec, environment=BASE_ENV, evidence_dir=str(evidence), worktree_id="wt-1")

    def code_of(self, func, *args, **kwargs):
        with self.assertRaises(RuntimeErrorCode) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def kill_pid_in(self, path: Path):
        try:
            pid = int(path.read_text().strip())
        except (OSError, ValueError):
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def wait_for(self, path: Path, seconds: float = 10.0):
        deadline = time.monotonic() + seconds
        while not path.exists():
            if time.monotonic() >= deadline:
                self.fail("%s never appeared" % path.name)
            time.sleep(0.01)

    def write_pair(self, evidence: Path, handoff: bytes, observation: dict) -> None:
        (evidence / HANDOFF_NAME).write_bytes(handoff)
        (evidence / OBSERVATION_NAME).write_bytes(canonical_json(observation))

    # the facts of a finished run ---------------------------------------------

    def test_a_completed_run_records_the_facts_of_both_streams(self):
        evidence = self.evidence_dir()
        run = self.supervise(self.spec(WRITE_BOTH), evidence)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(set(run.observation), STARTED_KEYS, "the observation itself is unchanged")
        self.assertIs(type(run.streams), dict)
        self.assertEqual(run.streams, {"stdout": facts_of(evidence / "stdout.log"),
                                       "stderr": facts_of(evidence / "stderr.log")})
        self.assertEqual((run.streams["stdout"]["bytes"], run.streams["stdout"]["sha256"]),
                         (12, sha(b"out-1\nout-2\n")))
        self.assertEqual((run.streams["stderr"]["bytes"], run.streams["stderr"]["sha256"]), (6, sha(b"err-1\n")))
        for facts in run.streams.values():
            self.assertIs(type(facts), dict)
            self.assertEqual(set(facts), FACT_KEYS)
            for key in ("device", "inode", "bytes"):
                self.assertIs(type(facts[key]), int)
            self.assertIs(type(facts["sha256"]), str)
        self.assertIs(streams_match(str(evidence), run.streams), True)

    def test_the_observation_record_v2_carries_the_streams(self):
        spec = self.spec(WRITE_BOTH)
        evidence = self.evidence_dir()
        run = self.record(spec, evidence)
        self.assertEqual((evidence / OBSERVATION_NAME).read_bytes(), canonical_json({
            "schema": SCHEMA, "spec_sha256": spec.digest(), "observation": dict(run.observation),
            "streams": run.streams}))
        decision = replay_decision(spec, str(evidence))
        self.assertEqual(decision, dict(reconcile(spec, run.observation)))
        self.assertEqual((decision["status"], decision["replay_allowed"]), ("STOPPED", True))
        self.assertEqual(recorded_streams(spec, str(evidence)), run.streams)
        self.assertIs(streams_match(str(evidence), run.streams), True)

    def test_a_run_that_never_started_has_no_streams(self):
        spec = self.garbage_spec()
        evidence = self.evidence_dir()
        run = self.record(spec, evidence)
        self.assertEqual(run.stop_reason, "start_failed")
        self.assertIsNone(run.streams)
        self.assertEqual((evidence / OBSERVATION_NAME).read_bytes(), canonical_json({
            "schema": SCHEMA, "spec_sha256": spec.digest(), "observation": dict(run.observation),
            "streams": None}))
        self.assertEqual(replay_decision(spec, str(evidence))["status"], "NOT_STARTED")
        self.assertIsNone(recorded_streams(spec, str(evidence)))
        self.assertIs(streams_match(str(evidence), run.streams), False)

    def test_a_timed_out_run_records_its_streams(self):
        evidence = self.evidence_dir()
        run = self.supervise(self.spec(WRITE_THEN_SLEEP, timeout=1), evidence)
        self.assertEqual(run.stop_reason, "timeout")
        self.assertIs(run.observation["cleanup_confirmed"], True)
        self.assertEqual(run.streams["stdout"], facts_of(evidence / "stdout.log"))
        self.assertEqual(run.streams["stdout"]["sha256"], sha(b"partial\n"))
        self.assertEqual(run.streams["stderr"], facts_of(evidence / "stderr.log"))
        self.assertEqual((run.streams["stderr"]["bytes"], run.streams["stderr"]["sha256"]), (0, sha(b"")))
        self.assertIs(streams_match(str(evidence), run.streams), True)

    def test_an_unconfirmed_cleanup_still_records_its_streams(self):
        escaped = self.work / "escaped.pid"
        self.addCleanup(self.kill_pid_in, escaped)
        evidence = self.evidence_dir()
        run = self.supervise(self.spec(ZOMBIE_GROUP_LEADER, str(escaped)), evidence)
        self.assertIs(run.observation["cleanup_confirmed"], False)
        self.assertEqual(run.streams, {"stdout": facts_of(evidence / "stdout.log"),
                                       "stderr": facts_of(evidence / "stderr.log")})
        self.assertEqual(run.streams["stdout"]["sha256"], sha(b"leader\n"))

    # the facts come from the pinned file, and later writes are detectable ----

    def test_the_facts_describe_the_file_the_child_wrote_even_after_a_rename(self):
        evidence = self.evidence_dir()
        run = self.supervise(self.spec(RENAME_DURING_RUN, str(evidence)), evidence)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        moved = evidence / "moved.log"
        self.assertEqual(moved.read_bytes(), b"first\nsecond\n")
        self.assertEqual((evidence / "stdout.log").read_bytes(), b"forged\n")
        self.assertEqual(run.streams["stdout"], facts_of(moved))
        self.assertIs(streams_match(str(evidence), run.streams), False)
        # Putting the child's own file back at the path makes the evidence whole
        # again, so the refusal above came from the substitution alone.
        os.replace(moved, evidence / "stdout.log")
        self.assertIs(streams_match(str(evidence), run.streams), True)

    def test_writes_after_the_reap_by_an_escaped_descendant_are_detected(self):
        variants = (("stdout-append", b"leader\nlate\n", b""),
                    ("stderr-append", b"leader\n", b"late\n"),
                    ("stdout-rewrite", b"Leader\n", b""))
        for mode, stdout_after, stderr_after in variants:
            with self.subTest(mode=mode):
                evidence = self.evidence_dir()
                pidfile, gate, done = (self.work / (mode + suffix) for suffix in (".pid", ".gate", ".done"))
                self.addCleanup(self.kill_pid_in, pidfile)
                run = self.supervise(self.spec(CLEAN_ESCAPE, str(pidfile), str(gate), str(done), mode), evidence)
                # The escape is still invisible to the group check ...
                self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
                self.assertIs(run.observation["cleanup_confirmed"], True)
                self.assertEqual(run.streams["stdout"]["sha256"], sha(b"leader\n"))
                self.assertEqual(run.streams["stderr"]["sha256"], sha(b""))
                self.assertIs(streams_match(str(evidence), run.streams), True, "nothing was written after the reap yet")
                # ... but what it writes after the reap no longer matches.
                gate.touch()
                self.wait_for(done)
                self.assertEqual((evidence / "stdout.log").read_bytes(), stdout_after)
                self.assertEqual((evidence / "stderr.log").read_bytes(), stderr_after)
                self.assertIs(streams_match(str(evidence), run.streams), False)

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
            os.link(other, link)
            os.replace(link, path)

        def a_symlink_to_the_original_file(path):
            original = path.with_name("original.log")
            os.rename(path, original)
            os.symlink(original, path)

        def a_directory(path):
            os.unlink(path)
            os.mkdir(path)

        def removed(path):
            os.unlink(path)

        def unreadable(path):
            os.chmod(path, 0)

        changes = {"stdout appended": ("stdout.log", appended),
                   "stdout truncated": ("stdout.log", truncated),
                   "stdout rewritten in place": ("stdout.log", rewritten_in_place),
                   "stdout replaced by an identical copy": ("stdout.log", replaced_by_an_identical_copy),
                   "stdout replaced by an identical hard link": ("stdout.log", replaced_by_an_identical_hard_link),
                   "stdout reached through a symlink": ("stdout.log", a_symlink_to_the_original_file),
                   "stdout replaced by a directory": ("stdout.log", a_directory),
                   "stdout removed": ("stdout.log", removed),
                   "stdout unreadable": ("stdout.log", unreadable),
                   "stderr appended": ("stderr.log", appended),
                   "stderr removed": ("stderr.log", removed)}
        for label, (name, change) in changes.items():
            with self.subTest(case=label):
                evidence = self.evidence_dir()
                run = self.supervise(self.spec(WRITE_BOTH), evidence)
                self.assertIs(streams_match(str(evidence), run.streams), True, "legal baseline")
                change(evidence / name)
                self.assertIs(streams_match(str(evidence), run.streams), False)

    def test_a_fifo_in_place_of_a_stream_is_refused_without_blocking(self):
        evidence = self.evidence_dir()
        run = self.supervise(self.spec(WRITE_BOTH), evidence)
        path = evidence / "stdout.log"
        os.unlink(path)
        os.mkfifo(path)
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("value", streams_match(str(evidence), run.streams)),
                                  daemon=True)
        thread.start()
        thread.join(5)
        if thread.is_alive():
            # Release a reader stuck in a blocking open before failing.
            try:
                os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
            thread.join(5)
            self.fail("streams_match blocked on a fifo")
        self.assertIs(result["value"], False)

    def test_streams_match_refuses_malformed_arguments_without_raising(self):
        evidence = self.evidence_dir()
        run = self.supervise(self.spec(ONE_BYTE), evidence)
        good = run.streams
        out, err = good["stdout"], good["stderr"]
        self.assertEqual((out["bytes"], err["bytes"]), (1, 0))
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
            # ``True == 1`` and ``False == 0``: only an exact int may stand for a byte count.
            "stdout bytes True for a one-byte stream": {"stdout": dict(out, bytes=True), "stderr": err},
            "stderr bytes False for an empty stream": {"stdout": out, "stderr": dict(err, bytes=False)},
            "stdout sha256 in upper case": {"stdout": dict(out, sha256=out["sha256"].upper()), "stderr": err},
            "stdout sha256 as bytes": {"stdout": dict(out, sha256=out["sha256"].encode()), "stderr": err},
        }
        for label, streams in cases.items():
            with self.subTest(case=label):
                self.assertIs(streams_match(str(evidence), streams), False)
        for label, directory in {"evidence_dir None": None, "evidence_dir as bytes": os.fsencode(str(evidence))}.items():
            with self.subTest(case=label):
                self.assertIs(streams_match(directory, good), False)
        self.assertIs(streams_match(str(evidence), good), True, "a case above changed the files")

    # the record -------------------------------------------------------------

    def test_recorded_streams_reads_the_record_without_changing_it(self):
        spec = self.spec(WRITE_BOTH)
        evidence = self.evidence_dir()
        run = self.record(spec, evidence)
        before = {name: (evidence / name).read_bytes() for name in os.listdir(evidence)}
        self.assertEqual(recorded_streams(spec, str(evidence)), run.streams)
        self.assertEqual({name: (evidence / name).read_bytes() for name in os.listdir(evidence)}, before)
        handoff = (evidence / HANDOFF_NAME).read_bytes()
        record = json.loads((evidence / OBSERVATION_NAME).read_bytes())
        with self.subTest(case="another invocation"):
            other = self.spec(WRITE_BOTH, run_id="run-2")
            self.assertIsNone(recorded_streams(other, str(evidence)))
        with self.subTest(case="another command"):
            self.assertIsNone(recorded_streams(self.spec(ONE_BYTE), str(evidence)))
        with self.subTest(case="no records"):
            self.assertIsNone(recorded_streams(spec, str(self.evidence_dir())))
        with self.subTest(case="only the handoff"):
            alone = self.evidence_dir()
            (alone / HANDOFF_NAME).write_bytes(handoff)
            self.assertIsNone(recorded_streams(spec, str(alone)))
        with self.subTest(case="a handoff of another specification"):
            # The observation alone names this specification: only the handoff
            # comparison can refuse the pair.
            other = self.spec(ONE_BYTE)
            borrowed = self.evidence_dir()
            (borrowed / HANDOFF_NAME).write_bytes(canonical_json({
                "schema": "sentinelmesh-handoff-v1", "request_id": other.request_id, "run_id": other.run_id,
                "environment_sha256": other.environment_sha256, "spec_sha256": other.digest()}))
            (borrowed / OBSERVATION_NAME).write_bytes((evidence / OBSERVATION_NAME).read_bytes())
            self.assertIsNone(recorded_streams(spec, str(borrowed)))
        with self.subTest(case="an observation of another specification"):
            foreign = self.evidence_dir()
            self.write_pair(foreign, handoff, dict(record, spec_sha256=self.spec(ONE_BYTE).digest()))
            self.assertIsNone(recorded_streams(spec, str(foreign)))
        with self.subTest(case="an observation without its handoff"):
            orphan = self.evidence_dir()
            (orphan / OBSERVATION_NAME).write_bytes((evidence / OBSERVATION_NAME).read_bytes())
            self.assertEqual(self.code_of(recorded_streams, spec, str(orphan)), "E_RUNTIME_EVIDENCE")
        with self.subTest(case="a damaged observation"):
            damaged = self.evidence_dir()
            (damaged / HANDOFF_NAME).write_bytes(handoff)
            (damaged / OBSERVATION_NAME).write_bytes((evidence / OBSERVATION_NAME).read_bytes() + b" ")
            self.assertEqual(self.code_of(recorded_streams, spec, str(damaged)), "E_RUNTIME_EVIDENCE")
        with self.subTest(case="a specification that is not an InvocationSpec"):
            self.assertEqual(self.code_of(recorded_streams, {"run_id": spec.run_id}, str(evidence)), "E_RUNTIME_INVOCATION")
        for label, directory in {"a relative directory": os.path.relpath(evidence),
                                 "a missing directory": str(self.work / "missing"),
                                 "a directory as bytes": os.fsencode(str(evidence))}.items():
            with self.subTest(case=label):
                self.assertEqual(self.code_of(recorded_streams, spec, directory), "E_RUNTIME_EVIDENCE")

    def test_the_v2_record_is_closed(self):
        spec = self.spec(WRITE_BOTH)
        source = self.evidence_dir()
        run = self.record(spec, source)
        handoff = (source / HANDOFF_NAME).read_bytes()
        record = json.loads((source / OBSERVATION_NAME).read_bytes())
        streams = record["streams"]
        out, err = streams["stdout"], streams["stderr"]

        def with_streams(value):
            return dict(record, streams=value)

        def with_stdout(**changes):
            return with_streams(dict(streams, stdout=dict(out, **changes)))

        refused = {
            "schema v1": dict(record, schema="sentinelmesh-observation-v1"),
            "without streams": {key: value for key, value in record.items() if key != "streams"},
            "an extra key": dict(record, note="x"),
            "streams null for a started run": with_streams(None),
            "streams as a list": with_streams([out, err]),
            "streams without stderr": with_streams({"stdout": out}),
            "streams with an extra key": with_streams(dict(streams, stdin=err)),
            "facts as a list": with_streams(dict(streams, stdout=[1, 2])),
            "facts without inode": with_streams(dict(streams, stdout={k: v for k, v in out.items() if k != "inode"})),
            "facts with an extra key": with_stdout(mtime=1),
            "device true": with_stdout(device=True),
            "inode true": with_stdout(inode=True),
            "bytes true": with_stdout(bytes=True),
            "device as text": with_stdout(device=str(out["device"])),
            "inode negative": with_stdout(inode=-1),
            "bytes negative": with_stdout(bytes=-1),
            "sha256 in upper case": with_stdout(sha256=out["sha256"].upper()),
            "sha256 one character short": with_stdout(sha256=out["sha256"][:-1]),
            "sha256 as a number": with_stdout(sha256=1),
        }
        accepted = {
            "the record as written": record,
            "stdout facts null": with_streams(dict(streams, stdout=None)),
            "both facts null": with_streams({"stdout": None, "stderr": None}),
            "a negative device": with_stdout(device=-5),
            "an empty stream": with_stdout(bytes=0, sha256=sha(b"")),
        }
        expected_decision = dict(reconcile(spec, run.observation))
        for label, content in accepted.items():
            with self.subTest(case=label):
                evidence = self.evidence_dir()
                self.write_pair(evidence, handoff, content)
                self.assertEqual(replay_decision(spec, str(evidence)), expected_decision)
                self.assertEqual(recorded_streams(spec, str(evidence)), content["streams"])
        for label, content in refused.items():
            with self.subTest(case=label):
                evidence = self.evidence_dir()
                self.write_pair(evidence, handoff, content)
                self.assertEqual(self.code_of(replay_decision, spec, str(evidence)), "E_RUNTIME_EVIDENCE")
                self.assertEqual(self.code_of(recorded_streams, spec, str(evidence)), "E_RUNTIME_EVIDENCE")
        # A run that never started has no streams, and only null says so.
        unstarted_spec = self.garbage_spec()
        unstarted = self.evidence_dir()
        self.record(unstarted_spec, unstarted)
        unstarted_handoff = (unstarted / HANDOFF_NAME).read_bytes()
        unstarted_record = json.loads((unstarted / OBSERVATION_NAME).read_bytes())
        with self.subTest(case="a run that never started, as written"):
            evidence = self.evidence_dir()
            self.write_pair(evidence, unstarted_handoff, unstarted_record)
            self.assertEqual(replay_decision(unstarted_spec, str(evidence))["status"], "NOT_STARTED")
        with self.subTest(case="streams for a run that never started"):
            evidence = self.evidence_dir()
            self.write_pair(evidence, unstarted_handoff, dict(unstarted_record, streams=streams))
            self.assertEqual(self.code_of(replay_decision, unstarted_spec, str(evidence)), "E_RUNTIME_EVIDENCE")

    # pinning and descriptor hygiene -------------------------------------------

    def test_the_child_gets_write_only_descriptors(self):
        modes = self.work / "modes.txt"
        run = self.supervise(self.spec(FD_MODES, str(modes)), self.evidence_dir())
        self.assertEqual(run.exit_code, 0)
        self.assertEqual(modes.read_text().split(), [str(os.O_WRONLY)] * 2)

    def test_a_pin_that_is_not_the_created_file_starts_nothing(self):
        real_open = os.open
        decoy = self.work / "decoy.log"
        decoy.write_bytes(b"decoy\n")

        def pins(name, action):
            def opening(path, flags, *args, **kwargs):
                if (flags & os.O_ACCMODE) == os.O_RDONLY and os.path.basename(os.fsdecode(path)) == name:
                    if action == "refuse":
                        raise OSError(errno.EACCES, "Permission denied")
                    return real_open(str(decoy), flags, *args, **kwargs)
                return real_open(path, flags, *args, **kwargs)
            return opening

        cases = {"the stdout pin is another file": pins("stdout.log", "redirect"),
                 "the stderr pin is another file": pins("stderr.log", "redirect"),
                 "the stdout pin cannot be opened": pins("stdout.log", "refuse"),
                 "the stderr pin cannot be opened": pins("stderr.log", "refuse")}
        before = fd_count()
        for index, (label, opening) in enumerate(cases.items()):
            with self.subTest(case=label):
                marker = self.work / ("started-%d" % index)
                spec = self.spec(MARK, str(marker))
                evidence = self.evidence_dir()
                with mock.patch.object(os, "open", opening):
                    self.assertEqual(self.code_of(self.record, spec, evidence), "E_RUNTIME_EVIDENCE")
                time.sleep(0.3)
                self.assertFalse(marker.exists(), "a process started")
                self.assertEqual(os.listdir(evidence), [], "evidence was left behind")
                self.assertEqual(decoy.read_bytes(), b"decoy\n")
                self.assertEqual(replay_decision(spec, str(evidence))["status"], "NOT_STARTED")
        self.assertEqual(fd_count(), before, "a refused pin leaked descriptors")

    def test_a_stream_that_cannot_be_read_is_recorded_as_null(self):
        spec = self.spec(WRITE_BOTH)
        evidence = self.evidence_dir()

        def failing_pread(*args, **kwargs):
            raise OSError(errno.EIO, "Input/output error")

        with mock.patch.object(os, "pread", failing_pread):
            run = self.record(spec, evidence)
        self.assertEqual((run.stop_reason, run.exit_code), ("completed", 0))
        self.assertEqual(run.streams, {"stdout": None, "stderr": None})
        self.assertEqual((evidence / OBSERVATION_NAME).read_bytes(), canonical_json({
            "schema": SCHEMA, "spec_sha256": spec.digest(), "observation": dict(run.observation),
            "streams": {"stdout": None, "stderr": None}}))
        self.assertEqual(replay_decision(spec, str(evidence)), dict(reconcile(spec, run.observation)))
        self.assertEqual(recorded_streams(spec, str(evidence)), {"stdout": None, "stderr": None})
        self.assertIs(streams_match(str(evidence), run.streams), False)

    def test_an_interrupt_while_digesting_propagates_and_closes_the_pins(self):
        spec = self.spec(WRITE_BOTH)
        evidence = self.evidence_dir()
        before = fd_count()

        real_pread = os.pread
        calls = []

        def interrupted(*args, **kwargs):
            # Only the first read is interrupted: an implementation that absorbs
            # the interrupt and retries finishes instead of looping forever.
            calls.append(args)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return real_pread(*args, **kwargs)

        with mock.patch.object(os, "pread", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.record(spec, evidence)
        self.assertEqual(fd_count(), before, "the pinned descriptors leaked")
        self.assertEqual(sorted(os.listdir(evidence)), [HANDOFF_NAME, "stderr.log", "stdout.log"])
        self.assertEqual(replay_decision(spec, str(evidence))["status"], "START_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
