"""Tests for the content-passing workspace store.

Two things these tests are deliberately careful about.

The store is exercised against a real `git` and a real local bare repository
rather than a mock, because the controls being asserted are properties of what
git does with the tree, and a mock would assert what this test file believes git
does. Where a case cannot be reached that way -- a push the remote rejects -- the
runner is swapped for a recorder, and the test says so.

Several test names describe what is *not* proven. `test_a_handle_is_unguessable
_and_minted_here` is not called `test_handle_proves_ownership` because it does
not: the broker cannot tell two sessions in the agent container apart, so a
handle is a bearer capability. A test whose name overstates its assertion is how
a gap gets closed on paper.
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import content_workspace
from content_workspace import (
    ContentWorkspaceStore,
    WorkspaceError,
    assert_disjoint_roots,
    validate_branch,
    validate_path,
    validate_repo,
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class PathValidatorTest(unittest.TestCase):
    """One validator serves reads and writes, so these cases bind both."""

    def test_accepts_a_repository_relative_name(self):
        self.assertEqual(validate_path("manifests/app.yaml"), "manifests/app.yaml")
        self.assertEqual(validate_path("  README.md  "), "README.md")

    def test_refuses_absolute_and_traversal(self):
        for bad in (
            "/etc/passwd",
            "../outside",
            "manifests/../../outside",
            "./manifests/app.yaml",
            "C:/windows",
            "manifests\\app.yaml",
        ):
            with self.subTest(bad=bad), self.assertRaises(WorkspaceError):
                validate_path(bad)

    def test_refuses_empty_and_control_characters(self):
        for bad in ("", "   ", "a\x00b", "a\nb", "a\rb", 17, None):
            with self.subTest(bad=bad), self.assertRaises(WorkspaceError):
                validate_path(bad)

    def test_refuses_every_spelling_of_dotgit(self):
        """Over-refusal is the point; see `_looks_like_dotgit`."""
        for bad in (
            ".git",
            ".git/config",
            ".GIT/config",
            ".Git/hooks/pre-commit",
            ".git./config",
            ".git /config",
            "git~1/config",
            "GIT~2/config",
            ".git::$DATA/config",
            ".gi\u200ct/config",
            ".g\ufeffit/config",
        ):
            with self.subTest(bad=bad), self.assertRaises(WorkspaceError):
                validate_path(bad)

    def test_refuses_dotgit_at_any_depth(self):
        """A first-segment-only check would let this through."""
        with self.assertRaises(WorkspaceError):
            validate_path("charts/vendored/.git/config")

    def test_allows_names_that_merely_start_with_git(self):
        for good in (".gitignore", ".gitattributes", "gitops/app.yaml", "git"):
            with self.subTest(good=good):
                self.assertEqual(validate_path(good), good)


class RepoAndBranchValidatorTest(unittest.TestCase):
    def test_repo_is_two_segments(self):
        self.assertEqual(validate_repo("acme/infra"), ("acme", "infra"))

    def test_repo_refuses_a_url(self):
        """There is no caller-supplied remote URL anywhere in this protocol.

        A URL chosen by the caller decides where the minted token is sent.
        """
        for bad in (
            "https://evil.example/acme/infra.git",
            "git@github.com:acme/infra.git",
            "acme",
            "acme/infra/extra",
            "../../acme/infra",
            "-acme/infra",
            42,
        ):
            with self.subTest(bad=bad), self.assertRaises(WorkspaceError):
                validate_repo(bad)

    def test_branch_refuses_option_and_revision_syntax(self):
        self.assertEqual(validate_branch("fix/thing-1"), "fix/thing-1")
        for bad in ("--force", "-x", "a..b", "HEAD@{1}", "a b", "x.lock", "", None):
            with self.subTest(bad=bad), self.assertRaises(WorkspaceError):
                validate_branch(bad)


class DisjointRootsTest(unittest.TestCase):
    def test_refuses_when_the_trees_sit_inside_the_agent_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = Path(tmp) / "shared"
            agent.mkdir()
            with self.assertRaises(RuntimeError):
                assert_disjoint_roots(agent / "content-workspaces", agent)

    def test_refuses_the_other_direction_too(self):
        """Containment is not symmetric and only one mistake is the obvious one."""
        with tempfile.TemporaryDirectory() as tmp:
            trees = Path(tmp) / "trees"
            trees.mkdir()
            with self.assertRaises(RuntimeError):
                assert_disjoint_roots(trees, trees / "shared")

    def test_refuses_when_they_are_the_same_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                assert_disjoint_roots(Path(tmp), Path(tmp))

    def test_accepts_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            assert_disjoint_roots(Path(tmp) / "trees", Path(tmp) / "shared")

    def test_a_symlinked_prefix_does_not_read_as_disjoint(self):
        """/var -> /private/var on macOS, or any subPath mount.

        Comparing an unresolved root against a resolved one silently passes this
        check and then refuses every legitimate call at containment time.
        """
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            (real / "trees").mkdir(parents=True)
            link = Path(tmp) / "link"
            link.symlink_to(real)
            with self.assertRaises(RuntimeError):
                assert_disjoint_roots(link / "trees", real)


class LimitParsingTest(unittest.TestCase):
    def test_a_misconfigured_ceiling_reads_as_the_default(self):
        """Not as unbounded. That failure mode is silent and permissive."""
        for raw in ("0", "-1", "banana", "  "):
            with self.subTest(raw=raw), mock.patch.dict(
                os.environ, {"CREDENTIAL_PROXY_MAX_FILE_BYTES": raw}
            ):
                self.assertEqual(
                    content_workspace._positive_int(
                        "CREDENTIAL_PROXY_MAX_FILE_BYTES",
                        content_workspace.DEFAULT_MAX_FILE_BYTES,
                    ),
                    content_workspace.DEFAULT_MAX_FILE_BYTES,
                )

    def test_an_operator_may_lower_or_raise_it(self):
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_MAX_FILE_BYTES": "4096"}):
            self.assertEqual(
                content_workspace._positive_int("CREDENTIAL_PROXY_MAX_FILE_BYTES", 1), 4096
            )

    def test_the_feature_is_off_unless_armed(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(content_workspace.content_workspaces_enabled())
        for raw in ("1", "true", "YES", "on"):
            with self.subTest(raw=raw), mock.patch.dict(
                os.environ, {"CREDENTIAL_PROXY_CONTENT_WORKSPACES": raw}
            ):
                self.assertTrue(content_workspace.content_workspaces_enabled())


class StoreTestCase(unittest.TestCase):
    """Base: a real bare repository, a real git, a real store."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

        self.remote = self.tmp / "remote.git"
        seed = self.tmp / "seed"
        seed.mkdir()
        self._git(seed, "init", "--quiet", "--initial-branch=main")
        (seed / "README.md").write_text("seed\n")
        (seed / "manifests").mkdir()
        (seed / "manifests" / "app.yaml").write_text("replicas: 1\n")
        self._git(seed, "add", "-A")
        self._git(seed, "commit", "--quiet", "-m", "seed")
        self._git(self.tmp, "clone", "--quiet", "--bare", str(seed), str(self.remote))

        self.trees = self.tmp / "trees"
        self.agent_workspace = self.tmp / "shared"
        self.agent_workspace.mkdir()
        self.store = ContentWorkspaceStore(
            self.trees, self.agent_workspace, runner=self._runner
        )
        # `open` composes https://github.com/<owner>/<name>.git and there is no
        # way for a caller to override it, which is the property being kept. The
        # test redirects that one URL at the git layer instead.
        self.url_map = {"https://github.com/acme/infra.git": str(self.remote)}

    def _git(self, cwd, *args):
        env = dict(os.environ)
        env.update(GIT_ENV)
        return subprocess.run(
            ["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, check=True
        )

    def _runner(self, argv, cwd, check=True):
        env = dict(os.environ)
        env.update(GIT_ENV)
        argv = [self.url_map.get(token, token) for token in argv]
        return subprocess.run(
            ["git", *argv[1:]] if argv[0] == "git" else argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=check,
        )

    def open_workspace(self):
        return self.store.open({"repo": "acme/infra"})


class OpenTest(StoreTestCase):
    def test_open_returns_a_handle_and_no_filesystem_path(self):
        result = self.open_workspace()
        self.assertEqual(
            set(result), {"handle", "repo", "base", "baseSha", "startedFrom"}
        )
        self.assertEqual(result["repo"], "acme/infra")
        self.assertEqual(result["base"], "main")
        self.assertEqual(result["startedFrom"], "origin/main")
        self.assertRegex(result["baseSha"], r"^[0-9a-f]{40}$")
        # Two values carry a slash and are meant to: `owner/name` and
        # `origin/<ref>`. Neither names a directory, which is the property under
        # test -- a path here is somewhere the agent can be told to `cd`.
        for key, value in result.items():
            self.assertNotIn(str(self.trees), str(value))
            self.assertFalse(str(value).startswith("/"))
            if key not in ("repo", "startedFrom"):
                self.assertNotIn("/", str(value))

    def test_a_handle_is_unguessable_and_minted_here(self):
        """Unguessable and broker-minted. Deliberately not "proves ownership".

        The broker cannot distinguish two sessions inside the agent container,
        so this is a bearer capability -- strictly better than the `.lease` file
        it replaces, which the agent could create, and still not an owner check.
        """
        first = self.open_workspace()["handle"]
        second = self.open_workspace()["handle"]
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        for forged in ("", "x" * 32, first[:-1] + ("0" if first[-1] != "0" else "1")):
            with self.subTest(forged=forged), self.assertRaises(WorkspaceError) as caught:
                self.store.read({"handle": forged, "path": "README.md"})
            self.assertEqual(caught.exception.status, 404)

    def test_an_unknown_and_a_malformed_handle_answer_alike(self):
        """Distinguishing them would make this an oracle."""
        malformed = self.assertRaises(WorkspaceError)
        with malformed as caught_a:
            self.store.read({"handle": 17, "path": "README.md"})
        with self.assertRaises(WorkspaceError) as caught_b:
            self.store.read({"handle": "0" * 32, "path": "README.md"})
        self.assertEqual(str(caught_a.exception), str(caught_b.exception))
        self.assertEqual(caught_a.exception.status, caught_b.exception.status)


class ReadAndListTest(StoreTestCase):
    def test_read_returns_base64_content(self):
        handle = self.open_workspace()["handle"]
        result = self.store.read({"handle": handle, "path": "manifests/app.yaml"})
        self.assertEqual(base64.b64decode(result["contentBase64"]), b"replicas: 1\n")
        self.assertEqual(result["size"], len(b"replicas: 1\n"))
        self.assertEqual(result["path"], "manifests/app.yaml")

    def test_read_refuses_the_git_directory(self):
        """The same validator the write path uses, which is the point of sharing it."""
        handle = self.open_workspace()["handle"]
        with self.assertRaises(WorkspaceError):
            self.store.read({"handle": handle, "path": ".git/config"})

    def test_read_refuses_a_symlink_out_of_the_tree(self):
        handle = self.open_workspace()["handle"]
        root = self.store._workspaces[handle].root
        (root / "escape").symlink_to("/etc/passwd")
        with self.assertRaises(WorkspaceError):
            self.store.read({"handle": handle, "path": "escape"})

    def test_list_returns_tracked_names_only(self):
        handle = self.open_workspace()["handle"]
        entries = self.store.list({"handle": handle})["entries"]
        names = {entry["path"] for entry in entries}
        self.assertEqual(names, {"README.md", "manifests/app.yaml"})
        self.assertFalse(any(name.startswith(".git/") for name in names))

    def test_list_honours_a_prefix(self):
        handle = self.open_workspace()["handle"]
        entries = self.store.list({"handle": handle, "prefix": "manifests"})["entries"]
        self.assertEqual([entry["path"] for entry in entries], ["manifests/app.yaml"])


class CommitTest(StoreTestCase):
    def test_commit_writes_content_the_agent_never_placed_on_disk(self):
        handle = self.open_workspace()["handle"]
        result = self.store.commit(
            {
                "handle": handle,
                "branch": "fix/replicas",
                "message": "raise replicas",
                "changes": [
                    {"path": "manifests/app.yaml", "contentBase64": b64("replicas: 3\n")},
                    {"path": "manifests/new.yaml", "contentBase64": b64("kind: X\n")},
                ],
            }
        )
        self.assertTrue(result["committed"])
        self.assertEqual(result["branch"], "fix/replicas")
        root = self.store._workspaces[handle].root
        self.assertEqual((root / "manifests" / "app.yaml").read_text(), "replicas: 3\n")
        show = self._git(root, "show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertEqual(sorted(show), ["manifests/app.yaml", "manifests/new.yaml"])

    def test_commit_deletes(self):
        handle = self.open_workspace()["handle"]
        self.store.commit(
            {
                "handle": handle,
                "branch": "fix/drop",
                "message": "drop it",
                "changes": [{"path": "README.md", "delete": True}],
            }
        )
        root = self.store._workspaces[handle].root
        self.assertFalse((root / "README.md").exists())

    def test_commit_refuses_a_dotgit_path_before_writing_anything(self):
        """Fail closed means before the side effects."""
        handle = self.open_workspace()["handle"]
        root = self.store._workspaces[handle].root
        with self.assertRaises(WorkspaceError):
            self.store.commit(
                {
                    "handle": handle,
                    "branch": "fix/evil",
                    "message": "no",
                    "changes": [
                        {"path": "manifests/ok.yaml", "contentBase64": b64("a: 1\n")},
                        {"path": ".git/config", "contentBase64": b64("[filter]\n")},
                    ],
                }
            )
        self.assertFalse((root / "manifests" / "ok.yaml").exists())
        self.assertEqual(
            (root / ".git" / "config").read_text().count("filter"),
            0,
            "the broker's own git config must be untouched",
        )

    def test_an_oversized_entry_leaves_the_tree_alone(self):
        handle = self.open_workspace()["handle"]
        root = self.store._workspaces[handle].root
        self.store.max_file_bytes = 16
        with self.assertRaises(WorkspaceError) as caught:
            self.store.commit(
                {
                    "handle": handle,
                    "branch": "fix/big",
                    "message": "too big",
                    "changes": [
                        {"path": "small.yaml", "contentBase64": b64("a: 1\n")},
                        {"path": "big.yaml", "contentBase64": b64("x" * 64)},
                    ],
                }
            )
        self.assertEqual(caught.exception.status, 413)
        self.assertFalse((root / "small.yaml").exists())

    def test_the_request_total_is_capped_independently_of_the_per_file_cap(self):
        handle = self.open_workspace()["handle"]
        self.store.max_file_bytes = 64
        self.store.max_request_bytes = 100
        with self.assertRaises(WorkspaceError) as caught:
            self.store.commit(
                {
                    "handle": handle,
                    "branch": "fix/many",
                    "message": "sum too big",
                    "changes": [
                        {"path": f"f{i}.yaml", "contentBase64": b64("x" * 50)}
                        for i in range(3)
                    ],
                }
            )
        self.assertEqual(caught.exception.status, 413)

    def test_entry_count_is_capped(self):
        handle = self.open_workspace()["handle"]
        self.store.max_entries = 2
        with self.assertRaises(WorkspaceError) as caught:
            self.store.commit(
                {
                    "handle": handle,
                    "branch": "fix/many",
                    "message": "too many",
                    "changes": [
                        {"path": f"f{i}.yaml", "contentBase64": b64("a")} for i in range(3)
                    ],
                }
            )
        self.assertEqual(caught.exception.status, 413)

    def test_a_duplicate_path_is_refused_rather_than_resolved(self):
        """Which write wins would otherwise depend on iteration order."""
        handle = self.open_workspace()["handle"]
        with self.assertRaises(WorkspaceError):
            self.store.commit(
                {
                    "handle": handle,
                    "branch": "fix/dup",
                    "message": "dup",
                    "changes": [
                        {"path": "a.yaml", "contentBase64": b64("1")},
                        {"path": "a.yaml", "contentBase64": b64("2")},
                    ],
                }
            )

    def test_content_must_be_base64(self):
        """One encoding, never "base64 or plaintext"."""
        handle = self.open_workspace()["handle"]
        for bad in ("not valid base64!!", 17, None):
            with self.subTest(bad=bad), self.assertRaises(WorkspaceError):
                self.store.commit(
                    {
                        "handle": handle,
                        "branch": "fix/enc",
                        "message": "m",
                        "changes": [{"path": "a.yaml", "contentBase64": bad}],
                    }
                )

    def test_a_symlink_in_the_tree_is_not_followed_out_of_it(self):
        handle = self.open_workspace()["handle"]
        root = self.store._workspaces[handle].root
        outside = self.tmp / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside)
        with self.assertRaises(WorkspaceError):
            self.store.commit(
                {
                    "handle": handle,
                    "branch": "fix/link",
                    "message": "m",
                    "changes": [{"path": "link/escaped.yaml", "contentBase64": b64("x")}],
                }
            )
        self.assertFalse((outside / "escaped.yaml").exists())

    def test_an_empty_commit_is_refused(self):
        handle = self.open_workspace()["handle"]
        with self.assertRaises(WorkspaceError) as caught:
            self.store.commit(
                {
                    "handle": handle,
                    "branch": "fix/noop",
                    "message": "m",
                    "changes": [
                        {"path": "manifests/app.yaml", "contentBase64": b64("replicas: 1\n")}
                    ],
                }
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.fields.get("code"), "EMPTY_COMMIT")

    def test_a_message_is_required(self):
        handle = self.open_workspace()["handle"]
        for bad in ("", "   ", None, 3):
            with self.subTest(bad=bad), self.assertRaises(WorkspaceError):
                self.store.commit(
                    {
                        "handle": handle,
                        "branch": "fix/m",
                        "message": bad,
                        "changes": [{"path": "a.yaml", "contentBase64": b64("x")}],
                    }
                )


class ConflictTest(StoreTestCase):
    def _land_on_base(self, path: str, text: str) -> None:
        """Move the remote's base branch, the way another PR merging would."""
        scratch = self.tmp / f"scratch-{path.replace('/', '-')}"
        self._git(self.tmp, "clone", "--quiet", str(self.remote), str(scratch))
        target = scratch / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self._git(scratch, "add", "-A")
        self._git(scratch, "commit", "--quiet", "-m", f"land {path}")
        self._git(scratch, "push", "--quiet", "origin", "main")

    def test_an_unrelated_advance_of_the_base_is_not_a_conflict(self):
        """Refusing every moved base would fail behind any unrelated merge."""
        opened = self.open_workspace()
        self._land_on_base("docs/notes.md", "unrelated\n")
        result = self.store.commit(
            {
                "handle": opened["handle"],
                "branch": "fix/replicas",
                "message": "raise replicas",
                "expectedBaseSha": opened["baseSha"],
                "changes": [
                    {"path": "manifests/app.yaml", "contentBase64": b64("replicas: 3\n")}
                ],
            }
        )
        self.assertTrue(result["committed"])
        self.assertNotEqual(result["baseSha"], opened["baseSha"])

    def test_a_collision_on_a_written_path_is_a_409_naming_the_files(self):
        opened = self.open_workspace()
        self._land_on_base("manifests/app.yaml", "replicas: 9\n")
        with self.assertRaises(WorkspaceError) as caught:
            self.store.commit(
                {
                    "handle": opened["handle"],
                    "branch": "fix/replicas",
                    "message": "raise replicas",
                    "expectedBaseSha": opened["baseSha"],
                    "changes": [
                        {"path": "manifests/app.yaml", "contentBase64": b64("replicas: 3\n")}
                    ],
                }
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.fields.get("code"), "BASE_MOVED")
        self.assertEqual(caught.exception.fields.get("paths"), ["manifests/app.yaml"])

    def test_an_unanswerable_expected_sha_counts_as_a_collision(self):
        """Not as consent. The sha names an object this clone does not have."""
        opened = self.open_workspace()
        self._land_on_base("docs/notes.md", "unrelated\n")
        with self.assertRaises(WorkspaceError) as caught:
            self.store.commit(
                {
                    "handle": opened["handle"],
                    "branch": "fix/x",
                    "message": "m",
                    "expectedBaseSha": "0" * 40,
                    "changes": [
                        {"path": "manifests/app.yaml", "contentBase64": b64("replicas: 3\n")}
                    ],
                }
            )
        self.assertEqual(caught.exception.status, 409)

    def test_no_expected_sha_means_no_conflict_check(self):
        opened = self.open_workspace()
        self._land_on_base("manifests/app.yaml", "replicas: 9\n")
        result = self.store.commit(
            {
                "handle": opened["handle"],
                "branch": "fix/replicas",
                "message": "m",
                "changes": [
                    {"path": "manifests/app.yaml", "contentBase64": b64("replicas: 3\n")}
                ],
            }
        )
        self.assertTrue(result["committed"])


class PushTest(StoreTestCase):
    def test_push_lands_the_branch_on_the_remote(self):
        handle = self.open_workspace()["handle"]
        self.store.commit(
            {
                "handle": handle,
                "branch": "fix/replicas",
                "message": "m",
                "changes": [
                    {"path": "manifests/app.yaml", "contentBase64": b64("replicas: 3\n")}
                ],
            }
        )
        result = self.store.push({"handle": handle, "branch": "fix/replicas"})
        self.assertTrue(result["pushed"])
        remote_refs = self._git(self.tmp, "ls-remote", "--heads", str(self.remote)).stdout
        self.assertIn("refs/heads/fix/replicas", remote_refs)

    def test_push_refuses_a_branch_this_handle_never_committed(self):
        handle = self.open_workspace()["handle"]
        with self.assertRaises(WorkspaceError) as caught:
            self.store.push({"handle": handle, "branch": "fix/never"})
        self.assertEqual(caught.exception.status, 409)

    def test_a_rejected_push_is_a_409_and_not_a_retry(self):
        handle = self.open_workspace()["handle"]
        self.store.commit(
            {
                "handle": handle,
                "branch": "fix/replicas",
                "message": "m",
                "changes": [
                    {"path": "manifests/app.yaml", "contentBase64": b64("replicas: 3\n")}
                ],
            }
        )
        calls: list[list[str]] = []
        real = self.store._runner

        def rejecting(argv, cwd, check=True):
            calls.append(list(argv))
            if argv[:2] == ["git", "push"]:
                return subprocess.CompletedProcess(argv, 1, "", "stale info")
            return real(argv, cwd, check)

        self.store._runner = rejecting
        with self.assertRaises(WorkspaceError) as caught:
            self.store.push({"handle": handle, "branch": "fix/replicas"})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.fields.get("code"), "LEASE_REJECTED")

    def test_push_uses_force_with_lease_and_does_not_fetch_first(self):
        """Fetching immediately before the push is how a lease gets defeated."""
        handle = self.open_workspace()["handle"]
        self.store.commit(
            {
                "handle": handle,
                "branch": "fix/replicas",
                "message": "m",
                "changes": [
                    {"path": "manifests/app.yaml", "contentBase64": b64("replicas: 3\n")}
                ],
            }
        )
        calls: list[list[str]] = []
        real = self.store._runner

        def recording(argv, cwd, check=True):
            calls.append(list(argv))
            return real(argv, cwd, check)

        self.store._runner = recording
        self.store.push({"handle": handle, "branch": "fix/replicas"})
        push_calls = [call for call in calls if call[:2] == ["git", "push"]]
        self.assertEqual(len(push_calls), 1)
        self.assertIn("--force-with-lease", push_calls[0])
        self.assertNotIn("--force", push_calls[0])
        index = calls.index(push_calls[0])
        self.assertNotIn("fetch", calls[index - 1])


