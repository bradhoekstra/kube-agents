import importlib
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

# Add the directory containing router_server.py to sys.path so it can be imported.
sys.path.insert(0, str(Path(__file__).parent.absolute()))


def _load_router_server():
    """Import the module under test.

    These tests are not wired into CI and exercise only stdlib logic (discovery
    + input validation). When the hermes runtime deps (FastMCP / pydantic) aren't
    importable, fall back to minimal stubs so the module still imports in a bare
    checkout. `FastMCP().tool()` returns identity, so the decorated tools remain
    plain callables.
    """
    try:
        return importlib.import_module("router_server")
    except Exception:
        mcp = types.ModuleType("mcp"); mcp.__path__ = []
        mcp_server = types.ModuleType("mcp.server"); mcp_server.__path__ = []
        fastmcp = types.ModuleType("mcp.server.fastmcp")
        fastmcp.FastMCP = lambda *a, **k: types.SimpleNamespace(
            tool=lambda *a, **k: (lambda f: f), run=lambda: None)
        pydantic = types.ModuleType("pydantic")
        pydantic.Field = lambda *a, **k: None
        sys.modules.update({
            "mcp": mcp, "mcp.server": mcp_server, "mcp.server.fastmcp": fastmcp,
            "pydantic": pydantic,
        })
        return importlib.import_module("router_server")


router = _load_router_server()


class TestDiscovery(unittest.TestCase):
    """list_agents enumerates every profile except the front door itself."""

    def _with_profiles(self, tmp, names):
        base = Path(tmp) / "profiles"
        for name in names:
            (base / name).mkdir(parents=True)
        router.PROFILES_BASE = base
        return base

    def test_excludes_default_and_lists_specialists(self):
        with TemporaryDirectory() as tmp:
            base = self._with_profiles(tmp, ["default", "platform", "cluster-a"])
            # A CAPABILITIES.md is the preferred description source.
            (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
            # SOUL.md is the fallback when no CAPABILITIES.md exists.
            (base / "cluster-a" / "SOUL.md").write_text("# Title\n\nRead-only cluster diagnostics.\n")

            out = router.list_agents()
            self.assertIn("- platform: Fleet + GitOps write path.", out)
            self.assertIn("- cluster-a: Read-only cluster diagnostics.", out)
            self.assertNotIn("default", out)

    def test_empty_when_no_specialists(self):
        with TemporaryDirectory() as tmp:
            self._with_profiles(tmp, ["default"])
            self.assertIn("No specialist agents", router.list_agents())


class TestAskAgentGuards(unittest.TestCase):
    """ask_agent must reject bad targets before ever invoking hermes."""

    def test_refuses_default(self):
        result = router.ask_agent("default", "do something")
        self.assertIn("refusing to route to 'default'", result)

    def test_rejects_invalid_name(self):
        for bad in ("", "has space", "../escape", "a/b"):
            self.assertTrue(router.ask_agent(bad, "q").startswith("ERROR: invalid target"))

    def test_missing_profile(self):
        with TemporaryDirectory() as tmp:
            router.PROFILES_BASE = Path(tmp) / "profiles"
            (router.PROFILES_BASE).mkdir()
            self.assertIn("does not exist", router.ask_agent("platform", "q"))


if __name__ == "__main__":
    unittest.main()
