"""Direct-call tests for process identity capture and explicit-environment digests.

Every rejection case first proves the legal baseline is accepted and then
changes exactly the one condition under test, so a rejection can never be
explained by an unrelated rule firing first.  Platform evidence is kept separate
by design: the native macOS boot-session assertions are real and are skipped off
macOS, while the Linux branch is only exercised through a patched file read of
`/proc/sys/kernel/random/boot_id` (that file does not exist on this machine, so
native Linux evidence stays NOT_RUN).
"""
from __future__ import annotations

import builtins
import collections.abc
import contextlib
import ctypes
import errno
import hashlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from sentinelmesh.process_identity import (
    boot_id,
    capture_process_identity,
    environment_digest,
    file_sha256,
    resolve_executable,
    verified_environment,
)
from sentinelmesh.runtime import InvocationSpec, RuntimeErrorCode

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINUX_BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
BOOT_ID_TEXT = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"

SHA_ENV_A = "fa0f07ab1a352dd1ee7596bc244caa6649e742dfe9f800f455ef68c822f75cde"
SHA_ENV_EMPTY = "16f1bf80d85f8f89ba69f102a9a8a35f07dd8b0ac4b26cb68a9c0b46e29fde7e"

ENV_BASELINE = {"LANG": "C", "PATH": "/usr/bin:/bin"}
LINUX_BOOT_ID_LEGAL = "123e4567-e89b-12d3-a456-426614174000"

# The ctypes prototype the darwin branch has to declare for ``sysctlbyname``: a
# caller-sized buffer addressed by pointer, or the reported size cannot be read
# back safely on a 64-bit ABI.
DARWIN_ARGUMENT_TYPES = [
    ctypes.c_char_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_size_t),
    ctypes.c_void_p,
    ctypes.c_size_t,
]


class NameSubclass(str):
    """A ``str`` subclass, which the module must not accept as a scalar."""


class ValueSubclass(str):
    """A ``str`` subclass, which the module must not accept as a scalar."""


class PathSubclass(str):
    """A ``str`` subclass, which must not be accepted as argv0, cwd or path."""


class FakePathLike:
    """An ``os.PathLike`` object, which must never be opened as a path."""

    def __init__(self, path):
        self._path = path

    def __fspath__(self):
        return self._path


class SnapshotMapping(collections.abc.Mapping):
    """Yield one environment on the first ``items()`` and another afterwards."""

    def __init__(self, first, later):
        self._first, self._later, self.reads = dict(first), dict(later), 0

    def items(self):
        self.reads += 1
        return (self._first if self.reads == 1 else self._later).items()

    def __iter__(self):
        return iter(self._first)

    def __getitem__(self, key):
        return self._first[key]

    def __len__(self):
        return len(self._first)


class FakeSysctlbyname:
    """Controllable stand-in for ``libSystem.sysctlbyname``.

    The real symbol is called twice per boot id: once with a ``None`` buffer to
    learn the size, once with a buffer to fill.  One scripted payload therefore
    serves both calls of a single ``boot_id()`` call, so a sequence of payloads
    is returned one ``boot_id()`` at a time.
    """

    def __init__(self, *payloads, length_status=0, value_status=0, reported_length=None):
        self.payloads = list(payloads)
        self.length_status = length_status
        self.value_status = value_status
        self.reported_length = reported_length
        self.argtypes = None
        self.restype = None
        self.calls = []
        self.round = -1

    def _current(self):
        if self.round < 0 or self.round >= len(self.payloads):
            raise AssertionError("the implementation asked for more boot ids than scripted")
        return self.payloads[self.round]

    def _report_length(self, pointer, payload):
        length = len(payload) + 1 if self.reported_length is None else self.reported_length
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_size_t)).contents.value = length

    def __call__(self, name, old_buffer, old_length, new_buffer, new_length):
        self.calls.append((name, old_buffer, old_length, new_buffer, new_length))
        if old_buffer is None:
            # The size query opens a new round and names the payload served.
            self.round += 1
            self._report_length(old_length, self._current())
            return self.length_status
        payload = self._current()
        self._report_length(old_length, payload)
        if payload:
            old_buffer.value = payload
        return self.value_status


class FakeLibc:
    """A libSystem handle whose only symbol is the scripted sysctl above."""

    def __init__(self, sysctlbyname):
        self.sysctlbyname = sysctlbyname


