"""Unit tests for the direct_agent_routing pre_gateway_dispatch hook.

Run: python3 -m unittest agents/chat/defaults/plugins/direct_agent_routing/test_plugin.py

The roster script is loaded by path at runtime and is not importable here, so
the tests patch ``resolve_profile``'s dependency — ``_load_roster_module`` — with
a stand-in exposing the same ``discover()`` contract: a list of
``{"name", "responsibilities"}`` dicts, or ``None`` when the profiles directory
could not be read at all.
"""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import plugin  # noqa: E402

ROSTER = ["platform", "cluster-prod", "cluster-staging"]


def fake_roster(names):
    """A stand-in for the agent_roster module. ``names=None`` means discovery failed."""
    def discover(base=None):
        if names is None:
            return None
        return [{"name": n, "responsibilities": ""} for n in names]

    return SimpleNamespace(discover=discover)


def event(text, profile=None):
    return SimpleNamespace(text=text, source=SimpleNamespace(profile=profile))


class RouteTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(plugin, "_load_roster_module", return_value=fake_roster(ROSTER))
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_platform_prefix_routes_and_strips(self):
        self.assertEqual(plugin.route("/platform scale the frontend"), ("platform", "scale the frontend"))

    def test_any_roster_name_routes(self):
        self.assertEqual(plugin.route("/cluster-prod why is the pod crashlooping?"),
                         ("cluster-prod", "why is the pod crashlooping?"))

    def test_leading_bot_mention_is_stripped(self):
        self.assertEqual(plugin.route("<@U0BKNNDJERG> /platform scale it"), ("platform", "scale it"))

    def test_multi_line_body_survives(self):
        self.assertEqual(
            plugin.route("/platform apply this:\n\nkind: Deployment\n"),
            ("platform", "apply this:\n\nkind: Deployment"),
        )

    def test_name_is_matched_case_insensitively_to_the_roster_spelling(self):
        self.assertEqual(plugin.route("/Platform scale it"), ("platform", "scale it"))

    def test_unknown_name_falls_through(self):
        self.assertIsNone(plugin.route("/nosuchagent hello"))

    def test_the_front_door_itself_is_not_a_target(self):
        # discover() excludes `default`, so this reads as an unknown name.
        self.assertIsNone(plugin.route("/default hello"))

    def test_bare_name_falls_through(self):
        # No text to run. The Chat Agent answers conversationally instead.
        for text in ("/platform", "/platform   "):
            self.assertIsNone(plugin.route(text))

    def test_other_slash_commands_fall_through(self):
        # legacy_slash_commands shares this hook and owns these; no profile is
        # named `hermes` or `sethome`, so the two never contend for a message.
        for text in ("/hermes sethome", "/sethome", "/help", "/model gpt-5"):
            self.assertIsNone(plugin.route(text))

    def test_ordinary_messages_fall_through(self):
        for text in ("what clusters do I have?", "ask /platform about it", "", None, 42):
            self.assertIsNone(plugin.route(text))

    def test_unreadable_roster_does_not_route(self):
        with mock.patch.object(plugin, "_load_roster_module", return_value=fake_roster(None)):
            self.assertIsNone(plugin.route("/platform scale it"))

    def test_missing_roster_module_does_not_route(self):
        with mock.patch.object(plugin, "_load_roster_module", return_value=None):
            self.assertIsNone(plugin.route("/platform scale it"))

    def test_empty_fleet_does_not_route(self):
        with mock.patch.object(plugin, "_load_roster_module", return_value=fake_roster([])):
            self.assertIsNone(plugin.route("/platform scale it"))


class PreGatewayDispatchHookTest(unittest.TestCase):
    def setUp(self):
        roster = mock.patch.object(plugin, "_load_roster_module", return_value=fake_roster(ROSTER))
        self.addCleanup(roster.stop)
        roster.start()
        gate = mock.patch.dict(os.environ, {plugin._GATE_ENV: "true"})
        self.addCleanup(gate.stop)
        gate.start()

    def test_hook_stamps_the_profile_and_rewrites_the_text(self):
        e = event("/platform scale the frontend", profile="default")
        self.assertEqual(
            plugin.handle_pre_gateway_dispatch(event=e, gateway=None, session_store=None),
            {"action": "rewrite", "text": "scale the frontend"},
        )
        self.assertEqual(e.source.profile, "platform")

    def test_fall_through_leaves_the_profile_untouched(self):
        for text in ("what clusters do I have?", "/hermes sethome", "/nosuchagent hi", "/platform"):
            e = event(text, profile="default")
            self.assertIsNone(
                plugin.handle_pre_gateway_dispatch(event=e, gateway=None, session_store=None),
                msg=text,
            )
            self.assertEqual(e.source.profile, "default", msg=text)

    def test_a_sourceless_event_is_not_rewritten(self):
        # Stripping the prefix without stamping a profile would run the message
        # on the front door as though no agent had been named.
        e = SimpleNamespace(text="/platform scale it", source=None)
        self.assertIsNone(
            plugin.handle_pre_gateway_dispatch(event=e, gateway=None, session_store=None)
        )

    def test_hook_never_raises(self):
        self.assertIsNone(
            plugin.handle_pre_gateway_dispatch(event=None, gateway=None, session_store=None)
        )
        with mock.patch.object(plugin, "route", side_effect=RuntimeError("boom")):
            self.assertIsNone(
                plugin.handle_pre_gateway_dispatch(
                    event=event("/platform scale it"), gateway=None, session_store=None
                )
            )


class GateTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(plugin, "_load_roster_module", return_value=fake_roster(ROSTER))
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_hook_is_inert_when_the_gate_is_unset_or_false(self):
        for value in ("", "false", "0", "off", "no", "maybe"):
            with mock.patch.dict(os.environ, {plugin._GATE_ENV: value}):
                e = event("/platform scale it", profile="default")
                self.assertIsNone(
                    plugin.handle_pre_gateway_dispatch(event=e, gateway=None, session_store=None),
                    msg=value,
                )
                self.assertEqual(e.source.profile, "default", msg=value)

    def test_gate_accepts_the_same_truthy_values_as_the_gateway_flag(self):
        for value in ("1", "true", "TRUE", "yes", "on", " true "):
            with mock.patch.dict(os.environ, {plugin._GATE_ENV: value}):
                self.assertTrue(plugin._enabled(), msg=value)


if __name__ == "__main__":
    unittest.main()
