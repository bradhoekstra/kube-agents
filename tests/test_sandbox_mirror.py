"""Tests for deploy/shared/sandbox_mirror.py.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching tests/test_profile_overlay.py.

Two things this script gets to decide, and both fail quietly if it decides
wrong. If the exclusion rules are too narrow it copies a credential or a
session database into the pod that exists so the model cannot reach them, and
nothing complains. If they are too wide it leaves the model's work on the agent
pod's volume after an upgrade, which is the failure the migration was written
to prevent and which looks exactly like the files having been deleted.

So most of what is below is the exclusion table, asserted against a home laid
out like the one on the install this was written against. The transfer itself
is covered end to end by driving a real tar into a real directory with the SSH
hop replaced by `sh -c`.
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[1] / "deploy" / "shared" / "sandbox_mirror.py"
)
_spec = importlib.util.spec_from_file_location("sandbox_mirror", _MODULE_PATH)
sm = importlib.util.module_from_spec(_spec)
sys.modules["sandbox_mirror"] = sm
_spec.loader.exec_module(sm)


# A trimmed copy of the machine home on kage-management: enough of each class
# for the exclusion rules to have something to be wrong about.
MACHINE_HOME_DIRS = [
    # the model's, and the whole point of the migration
    "scratch",
    "gitops",
    "artifacts",
    "plans",
    "workspace",
    "home",
    "tmp",
    "infra",
    "infra-repo",
    "infra_repo",
    "work-d0452361",
    # Hermes'
    "sessions",
    "logs",
    "cache",
    "cron",
    "memories",
    "hindsight",
    "kanban",
    "plugins",
    "hooks",
    "sandboxes",
    "lazy-packages",
    "venv-yaml",
    "lost+found",
    "__pycache__",
    # the image's
    "skills",
    "governance",
    "scripts",
    # credentials
    ".ssh",
    ".kubeconfigs",
]

MACHINE_HOME_FILES = [
    "AGENTS.md",
    "SOUL.md",
    "SETTINGS.md",
    "config.yaml",
    "config.yaml.bak",
    "kubeconfig.yaml",
    "state.db",
    "state.db-wal",
    "kanban.db",
    "models_dev_cache.json",
    ".env",
    ".bootstrap_completed",
    "unblock.py",
    "hermes-verify-export.py",
]


def build_home(root: pathlib.Path, profiles=("platform", "cluster-a")) -> None:
    for name in MACHINE_HOME_DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "content").write_text("x")
    for name in MACHINE_HOME_FILES:
        (root / name).write_text("x")
    for profile in profiles:
        home = root / "profiles" / profile
        home.mkdir(parents=True, exist_ok=True)
        for name in ("workspace", "plans", "logs", "sessions", "governance"):
            (home / name).mkdir(exist_ok=True)
            (home / name / "content").write_text("x")
        for name in ("config.yaml", ".env", "SOUL.md", "state.db"):
            (home / name).write_text("x")


class ExclusionRules(unittest.TestCase):
    def test_credentials_never_cross(self):
        for name in (
            ".env",
            ".ssh",
            ".kubeconfigs",
            "kubeconfig.yaml",
            "auth.lock",
            # A file, not the directory the name suggests: a cached GKE token.
            "gke_gcloud_auth_plugin_cache",
        ):
            self.assertIsNotNone(
                sm.is_excluded(name),
                f"{name} holds or names a credential and must stay on the agent pod",
            )

    def test_the_process_home_never_crosses(self):
        # $HERMES_HOME/home is the pod's $HOME. On the install this was written
        # against it held 831 MiB of pip and gcloud cache, 46 MiB of
        # kubeconfigs under .kube, and gcloud's credentials under .config —
        # a fifth of the sandbox's volume, and a credential path into the pod
        # that exists to have none.
        self.assertIsNotNone(sm.is_excluded("home"))

    def test_hermes_log_and_process_state_never_cross(self):
        for name in (
            "logs",
            "agent.log",
            "agent.log.1",
            "gateway.pid",
            "gateway.lock",
            "gateway-starts.log",
            "gateway_state.json",
            "processes.json",
            "channel_directory.json",
            "google_chat_thread_counts.json",
        ):
            self.assertIsNotNone(sm.is_excluded(name), name)

    def test_databases_and_their_write_ahead_logs_never_cross(self):
        for name in ("state.db", "state.db-wal", "state.db-shm", "kanban.db", "sessions.db"):
            self.assertIsNotNone(sm.is_excluded(name), name)

    def test_image_owned_trees_never_cross(self):
        # The sandbox entrypoint replaces these from /opt/defaults on every
        # start, so a copy from the agent pod is undone at the next restart at
        # best and shadows a newer image at worst.
        for name in ("skills", "governance", "scripts"):
            self.assertEqual(sm.is_excluded(name), "delivered by the sandbox image", name)

    def test_image_owned_names_what_the_sandbox_image_stages(self):
        # These two sets are excluded for opposite reasons and the log line says
        # which, so a name in the wrong one makes the audit trail lie. The live
        # sandbox stages governance, scripts and skills at /opt/defaults and
        # nothing else; the persona files are withheld because nothing reads
        # them through the shell, not because something replaces them.
        dockerfile = (
            pathlib.Path(__file__).resolve().parents[1] / "deploy/sandbox/Dockerfile"
        ).read_text()
        for name in sm.IMAGE_OWNED:
            self.assertIn(
                f"/opt/defaults/{name}",
                dockerfile,
                f"{name} is called image-owned but the sandbox image does not stage it",
            )
        self.assertFalse(sm.IMAGE_OWNED & sm.AGENT_POD_ONLY)

    def test_the_persona_stays_in_the_agent_pod(self):
        for name in ("SOUL.md", "AGENTS.md", "CAPABILITIES.md", "USER.md", "profile.yaml"):
            self.assertEqual(
                sm.is_excluded(name),
                "stays in the agent pod; nothing reads it through the shell",
                name,
            )

    def test_settings_md_is_left_to_the_configmap_mount(self):
        # The operator mounts the rendered per-install SETTINGS.md into the
        # sandbox. Migrating the agent pod's copy would land on top of it.
        self.assertIsNotNone(sm.is_excluded("SETTINGS.md"))

    def test_the_model_s_working_directories_do_cross(self):
        for name in (
            "scratch",
            "gitops",
            "artifacts",
            "plans",
            "workspace",
            "tmp",
            # None of these four is named by any instruction; the model
            # invented them. An allowlist would have dropped all four, which
            # is the case the denylist exists for.
            "infra",
            "infra-repo",
            "infra_repo",
            "work-d0452361",
        ):
            self.assertIsNone(
                sm.is_excluded(name), f"{name} is the model's work and must be migrated"
            )

    def test_every_skeleton_directory_is_one_the_rules_would_migrate(self):
        # Otherwise the layout and the migration disagree: the directory is
        # created empty on the sandbox and its contents are then withheld.
        for name in sm.SKELETON_DIRS:
            self.assertIsNone(sm.is_excluded(name), name)


class HomeEnumeration(unittest.TestCase):
    def test_machine_home_first_then_every_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            build_home(root)
            self.assertEqual(
                sm.home_relative_paths(root),
                ["", "profiles/cluster-a", "profiles/platform"],
            )

    def test_a_home_with_no_profiles_directory_is_still_a_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sm.home_relative_paths(pathlib.Path(tmp)), [""])


class Candidates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        build_home(self.root)
        self.homes = sm.home_relative_paths(self.root)
        self.include, self.skipped = sm.migration_candidates(self.root, self.homes)
        self.addCleanup(self.tmp.cleanup)

    def test_nothing_under_a_profile_home_leaks_a_credential(self):
        for rel in self.include:
            self.assertNotIn(".env", rel)
            self.assertNotIn("config.yaml", rel)

    def test_profile_working_directories_are_included_under_their_profile(self):
        self.assertIn("profiles/platform/workspace", self.include)
        self.assertIn("profiles/cluster-a/plans", self.include)

    def test_the_profiles_directory_itself_is_not_a_candidate(self):
        # It is walked one level down instead. Including it too would copy
        # every profile home wholesale, exclusion rules and all.
        self.assertNotIn("profiles", self.include)
        self.assertIn(("profiles", "walked separately"), self.skipped)

    def test_every_entry_is_either_included_or_skipped_with_a_reason(self):
        seen = set(self.include) | {rel for rel, _ in self.skipped}
        for home in self.homes:
            base = self.root / home if home else self.root
            for entry in base.iterdir():
                rel = f"{home}/{entry.name}" if home else entry.name
                self.assertIn(rel, seen, f"{rel} was neither copied nor accounted for")


class Budget(unittest.TestCase):
    def test_smallest_first_so_one_huge_clone_does_not_evict_everything(self):
        sizes = {"scratch": 10, "gitops": 100, "plans": 1}
        kept, dropped = sm.apply_budget(sizes, 20)
        self.assertEqual(kept, ["plans", "scratch"])
        self.assertEqual(dropped, [("gitops", 100)])

    def test_a_budget_of_zero_drops_everything_and_names_it(self):
        kept, dropped = sm.apply_budget({"scratch": 10}, 0)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, [("scratch", 10)])

    def test_no_budget_keeps_everything(self):
        # The sandbox volume is sized from the agent's, so the default copy is
        # bounded only by free space. A cap that reappears here truncates a
        # migration silently, which is the failure this path exists to prevent.
        sizes = {"scratch": 10, "gitops": 10**12}
        kept, dropped = sm.apply_budget(sizes, None)
        self.assertEqual(kept, ["gitops", "scratch"])
        self.assertEqual(dropped, [])

    def test_the_default_cap_is_off_and_free_space_is_what_bounds_the_copy(self):
        self.assertIsNone(sm.effective_budget(sm.DEFAULT_MAX_BYTES, None))

        # Free space always applies, less the headroom that keeps the volume
        # writable for sshd and the shell.
        free = 4 * 1024 * 1024 * 1024
        self.assertEqual(
            sm.effective_budget(sm.DEFAULT_MAX_BYTES, free),
            free - sm.FREE_SPACE_HEADROOM,
        )

        # An explicit --max-bytes is an escape hatch, and the tighter of the two wins.
        self.assertEqual(sm.effective_budget(1024, free), 1024)
        self.assertEqual(sm.effective_budget(free, 1024 + sm.FREE_SPACE_HEADROOM), 1024)

    def test_a_full_volume_yields_a_zero_budget_rather_than_a_negative_one(self):
        kept, dropped = sm.apply_budget(
            {"scratch": 10}, sm.effective_budget(sm.DEFAULT_MAX_BYTES, 0)
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, [("scratch", 10)])


def gnu_tar() -> bool:
    try:
        out = subprocess.run(["tar", "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return "GNU tar" in out.stdout


@unittest.skipUnless(
    gnu_tar(),
    "needs GNU tar: --skip-old-files and --files-from --null are GNU spellings, and "
    "the pod this runs in is Debian. macOS ships bsdtar, so these skip there and "
    "run in CI",
)
class Transfer(unittest.TestCase):
    """The real tar pipeline, with `sh -c` standing in for the SSH hop.

    ssh_base_command returns an argv the transfer appends `--` and a command
    string to, and `sh -c` has the same shape. That makes the pipe, the
    NUL-separated file list and --skip-old-files all exercised for real.
    """

    def setUp(self):
        self.src = tempfile.TemporaryDirectory()
        self.dst = tempfile.TemporaryDirectory()
        self.addCleanup(self.src.cleanup)
        self.addCleanup(self.dst.cleanup)
        self.source = pathlib.Path(self.src.name)
        self.dest = pathlib.Path(self.dst.name)

    def fake_ssh(self):
        return ["sh", "-c"]

    def run_transfer(self, paths):
        # `sh -c CMD -- ` would make "--" $0, so the command string has to be
        # the last argument. transfer() appends ["--", cmd]; sh reads the "--"
        # as end-of-options and the command as the script. Same shape as ssh.
        sm.transfer(self.fake_ssh(), self.source, str(self.dest), paths)

    def test_a_directory_tree_arrives_intact(self):
        (self.source / "scratch" / "deep").mkdir(parents=True)
        (self.source / "scratch" / "deep" / "note.md").write_text("kept")
        self.run_transfer(["scratch"])
        self.assertEqual((self.dest / "scratch" / "deep" / "note.md").read_text(), "kept")

    def test_an_existing_file_on_the_sandbox_is_not_overwritten(self):
        # The two pods have no start ordering, so this can land after the model
        # has already written in the sandbox. --skip-old-files is what keeps a
        # late migration from replacing a newer file with the agent pod's copy.
        (self.source / "scratch").mkdir()
        (self.source / "scratch" / "note.md").write_text("older, from the agent pod")
        (self.dest / "scratch").mkdir()
        (self.dest / "scratch" / "note.md").write_text("newer, written in the sandbox")
        self.run_transfer(["scratch"])
        self.assertEqual(
            (self.dest / "scratch" / "note.md").read_text(),
            "newer, written in the sandbox",
        )

    def test_an_executable_stays_executable(self):
        (self.source / "scratch").mkdir()
        script = self.source / "scratch" / "run.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        script.chmod(0o755)
        self.run_transfer(["scratch"])
        self.assertTrue(os.access(self.dest / "scratch" / "run.sh", os.X_OK))

    def test_a_path_with_a_space_survives_the_nul_separated_list(self):
        (self.source / "my work").mkdir()
        (self.source / "my work" / "a.txt").write_text("ok")
        self.run_transfer(["my work"])
        self.assertEqual((self.dest / "my work" / "a.txt").read_text(), "ok")

    def test_a_credential_nested_inside_a_migrated_directory_does_not_cross(self):
        # The first live run of this script copied /opt/data/tmp, which the
        # model had been running gcloud inside, and carried a cached GKE access
        # token across in tmp/gke_gcloud_auth_plugin_cache. `tmp` is a directory
        # the rules are right to migrate; what has to be dropped is what is
        # inside it, which only tar's --exclude can see.
        work = self.source / "tmp"
        (work / ".kube").mkdir(parents=True)
        (work / ".kube" / "config").write_text("clusters: [...]")
        (work / "gke_gcloud_auth_plugin_cache").write_text('{"access_token": "ya29.fake"}')
        (work / ".config" / "gcloud").mkdir(parents=True)
        (work / ".config" / "gcloud" / "credentials.db").write_text("secret")
        (work / "deeper" / "sub").mkdir(parents=True)
        (work / "deeper" / "sub" / ".env").write_text("API_KEY=secret")
        (work / "notes.md").write_text("kept")

        self.run_transfer(["tmp"])

        self.assertEqual((self.dest / "tmp" / "notes.md").read_text(), "kept")
        for leaked in (
            "tmp/.kube/config",
            "tmp/gke_gcloud_auth_plugin_cache",
            "tmp/.config/gcloud/credentials.db",
            "tmp/deeper/sub/.env",
        ):
            self.assertFalse(
                (self.dest / leaked).exists(), f"{leaked} reached the sandbox"
            )

    def test_a_nested_cache_does_not_spend_the_sandbox_volume(self):
        (self.source / "scratch" / "repo" / "node_modules" / "left-pad").mkdir(parents=True)
        (self.source / "scratch" / "repo" / "node_modules" / "left-pad" / "i.js").write_text("x")
        (self.source / "scratch" / "repo" / "main.py").write_text("keep")
        self.run_transfer(["scratch"])
        self.assertEqual((self.dest / "scratch" / "repo" / "main.py").read_text(), "keep")
        self.assertFalse((self.dest / "scratch" / "repo" / "node_modules").exists())


class RecursiveExclusion(unittest.TestCase):
    """The Python mirror of tar's --exclude matching, used by the size estimate.

    Transfer above proves tar's behaviour; this proves the estimate agrees with
    it, so the budget and the dry-run plan count the bytes that actually move.
    """

    def test_a_bare_pattern_matches_at_any_depth(self):
        for path in (".kube", "tmp/.kube", "scratch/a/b/.kube"):
            self.assertEqual(sm.recursively_excluded(path), ".kube", path)

    def test_a_two_component_pattern_needs_both_components(self):
        self.assertEqual(sm.recursively_excluded("home/.config/gcloud"), ".config/gcloud")
        self.assertIsNone(sm.recursively_excluded("scratch/gcloud"))

    def test_a_partial_name_is_not_a_match(self):
        self.assertIsNone(sm.recursively_excluded("scratch/kubernetes"))
        self.assertIsNone(sm.recursively_excluded("scratch/.kubernetes-notes"))
        self.assertIsNone(sm.recursively_excluded("gitops/env"))

    def test_the_model_s_own_files_are_untouched(self):
        for path in ("scratch/notes.md", "gitops/repo/.git/HEAD", "plans/q3.md"):
            self.assertIsNone(sm.recursively_excluded(path), path)

    def test_measure_does_not_count_what_will_not_be_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "scratch" / ".cache").mkdir(parents=True)
            (home / "scratch" / ".cache" / "blob").write_bytes(b"x" * 10_000)
            (home / "scratch" / "note.md").write_bytes(b"y" * 100)
            self.assertEqual(sm.measure(home, ["scratch"]), {"scratch": 100})


class ManagedConfig(unittest.TestCase):
    def write(self, body):
        handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
        handle.write(body)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_no_terminal_block_means_no_sandbox(self):
        self.assertIsNone(sm.read_terminal_config(self.write("model: x\n")))

    def test_a_local_backend_means_no_sandbox(self):
        self.assertIsNone(sm.read_terminal_config(self.write("terminal:\n  backend: local\n")))

    def test_a_missing_file_means_no_sandbox(self):
        self.assertIsNone(sm.read_terminal_config("/nonexistent/config.yaml"))

    def test_the_ssh_block_comes_back_whole(self):
        path = self.write(
            "terminal:\n"
            "  backend: ssh\n"
            "  ssh_host: platform-agent-shell-0.example\n"
            "  ssh_user: agent\n"
            "  ssh_port: 2222\n"
            "  ssh_key: /etc/sandbox-ssh/id_ed25519\n"
        )
        terminal = sm.read_terminal_config(path)
        argv = sm.ssh_base_command(terminal)
        self.assertIn("-p", argv)
        self.assertIn("2222", argv)
        self.assertIn("/etc/sandbox-ssh/id_ed25519", argv)
        self.assertEqual(argv[-1], "agent@platform-agent-shell-0.example")
        # BatchMode, or a sandbox that has lost its host key turns a background
        # startup step into one that blocks on a password prompt forever.
        self.assertIn("BatchMode=yes", argv)


class DryRun(unittest.TestCase):
    def test_dry_run_reports_the_plan_and_touches_nothing_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            build_home(root)
            config = root / "managed.yaml"
            config.write_text(
                "terminal:\n  backend: ssh\n  ssh_host: unreachable.invalid\n"
            )
            out = subprocess.run(
                [
                    sys.executable,
                    str(_MODULE_PATH),
                    "--agent-home",
                    str(root),
                    "--config",
                    str(config),
                    "--dry-run",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            report = json.loads(out.stdout)
            self.assertIn("scratch", report["would_copy"])
            self.assertIn(".env", report["skipped"])
            self.assertIn("profiles/platform/workspace", report["would_copy"])


if __name__ == "__main__":
    unittest.main()