def make_spec(**overrides):
    """Return a legal baseline ``InvocationSpec`` with optional overrides."""
    values = {
        "request_id": "req-1",
        "run_id": "run-1",
        "command": ("python", "-c", "print(1)"),
        "cwd": "/work/tree",
        "environment_sha256": SHA_ENV_A,
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return InvocationSpec(**values)


def script_executable(directory, name, body="#!/bin/sh\nexit 0\n", mode=0o755):
    """Write an executable regular file into ``directory`` and return its path."""
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.chmod(path, mode)
    return path


class RejectionAssertions(unittest.TestCase):
    """Shared helpers: prove the legal baseline first, then the single rejection."""

    def assert_baseline_digest(self):
        self.assertEqual(environment_digest(ENV_BASELINE), SHA_ENV_A)
        self.assertEqual(environment_digest({}), SHA_ENV_EMPTY)

    def assert_rejected(self, call, code):
        with self.assertRaises(RuntimeErrorCode) as caught:
            call()
        self.assertEqual(caught.exception.code, code)


class EnvironmentDigestTests(RejectionAssertions):
    def test_golden_vectors(self):
        self.assertEqual(environment_digest({"LANG": "C", "PATH": "/usr/bin:/bin"}), SHA_ENV_A)
        self.assertEqual(environment_digest({}), SHA_ENV_EMPTY)

    def test_digest_is_canonical_json_sha256(self):
        for environment in (ENV_BASELINE, {}, {"B": "2", "A": "1"}, {"LANG": "C=1"}):
            payload = {"schema": "sentinelmesh-environment-v1", "variables": dict(environment)}
            expected = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            with self.subTest(environment=environment):
                self.assertEqual(environment_digest(environment), expected)

    def test_digest_ignores_key_order_and_mapping_type(self):
        self.assertEqual(environment_digest({"PATH": "/usr/bin:/bin", "LANG": "C"}), SHA_ENV_A)
        proxy = types.MappingProxyType(ENV_BASELINE)
        self.assertEqual(environment_digest(proxy), SHA_ENV_A)
        self.assertIs(type(environment_digest(proxy)), str)

    def test_every_name_and_value_change_changes_digest(self):
        self.assert_baseline_digest()
        variants = {
            "other LANG value": {"LANG": "en_US.UTF-8", "PATH": "/usr/bin:/bin"},
            "other PATH value": {"LANG": "C", "PATH": "/usr/bin"},
            "shorter PATH value": {"LANG": "C", "PATH": "/usr/bin:/bin:"},
            "dropped key": {"LANG": "C"},
            "renamed key": {"LANG": "C", "PATHS": "/usr/bin:/bin"},
            "added key": {"LANG": "C", "PATH": "/usr/bin:/bin", "EXTRA": "1"},
            "empty value": {"LANG": "C", "PATH": ""},
            "empty mapping": {},
        }
        for label, variant in variants.items():
            with self.subTest(label):
                self.assertNotEqual(environment_digest(variant), SHA_ENV_A)
        # An empty value is not the same as a missing key.
        self.assertNotEqual(environment_digest({"LANG": ""}), environment_digest({}))
        self.assertNotEqual(
            environment_digest({"LANG": "C", "PATH": ""}),
            environment_digest({"LANG": "C", "PATH": "/usr/bin:/bin"}),
        )

    def test_non_mapping_is_rejected(self):
        self.assert_baseline_digest()
        for label, value in {
            "list": [("LANG", "C")],
            "string": "LANG=C",
            "none": None,
            "integer": 7,
            "set": {"LANG"},
            "tuple of pairs": (("LANG", "C"),),
        }.items():
            with self.subTest(label):
                self.assert_rejected(lambda value=value: environment_digest(value), "E_RUNTIME_ENVIRONMENT")

    def test_str_subclass_name_or_value_is_rejected(self):
        self.assert_baseline_digest()
        for label, value in {
            "subclass name": {NameSubclass("LANG"): "C"},
            "subclass value": {"LANG": ValueSubclass("C")},
            "subclass name and value": {NameSubclass("LANG"): ValueSubclass("C")},
        }.items():
            with self.subTest(label):
                self.assert_rejected(lambda value=value: environment_digest(value), "E_RUNTIME_ENVIRONMENT")

    def test_mapping_is_read_once_and_the_first_read_wins(self):
        first = dict(ENV_BASELINE)
        later = {"LANG": "C", "PATH": "/evil"}
        probe = SnapshotMapping(first, later)
        self.assertEqual(environment_digest(probe), SHA_ENV_A)
        # One read only: a second read would have digested (and validated) a
        # different mapping than the one the caller handed over.
        self.assertEqual(probe.reads, 1)

    def test_non_text_name_or_value_is_rejected(self):
        self.assert_baseline_digest()
        for label, value in {
            "integer value": {"LANG": 1},
            "bytes value": {"LANG": b"C"},
            "none value": {"LANG": None},
            "boolean value": {"LANG": True},
            "integer name": {1: "C"},
            "bytes name": {b"LANG": "C"},
            "none name": {None: "C"},
            "list name": {("LANG",): "C"},
        }.items():
            with self.subTest(label):
                self.assert_rejected(lambda value=value: environment_digest(value), "E_RUNTIME_ENVIRONMENT")

    def test_empty_or_equals_name_is_rejected(self):
        self.assert_baseline_digest()
        for label, value in {
            "empty name": {"": "C"},
            "name is only an equals": {"=": "C"},
            "name with equals": {"LANG=C": "x"},
            "empty name next to a legal name": {"LANG": "C", "": "x"},
        }.items():
            with self.subTest(label):
                self.assert_rejected(lambda value=value: environment_digest(value), "E_RUNTIME_ENVIRONMENT")
        # A value keeps '=' legal: only names are constrained.
        self.assertNotEqual(environment_digest({"LANG": "C=1"}), SHA_ENV_A)

    def test_control_characters_in_names_and_values_are_rejected(self):
        self.assert_baseline_digest()
        bad_characters = {
            "NUL": "\x00",
            "SOH": "\x01",
            "TAB": "\t",
            "LF": "\n",
            "CR": "\r",
            "US": "\x1f",
            "DEL": "\x7f",
        }
        for label, character in bad_characters.items():
            with self.subTest(f"{label} in name"):
                self.assert_rejected(
                    lambda character=character: environment_digest({"LANG" + character: "C"}),
                    "E_RUNTIME_ENVIRONMENT",
                )
            with self.subTest(f"{label} in value"):
                self.assert_rejected(
                    lambda character=character: environment_digest({"LANG": "C" + character}),
                    "E_RUNTIME_ENVIRONMENT",
                )

    def test_surrogates_in_names_and_values_are_rejected(self):
        self.assert_baseline_digest()
        for label, character in {
            "lone high surrogate": "\ud800",
            "lone low surrogate": "\udfff",
        }.items():
            with self.subTest(f"{label} in name"):
                self.assert_rejected(
                    lambda character=character: environment_digest({"LANG" + character: "C"}),
                    "E_RUNTIME_ENVIRONMENT",
                )
            with self.subTest(f"{label} in value"):
                self.assert_rejected(
                    lambda character=character: environment_digest({"LANG": character}),
                    "E_RUNTIME_ENVIRONMENT",
                )

    def test_unicode_values_are_allowed(self):
        digest = environment_digest({"LANG": "C", "NOTE": "树"})
        payload = {"schema": "sentinelmesh-environment-v1", "variables": {"LANG": "C", "NOTE": "树"}}
        expected = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(digest, expected)
        self.assertNotEqual(digest, environment_digest({"LANG": "C", "NOTE": "树 "}))
        self.assertNotEqual(digest, environment_digest({"LANG": "C"}))


class VerifiedEnvironmentTests(RejectionAssertions):
    def test_matching_environment_returns_independent_plain_dict_copy(self):
        environment = dict(ENV_BASELINE)
        verified = verified_environment(make_spec(), environment)
        self.assertEqual(verified, ENV_BASELINE)
        self.assertIs(type(verified), dict)
        self.assertIsNot(verified, environment)
        environment["PATH"] = "/mutated"
        environment["EXTRA"] = "1"
        self.assertEqual(verified, ENV_BASELINE)
        self.assertEqual(verified_environment(make_spec(), ENV_BASELINE), ENV_BASELINE)

    def test_verified_environment_returns_the_first_snapshot_only(self):
        first = dict(ENV_BASELINE)
        later = {"LANG": "C", "PATH": "/evil"}
        probe = SnapshotMapping(first, later)
        verified = verified_environment(make_spec(), probe)
        self.assertEqual(verified, first)
        self.assertIs(type(verified), dict)
        self.assertIsNot(verified, first)
        self.assertEqual(probe.reads, 1)

    def test_mismatching_environment_is_rejected(self):
        spec = make_spec()
        self.assertEqual(verified_environment(spec, ENV_BASELINE), ENV_BASELINE)
        for label, environment in {
            "different mapping": {"LANG": "C", "PATH": "/usr/bin"},
            "missing key": {"LANG": "C"},
            "extra key": {"LANG": "C", "PATH": "/usr/bin:/bin", "EXTRA": "1"},
            "empty value": {"LANG": "C", "PATH": ""},
            "empty mapping": {},
        }.items():
            with self.subTest(label):
                self.assert_rejected(
                    lambda environment=environment: verified_environment(spec, environment),
                    "E_RUNTIME_ENVIRONMENT",
                )
        # The empty-document digest verifies the empty mapping and nothing else.
        empty_spec = make_spec(environment_sha256=SHA_ENV_EMPTY)
        self.assertEqual(verified_environment(empty_spec, {}), {})
        self.assert_rejected(
            lambda: verified_environment(empty_spec, ENV_BASELINE), "E_RUNTIME_ENVIRONMENT"
        )

    def test_non_spec_is_rejected(self):
        self.assertEqual(verified_environment(make_spec(), ENV_BASELINE), ENV_BASELINE)

        class SpecSubclass(InvocationSpec):
            pass

        for label, value in {
            "none": None,
            "empty dict": {},
            "mapping": {"request_id": "req-1"},
            "subclass": SpecSubclass(
                request_id="req-1",
                run_id="run-1",
                command=("python",),
                cwd="/work/tree",
                environment_sha256=SHA_ENV_A,
                timeout_seconds=30,
            ),
        }.items():
            with self.subTest(label):
                self.assert_rejected(
                    lambda value=value: verified_environment(value, ENV_BASELINE),
                    "E_RUNTIME_INVOCATION",
                )

    def test_invalid_environment_shape_is_rejected(self):
        spec = make_spec()
        self.assertEqual(verified_environment(spec, ENV_BASELINE), ENV_BASELINE)
        for label, environment in {
            "non mapping": [("LANG", "C")],
            "empty name": {"": "C"},
            "control character": {"LANG": "C\n"},
            "surrogate": {"LANG": "\ud800"},
        }.items():
            with self.subTest(label):
                self.assert_rejected(
                    lambda environment=environment: verified_environment(spec, environment),
                    "E_RUNTIME_ENVIRONMENT",
                )


class BootIdTests(RejectionAssertions):
    @unittest.skipUnless(sys.platform == "darwin", "native macOS boot session evidence only")
    def test_matches_native_sysctl_output_and_is_stable(self):
        native = self.native_sysctl_boot_id()
        self.assertEqual(boot_id(), native)
        self.assertEqual(boot_id(), native)

    def test_unsupported_platform_fails_closed(self):
        for platform in ("win32", "sunos5", "", "freebsd13"):
            with self.subTest(platform=platform):
                with mock.patch.object(sys, "platform", platform):
                    self.assert_rejected(boot_id, "E_RUNTIME_IDENTITY")

    @contextlib.contextmanager
    def patched_linux_boot_id(self, *contents, error=None):
        """Serve a fake ``/proc/sys/kernel/random/boot_id`` on the Linux branch."""
        platform = mock.patch.object(sys, "platform", "linux")
        real_open = builtins.open
        served = []

        def fake_open(file, *args, **kwargs):
            if str(file) == LINUX_BOOT_ID_PATH:
                if error is not None:
                    raise error
                served.append(contents[min(len(served), len(contents) - 1)])
                return io.StringIO(served[-1])
            return real_open(file, *args, **kwargs)

        patched_open = mock.patch("builtins.open", fake_open)
        with platform, patched_open:
            yield

    @contextlib.contextmanager
    def patched_darwin_boot_id(self, *payloads, **options):
        """Serve a fake ``libSystem.sysctlbyname`` on the darwin branch.

        The patch replaces ``ctypes.CDLL`` only, so the declarations, the buffer
        arithmetic and the UTF-8/format checks stay the real ones.  The cases run
        on every platform: nothing here reads this machine's boot session.
        """
        sysctlbyname = FakeSysctlbyname(*payloads, **options)

        def fake_cdll(name, *args, **kwargs):
            return FakeLibc(sysctlbyname)

        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(ctypes, "CDLL", fake_cdll):
            yield sysctlbyname

    def test_darwin_declares_the_sysctlbyname_prototype_and_returns_the_uuid(self):
        upper = LINUX_BOOT_ID_LEGAL.upper()
        with self.patched_darwin_boot_id(upper.encode("utf-8")) as sysctlbyname:
            self.assertEqual(boot_id(), upper)
        # The declared prototype is part of the contract: a size argument typed
        # as a plain int would truncate the reported length on a 64-bit ABI.
        self.assertEqual(sysctlbyname.argtypes, DARWIN_ARGUMENT_TYPES)
        self.assertIs(sysctlbyname.restype, ctypes.c_int)
        self.assertEqual([call[0] for call in sysctlbyname.calls], [b"kern.bootsessionuuid"] * 2)

    def test_darwin_branch_is_not_cached(self):
        second = "999e4567-e89b-12d3-a456-426614174000"
        with self.patched_darwin_boot_id(
            LINUX_BOOT_ID_LEGAL.encode("utf-8"), second.encode("utf-8")
        ):
            self.assertEqual(boot_id(), LINUX_BOOT_ID_LEGAL)
            self.assertEqual(boot_id(), second)

    def test_darwin_branch_rejects_a_failed_size_or_read_call(self):
        for label, options in {
            "size call fails": {"length_status": 1},
            "size call reports zero": {"reported_length": 0},
            "read call fails": {"value_status": 1},
        }.items():
            with self.subTest(label):
                with self.patched_darwin_boot_id(b"x" * 36, **options):
                    self.assert_rejected(boot_id, "E_RUNTIME_IDENTITY")

    def test_darwin_branch_rejects_illegal_content(self):
        legal = LINUX_BOOT_ID_LEGAL.encode("utf-8")
        for label, payload in {
            "not utf-8": b"\xff\xfe\xff\xfe\xff\xfe\xff\xfe",
            "not a uuid": b"not-a-boot-id",
            "empty payload": b"",
            "truncated uuid": legal[:-1],
            "prefixed uuid": b"boot_id=" + legal,
            "embedded nul": LINUX_BOOT_ID_LEGAL[:24].encode("utf-8") + b"\x00" + LINUX_BOOT_ID_LEGAL[24:].encode("utf-8"),
        }.items():
            with self.subTest(label):
                with self.patched_darwin_boot_id(payload):
                    self.assert_rejected(boot_id, "E_RUNTIME_IDENTITY")

    def test_darwin_branch_keeps_the_reported_text(self):
        # A legal value proves the baseline before every rejection above, and
        # the platform's own upper-case spelling is preserved verbatim.
        upper = LINUX_BOOT_ID_LEGAL.upper()
        with self.patched_darwin_boot_id(upper.encode("utf-8"), upper.encode("utf-8")):
            self.assertEqual(boot_id(), upper)
            self.assertRegex(boot_id(), BOOT_ID_TEXT)

    def test_linux_branch_returns_legal_boot_id_stripped_and_never_cached(self):
        second = "999e4567-e89b-12d3-a456-426614174000"
        with self.patched_linux_boot_id(LINUX_BOOT_ID_LEGAL + "\n", second + "\n"):
            self.assertEqual(boot_id(), LINUX_BOOT_ID_LEGAL)
            # The value is re-read on every call: a cached boot id would keep
            # "verifying" a process across a reboot.
            self.assertEqual(boot_id(), second)

    def test_linux_branch_rejects_illegal_content(self):
        for label, content in {
            "not a uuid": "not-a-boot-id\n",
            "empty file": "",
            "only whitespace": "   \n",
            "truncated uuid": LINUX_BOOT_ID_LEGAL[:-1] + "\n",
            "prefixed uuid": "boot_id=" + LINUX_BOOT_ID_LEGAL + "\n",
            "uuid with inner nuls": LINUX_BOOT_ID_LEGAL[:-1] + "\x00\n",
        }.items():
            with self.subTest(label):
                with self.patched_linux_boot_id(content):
                    self.assert_rejected(boot_id, "E_RUNTIME_IDENTITY")

    def test_linux_branch_rejects_unreadable_file(self):
        for label, error in {
            "missing file": OSError(errno.ENOENT, "No such file or directory"),
            "permission denied": OSError(errno.EACCES, "Permission denied"),
        }.items():
            with self.subTest(label):
                with self.patched_linux_boot_id(LINUX_BOOT_ID_LEGAL + "\n", error=error):
                    self.assert_rejected(boot_id, "E_RUNTIME_IDENTITY")

    def test_linux_branch_preserves_the_reported_text(self):
        upper = LINUX_BOOT_ID_LEGAL.upper()
        self.assertRegex(upper, BOOT_ID_TEXT)
        with self.patched_linux_boot_id(upper + "\n"):
            self.assertEqual(boot_id(), upper)

    def native_sysctl_boot_id(self):
        completed = subprocess.run(
            ["sysctl", "-n", "kern.bootsessionuuid"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        native = completed.stdout.strip()
        self.assertRegex(native, BOOT_ID_TEXT)
        return native


class ResolveExecutableTests(RejectionAssertions):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name
        self._other = tempfile.TemporaryDirectory()
        self.addCleanup(self._other.cleanup)
        self.other_directory = self._other.name
        self.environment = {"PATH": self.directory}

    def resolved(self, argv0, *, cwd=None):
        return resolve_executable(
            argv0,
            cwd=self.directory if cwd is None else cwd,
            environment=dict(self.environment),
        )

    def test_bare_name_resolves_only_through_environment_path(self):
        tool = script_executable(self.directory, "identity-tool")
        # The tool is reachable only through the explicit mapping: the parent
        # process PATH is patched to a directory without any executable.
        inherited = os.environ.get("PATH", "").split(os.pathsep)
        self.assertNotIn(str(self.directory), inherited)
        self.assertNotIn(
            os.path.realpath(self.directory), [os.path.realpath(entry) for entry in inherited]
        )
        with mock.patch.dict(os.environ, {"PATH": self.other_directory}):
            found = self.resolved("identity-tool")
        self.assertEqual(found, os.path.realpath(tool))
        self.assertTrue(os.path.isfile(found))

    def test_bare_name_without_a_usable_path_is_rejected(self):
        tool = script_executable(self.directory, "identity-tool")
        self.assertEqual(self.resolved("identity-tool"), os.path.realpath(tool))
        for label, environment in {
            "missing PATH key": {"LANG": "C"},
            "empty PATH": {"PATH": ""},
            "PATH without the tool": {"PATH": self.other_directory},
            "other keys only": {"LANG": "C", "HOME": "/work/tree"},
        }.items():
            with self.subTest(label):
                self.assert_rejected(
                    lambda environment=environment: resolve_executable(
                        "identity-tool", cwd=self.directory, environment=environment
                    ),
                    "E_RUNTIME_IDENTITY",
                )

    def test_unknown_bare_name_is_rejected(self):
        tool = script_executable(self.directory, "identity-tool")
        self.assertEqual(self.resolved("identity-tool"), os.path.realpath(tool))
        for name in ("identity-tools", "missing-binary", "identity_tool"):
            with self.subTest(name):
                self.assert_rejected(lambda name=name: self.resolved(name), "E_RUNTIME_IDENTITY")

    def test_non_executable_file_is_rejected(self):
        script_executable(self.directory, "plain-tool", mode=0o644)
        # A bare name lookup never returns a file it cannot execute...
        self.assert_rejected(lambda: self.resolved("plain-tool"), "E_RUNTIME_IDENTITY")
        # ...and reaching the same file through an explicit path fails the
        # executable-regular-file check itself.
        self.assert_rejected(lambda: self.resolved("./plain-tool"), "E_RUNTIME_IDENTITY")

    def test_explicit_paths_are_used_directly(self):
        nested = os.path.join(self.directory, "bin")
        os.mkdir(nested)
        tool = script_executable(nested, "nested-tool")
        self.assertEqual(self.resolved("bin/nested-tool"), os.path.realpath(tool))
        self.assertEqual(self.resolved("./bin/nested-tool"), os.path.realpath(tool))
        self.assertEqual(self.resolved(os.path.realpath(tool)), os.path.realpath(tool))

    def test_relative_path_is_resolved_against_cwd(self):
        tool = script_executable(self.other_directory, "cwd-tool")
        elsewhere = os.path.join(self.directory, "elsewhere")
        os.mkdir(elsewhere)
        self.assertEqual(
            resolve_executable("./cwd-tool", cwd=self.other_directory, environment={"LANG": "C"}),
            os.path.realpath(tool),
        )
        self.assert_rejected(
            lambda: resolve_executable("./cwd-tool", cwd=elsewhere, environment={"LANG": "C"}),
            "E_RUNTIME_IDENTITY",
        )

    def test_symlink_resolves_to_the_real_path(self):
        target = script_executable(self.directory, "target-tool")
        link = os.path.join(self.directory, "linked-tool")
        os.symlink(os.path.basename(target), link)
        self.assertEqual(self.resolved("linked-tool"), os.path.realpath(target))
        self.assertEqual(self.resolved("./linked-tool"), os.path.realpath(target))

    def test_directory_or_missing_path_is_rejected(self):
        os.mkdir(os.path.join(self.directory, "bin"))
        self.assert_rejected(lambda: self.resolved("./bin"), "E_RUNTIME_IDENTITY")
        self.assert_rejected(lambda: self.resolved("./missing-tool"), "E_RUNTIME_IDENTITY")

    def test_invalid_arguments_are_rejected(self):
        tool = script_executable(self.directory, "identity-tool")
        self.assertEqual(self.resolved("identity-tool"), os.path.realpath(tool))
        for label, argv0 in {"empty": "", "non text": b"identity-tool", "none": None}.items():
            with self.subTest(f"argv0 {label}"):
                self.assert_rejected(
                    lambda argv0=argv0: resolve_executable(
                        argv0, cwd=self.directory, environment=dict(self.environment)
                    ),
                    "E_RUNTIME_IDENTITY",
                )
        for label, cwd in {"empty": "", "non text": None}.items():
            with self.subTest(f"cwd {label}"):
                self.assert_rejected(
                    lambda cwd=cwd: resolve_executable(
                        "identity-tool", cwd=cwd, environment=dict(self.environment)
                    ),
                    "E_RUNTIME_IDENTITY",
                )
        for label, environment in {
            "non mapping": ["identity-tool"],
            "empty name": {"": "x"},
            "control character": {"LANG": "C\n"},
        }.items():
            with self.subTest(f"environment {label}"):
                self.assert_rejected(
                    lambda environment=environment: resolve_executable(
                        "identity-tool", cwd=self.directory, environment=environment
                    ),
                    "E_RUNTIME_ENVIRONMENT",
                )

    def test_path_entries_must_be_absolute(self):
        tool = script_executable(self.directory, "identity-tool")
        directory = self.directory
        self.assertEqual(self.resolved("identity-tool"), os.path.realpath(tool))
        # Several absolute entries are legal, in any order.
        self.assertEqual(
            resolve_executable("identity-tool", cwd=directory, environment={"PATH": "/usr/bin:" + directory}),
            os.path.realpath(tool),
        )
        for label, path in {
            "relative entry": "./bin:" + directory,
            "bare entry": "bin:" + directory,
            "tilde entry": "~/bin:" + directory,
            "empty entry in the middle": directory + "::/usr/bin",
            "empty entry in front": ":" + directory,
            "empty entry at the end": directory + ":",
            "entry is only empty": "",
            "one relative entry between absolute ones": "/usr/bin:bin:/bin",
        }.items():
            with self.subTest(label):
                self.assert_rejected(
                    lambda path=path: resolve_executable(
                        "identity-tool", cwd=directory, environment={"PATH": path}
                    ),
                    "E_RUNTIME_IDENTITY",
                )

    def test_bare_name_never_falls_back_to_the_parent_path(self):
        tool = script_executable(self.directory, "identity-tool")
        # The inherited PATH really does contain ``sh``: the only thing that can
        # keep the lookup from finding it is the missing explicit PATH.
        inherited = shutil.which("sh", path=os.environ.get("PATH", ""))
        self.assertTrue(inherited and os.path.isfile(inherited), inherited)
        with mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}):
            for label, environment in {
                "PATH key missing": {"LANG": "C"},
                "PATH empty": {"PATH": ""},
                "other keys only": {"LANG": "C", "HOME": "/work/tree"},
            }.items():
                with self.subTest(label):
                    self.assert_rejected(
                        lambda environment=environment: resolve_executable(
                            "sh", cwd=self.directory, environment=environment
                        ),
                        "E_RUNTIME_IDENTITY",
                    )
        self.assertEqual(self.resolved("identity-tool"), os.path.realpath(tool))

    def test_argv0_and_cwd_character_rules_are_rejected(self):
        tool = script_executable(self.directory, "identity-tool")
        self.assertEqual(self.resolved("identity-tool"), os.path.realpath(tool))
        for label, argv0 in {
            "nul": "identity-tool\x00",
            "control character": "identity\x01-tool",
            "newline": "identity-tool\n",
            "delete": "identity-tool\x7f",
            "lone high surrogate": "identity\ud800-tool",
            "lone low surrogate": "identity\udfff-tool",
            "str subclass": PathSubclass("identity-tool"),
        }.items():
            with self.subTest(f"argv0 {label}"):
                self.assert_rejected(
                    lambda argv0=argv0: resolve_executable(
                        argv0, cwd=self.directory, environment=dict(self.environment)
                    ),
                    "E_RUNTIME_IDENTITY",
                )
        for label, cwd in {
            "nul": self.directory + "\x00",
            "control character": self.directory + "\x01",
            "newline": self.directory + "\n",
            "lone high surrogate": self.directory + "\ud800",
            "str subclass": PathSubclass(self.directory),
        }.items():
            with self.subTest(f"cwd {label}"):
                self.assert_rejected(
                    lambda cwd=cwd: resolve_executable(
                        "identity-tool", cwd=cwd, environment=dict(self.environment)
                    ),
                    "E_RUNTIME_IDENTITY",
                )

    def test_platform_path_errors_map_to_identity_errors(self):
        tool = script_executable(self.directory, "identity-tool")
        self.assertEqual(self.resolved("./identity-tool"), os.path.realpath(tool))
        # ``./identity-tool`` exercises every path operation the explicit-path
        # branch uses: join, isabs, realpath, isfile and access.
        operations = {
            "os.path.join": (os.path, "join"),
            "os.path.isabs": (os.path, "isabs"),
            "os.path.realpath": (os.path, "realpath"),
            "os.path.isfile": (os.path, "isfile"),
            "os.access": (os, "access"),
        }
        for label, (owner, attribute) in operations.items():
            for failure in (OSError, ValueError, UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates")):
                with self.subTest(operation=label, error=type(failure).__name__):
                    with mock.patch.object(owner, attribute, side_effect=failure):
                        self.assert_rejected(
                            lambda: resolve_executable(
                                "./identity-tool", cwd=self.directory, environment=dict(self.environment)
                            ),
                            "E_RUNTIME_IDENTITY",
                        )
        self.assertEqual(self.resolved("./identity-tool"), os.path.realpath(tool))

    def test_shutil_which_errors_map_to_identity_errors(self):
        tool = script_executable(self.directory, "identity-tool")
        self.assertEqual(self.resolved("identity-tool"), os.path.realpath(tool))
        with mock.patch.object(shutil, "which", side_effect=ValueError):
            self.assert_rejected(lambda: self.resolved("identity-tool"), "E_RUNTIME_IDENTITY")
        self.assertEqual(self.resolved("identity-tool"), os.path.realpath(tool))


class FileSha256Tests(RejectionAssertions):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = self._directory.name

    def write(self, name, payload):
        path = os.path.join(self.directory, name)
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    def test_equals_hashlib_digest(self):
        for label, payload in {
            "empty": b"",
            "text": b"identity payload\n",
            "binary": bytes(range(256)) * 7,
        }.items():
            with self.subTest(label):
                path = self.write(label.replace(" ", "-"), payload)
                self.assertEqual(file_sha256(path), hashlib.sha256(payload).hexdigest())

    def test_reads_in_bounded_chunks(self):
        payload = os.urandom((1 << 20) + 1024)
        path = self.write("large-payload", payload)
        sizes = []
        real_open = builtins.open

        class ReadSpy:
            """Record every read size the implementation asks for."""

            def __init__(self, handle):
                self._handle = handle

            def read(self, size=-1):
                sizes.append(size)
                return self._handle.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()
                return False

        def spying_open(file, *args, **kwargs):
            handle = real_open(file, *args, **kwargs)
            return ReadSpy(handle) if str(file) == path else handle

        with mock.patch("builtins.open", spying_open):
            digest = file_sha256(path)
        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
        # A multi-megabyte file is read in more than one bounded request, never
        # as a single unbounded ``read()``.
        self.assertGreater(len(sizes), 1)
        self.assertTrue(all(isinstance(size, int) and size > 0 for size in sizes), sizes)

    @unittest.skipIf(os.name == "nt", "POSIX device nodes only")
    def test_device_nodes_are_not_regular_files(self):
        # /dev/null is a readable character device, not a regular file: hashing
        # it would report the digest of an empty byte stream instead of failing.
        # (/dev/zero must not be probed here: without the regular-file gate its
        # endless stream of zeros would block the read loop.)
        self.assertFalse(os.path.isfile("/dev/null"))
        self.assert_rejected(lambda: file_sha256("/dev/null"), "E_RUNTIME_IDENTITY")

    def test_directories_and_missing_files_are_rejected(self):
        directory = os.path.join(self.directory, "a-directory")
        os.mkdir(directory)
        path = self.write("payload", b"identity payload\n")
        self.assertEqual(file_sha256(path), hashlib.sha256(b"identity payload\n").hexdigest())
        for label, target in {
            "directory": directory,
            "missing file": os.path.join(self.directory, "missing"),
            "root directory": os.sep,
        }.items():
            with self.subTest(label):
                self.assert_rejected(lambda target=target: file_sha256(target), "E_RUNTIME_IDENTITY")

    def test_non_text_path_is_rejected_with_a_runtime_error_code(self):
        path = self.write("payload", b"identity payload\n")
        self.assertEqual(file_sha256(path), hashlib.sha256(b"identity payload\n").hexdigest())
        for label, target in {"none": None, "integer": 17}.items():
            with self.subTest(label):
                with self.assertRaises(RuntimeErrorCode) as caught:
                    file_sha256(target)
                self.assertEqual(caught.exception.code, "E_RUNTIME_IDENTITY")

    def test_non_str_path_is_rejected_before_any_os_call(self):
        path = self.write("payload", b"identity payload\n")
        self.assertEqual(file_sha256(path), hashlib.sha256(b"identity payload\n").hexdigest())
        for label, target in {
            "boolean": True,
            "bytes": b"payload",
            "pathlib Path": pathlib.Path(path),
            "os.PathLike object": FakePathLike(path),
            "str subclass": PathSubclass(path),
        }.items():
            with self.subTest(label):
                self.assert_rejected(lambda target=target: file_sha256(target), "E_RUNTIME_IDENTITY")
        # The legal baseline still hashes after every rejection above.
        self.assertEqual(file_sha256(path), hashlib.sha256(b"identity payload\n").hexdigest())

    def test_open_descriptor_is_not_a_path_and_stays_open(self):
        path = self.write("payload", b"identity payload\n")
        self.assertEqual(file_sha256(path), hashlib.sha256(b"identity payload\n").hexdigest())
        with tempfile.TemporaryFile() as handle:
            handle.write(b"descriptor bytes")
            handle.flush()
            handle.seek(0)
            descriptor = handle.fileno()
            offset_before = os.lseek(descriptor, 0, os.SEEK_CUR)
            self.assert_rejected(lambda: file_sha256(descriptor), "E_RUNTIME_IDENTITY")
            # Still open, and not read: a path that is really a descriptor must
            # never be consumed, hashed or closed by the identity capture.
            os.fstat(descriptor)
            self.assertEqual(os.lseek(descriptor, 0, os.SEEK_CUR), offset_before)
            self.assertEqual(offset_before, 0)
            self.assertEqual(handle.read(), b"descriptor bytes")

    def test_paths_with_control_characters_or_surrogates_are_rejected(self):
        path = self.write("payload", b"identity payload\n")
        self.assertEqual(file_sha256(path), hashlib.sha256(b"identity payload\n").hexdigest())
        for label, target in {
            "empty": "",
            "nul in the middle": path + "\x00",
            "newline": path + "\n",
            "delete": path + "\x7f",
            "lone high surrogate": path + "\ud800",
            "lone low surrogate": "\udfff" + path,
        }.items():
            with self.subTest(label):
                self.assert_rejected(lambda target=target: file_sha256(target), "E_RUNTIME_IDENTITY")
        self.assertEqual(file_sha256(path), hashlib.sha256(b"identity payload\n").hexdigest())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable_file_is_rejected(self):
        path = self.write("unreadable", b"identity payload\n")
        self.assertEqual(file_sha256(path), hashlib.sha256(b"identity payload\n").hexdigest())
        os.chmod(path, 0o000)
        self.addCleanup(os.chmod, path, 0o644)
        self.assert_rejected(lambda: file_sha256(path), "E_RUNTIME_IDENTITY")
        os.chmod(path, 0o644)
        self.assertEqual(file_sha256(path), hashlib.sha256(b"identity payload\n").hexdigest())


class CaptureProcessIdentityTests(RejectionAssertions):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.executable = script_executable(self._directory.name, "identity-tool")

    def test_assembles_the_four_canonical_fields(self):
        identity = capture_process_identity(
            os.getpid(), executable=os.path.realpath(self.executable), worktree_id="wt-1"
        )
        with open(self.executable, "rb") as handle:
            expected_digest = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(identity.pid, os.getpid())
        self.assertEqual(identity.worktree_id, "wt-1")
        self.assertRegex(identity.boot_id, BOOT_ID_TEXT)
        self.assertEqual(identity.executable_sha256, expected_digest)
        self.assertEqual(
            identity.to_dict(),
            {
                "pid": os.getpid(),
                "boot_id": identity.boot_id,
                "executable_sha256": expected_digest,
                "worktree_id": "wt-1",
            },
        )

    def test_interpreter_identity_asserts_every_canonical_field(self):
        # The live interpreter is a real executable regular file, so all four
        # fields can be asserted without any fake: the captured boot id equals
        # a fresh boot_id() read and the digest equals a fresh file_sha256.
        executable = os.path.realpath(sys.executable)
        identity = capture_process_identity(os.getpid(), executable=executable, worktree_id="wt-1")
        self.assertEqual(identity.pid, os.getpid())
        self.assertEqual(identity.boot_id, boot_id())
        self.assertEqual(identity.worktree_id, "wt-1")
        self.assertEqual(identity.executable_sha256, file_sha256(executable))
        self.assertEqual(
            identity.to_dict(),
            {
                "pid": os.getpid(),
                "boot_id": identity.boot_id,
                "executable_sha256": identity.executable_sha256,
                "worktree_id": "wt-1",
            },
        )

    @unittest.skipUnless(sys.platform == "darwin", "native macOS boot session evidence only")
    def test_native_boot_id_and_interpreter_digest(self):
        native = subprocess.run(
            ["sysctl", "-n", "kern.bootsessionuuid"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout.strip()
        interpreter = os.path.realpath(sys.executable)
        identity = capture_process_identity(
            os.getpid(), executable=interpreter, worktree_id="wt-native"
        )
        with open(interpreter, "rb") as handle:
            expected_digest = hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(identity.boot_id, native)
        self.assertEqual(identity.executable_sha256, expected_digest)

    def test_identity_field_validation_reaches_the_caller_unchanged(self):
        for label, pid in {"zero": 0, "negative": -1}.items():
            with self.subTest(pid=label):
                with self.assertRaises(RuntimeErrorCode) as caught:
                    capture_process_identity(pid, executable=self.executable, worktree_id="wt-1")
                self.assertEqual(caught.exception.code, "E_RUNTIME_PID")
        for label, worktree_id in {"empty": "", "non text": 7}.items():
            with self.subTest(worktree_id=label):
                with self.assertRaises(RuntimeErrorCode) as caught:
                    capture_process_identity(
                        os.getpid(), executable=self.executable, worktree_id=worktree_id
                    )
                self.assertEqual(caught.exception.code, "E_RUNTIME_IDENTITY")

    def test_unreadable_executable_is_rejected(self):
        missing = os.path.join(self._directory.name, "missing")
        with self.assertRaises(RuntimeErrorCode) as caught:
            capture_process_identity(os.getpid(), executable=missing, worktree_id="wt-1")
        self.assertEqual(caught.exception.code, "E_RUNTIME_IDENTITY")


class ImportSideEffectTests(unittest.TestCase):
    def run_probe(self, code):
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": REPO_ROOT},
            check=False,
        )

    def test_import_does_not_import_ctypes(self):
        completed = self.run_probe(
            "import sys; import sentinelmesh.process_identity as module; "
            "assert 'ctypes' not in sys.modules, sorted("
            "name for name in sys.modules if name.startswith('ctypes')); "
            "print('IMPORT_SAFE_OK')"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("IMPORT_SAFE_OK", completed.stdout)

    def test_import_digests_without_touching_the_parent_environment(self):
        completed = self.run_probe(
            "import sentinelmesh.process_identity as module; "
            "print(module.environment_digest({}))"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(SHA_ENV_EMPTY, completed.stdout)


if __name__ == "__main__":
    unittest.main()
