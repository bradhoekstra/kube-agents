"""fleet_scope_args: the flags that carry the declared scope into a collector.

Run: python3 -m unittest discover -s agents/platform/scripts -p 'test_fleet_scope_args.py' -v
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fleet_scope_args as fsa  # noqa: E402


class FleetScopeArgsTest(unittest.TestCase):
    def test_the_two_flags_parse_as_the_tool_spells_them(self):
        parser = argparse.ArgumentParser()
        fsa.add_scope_arguments(parser)
        args = parser.parse_args(["--scope-projects", "ops-mgmt,payments-prod", "--scope-unread", "payments-staging=denied"])
        self.assertEqual(fsa.parse_scope_projects(args.scope_projects), ["ops-mgmt", "payments-prod"])
        self.assertEqual(fsa.parse_scope_unread(args.scope_unread), [("payments-staging", "denied")])
        self.assertEqual((args.scope_projects, args.scope_unread), ("ops-mgmt,payments-prod", "payments-staging=denied"))
        self.assertEqual(parser.parse_args([]).scope_projects, None)

    def test_separators_and_a_missing_outcome(self):
        self.assertEqual(fsa.parse_scope_projects(" a, b  c\n"), ["a", "b", "c"])
        self.assertEqual(fsa.parse_scope_projects(None), [])
        self.assertEqual(fsa.parse_scope_unread("p=denied q"), [("p", "denied"), ("q", "unknown")])

    def test_the_holder_records_the_flags_and_reads_back_as_the_resolver_does(self):
        scope = fsa.DeclaredScope()
        self.assertIsNone(scope.projects)
        scope.set("a,b", "p=denied")
        self.assertEqual((scope.projects, scope.unread), (["a", "b"], [("p", "denied")]))
        self.assertIn("p (denied)", scope.note())
        scope.set("", None)
        self.assertEqual((scope.projects, scope.note()), (None, None))

    def test_the_note_names_each_unread_project(self):
        self.assertIsNone(fsa.unread_note([]))
        note = fsa.unread_note([("p", "denied"), ("q", "over-cap")])
        self.assertIn("2 project(s)", note)
        self.assertIn("p (denied), q (over-cap)", note)
        self.assertIn("fleet_scope tool", note)


if __name__ == "__main__":
    unittest.main()
