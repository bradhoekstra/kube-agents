"""The audit file: where it is, one object per line, created on demand, rotated at the cap."""

import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import audit_sink  # noqa: E402

LOGGER = logging.getLogger("test.audit_sink")


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

    @staticmethod
    def _restore_hermes_constants(module):
        sys.modules.pop("hermes_constants", None)
        if module is not None:
            sys.modules["hermes_constants"] = module

    def lines(self):
        return self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []


class TestWhereTheFileIs(SinkTestCase):

    def test_under_the_profile_logs_directory(self):
        self.assertEqual(audit_sink.audit_file_path(), self.path)
        self.assertEqual(audit_sink.audit_file_path(Path("/opt/data/profiles/platform")),
                         Path("/opt/data/profiles/platform/logs/audit.jsonl"))

    def test_hermes_names_the_home_when_the_process_is_hermes(self):
        # Inside Hermes, get_hermes_home() carries the profile the gateway is
        # serving a turn for, which HERMES_HOME alone does not.
        served = self.home / "profiles" / "platform"
        sys.modules["hermes_constants"] = types.SimpleNamespace(get_hermes_home=lambda: served)
        self.assertEqual(audit_sink.audit_file_path(), served / "logs" / "audit.jsonl")

    def test_a_failing_hermes_lookup_falls_back_to_the_environment(self):
        def broken():
            raise RuntimeError("no profile")

        sys.modules["hermes_constants"] = types.SimpleNamespace(get_hermes_home=broken)
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

    def test_the_file_is_owner_writable_and_group_readable_only(self):
        previous = os.umask(0o002)
        self.addCleanup(os.umask, previous)
        audit_sink.append_line("{}")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)

    def test_an_unencodable_value_is_rendered_not_refused(self):
        line = audit_sink.serialize({"obj": object()})
        self.assertIn("<object object at", json.loads(line)["obj"])

    def test_emit_writes_the_record_and_nothing_to_the_logger(self):
        with self.assertNoLogs(LOGGER, level="INFO"):
            audit_sink.emit({"audit_event": "x", "n": 1}, LOGGER)
        self.assertEqual([json.loads(line) for line in self.lines()], [{"audit_event": "x", "n": 1}])

    def test_emit_hands_the_record_to_the_logger_when_the_file_cannot_be_written(self):
        self.home.joinpath("logs").write_text("not a directory")
        with self.assertLogs(LOGGER, level="ERROR") as captured:
            audit_sink.emit({"audit_event": "x"}, LOGGER)
        line = captured.output[0]
        self.assertIn(str(self.path), line)
        # The record is the tail of the line, which is the shape the Admin
        # Console's wrapped-record reader expects.
        self.assertTrue(line.endswith('record: {"audit_event": "x"}'), line)


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

    def test_a_rotation_lost_to_another_writer_does_not_lose_the_record(self):
        audit_sink.append_line(self.LINE)
        with mock.patch.object(audit_sink.os, "replace", side_effect=FileNotFoundError("gone")):
            audit_sink.append_line(self.LINE + "2")
        self.assertEqual(self.lines(), [self.LINE, self.LINE + "2"])


if __name__ == "__main__":
    unittest.main()
