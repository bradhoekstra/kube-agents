"""Every plugin the chat profile ships is one the chat profile enables.

Hermes calls a plugin's register(ctx) only when the profile's plugins.enabled
names it, so a plugin directory under agents/chat/defaults/plugins/ that the
list in agents/chat/config.yaml omits is copied into every image and never
loaded, and nothing in the suite says so: the plugin's own tests call
register() directly. This holds the two together. The operator's own copy of
the list, frontDoorPlugins, is held to config.yaml by
TestFrontDoorPluginsMatchChatConfig in the operator's tests.
"""

from __future__ import annotations

import pathlib
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHAT_CONFIG = _REPO_ROOT / "agents" / "chat" / "config.yaml"
_CHAT_PLUGINS = _REPO_ROOT / "agents" / "chat" / "defaults" / "plugins"
# A plugin is a directory with a plugin.yaml; a directory without one is a
# shared module (common/) and nothing Hermes loads.
_PLUGIN_MANIFEST = "plugin.yaml"


class ChatProfilePluginsTest(unittest.TestCase):
    def test_every_shipped_chat_plugin_exposes_register_from_its_package(self):
        """Hermes imports the plugin package and calls register() on it; a register() that
        lives only in a submodule loads as "Plugin 'x' has no register() function" and is
        never called. Static: the package's __init__.py defines or imports the name."""
        shipped = sorted(p.parent for p in _CHAT_PLUGINS.glob(f"*/{_PLUGIN_MANIFEST}"))
        self.assertTrue(shipped)
        without = [d.name for d in shipped if "register" not in (d / "__init__.py").read_text()]
        self.assertEqual([], without, f"plugin package __init__.py neither defines nor imports register: {without}")

    def test_every_shipped_chat_plugin_is_enabled_on_the_chat_profile(self):
        enabled = (yaml.safe_load(_CHAT_CONFIG.read_text()) or {}).get("plugins", {}).get("enabled") or []
        self.assertTrue(enabled, f"plugins.enabled is gone from {_CHAT_CONFIG}; this test would pass against nothing")
        shipped = sorted(p.parent.name for p in _CHAT_PLUGINS.glob(f"*/{_PLUGIN_MANIFEST}"))
        self.assertTrue(shipped, f"no plugin under {_CHAT_PLUGINS}; this test would pass against nothing")
        missing = [name for name in shipped if name not in enabled]
        self.assertEqual([], missing, f"shipped under {_CHAT_PLUGINS} but not in {_CHAT_CONFIG} plugins.enabled: {missing}")


if __name__ == "__main__":
    unittest.main()
