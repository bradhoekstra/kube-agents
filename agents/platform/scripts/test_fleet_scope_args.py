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
        self.assertFalse(scope.declared)
        self.assertIsNone(scope.projects)
        scope.set("a,b", "p=denied")
        self.assertTrue(scope.declared)
        self.assertEqual((scope.projects, scope.unread), (["a", "b"], [("p", "denied")]))
        self.assertIn("p (denied)", scope.note())
        scope.set(None, None)
        self.assertEqual((scope.declared, scope.projects, scope.note()), (False, None, None))

    def test_a_declared_scope_with_nothing_readable_is_declared_and_empty_not_absent(self):
        # `--scope-unread` alone, or a blank `--scope-projects`, is a boundary the
        # install could read nothing inside: the collector reports that and
        # must not fall through to the listing.
        scope = fsa.DeclaredScope()
        scope.set(None, "p=denied,q=unreachable")
        self.assertEqual((scope.declared, scope.projects), (True, []))
        self.assertIn("no project this install could read", scope.empty_error())
        self.assertIn("p (denied), q (unreachable)", scope.empty_error())
        scope.set("", None)
        self.assertEqual((scope.declared, scope.projects), (True, []))
        self.assertNotIn("(", scope.empty_error().split("this run")[1][:2])

    def test_a_repeated_project_id_is_swept_once(self):
        self.assertEqual(fsa.parse_scope_projects("ops-mgmt,payments-prod,ops-mgmt"), ["ops-mgmt", "payments-prod"])

    def test_the_note_names_each_unread_project(self):
        self.assertIsNone(fsa.unread_note([]))
        note = fsa.unread_note([("p", "denied"), ("q", "over-cap")])
        self.assertIn("2 project(s)", note)
        self.assertIn("p (denied), q (over-cap)", note)
        self.assertIn("fleet_scope tool", note)


if __name__ == "__main__":
    unittest.main()
