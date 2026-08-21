#!/usr/bin/env python3
"""Unit tests for the always-loaded instruction files' context budget.

Run: cd scripts && python3 -m unittest test_context_budget

This guard fails *green* in the way that matters: if ``is_import`` stops
recognising CLAUDE.md's ``@AGENTS.md`` line, the total silently doubles
AGENTS.md and the check starts failing for a reason that has nothing to do with
anyone's pull request -- and the obvious fix, raising BUDGET, hides the real
size. The import handling is therefore tested rather than trusted.

The budget assertion at the end is the check itself, run against the real
files, so ``python3 -m unittest`` catches an over-budget tree even where the
Makefile target is not wired in.
"""

import contextlib
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import check_context_budget


def run_main() -> tuple[int, str]:
    """`main()`'s exit code and what it printed, with stdout kept out of the log."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = check_context_budget.main()
    return code, out.getvalue()


class IsImportTest(unittest.TestCase):
    """`is_import` -- what gets excluded from the char count."""

    def test_bare_import_directive(self):
        self.assertTrue(check_context_budget.is_import("@AGENTS.md\n"))

    def test_indented_import_directive(self):
        self.assertTrue(check_context_budget.is_import("  @AGENTS.md  \n"))

    def test_prose_mentioning_an_at_sign_is_content(self):
        self.assertFalse(check_context_budget.is_import("@AGENTS.md is the entry point\n"))

    def test_email_style_handle_alone_is_not_a_path(self):
        # `@me` has no space either, so only the path shape tells the two apart.
        # It is charged as content, which is the safe direction -- over-counting
        # fails loudly, under-counting hides growth.
        self.assertFalse(check_context_budget.is_import("@me\n"))

    def test_handle_with_a_hyphen_is_not_a_path(self):
        self.assertFalse(check_context_budget.is_import("@platform-agent\n"))

    def test_tab_separated_trailer_is_not_an_import(self):
        self.assertFalse(check_context_budget.is_import("@AGENTS.md\tsee also\n"))

    def test_bare_at_is_not_an_import(self):
        self.assertFalse(check_context_budget.is_import("@\n"))

    def test_ordinary_line(self):
        self.assertFalse(check_context_budget.is_import("- Keep changes scoped.\n"))


class LoadedSizeTest(unittest.TestCase):
    """`loaded_size` -- the import line is not charged to the importing file."""

    def test_import_line_is_not_counted(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "CLAUDE.md"
            path.write_text("@AGENTS.md\nrule\n", encoding="utf-8")
            self.assertEqual(check_context_budget.loaded_size(path), len("rule\n"))

    def test_content_is_counted_whole(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "AGENTS.md"
            path.write_text("# Title\n\nbody\n", encoding="utf-8")
            self.assertEqual(check_context_budget.loaded_size(path), len("# Title\n\nbody\n"))


class RealFilesTest(unittest.TestCase):
    """The repository's own files are inside the budget."""

    def test_within_budget(self):
        total = sum(
            check_context_budget.loaded_size(check_context_budget.REPO / name)
            for name in check_context_budget.FILES
        )
        self.assertLessEqual(
            total,
            check_context_budget.BUDGET,
            f"{total} chars across {check_context_budget.FILES} exceeds the "
            f"{check_context_budget.BUDGET}-char budget; see the module docstring "
            "in check_context_budget.py for what to do about it",
        )

    def test_check_passes(self):
        code, output = run_main()
        self.assertEqual(code, 0)
        self.assertIn("under the", output)


class FailurePathTest(unittest.TestCase):
    """`main()` reports failure loudly.

    Without these, an inverted comparison or a dropped ``return 1`` leaves a
    gate that passes on every input -- and every other test in this file still
    goes green, because they exercise the classifier rather than the verdict.
    """

    def test_over_budget_fails(self):
        with mock.patch.object(check_context_budget, "BUDGET", 100):
            code, output = run_main()
        self.assertEqual(code, 1)
        self.assertIn("FAIL", output)
        # The remedy is in the message, not just the number: a gate that says
        # only "too big" gets answered by deleting a rule.
        self.assertIn("docs/pull-request-workflow.md", output)

    def test_small_overage_is_not_reported_as_zero(self):
        real = sum(
            check_context_budget.loaded_size(check_context_budget.REPO / name)
            for name in check_context_budget.FILES
        )
        with mock.patch.object(check_context_budget, "BUDGET", real - 200):
            code, output = run_main()
        self.assertEqual(code, 1)
        self.assertIn("200 over", output)

    def test_missing_file_fails(self):
        with mock.patch.object(check_context_budget, "FILES", ("NOT_A_REAL_FILE.md",)):
            code, output = run_main()
        self.assertEqual(code, 1)
        self.assertIn("MISSING", output)


if __name__ == "__main__":
    unittest.main()
