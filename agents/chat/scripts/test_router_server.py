import importlib
import os
import sys
import types
import unittest
from contextlib import contextmanager
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


@unittest.skipIf(os.geteuid() == 0, "root bypasses the mode bits these tests rely on")
class TestDiscoveryDegradesOnIOError(unittest.TestCase):
    """Discovery must degrade, never raise: it backs the Chat Agent's only routing tool.

    pathlib swallows only ENOENT/ENOTDIR/EBADF/ELOOP, so `is_dir()`/`is_file()`/
    `iterdir()` on the shared PVC raise PermissionError (EACCES) for real. An
    unguarded raise here turns every routing attempt into a traceback.
    """

    @contextmanager
    def _locked(self, path):
        """Make `path` unreadable, restoring the mode so TemporaryDirectory can clean up."""
        original = path.stat().st_mode
        path.chmod(0o000)
        try:
            yield
        finally:
            path.chmod(original)

    def test_unreadable_profile_costs_only_that_agent(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            for name in ("default", "platform", "broken"):
                (base / name).mkdir(parents=True)
            (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
            router.PROFILES_BASE = base

            with self._locked(base / "broken"):
                out = router.list_agents()

            # The healthy specialist still routes; the unreadable one is listed
            # without a description rather than taking down the whole roster.
            self.assertIn("- platform: Fleet + GitOps write path.", out)
            self.assertIn("- broken: (no description provided)", out)

    def test_unreadable_profiles_base_returns_empty_roster(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            (base / "platform").mkdir(parents=True)
            router.PROFILES_BASE = base

            with self._locked(base):
                out = router.list_agents()

            self.assertIn("No specialist agents", out)

    def test_missing_profiles_base_returns_empty_roster(self):
        with TemporaryDirectory() as tmp:
            router.PROFILES_BASE = Path(tmp) / "does-not-exist"
            self.assertIn("No specialist agents", router.list_agents())


class TestKanbanOnly(unittest.TestCase):
    """The router is discovery-only: the synchronous ask_agent relay is gone.

    Delegation happens exclusively via the asynchronous kanban board so the user
    sees non-blocking progress in the thread; the router only advertises the
    dynamic specialist roster used to pick an assignee.
    """

    def test_ask_agent_removed(self):
        self.assertFalse(hasattr(router, "ask_agent"))

    def test_no_blocking_subprocess_machinery(self):
        # These only existed to support the removed synchronous relay.
        self.assertFalse(hasattr(router, "INVOKE_TIMEOUT"))
        self.assertFalse(hasattr(router, "_run_env"))


if __name__ == "__main__":
    unittest.main()
