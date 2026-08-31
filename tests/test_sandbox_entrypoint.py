"""Tests for the home-root sync in deploy/sandbox/entrypoint.sh.

    python3 -m unittest discover -s tests -p 'test_*.py'

The script runs as root inside the sandbox and chowns to uid 1000, neither of
which a test host can do. So `chown` and `install` are stubbed onto PATH and the
assertions are about which paths the script hands them — the same technique
tests/test_docker_entrypoint.py uses for the gate next door, and for the same
reason: the interesting behaviour is a decision, not a side effect.

What is being pinned is that every component between $DATA and a nested home root
ends up owned by the sandboxed account, not just the leaf. `install -d -o/-g`
applies the ownership to the last component only, so `profiles/platform` used to
leave `$DATA/profiles` root-owned. That state is readable and traversable, which
is why it survived review and a live upgrade: the platform profile is agent-owned,
the shell works, every skill works. It fails only when something creates a sibling
of `platform` — which is what sandbox_mirror.py does for each of the agent pod's
other profiles, so the migration aborts and the model's files never arrive.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_ENTRYPOINT = _REPO / "deploy" / "sandbox" / "entrypoint.sh"

# The script exits non-zero well after the part under test: step 3 refuses an
# sshd state directory this test has no way to create root-owned. Everything
# asserted here happens at step 1a, above that.
_CHOWN_STUB = """#!/bin/sh
for arg in "$@"; do
  case "$arg" in
    -*|*:*) ;;
    *) echo "$arg" >>"$CHOWN_LOG" ;;
  esac
done
exit 0
"""

# Drops -o/-g -- the real ones need root -- and keeps the directory creation the
# loop depends on. Deliberately NOT a passthrough to /usr/bin/install: stubbing
# out the ownership is what makes the chown log the only record of it.
_INSTALL_STUB = """#!/bin/sh
dirs=""
while [ $# -gt 0 ]; do
  case "$1" in
    -d) ;;
    -o|-g) shift ;;
    -*) ;;
    *) dirs="$dirs $1" ;;
  esac
  shift
done
mkdir -p $dirs
"""


class SandboxEntrypointHomeRootsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.data = self.tmp / "data"
        self.data.mkdir()
        self.defaults = self.tmp / "defaults"
        (self.defaults / "scripts").mkdir(parents=True)
        (self.defaults / "scripts" / "forge.py").write_text("# placeholder\n")

        self.chown_log = self.tmp / "chown.log"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("chown", _CHOWN_STUB), ("install", _INSTALL_STUB)):
            stub = bin_dir / name
            stub.write_text(body)
            stub.chmod(0o755)
        self.bin_dir = bin_dir

    def _run(self, home_roots: str) -> list[str]:
        env = dict(os.environ)
        env.update(
            {
                "PATH": f"{self.bin_dir}{os.pathsep}{env['PATH']}",
                "CHOWN_LOG": str(self.chown_log),
                "SANDBOX_DATA": str(self.data),
                "SANDBOX_DEFAULTS": str(self.defaults),
                "SANDBOX_HOME_ROOTS": home_roots,
                # Absent on purpose: step 2 and step 3 are past the part under
                # test and are expected to end the run.
                "SANDBOX_SSHD_STATE": str(self.tmp / "absent-sshd"),
                "SANDBOX_AUTHORIZED_KEYS": str(self.tmp / "absent-keys"),
            }
        )
        subprocess.run(
            ["bash", str(_ENTRYPOINT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if not self.chown_log.exists():
            return []
        return [line for line in self.chown_log.read_text().splitlines() if line]

    def test_intermediate_directories_are_chowned_with_the_leaf(self) -> None:
        """`profiles/platform` must leave $DATA/profiles agent-owned too."""
        chowned = self._run(". profiles/platform")
        self.assertIn(
            str(self.data / "profiles"),
            chowned,
            "the parent of a nested home root was left with the entrypoint's own "
            "ownership; sandbox_mirror.py cannot create the other profiles' homes "
            "beside it and the migration aborts",
        )

    def test_the_data_root_itself_is_not_walked_past(self) -> None:
        """The walk stops at $DATA, which step 1 already owns."""
        chowned = self._run(". profiles/platform")
        parent = str(self.data.parent)
        self.assertNotIn(
            parent,
            chowned,
            "the walk escaped $DATA and chowned its parent, which belongs to the "
            "image rather than to the model",
        )

    def test_a_deeper_root_chowns_every_component(self) -> None:
        """Nothing here is special-cased to one level of nesting."""
        chowned = self._run("profiles/a/b/c")
        for component in ("profiles", "profiles/a", "profiles/a/b"):
            self.assertIn(str(self.data / component), chowned, component)


if __name__ == "__main__":
    unittest.main()