class SecondRoundTest(StoreTestCase):
    """A branch that already exists on the remote is continued, not replaced."""

    def _round(self, branch, path, text, base_sha=None):
        opened = self.store.open({"repo": "acme/infra", "branch": branch})
        payload = {
            "handle": opened["handle"],
            "branch": branch,
            "message": "m",
            "changes": [{"path": path, "contentBase64": b64(text)}],
        }
        committed = self.store.commit(payload)
        self.store.push({"handle": opened["handle"], "branch": branch})
        return opened, committed

    def test_the_reviewed_commits_survive_the_second_round(self):
        """Directory mode already shipped this data loss once.

        Checking out `origin/<base>` unconditionally makes round two a single
        commit that does not contain round one, and `--force-with-lease` cannot
        object: `commit` fetches the very ref the lease compares against.
        """
        first, _ = self._round("fix/replicas", "manifests/app.yaml", "replicas: 3\n")
        self.assertEqual(first["startedFrom"], "origin/main")

        second, committed = self._round("fix/replicas", "notes.md", "round two\n")
        self.assertEqual(second["startedFrom"], "origin/fix/replicas")
        self.assertEqual(committed["startedFrom"], "origin/fix/replicas")

        files = self._git(
            self.tmp, f"--git-dir={self.remote}", "ls-tree", "-r", "--name-only",
            "fix/replicas",
        ).stdout.split()
        self.assertIn("notes.md", files)
        self.assertIn("manifests/app.yaml", files)
        content = self._git(
            self.tmp, f"--git-dir={self.remote}", "show", "fix/replicas:manifests/app.yaml"
        ).stdout
        self.assertEqual(content, "replicas: 3\n", "round one's reviewed work was erased")

    def test_reopening_reads_the_branch_rather_than_the_base(self):
        self._round("fix/replicas", "manifests/app.yaml", "replicas: 3\n")
        opened = self.store.open({"repo": "acme/infra", "branch": "fix/replicas"})
        read = self.store.read(
            {"handle": opened["handle"], "path": "manifests/app.yaml"}
        )
        self.assertEqual(base64.b64decode(read["contentBase64"]), b"replicas: 3\n")

    def test_an_unknown_branch_still_opens_on_the_base(self):
        opened = self.store.open({"repo": "acme/infra", "branch": "fix/never-pushed"})
        self.assertEqual(opened["startedFrom"], "origin/main")
        read = self.store.read(
            {"handle": opened["handle"], "path": "manifests/app.yaml"}
        )
        self.assertEqual(base64.b64decode(read["contentBase64"]), b"replicas: 1\n")

    def test_a_branch_name_that_is_not_a_ref_name_is_refused_at_open(self):
        for branch in ("-delete-everything", "fix/..", "fix/x@{0}"):
            with self.subTest(branch=branch), self.assertRaises(WorkspaceError):
                self.store.open({"repo": "acme/infra", "branch": branch})


