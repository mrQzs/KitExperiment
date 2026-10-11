"""Planner oracle for the win-identity contract (`.agent/design/windows-supervisor.md`, sections 3.2 and 3.3).

Three groups:

* the Windows boot id branch, exercised on every platform through a scripted
  ``winreg`` module put in ``sys.modules`` -- the branch imports it lazily, so
  the registry rules can be pinned without reading this machine;
* the Windows ``resolve_executable`` rules, native on Windows with real files
  (never executed) under temporary directories;
* the native facts on this machine: the boot id the registry reports,
  ``capture_process_identity`` for the interpreter, and `boot_id` dispatch.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from sentinelmesh import process_identity
from sentinelmesh.process_identity import boot_id, capture_process_identity, resolve_executable
from sentinelmesh.runtime import RuntimeErrorCode

SEP = "\\"
GUID = "7718b16c-1234-4abc-9def-0123456789ab"
UPPER_GUID = "7718B16C-1234-4ABC-9DEF-0123456789AB"
MACHINE_GUID_KEY = SEP.join(["SOFTWARE", "Microsoft", "Cryptography"])
BOOT_COUNT_KEY = SEP.join(["SYSTEM", "CurrentControlSet", "Control", "Session Manager", "Memory Management",
                           "PrefetchParameters"])
WINDOWS_BOOT_ID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}:(?:0|[1-9][0-9]*)")
REG_SZ, REG_DWORD, REG_BINARY = 1, 4, 3


class FakeKey:
    """One opened registry key: records that it was closed."""

    def __init__(self, registry, path):
        self.registry, self.path, self.closed = registry, path, False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        self.registry.closed.append(self.path)
        return False


class FakeWinreg(types.ModuleType):
    """A ``winreg`` stand-in: ``values`` maps (key path, value name) to (value, kind) or an exception."""

    HKEY_LOCAL_MACHINE = object()
    HKEY_CURRENT_USER = object()
    REG_SZ, REG_DWORD, REG_BINARY = REG_SZ, REG_DWORD, REG_BINARY

    def __init__(self, values, open_error=None):
        super().__init__("winreg")
        self.values, self.open_error = dict(values), open_error
        self.opened, self.queried, self.closed = [], [], []

    def OpenKey(self, hive, path, reserved=0, access=0):  # noqa: N802 - winreg name
        self.opened.append((hive, path))
        if self.open_error is not None and (self.open_error[0] is None or self.open_error[0] == path):
            raise self.open_error[1]
        return FakeKey(self, path)

    def QueryValueEx(self, key, name):  # noqa: N802 - winreg name
        self.queried.append((key.path, name))
        if (key.path, name) not in self.values:
            raise FileNotFoundError(2, "value not found")
        answer = self.values[(key.path, name)]
        if isinstance(answer, BaseException):
            raise answer
        return answer


GOOD = {(MACHINE_GUID_KEY, "MachineGuid"): (GUID, REG_SZ), (BOOT_COUNT_KEY, "BootId"): (13, REG_DWORD)}


@contextlib.contextmanager
def fake_registry(values=GOOD, open_error=None, platform="win32"):
    module = FakeWinreg(values, open_error)
    with mock.patch.object(sys, "platform", platform), mock.patch.dict(sys.modules, {"winreg": module}):
        yield module


class Rejections(unittest.TestCase):
    def assert_rejected(self, call, code="E_RUNTIME_IDENTITY"):
        with self.assertRaises(RuntimeErrorCode) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception


class WindowsBootIdRules(Rejections):
    def test_the_two_registry_values_are_joined_as_reported(self):
        with fake_registry() as registry:
            self.assertEqual(boot_id(), GUID + ":13")
        self.assertEqual(registry.opened, [(FakeWinreg.HKEY_LOCAL_MACHINE, MACHINE_GUID_KEY),
                                           (FakeWinreg.HKEY_LOCAL_MACHINE, BOOT_COUNT_KEY)])
        self.assertEqual(registry.queried, [(MACHINE_GUID_KEY, "MachineGuid"), (BOOT_COUNT_KEY, "BootId")])
        self.assertEqual(registry.closed, [MACHINE_GUID_KEY, BOOT_COUNT_KEY])

    def test_the_guid_text_is_kept_exactly_and_the_count_is_decimal(self):
        values = dict(GOOD)
        values[(MACHINE_GUID_KEY, "MachineGuid")] = (UPPER_GUID, REG_SZ)
        values[(BOOT_COUNT_KEY, "BootId")] = (0, REG_DWORD)
        with fake_registry(values):
            self.assertEqual(boot_id(), UPPER_GUID + ":0")
        values[(BOOT_COUNT_KEY, "BootId")] = (4294967295, REG_DWORD)
        with fake_registry(values):
            self.assertEqual(boot_id(), UPPER_GUID + ":4294967295")

    def test_never_cached(self):
        with fake_registry() as registry:
            first = boot_id()
            registry.values[(BOOT_COUNT_KEY, "BootId")] = (14, REG_DWORD)
            second = boot_id()
        self.assertEqual((first, second), (GUID + ":13", GUID + ":14"))
        self.assertEqual(len(registry.queried), 4)

    def test_unreadable_keys_fail_closed(self):
        for path, error in ((MACHINE_GUID_KEY, FileNotFoundError(2, "no key")),
                            (BOOT_COUNT_KEY, PermissionError(5, "access denied")),
                            (None, OSError(1, "any"))):
            with self.subTest(path=path, error=type(error).__name__):
                with fake_registry(open_error=(path, error)):
                    self.assert_rejected(boot_id)

    def test_missing_or_failing_values_fail_closed(self):
        for name_key, failure in (((MACHINE_GUID_KEY, "MachineGuid"), None),
                                  ((BOOT_COUNT_KEY, "BootId"), None),
                                  ((MACHINE_GUID_KEY, "MachineGuid"), OSError(13, "read failed")),
                                  ((BOOT_COUNT_KEY, "BootId"), FileNotFoundError(2, "gone"))):
            with self.subTest(value=name_key[1], failure=failure):
                values = dict(GOOD)
                if failure is None:
                    del values[name_key]
                else:
                    values[name_key] = failure
                with fake_registry(values):
                    self.assert_rejected(boot_id)

    def test_wrong_kinds_and_shapes_fail_closed(self):
        bad = (
            ("guid as dword", (MACHINE_GUID_KEY, "MachineGuid"), (13, REG_DWORD)),
            ("guid text with the wrong kind", (MACHINE_GUID_KEY, "MachineGuid"), (GUID, REG_BINARY)),
            ("guid that is not a uuid", (MACHINE_GUID_KEY, "MachineGuid"), ("not-a-guid", REG_SZ)),
            ("guid with braces", (MACHINE_GUID_KEY, "MachineGuid"), ("{" + GUID + "}", REG_SZ)),
            ("guid with a trailing newline", (MACHINE_GUID_KEY, "MachineGuid"), (GUID + "\n", REG_SZ)),
            ("empty guid", (MACHINE_GUID_KEY, "MachineGuid"), ("", REG_SZ)),
            ("guid as bytes", (MACHINE_GUID_KEY, "MachineGuid"), (GUID.encode(), REG_SZ)),
            ("count as text", (BOOT_COUNT_KEY, "BootId"), ("13", REG_SZ)),
            ("count text with the dword kind", (BOOT_COUNT_KEY, "BootId"), ("13", REG_DWORD)),
            ("count int with the string kind", (BOOT_COUNT_KEY, "BootId"), (13, REG_SZ)),
            ("guid text with the dword kind", (MACHINE_GUID_KEY, "MachineGuid"), (GUID, REG_DWORD)),
            ("count as bool", (BOOT_COUNT_KEY, "BootId"), (True, REG_DWORD)),
            ("count as binary", (BOOT_COUNT_KEY, "BootId"), (b"\r", REG_BINARY)),
            ("negative count", (BOOT_COUNT_KEY, "BootId"), (-1, REG_DWORD)),
            ("count as float", (BOOT_COUNT_KEY, "BootId"), (13.0, REG_DWORD)),
        )
        for label, name_key, answer in bad:
            with self.subTest(label):
                values = dict(GOOD)
                values[name_key] = answer
                with fake_registry(values):
                    self.assert_rejected(boot_id)

    def test_a_guid_failure_stops_before_the_count_is_read(self):
        values = dict(GOOD)
        values[(MACHINE_GUID_KEY, "MachineGuid")] = (13, REG_DWORD)
        with fake_registry(values) as registry:
            self.assert_rejected(boot_id)
        self.assertEqual(registry.queried, [(MACHINE_GUID_KEY, "MachineGuid")])

    def test_only_win32_dispatches_to_the_registry(self):
        for platform in ("cygwin", "win64", "sunos5", "", "freebsd13"):
            with self.subTest(platform=platform):
                with fake_registry(platform=platform) as registry:
                    self.assert_rejected(boot_id)
                self.assertEqual(registry.opened, [])


class WindowsExecutableRules(Rejections):
    """Real files under temporary directories; nothing is ever executed."""

    @classmethod
    def setUpClass(cls):
        if os.name != "nt":
            raise unittest.SkipTest("the Windows executable rules need Windows paths and drives")

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = os.path.realpath(self._directory.name)
        self._other = tempfile.TemporaryDirectory()
        self.addCleanup(self._other.cleanup)
        self.other = os.path.realpath(self._other.name)
        self.environment = {"PATH": self.directory}

    def file(self, directory, name, content=b"MZ"):
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def resolved(self, argv0, *, cwd=None, environment=None):
        return resolve_executable(argv0, cwd=self.directory if cwd is None else cwd,
                                  environment=self.environment if environment is None else environment)

    def test_bare_stem_completes_with_exe_then_com_only(self):
        self.file(self.directory, "tool.cmd")
        self.file(self.directory, "tool.bat")
        self.assert_rejected(lambda: self.resolved("tool"))
        com = self.file(self.directory, "tool.com")
        self.assertEqual(self.resolved("tool"), com)
        exe = self.file(self.directory, "tool.exe")
        self.assertEqual(self.resolved("tool"), exe)
        self.assertEqual(self.resolved("tool.exe"), exe)
        self.assertEqual(self.resolved("tool.com"), com)
        self.assertEqual(self.resolved("TOOL"), os.path.realpath(os.path.join(self.directory, "TOOL.exe")))
        # The suffix rule is case-insensitive: a file stored as ``.EXE`` is a
        # program, and ``realpath`` reports the case the disk holds.
        shout = self.file(self.directory, "SHOUT.EXE")
        self.assertEqual(self.resolved("shout"), shout)
        self.assertEqual(self.resolved(shout), shout)
        self.assertEqual(self.resolved(self.file(self.directory, "Mixed.Com")), os.path.join(self.directory, "Mixed.Com"))

    def test_shims_and_other_suffixes_are_refused_by_every_route(self):
        shim = self.file(self.directory, "pi.cmd")
        batch = self.file(self.directory, "run.bat")
        text = self.file(self.directory, "notes.txt")
        bare = self.file(self.directory, "bare")
        twice = self.file(self.directory, "tool.exe.txt")
        for argv0 in ("pi.cmd", "run.bat", "notes.txt", "bare", "tool.exe.txt",
                      shim, batch, text, bare, twice,
                      "." + SEP + "pi.cmd", "./run.bat", "." + SEP + "bare"):
            with self.subTest(argv0=argv0):
                self.assert_rejected(lambda: self.resolved(argv0))

    def test_pathext_is_never_read(self):
        self.file(self.directory, "tool.cmd")
        environment = {"PATH": self.directory, "PATHEXT": ".CMD;.BAT;.EXE"}
        with mock.patch.dict(os.environ, {"PATHEXT": ".CMD;.BAT", "PATH": self.directory}):
            self.assert_rejected(lambda: self.resolved("tool", environment=environment))
            exe = self.file(self.directory, "tool.exe")
            self.assertEqual(self.resolved("tool", environment=environment), exe)

    def test_lookup_uses_the_snapshot_path_only_and_not_which(self):
        exe = self.file(self.directory, "tool.exe")
        with mock.patch.dict(os.environ, {"PATH": self.directory}):
            self.assert_rejected(lambda: self.resolved("tool", environment={"PATH": self.other}))
        with mock.patch.dict(os.environ, {"PATH": self.other}), \
                mock.patch.object(shutil, "which", side_effect=AssertionError("which must not be used")):
            self.assertEqual(self.resolved("tool"), exe)
        first = self.file(self.other, "tool.exe")
        both = {"PATH": self.other + ";" + self.directory}
        self.assertEqual(self.resolved("tool", environment=both), first)

    def test_path_entries_must_be_rooted_on_a_drive_or_share(self):
        self.file(self.directory, "tool.exe")
        drive = os.path.splitdrive(self.directory)[0]
        for path in ("", self.directory + ";", ";" + self.directory, self.directory + ";relative",
                     self.directory + ";" + SEP + "rooted", self.directory + ";/rooted",
                     self.directory + ";" + drive + "driverelative", "." + ";" + self.directory,
                     self.directory + ":" + self.other):
            with self.subTest(path=path):
                self.assert_rejected(lambda: self.resolved("tool", environment={"PATH": path}))
        self.assert_rejected(lambda: self.resolved("tool", environment={}))
        unc = "\\\\server\\share\\bin;" + self.directory
        self.assertEqual(self.resolved("tool", environment={"PATH": unc}),
                         os.path.join(self.directory, "tool.exe"))

    def test_explicit_paths_are_used_as_given_or_joined_with_cwd(self):
        sub = os.path.join(self.directory, "sub")
        os.mkdir(sub)
        exe = self.file(sub, "tool.exe")
        self.assertEqual(self.resolved(exe), exe)
        self.assertEqual(self.resolved(exe.upper()), os.path.realpath(exe.upper()))
        self.assertEqual(self.resolved(exe.replace(SEP, "/")), exe)
        self.assertEqual(self.resolved("sub" + SEP + "tool.exe"), exe)
        self.assertEqual(self.resolved("sub/tool.exe"), exe)
        self.assertEqual(self.resolved("." + SEP + "sub" + SEP + "tool.exe"), exe)
        self.assertEqual(self.resolved("sub" + SEP + "tool.exe", cwd=self.other + SEP + ".." + SEP + os.path.basename(self.directory)), exe)
        self.assert_rejected(lambda: self.resolved("sub" + SEP + "tool.exe", cwd=self.other))
        self.assert_rejected(lambda: self.resolved(os.path.join(self.other, "tool.exe")))

    def test_rootless_and_drive_relative_paths_are_refused(self):
        exe = self.file(self.directory, "tool.exe")
        drive, rest = os.path.splitdrive(exe)
        for argv0 in (rest, rest.replace(SEP, "/"), drive + "tool.exe", drive + "sub" + SEP + "tool.exe",
                      SEP + "tool.exe", "/tool.exe"):
            with self.subTest(argv0=argv0):
                self.assert_rejected(lambda: self.resolved(argv0))

    def test_directories_and_missing_targets_are_refused(self):
        os.mkdir(os.path.join(self.directory, "dir.exe"))
        for argv0 in ("dir.exe", os.path.join(self.directory, "dir.exe"), "missing", "missing.exe",
                      os.path.join(self.directory, "missing.exe"), self.directory):
            with self.subTest(argv0=argv0):
                self.assert_rejected(lambda: self.resolved(argv0))

    def test_links_resolve_to_the_real_file(self):
        exe = self.file(self.other, "real.exe")
        link = os.path.join(self.directory, "tool.exe")
        try:
            os.symlink(exe, link)
        except (OSError, NotImplementedError) as exc:
            raise unittest.SkipTest("symbolic links cannot be created here: %s" % exc)
        self.assertEqual(self.resolved("tool"), exe)
        self.assertEqual(self.resolved(link), exe)
        wrong = os.path.join(self.directory, "wrong.exe")
        os.symlink(self.file(self.other, "script.cmd"), wrong)
        self.assert_rejected(lambda: self.resolved("wrong"))

    def test_hostile_text_is_an_identity_error(self):
        self.file(self.directory, "tool.exe")
        for argv0 in ("tool\x00", "tool" + SEP + "\x00.exe", "\udcff.exe", "C:\x00"):
            with self.subTest(argv0=argv0):
                self.assert_rejected(lambda: self.resolved(argv0))
        # A control character in an environment value is the environment's own
        # rejection, raised before any path is looked at.
        for environment in ({"PATH": self.directory + "\x00"}, {"PATH": self.directory + ";" + self.directory + "\x00"}):
            with self.subTest(environment=environment):
                self.assert_rejected(lambda: self.resolved("tool", environment=environment), "E_RUNTIME_ENVIRONMENT")


@unittest.skipIf(os.name != "nt", "native Windows identity evidence only")
class NativeWindowsIdentity(Rejections):
    def native_boot_id(self):
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, MACHINE_GUID_KEY) as key:
            guid, kind = winreg.QueryValueEx(key, "MachineGuid")
            self.assertEqual(kind, winreg.REG_SZ)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, BOOT_COUNT_KEY) as key:
            count, kind = winreg.QueryValueEx(key, "BootId")
            self.assertEqual(kind, winreg.REG_DWORD)
        return "%s:%d" % (guid, count)

    def test_boot_id_is_the_registry_composition_and_stable(self):
        native = self.native_boot_id()
        self.assertRegex(native, WINDOWS_BOOT_ID)
        self.assertEqual(boot_id(), native)
        self.assertEqual(boot_id(), native)
        self.assertTrue(WINDOWS_BOOT_ID.fullmatch(boot_id()))

    def test_capture_process_identity_for_the_interpreter(self):
        interpreter = os.path.realpath(sys.executable)
        with open(interpreter, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        identity = capture_process_identity(os.getpid(), executable=interpreter, worktree_id="wt-1")
        self.assertEqual(identity.to_dict(), {"pid": os.getpid(), "boot_id": boot_id(),
                                              "executable_sha256": digest, "worktree_id": "wt-1"})

    def test_the_interpreter_resolves_through_its_own_directory(self):
        interpreter = os.path.realpath(sys.executable)
        directory, name = os.path.split(interpreter)
        stem = os.path.splitext(name)[0]
        environment = {"PATH": directory}
        self.assertEqual(resolve_executable(stem, cwd=directory, environment=environment), interpreter)
        self.assertEqual(resolve_executable(name, cwd=directory, environment=environment), interpreter)
        self.assertEqual(resolve_executable(interpreter, cwd=os.getcwd(), environment={}), interpreter)

    def test_import_is_free_of_ctypes(self):
        self.assertNotIn("ctypes", sys.modules.get("sentinelmesh.process_identity").__dict__)
        self.assertFalse(hasattr(process_identity, "ctypes"))


if __name__ == "__main__":
    unittest.main()
