"""Planner probes for the defects reproduced in posix-identity-env attempt-001.

Planner-provided acceptance probes for posix-identity-env v2; executors must not
modify this file.  Each case reproduces one defect the review found in the v1
candidate: a file descriptor accepted (and closed) as a path, path errors that
escaped as plain ValueError/UnicodeEncodeError, relative or empty PATH entries
resolved against the parent's working directory, and an environment mapping
read more than once.
"""
from __future__ import annotations

import collections.abc
import os
import tempfile
import unittest

from sentinelmesh.process_identity import environment_digest, file_sha256, resolve_executable, verified_environment
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode


class FlippingEnvironment(collections.abc.Mapping):
    """Yields one environment on the first full read and another afterwards."""

    def __init__(self, first, later):
        self._first, self._later, self._reads = dict(first), dict(later), 0

    def _current(self):
        return self._first if self._reads <= 1 else self._later

    def __iter__(self):
        self._reads += 1
        return iter(dict(self._current()))

    def __getitem__(self, key):
        return self._current()[key]

    def __len__(self):
        return len(self._current())


class ProcessIdentityProbes(unittest.TestCase):
    def code_of(self, func, *args, **kwargs):
        with self.assertRaises(RuntimeErrorCode) as ctx:
            func(*args, **kwargs)
        return ctx.exception.code

    def test_open_descriptor_is_not_a_path_and_stays_open(self):
        with tempfile.TemporaryFile() as handle:
            handle.write(b"descriptor bytes")
            handle.flush()
            descriptor = handle.fileno()
            self.assertEqual(self.code_of(file_sha256, descriptor), "E_RUNTIME_IDENTITY")
            os.fstat(descriptor)  # still open

    def test_path_errors_map_to_identity_errors(self):
        directory = tempfile.mkdtemp(prefix="identity-probe-")
        self.addCleanup(os.rmdir, directory)
        for argv0, cwd in (("./a\x00b", directory), ("./tool", "bad\x00cwd"), ("x/\ud800", directory)):
            with self.subTest(argv0=argv0, cwd=cwd):
                self.assertEqual(self.code_of(resolve_executable, argv0, cwd=cwd, environment={}), "E_RUNTIME_IDENTITY")

    def test_path_entries_must_be_absolute(self):
        self.assertTrue(resolve_executable("sh", cwd="/", environment={"PATH": "/usr/bin:/bin"}).endswith("sh"))
        for path in ("bin:/usr/bin:/bin", "/usr/bin::/bin", ":/bin"):
            with self.subTest(path=path):
                self.assertEqual(self.code_of(resolve_executable, "sh", cwd="/", environment={"PATH": path}),
                                 "E_RUNTIME_IDENTITY")

    def test_environment_is_read_once(self):
        first = {"LANG": "C", "PATH": "/usr/bin:/bin"}
        spec = InvocationSpec("req", "run", ("sh",), "/", environment_digest(first), 30)
        verified = verified_environment(spec, FlippingEnvironment(first, {"LANG": "C", "PATH": "/evil"}))
        self.assertEqual(verified, first)


if __name__ == "__main__":
    unittest.main()
