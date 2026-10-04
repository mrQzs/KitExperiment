"""Planner follow-up cases for process_identity.

The review of posix-identity-env a002 found rules whose tests were masked by
another rule producing the same code (mutants V2-M03, V2-M14, V2-M16, and V2-M08
outside the planner probes), plus a protocol-breaking mapping that escaped as a
plain ValueError/TypeError.  Each case below isolates its rule and fails under
its mutant or against the pre-fix code.
"""
from __future__ import annotations

import collections.abc
import ctypes
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from sentinelmesh.process_identity import boot_id, environment_digest, file_sha256, resolve_executable, verified_environment
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode

VALID_ID = b"FBFA2D40-4D9D-4995-8F7B-F16C5602F4F0"


class ScriptedSysctl:
    """Stand-in for sysctlbyname: reports ``len(payload) + 1`` and writes exact bytes."""

    def __init__(self, payload: bytes, value_status: int = 0):
        self.payload = payload
        self.value_status = value_status
        self.argtypes = None
        self.restype = None

    def __call__(self, name, old_buffer, old_length, new_buffer, new_length):
        ctypes.cast(old_length, ctypes.POINTER(ctypes.c_size_t)).contents.value = len(self.payload) + 1
        if old_buffer is None:
            return 0
        ctypes.memmove(old_buffer, self.payload + b"\x00", len(self.payload) + 1)
        return self.value_status


class FakeLibc:
    def __init__(self, sysctl):
        self.sysctlbyname = sysctl


class ReadOnceThenChange(collections.abc.Mapping):
    """The first full read sees ``first``; every access after it sees ``later``."""

    def __init__(self, first, later):
        self._first, self._later, self._read = dict(first), dict(later), False

    def _view(self):
        return self._later if self._read else self._first

    def items(self):
        view = self._view()
        self._read = True
        return list(view.items())

    def __iter__(self):
        view = self._view()
        self._read = True
        return iter(list(view))

    def __getitem__(self, key):
        return self._view()[key]

    def __len__(self):
        return len(self._view())


class ProtocolBreaking(collections.abc.Mapping):
    def __init__(self, items):
        self._items = items

    def items(self):
        return self._items

    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


class ProcessIdentityGaps(unittest.TestCase):
    def code_of(self, func, *args, **kwargs):
        with self.assertRaises(RuntimeErrorCode) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def darwin_boot_id(self, sysctl):
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(ctypes, "CDLL", lambda *args, **kwargs: FakeLibc(sysctl)):
            return boot_id()

    def test_scripted_valid_id_is_returned(self):
        # Legal baseline for the two darwin cases below.
        self.assertEqual(self.darwin_boot_id(ScriptedSysctl(VALID_ID)), VALID_ID.decode())

    def test_failing_value_call_fails_even_with_a_valid_buffer(self):
        # Only the return code tells the second call failed (V2-M14).
        with self.assertRaises(RuntimeErrorCode) as ctx:
            self.darwin_boot_id(ScriptedSysctl(VALID_ID, value_status=1))
        self.assertEqual(ctx.exception.code, "E_RUNTIME_IDENTITY")

    def test_embedded_nul_after_a_valid_id_is_rejected(self):
        # Truncating at the first NUL would turn this into a valid id (V2-M16).
        with self.assertRaises(RuntimeErrorCode) as ctx:
            self.darwin_boot_id(ScriptedSysctl(VALID_ID + b"\x00tail"))
        self.assertEqual(ctx.exception.code, "E_RUNTIME_IDENTITY")

    @unittest.skipIf(os.name == "nt", "POSIX file names only")
    def test_control_character_argv0_is_rejected_even_when_the_file_exists(self):
        # The file exists and is executable, so only the character rule rejects it (V2-M03).
        directory = tempfile.mkdtemp(prefix="identity-gaps-")
        self.addCleanup(shutil.rmtree, directory, True)
        for name in ("tool", "tool\x01x"):
            path = os.path.join(directory, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\nexit 0\n")
            os.chmod(path, 0o755)
        self.assertEqual(resolve_executable("./tool", cwd=directory, environment={}),
                         os.path.realpath(os.path.join(directory, "tool")))
        self.assertEqual(self.code_of(resolve_executable, "./tool\x01x", cwd=directory, environment={}),
                         "E_RUNTIME_IDENTITY")


    def test_protocol_breaking_mapping_is_an_environment_error(self):
        self.assertEqual(environment_digest({"A": "1"}), environment_digest(dict(A="1")))
        for items in ([("A",)], [([], "value")]):
            with self.subTest(items=items):
                self.assertEqual(self.code_of(environment_digest, ProtocolBreaking(items)), "E_RUNTIME_ENVIRONMENT")

    def test_every_access_after_the_snapshot_is_ignored(self):
        first = {"LANG": "C", "PATH": "/usr/bin:/bin"}
        spec = InvocationSpec("req", "run", ("sh",), "/", environment_digest(first), 30)
        self.assertEqual(verified_environment(spec, ReadOnceThenChange(first, {"LANG": "C", "PATH": "/evil"})), first)

    @unittest.skipIf(os.name == "nt", "POSIX file modes only")
    def test_resolution_uses_the_path_from_the_snapshot(self):
        first_dir = tempfile.mkdtemp(prefix="identity-gaps-a-")
        later_dir = tempfile.mkdtemp(prefix="identity-gaps-b-")
        for directory in (first_dir, later_dir):
            self.addCleanup(shutil.rmtree, directory, True)
            path = os.path.join(directory, "tool")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\nexit 0\n")
            os.chmod(path, 0o755)
        environment = ReadOnceThenChange({"PATH": first_dir}, {"PATH": later_dir})
        self.assertEqual(resolve_executable("tool", cwd="/", environment=environment),
                         os.path.realpath(os.path.join(first_dir, "tool")))

    def test_bytes_form_of_an_existing_path_is_rejected(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(b"identity bytes")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        self.assertEqual(len(file_sha256(handle.name)), 64)
        self.assertEqual(self.code_of(file_sha256, os.fsencode(handle.name)), "E_RUNTIME_IDENTITY")


if __name__ == "__main__":
    unittest.main()
