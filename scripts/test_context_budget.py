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

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import check_context_budget


class IsImportTest(unittest.TestCase):
    """`is_import` -- what gets excluded from the char count."""

    def test_bare_import_directive(self):
        self.assertTrue(check_context_budget.is_import("@AGENTS.md\n"))

    def test_indented_import_directive(self):
        self.assertTrue(check_context_budget.is_import("  @AGENTS.md  \n"))

    def test_prose_mentioning_an_at_sign_is_content(self):
        self.assertFalse(check_context_budget.is_import("@AGENTS.md is the entry point\n"))

    def test_email_style_handle_alone_is_not_a_path(self):
        # `@me` has no space either; it is charged as content, which is the safe
        # direction -- over-counting fails loudly, under-counting hides growth.
        self.assertTrue(check_context_budget.is_import("@me\n"))

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
        self.assertEqual(check_context_budget.main(), 0)


if __name__ == "__main__":
    unittest.main()
