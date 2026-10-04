"""The audit file: where it is, one object per line, created on demand, locked, rotated at the cap,
and where a record goes when the file cannot take it."""

import contextlib
import fcntl
import fnmatch
import io
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import audit_sink  # noqa: E402

LOGGER = logging.getLogger("test.audit_sink")
# The two globs the sidecar tails, as basenames (buildFluentBitConfigMap).
SIDECAR_GLOBS = ("audit.jsonl", "*.log")


class SinkTestCase(unittest.TestCase):

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {audit_sink.HERMES_HOME_ENV: str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        # These tests do not run inside Hermes; nothing may answer for it.
        self.addCleanup(self._restore_hermes_constants, sys.modules.pop("hermes_constants", None))
        self.path = self.home / "logs" / "audit.jsonl"
        self.lock = self.home / "logs" / "audit.jsonl.lock"

    @staticmethod
    def _restore_hermes_constants(module):
        sys.modules.pop("hermes_constants", None)
        if module is not None:
            sys.modules["hermes_constants"] = module

    def fake_hermes(self, **attributes):
        sys.modules["hermes_constants"] = types.SimpleNamespace(**attributes)

    def lines(self):
        return self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []


class TestWhereTheFileIs(SinkTestCase):

    def test_under_the_profile_logs_directory(self):
        self.assertEqual(audit_sink.audit_file_path(), self.path)
        self.assertEqual(audit_sink.audit_file_path(Path("/opt/data/profiles/platform")),
                         Path("/opt/data/profiles/platform/logs/audit.jsonl"))
        self.assertEqual(audit_sink.lock_file_path(self.path), self.lock)

    def test_hermes_names_the_home_when_the_process_is_hermes(self):
        # Inside Hermes, get_hermes_home() carries the profile the gateway is
        # serving a turn for, which HERMES_HOME alone does not.
        served = self.home / "profiles" / "platform"
        self.fake_hermes(get_hermes_home=lambda: served)
        self.assertEqual(audit_sink.audit_file_path(), served / "logs" / "audit.jsonl")

    def test_a_failing_hermes_lookup_falls_back_to_the_environment(self):
        def broken():
            raise RuntimeError("no profile")

        self.fake_hermes(get_hermes_home=broken)
        self.assertEqual(audit_sink.audit_file_path(), self.path)

    def test_without_either_the_image_default_applies(self):
        with mock.patch.dict(os.environ, {audit_sink.HERMES_HOME_ENV: ""}):
            self.assertEqual(audit_sink.audit_file_path(), Path("/opt/data/logs/audit.jsonl"))


class TestAppending(SinkTestCase):

    def test_one_object_per_line_in_a_directory_created_on_demand(self):
        self.assertFalse(self.path.parent.exists())
        audit_sink.append_line(audit_sink.serialize({"b": 1, "a": "x\ny"}))
        audit_sink.append_line(audit_sink.serialize({"audit_event": "e"}))
        raw = self.path.read_text(encoding="utf-8")
        self.assertEqual(raw.count("\n"), 2)
        self.assertTrue(raw.endswith("\n"))
        first, second = raw.splitlines()
        self.assertEqual(first, '{"a": "x\\ny", "b": 1}')
        self.assertEqual(json.loads(first), {"a": "x\ny", "b": 1})
        self.assertEqual(json.loads(second), {"audit_event": "e"})

    def test_the_files_are_owner_writable_and_group_readable_only(self):
        previous = os.umask(0o002)
        self.addCleanup(os.umask, previous)
        audit_sink.append_line("{}")
        for path in (self.path, self.lock):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640, path)

    def test_the_lock_file_sits_beside_the_audit_file_outside_the_sidecar_globs(self):
        audit_sink.append_line("{}")
        self.assertTrue(self.lock.is_file())
        for pattern in SIDECAR_GLOBS:
            self.assertFalse(fnmatch.fnmatch(self.lock.name, pattern), pattern)
        self.assertTrue(fnmatch.fnmatch(self.path.name, SIDECAR_GLOBS[0]))

    def test_a_write_waits_for_the_lock_another_writer_holds(self):
        # Another process mid-rotation: it holds the lock, and this write has
        # to wait for it rather than race it.
        self.path.parent.mkdir(parents=True)
        holder = os.open(self.lock, os.O_WRONLY | os.O_CREAT, 0o640)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        writer = threading.Thread(target=audit_sink.append_line, args=("{}",))
        writer.start()
        time.sleep(0.2)
        self.assertEqual(self.lines(), [], "the write went ahead while another writer held the lock")
        fcntl.flock(holder, fcntl.LOCK_UN)
        writer.join(timeout=5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(self.lines(), ["{}"])

    def test_hermes_makes_the_directory_when_the_process_is_hermes(self):
        made = []

        def mkdir_under_hermes_home(directory):
            made.append(Path(directory))
            Path(directory).mkdir(parents=True, exist_ok=True)
            return Path(directory)

        self.fake_hermes(get_hermes_home=lambda: self.home, mkdir_under_hermes_home=mkdir_under_hermes_home)
        audit_sink.append_line("{}")
        self.assertEqual(made, [self.path.parent])
        self.assertEqual(self.lines(), ["{}"])

    def test_an_unencodable_value_is_rendered_not_refused(self):
        line = audit_sink.serialize({"obj": object()})
        self.assertIn("<object object at", json.loads(line)["obj"])


class TestEmit(SinkTestCase):

    def emit(self, record):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            audit_sink.emit(record, LOGGER)
        return out.getvalue()

    def test_the_record_goes_to_the_file_and_nowhere_else(self):
        with self.assertNoLogs(LOGGER, level="INFO"):
            printed = self.emit({"audit_event": "x", "n": 1})
        self.assertEqual(printed, "")
        self.assertEqual([json.loads(line) for line in self.lines()], [{"audit_event": "x", "n": 1}])

    def test_a_record_the_file_cannot_take_is_printed_to_stdout(self):
        self.home.joinpath("logs").write_text("not a directory")
        with self.assertLogs(LOGGER, level="ERROR") as captured:
            printed = self.emit({"audit_event": "x", "tool": "Bash"})
        # The record, whole, as the one line stdout receives.
        self.assertEqual(printed, '{"audit_event": "x", "tool": "Bash"}\n')
        # The notice names the file and the error and nothing of the record,
        # so the console's text-form query does not count it.
        notice = captured.output[0]
        self.assertIn(str(self.path), notice)
        self.assertIn("Not a directory", notice)
        self.assertNotIn("audit_event", notice)
        self.assertNotIn("Bash", notice)

    def test_a_directory_hermes_refuses_sends_the_record_to_stdout(self):
        # What mkdir_under_hermes_home raises for a missing or tombstoned
        # named profile (hermes_constants.assert_named_profile_home_live).
        def refuse(directory):
            raise FileNotFoundError(f"Named profile home does not exist: {directory}")

        self.fake_hermes(get_hermes_home=lambda: self.home, mkdir_under_hermes_home=refuse)
        with self.assertLogs(LOGGER, level="ERROR") as captured:
            printed = self.emit({"audit_event": "x"})
        self.assertEqual(printed, '{"audit_event": "x"}\n')
        self.assertIn("Named profile home does not exist", captured.output[0])
        self.assertNotIn("audit_event", captured.output[0])
        self.assertFalse(self.path.parent.exists(), "the sink made the directory Hermes refused")

    def test_a_record_lost_to_both_is_said_so(self):
        self.home.joinpath("logs").write_text("not a directory")

        class Refusing(io.StringIO):
            def write(self, text):
                raise OSError("stdout is closed")

        with contextlib.redirect_stdout(Refusing()), self.assertLogs(LOGGER, level="ERROR") as captured:
            audit_sink.emit({"audit_event": "x"}, LOGGER)
        self.assertIn("the record is lost", captured.output[0])
        self.assertNotIn("audit_event", captured.output[0])


class TestRotation(SinkTestCase):

    LINE = "x" * 40  # 41 bytes with its newline

    def setUp(self):
        super().setUp()
        cap = mock.patch.object(audit_sink, "AUDIT_FILE_MAX_BYTES", 64)
        cap.start()
        self.addCleanup(cap.stop)

    def rotated(self, index):
        return self.path.with_name(f"{self.path.name}.{index}")

    def test_the_file_is_rotated_at_the_cap_and_the_backups_are_bounded(self):
        audit_sink.append_line(self.LINE)
        self.assertFalse(self.rotated(1).exists())
        # A second line would pass the cap, so the first is moved aside first.
        audit_sink.append_line(self.LINE + "2")
        self.assertEqual(self.lines(), [self.LINE + "2"])
        self.assertEqual(self.rotated(1).read_text(encoding="utf-8"), self.LINE + "\n")
        for suffix in ("3", "4", "5"):
            audit_sink.append_line(self.LINE + suffix)
        self.assertEqual(self.lines(), [self.LINE + "5"])
        self.assertEqual(self.rotated(1).read_text(encoding="utf-8"), self.LINE + "4\n")
        self.assertEqual(self.rotated(2).read_text(encoding="utf-8"), self.LINE + "3\n")
        self.assertEqual(self.rotated(3).read_text(encoding="utf-8"), self.LINE + "2\n")
        self.assertFalse(self.rotated(4).exists(), "more backups than AUDIT_FILE_BACKUP_COUNT")

    def test_a_single_line_over_the_cap_is_still_written(self):
        audit_sink.append_line("y" * 100)
        self.assertEqual(self.lines(), ["y" * 100])
        self.assertFalse(self.rotated(1).exists())

    def test_a_backup_that_cannot_be_moved_does_not_cost_the_record(self):
        audit_sink.append_line(self.LINE)
        with mock.patch.object(audit_sink.os, "replace", side_effect=PermissionError("read-only")):
            audit_sink.append_line(self.LINE + "2")
        self.assertEqual(self.lines(), [self.LINE, self.LINE + "2"])


if __name__ == "__main__":
    unittest.main()
