"""Executor tests for the Windows identity branch (`.agent/design/windows-supervisor.md`, sections 3.2 and 3.3).

The planner oracle (`tests/test_win_identity_oracle.py`) already pins every rule
boundary; this file is deliberately short and covers only a complementary slice:

* the boot id composition and its re-read/stop-early behaviour, exercised on any
  platform through a scripted ``winreg`` module placed in ``sys.modules`` (the
  branch imports it lazily);
* the ``.exe``/``.com`` shim rule, the ``;``-separated ``PATH`` rules and the
  explicit-path rules, native on Windows with real files that are only created,
  never executed;
* the native ``capture_process_identity`` for this interpreter.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sys
import tempfile
import types
import unittest
from unittest import mock

from sentinelmesh.process_identity import boot_id, capture_process_identity, resolve_executable
from sentinelmesh.runtime import RuntimeErrorCode

SEP = "\\"
GUID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
UPPER_GUID = GUID.upper()
MACHINE_KEY = SEP.join(["SOFTWARE", "Microsoft", "Cryptography"])
BOOT_KEY = SEP.join(["SYSTEM", "CurrentControlSet", "Control", "Session Manager", "Memory Management",
                     "PrefetchParameters"])
REG_SZ, REG_DWORD = 1, 4
WINDOWS_BOOT_ID = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}:(?:0|[1-9][0-9]*)"
)
GOOD = {(MACHINE_KEY, "MachineGuid"): (GUID, REG_SZ), (BOOT_KEY, "BootId"): (7, REG_DWORD)}


class _FakeKey:
    """One opened key: a context manager that records its own close."""

    def __init__(self, log, path):
        self._log, self.path = log, path

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._log["closed"].append(self.path)
        return False


class _FakeWinreg(types.ModuleType):
    """A ``winreg`` stand-in: ``values`` maps (key, value) to (value, kind) or an exception."""

    HKEY_LOCAL_MACHINE = object()
    REG_SZ, REG_DWORD = REG_SZ, REG_DWORD

    def __init__(self, values, open_errors=None):
        super().__init__("winreg")
        self.values = dict(values)
        self.open_errors = dict(open_errors or {})
        self.log = {"opened": [], "queried": [], "closed": []}

    def OpenKey(self, hive, path):  # noqa: N802 - winreg name
        self.log["opened"].append(path)
        if path in self.open_errors:
            raise self.open_errors[path]
        return _FakeKey(self.log, path)

    def QueryValueEx(self, key, name):  # noqa: N802 - winreg name
        self.log["queried"].append((key.path, name))
        if (key.path, name) not in self.values:
            raise FileNotFoundError(2, "value not found")
        answer = self.values[(key.path, name)]
        if isinstance(answer, BaseException):
            raise answer
        return answer


@contextlib.contextmanager
def fake_registry(values=GOOD, open_errors=None, platform="win32"):
    module = _FakeWinreg(values, open_errors)
    with mock.patch.object(sys, "platform", platform), mock.patch.dict(sys.modules, {"winreg": module}):
        yield module


class WindowsCase(unittest.TestCase):
    def assertIdentityError(self, call):
        with self.assertRaises(RuntimeErrorCode) as caught:
            call()
        self.assertEqual(caught.exception.code, "E_RUNTIME_IDENTITY")
        return caught.exception


class BootIdComposition(WindowsCase):
    def test_uppercase_guid_and_zero_count_join_verbatim(self):
        values = dict(GOOD)
        values[(MACHINE_KEY, "MachineGuid")] = (UPPER_GUID, REG_SZ)
        values[(BOOT_KEY, "BootId")] = (0, REG_DWORD)
        with fake_registry(values):
            self.assertEqual(boot_id(), UPPER_GUID + ":0")

    def test_boot_count_is_re_read_on_every_call(self):
        with fake_registry() as registry:
            first = boot_id()
            registry.values[(BOOT_KEY, "BootId")] = (8, REG_DWORD)
            second = boot_id()
        self.assertEqual((first, second), (GUID + ":7", GUID + ":8"))
        self.assertEqual(len(registry.log["queried"]), 4)

    def test_wrong_guid_kind_stops_before_the_second_key(self):
        values = dict(GOOD)
        values[(MACHINE_KEY, "MachineGuid")] = (GUID, REG_DWORD)
        with fake_registry(values) as registry:
            self.assertIdentityError(boot_id)
        self.assertEqual(registry.log["opened"], [MACHINE_KEY])
        self.assertEqual(registry.log["queried"], [(MACHINE_KEY, "MachineGuid")])


class BootIdRejections(WindowsCase):
    def test_second_key_open_error(self):
        with fake_registry(open_errors={BOOT_KEY: PermissionError(5, "access denied")}):
            self.assertIdentityError(boot_id)

    def test_first_value_query_error(self):
        values = dict(GOOD)
        values[(MACHINE_KEY, "MachineGuid")] = FileNotFoundError(2, "gone")
        with fake_registry(values):
            self.assertIdentityError(boot_id)

    def test_guid_with_the_wrong_kind(self):
        values = dict(GOOD)
        values[(MACHINE_KEY, "MachineGuid")] = (GUID, REG_DWORD)
        with fake_registry(values):
            self.assertIdentityError(boot_id)

    def test_count_as_text_with_the_wrong_kind(self):
        values = dict(GOOD)
        values[(BOOT_KEY, "BootId")] = ("7", REG_SZ)
        with fake_registry(values):
            self.assertIdentityError(boot_id)

    def test_cygwin_platform_never_opens_the_registry(self):
        with fake_registry(platform="cygwin") as registry:
            self.assertIdentityError(boot_id)
        self.assertEqual(registry.log["opened"], [])


@unittest.skipIf(os.name != "nt", "the Windows executable rules need Windows paths and drives")
class Shims(WindowsCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = os.path.realpath(self._tmp.name)

    def write(self, name):
        path = os.path.join(self.directory, name)
        with open(path, "wb") as handle:
            handle.write(b"MZ")
        return path

    def resolve(self, argv0, *, environment=None, cwd=None):
        return resolve_executable(argv0, cwd=self.directory if cwd is None else cwd,
                                  environment={"PATH": self.directory} if environment is None else environment)

    def test_bare_stem_prefers_exe_and_shims_are_never_programs(self):
        self.write("tool.cmd")
        self.write("tool.bat")
        exe = self.write("tool.exe")
        # Legal baseline: the bare stem completes to the .exe program.
        self.assertEqual(self.resolve("tool"), exe)
        # One change: the .exe is gone; the .cmd/.bat shims no longer match.
        os.remove(exe)
        self.assertIdentityError(lambda: self.resolve("tool"))
        # The shim's own absolute path is a .cmd file and is refused too.
        self.assertIdentityError(lambda: self.resolve(os.path.join(self.directory, "tool.cmd")))

    def test_pathext_is_ignored_in_the_snapshot_and_in_os_environ(self):
        exe = self.write("tool.exe")
        self.write("tool.cmd")
        environment = {"PATH": self.directory, "PATHEXT": ".CMD"}
        with mock.patch.dict(os.environ, {"PATHEXT": ".CMD"}):
            self.assertEqual(self.resolve("tool", environment=environment), exe)


@unittest.skipIf(os.name != "nt", "the Windows executable rules need Windows paths and drives")
class PathRules(WindowsCase):
    def setUp(self):
        self._dirs = [tempfile.TemporaryDirectory() for _ in range(3)]
        for handle in self._dirs:
            self.addCleanup(handle.cleanup)
        self.first, self.second, self.empty = (os.path.realpath(handle.name) for handle in self._dirs)

    def write(self, directory, name):
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(b"MZ")
        return path

    def resolve(self, path_value):
        return resolve_executable("tool", cwd=self.first, environment={"PATH": path_value})

    def test_malformed_path_entries_are_refused_with_path_in_the_message(self):
        target = self.write(self.first, "tool.exe")
        # Legal baseline: one rooted entry finds the program.
        self.assertEqual(self.resolve(self.first), target)
        drive = os.path.splitdrive(self.first)[0]
        bad = (
            self.first + ";",                          # empty trailing entry
            ";" + self.first,                          # empty leading entry
            self.first + ";relative",                  # relative entry
            self.first + ";" + SEP + "rooted",         # rootless (no drive) entry
            self.first + ";" + drive + "driverel",     # drive-relative entry
            self.first + ":" + self.second,            # colon is not a separator
        )
        for path_value in bad:
            with self.subTest(path=path_value):
                error = self.assertIdentityError(lambda: self.resolve(path_value))
                self.assertIn("PATH", str(error))

    def test_the_first_rooted_entry_that_holds_the_file_wins(self):
        in_first = self.write(self.first, "tool.exe")
        in_second = self.write(self.second, "tool.exe")
        self.assertEqual(self.resolve(self.first + ";" + self.second), in_first)
        self.assertEqual(self.resolve(self.second + ";" + self.first), in_second)

    def test_os_environ_path_is_never_read(self):
        target = self.write(self.first, "tool.exe")
        # Legal baseline: the snapshot PATH points at the program's directory.
        self.assertEqual(self.resolve(self.first), target)
        # One change: the snapshot points at an empty directory; os.environ still
        # names the real one, which must be ignored.
        with mock.patch.dict(os.environ, {"PATH": self.first}):
            self.assertIdentityError(lambda: self.resolve(self.empty))


@unittest.skipIf(os.name != "nt", "the Windows executable rules need Windows paths and drives")
class ExplicitPaths(WindowsCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = os.path.realpath(self._tmp.name)

    def resolve(self, argv0, *, cwd=None):
        return resolve_executable(argv0, cwd=self.directory if cwd is None else cwd, environment={})

    def test_rooted_and_relative_paths(self):
        sub = os.path.join(self.directory, "sub")
        os.mkdir(sub)
        exe = os.path.join(sub, "tool.exe")
        with open(exe, "wb") as handle:
            handle.write(b"MZ")
        # Legal baselines: absolute (verbatim), forward-slash, and both relative
        # forms all reach the same real file.
        self.assertEqual(self.resolve(exe), exe)
        self.assertEqual(self.resolve(exe.replace(SEP, "/")), exe)
        self.assertEqual(self.resolve("sub" + SEP + "tool.exe"), exe)
        self.assertEqual(self.resolve("sub/tool.exe"), exe)
        # One change each: a rootless path and a drive-only name are refused.
        drive, rest = os.path.splitdrive(exe)
        self.assertIdentityError(lambda: self.resolve(rest))
        self.assertIdentityError(lambda: self.resolve(drive + "tool.exe"))

    def test_a_directory_that_looks_like_a_program_is_refused(self):
        program = os.path.join(self.directory, "tool.exe")
        with open(program, "wb") as handle:
            handle.write(b"MZ")
        # Legal baseline: a real program file at an absolute path resolves.
        self.assertEqual(self.resolve(program), program)
        # One change: the same path is now a directory, not a regular file.
        os.remove(program)
        os.mkdir(program)
        self.assertIdentityError(lambda: self.resolve(program))


@unittest.skipIf(os.name != "nt", "native Windows identity evidence only")
class NativeIdentity(WindowsCase):
    def test_capture_process_identity_for_the_interpreter(self):
        interpreter = os.path.realpath(sys.executable)
        with open(interpreter, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        identity = capture_process_identity(os.getpid(), executable=interpreter, worktree_id="wt-1")
        self.assertEqual(set(identity.to_dict()), {"pid", "boot_id", "executable_sha256", "worktree_id"})
        self.assertEqual(identity.boot_id, boot_id())
        self.assertTrue(WINDOWS_BOOT_ID.fullmatch(identity.boot_id))
        self.assertEqual(identity.executable_sha256, digest)


if __name__ == "__main__":
    unittest.main()