class CloseTest(StoreTestCase):
    def test_close_removes_the_tree_and_the_handle(self):
        handle = self.open_workspace()["handle"]
        root = self.store._workspaces[handle].root
        self.assertTrue(root.exists())
        self.assertEqual(self.store.close({"handle": handle}), {"closed": True})
        self.assertFalse(root.exists())
        with self.assertRaises(WorkspaceError):
            self.store.read({"handle": handle, "path": "README.md"})


class ResponseShapeTest(StoreTestCase):
    def test_no_response_carries_a_filesystem_path(self):
        """The invariant, written as something a test can check.

        A path handed back is a directory the agent can be told to `cd` into,
        and the whole design is that it has no name for these trees.
        """
        handle = self.open_workspace()["handle"]
        responses = [
            self.store.read({"handle": handle, "path": "README.md"}),
            self.store.list({"handle": handle}),
            self.store.commit(
                {
                    "handle": handle,
                    "branch": "fix/x",
                    "message": "m",
                    "changes": [
                        {"path": "manifests/app.yaml", "contentBase64": b64("replicas: 3\n")}
                    ],
                }
            ),
            self.store.push({"handle": handle, "branch": "fix/x"}),
            self.store.close({"handle": handle}),
        ]
        blob = repr(responses)
        for secret in (str(self.trees), str(self.tmp), str(self.remote)):
            self.assertNotIn(secret, blob)


if __name__ == "__main__":
    unittest.main()
