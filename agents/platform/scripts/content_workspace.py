#!/usr/bin/env python3
"""Git working trees the broker owns and the agent cannot name.

The credential proxy runs `git` on the agent's behalf. Until now it ran it in a
directory the agent wrote — the shared PVC — which is the arrangement behind
every code-execution finding this module exists to retire. A repository the
agent can write is a repository whose `.git/config` names programs for git to
run, and the dangerous keys take arbitrary names inside the key
(`filter.<name>.clean`, `alias.<name>`), so there is no finite set to pin. An
enumeration against a surface whose design principle is extensibility does not
terminate; `credential_proxy._GIT_HARDENING_CONFIG` closes eight doors and says
in its own comment that `filter.<driver>` cannot be closed the same way.

So the agent stops handing over a directory and starts handing over content. It
sends `{path, content}` pairs and a commit message; the broker writes them into
a tree the agent has no path to, commits, and pushes. `.git` never exists
anywhere the agent can reach, which closes the class rather than another
instance of it — the agent may still supply a `.gitattributes` naming a filter,
but it cannot supply the `.git/config` that would define one, and an undefined
filter driver is inert.

The check that replaces the enumeration is finite: reject any path under `.git`,
in both directions. One validator serves reads and writes deliberately. A
checker that disagreed with itself about what `manifests/../.git/config` means
would be a parser differential with both halves inside one module, which is the
easiest kind to ship and the hardest to notice.

Nothing in any response is a filesystem path. That is the invariant, written as
something a test can check rather than as an intention: a path handed back is a
directory the agent can be told to `cd` into. A handle is an opaque token; a
`path` is a repository-relative name. It is the same distinction
`CommandExecutor._resolve_kubeconfig` already draws when it treats the caller's
kubeconfig as a name and regenerates the document rather than reading it.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

LOGGER = logging.getLogger("credential-proxy.workspace")

# Small on purpose. This carries Kubernetes manifests and pull-request bodies,
# not build artefacts, and a ceiling sized for the former is a ceiling that
# makes the latter fail loudly instead of quietly becoming a supported use.
DEFAULT_MAX_FILE_BYTES = 1 << 20  # 1 MiB
DEFAULT_MAX_REQUEST_BYTES = 8 << 20  # 8 MiB
DEFAULT_MAX_ENTRIES = 256

# A handle is 128 bits from os.urandom and lives only in this process's memory.
# The agent cannot fabricate one, which is the property the `.lease` file it
# replaces never had -- that was a file on a shared volume, and creating it
# unlocked every mutating verb.
_HANDLE_BYTES = 16

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# Codepoints HFS+ drops when it compares names, so `.gi<U+200C>t` opens `.git`
# on a Mac. Git carries its own copy of this list in `is_hfs_dotgit`; this one
# is deliberately not a port of it. See `_looks_like_dotgit`.
_HFS_IGNORABLE = {
    0x200C, 0x200D, 0x200E, 0x200F,
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x206A, 0x206B, 0x206C, 0x206D, 0x206E, 0x206F,
    0xFEFF,
}


def _looks_like_dotgit(segment: str) -> bool:
    """True for anything a filesystem might open as `.git`.

    This refuses more spellings than git itself accepts, on purpose. Git has two
    functions for the same question -- `is_ntfs_dotgit` and `is_hfs_dotgit` --
    and matching them exactly would mean porting both and then betting the port
    agrees with whichever git version is in the image. That is the bet this
    project keeps losing: a check written against a different parser than the
    enforcer fails silently, and it fails permissive.

    Over-refusing is free here. The repositories this carries hold Kubernetes
    manifests, and none of them contains a file called `.git.` or `git~1`.

    Covered: case (`.GIT`, case-insensitive filesystems fold it); the NTFS 8.3
    short name (`git~1`, and any `git~<n>`); trailing dots and spaces, which
    Windows strips before it opens the name; the NTFS alternate-data-stream
    suffix (`.git::$DATA`); and HFS+ ignorable codepoints anywhere inside.
    """
    text = "".join(ch for ch in segment if ord(ch) not in _HFS_IGNORABLE)
    text = text.split(":", 1)[0]
    text = text.rstrip(". ").lower()
    if text == ".git":
        return True
    # `git~1`, the short name Windows generates for `.git`, and every numbered
    # sibling of it.
    return text.startswith("git~") and text[4:].isdigit()


class WorkspaceError(Exception):
    """A request the broker refuses. Carries the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400, **fields: Any) -> None:
        super().__init__(message)
        self.status = status
        self.fields = fields


def _positive_int(name: str, default: int) -> int:
    """An operator-set ceiling, or the default when it is not usable.

    Zero, negative and unparseable all read as the default rather than as
    unbounded. A misconfigured limit that removes the limit is the failure mode
    worth designing against here: it is silent, and it is in the permissive
    direction.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    if value <= 0:
        LOGGER.warning("%s=%r is not positive; using %d", name, raw, default)
        return default
    return value


def content_workspaces_enabled() -> bool:
    """Off by default. The directory-passing path keeps working beside it."""
    return os.getenv("CREDENTIAL_PROXY_CONTENT_WORKSPACES", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def assert_disjoint_roots(tree_root: Path, agent_workspace_root: Path) -> None:
    """Refuse to start if the broker's trees sit inside the agent's volume.

    Checked in both directions, because containment is not symmetric and only
    one of the two mistakes is the obvious one. This runs at construction, so an
    edit that points both at the same volume produces a broker that will not
    start rather than a broker that starts without the property. That is the
    part of "unreachable" this process can actually enforce; the rest is the
    agent container not mounting the volume, which nothing in Python can see.
    """
    tree = Path(tree_root).resolve()
    agent = Path(agent_workspace_root).resolve()
    if tree == agent or tree in agent.parents or agent in tree.parents:
        raise RuntimeError(
            f"content workspace root {tree} overlaps the agent-shared workspace "
            f"root {agent}. The broker's trees must live on a volume the agent "
            "does not write, or content-passing protects nothing."
        )


def validate_repo(repo: Any) -> tuple[str, str]:
    """`owner/name`, or a refusal.

    There is deliberately no caller-supplied remote URL anywhere in this
    protocol. A URL chosen by the caller is `url.<host>.insteadOf` by another
    route: it decides where the minted GitHub token is sent. `open` takes the
    two path segments and composes the https URL itself.
    """
    if not isinstance(repo, str):
        raise WorkspaceError("repo must be a string as owner/name")
    owner, sep, name = repo.strip().partition("/")
    if not sep or not _REPO_SEGMENT_RE.match(owner) or not _REPO_SEGMENT_RE.match(name):
        raise WorkspaceError(f"expected a repository as owner/name, got {repo!r}")
    return owner, name


def validate_branch(branch: Any, field_name: str = "branch") -> str:
    if not isinstance(branch, str) or not _BRANCH_RE.match(branch.strip()):
        raise WorkspaceError(f"{field_name} is not an acceptable git ref name")
    branch = branch.strip()
    # `-` leading a ref makes it an option to whichever git command receives it,
    # and `..`/`@{` are revision syntax rather than names.
    if branch.startswith("-") or ".." in branch or "@{" in branch or branch.endswith(".lock"):
        raise WorkspaceError(f"{field_name} is not an acceptable git ref name")
    return branch


def validate_path(raw: Any) -> str:
    """A repository-relative name, or a refusal. One validator, both directions.

    Refuses, in order: a non-string; an empty name; a NUL or newline; an
    absolute path; a Windows drive or backslash separator; any `.` or `..`
    segment; and any path whose first segment is `.git`. The `..` rejection is
    outright rather than normalising, because normalising means reimplementing
    another library's edge cases and betting the two agree -- refusing the
    ambiguous form is the rule that does not depend on that bet.
    """
    if not isinstance(raw, str):
        raise WorkspaceError("path must be a string")
    text = raw.strip()
    if not text:
        raise WorkspaceError("path must not be empty")
    if "\x00" in text or "\n" in text or "\r" in text:
        raise WorkspaceError("path must not contain control characters")
    if "\\" in text:
        raise WorkspaceError(f"path {raw!r} must use / as its separator")
    if text.startswith("/") or (len(text) > 1 and text[1] == ":"):
        raise WorkspaceError(f"path {raw!r} must be repository-relative, not absolute")
    # Split by hand rather than through PurePosixPath. pathlib *normalises*:
    # it drops `.` segments and collapses `//`, so `./manifests/app.yaml` and
    # `manifests//app.yaml` arrive here looking clean. Normalising is the D15
    # defect -- this validator would be answering a different question from the
    # one the filesystem later answers. Refuse the ambiguous spelling instead.
    parts = text.split("/")
    if not parts:
        raise WorkspaceError("path must not be empty")
    for part in parts:
        if not part:
            raise WorkspaceError(
                f"path {raw!r} has an empty segment; write it without the extra /"
            )
        if part in (".", ".."):
            raise WorkspaceError(f"path {raw!r} must not contain . or .. segments")
        # Every segment, not just the first. A nested `.git` is inert in the
        # outer repository but is a live config directory for anything that
        # later treats that subdirectory as a repository of its own, and the
        # cost of refusing it is zero.
        if _looks_like_dotgit(part):
            raise WorkspaceError(
                f"path {raw!r} names a git directory. Nothing the agent authors "
                "belongs there: `.git/config` is where a filter driver, an alias "
                "or a hook path would be defined, and content-passing exists so "
                "that the agent cannot define one."
            )
    return str(PurePosixPath(*parts))


@dataclass
class _Workspace:
    handle: str
    repo: str
    root: Path
    base: str
    base_sha: str
    branch: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ContentWorkspaceStore:
    """Broker-owned git trees, addressed by handle rather than by path.

    `runner` is injected so tests drive this without a git binary, and so the
    broker's own git inherits the same hardening environment the agent-facing
    executor applies. Broker-internal git does not travel through
    `CommandExecutor` -- it never reaches the policy engine, because none of it
    is agent-issued argv. That is what makes the agent-facing git allowlist
    collapse once the skills migrate: the plumbing verbs stop being things the
    agent asks for.
    """

    def __init__(
        self,
        tree_root: Path | str,
        agent_workspace_root: Path | str,
        *,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        environment: dict[str, str] | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        self.tree_root = Path(tree_root).resolve()
        assert_disjoint_roots(self.tree_root, Path(agent_workspace_root))
        self.tree_root.mkdir(parents=True, exist_ok=True)
        self.max_file_bytes = _positive_int(
            "CREDENTIAL_PROXY_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES
        )
        self.max_request_bytes = _positive_int(
            "CREDENTIAL_PROXY_MAX_CONTENT_BYTES", DEFAULT_MAX_REQUEST_BYTES
        )
        self.max_entries = _positive_int(
            "CREDENTIAL_PROXY_MAX_ENTRIES", DEFAULT_MAX_ENTRIES
        )
        self.timeout_seconds = timeout_seconds
        self._environment = dict(environment or {})
        self._runner = runner or self._default_runner
        self._workspaces: dict[str, _Workspace] = {}

    # ---- git plumbing -------------------------------------------------

    def _default_runner(
        self, argv: list[str], cwd: Path, check: bool = True
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(self._environment)
        return subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=check,
            timeout=self.timeout_seconds,
        )

    def _git(
        self, workspace_or_root: _Workspace | Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess:
        root = (
            workspace_or_root.root
            if isinstance(workspace_or_root, _Workspace)
            else workspace_or_root
        )
        return self._runner(["git", *args], root, check)

    def _resolve(self, handle: Any) -> _Workspace:
        if not isinstance(handle, str) or handle not in self._workspaces:
            # Deliberately the same answer for malformed and unknown. A handle
            # is a bearer capability; distinguishing "wrong shape" from "not
            # yours" would turn this into an oracle.
            raise WorkspaceError("unknown workspace handle", status=404)
        return self._workspaces[handle]

    # ---- routes -------------------------------------------------------

    def open(self, payload: dict[str, Any]) -> dict[str, Any]:
        owner, name = validate_repo(payload.get("repo"))
        repo = f"{owner}/{name}"
        handle = os.urandom(_HANDLE_BYTES).hex()
        root = self.tree_root / handle
        root.mkdir(parents=True, exist_ok=False)
        url = f"https://github.com/{owner}/{name}.git"
        # --no-recurse-submodules: a .gitmodules in the remote would otherwise
        # fetch a second repository whose content nobody validated, and
        # submodule plumbing reads config keys this tree is not hardened for.
        self._git(root, "clone", "--quiet", "--no-recurse-submodules", url, ".")
        base = payload.get("base")
        base = validate_branch(base, "base") if base is not None else self._origin_head(root)
        self._git(root, "checkout", "--force", "-B", base, f"origin/{base}")
        base_sha = self._git(root, "rev-parse", "HEAD").stdout.strip()
        # An optional working branch, and the reason it is worth the parameter:
        # `read` and `list` answer from the tree that is checked out. Left on
        # the base, a second round of review feedback would be written against
        # the file as `main` has it rather than as the pull request has it, and
        # the reviewed work would be silently rewritten out of the file.
        requested = payload.get("branch")
        started_from = f"origin/{base}"
        if requested is not None:
            head = validate_branch(requested, "branch")
            if self._remote_branch_exists(root, head):
                self._git(root, "checkout", "--force", "-B", head, f"origin/{head}")
                started_from = f"origin/{head}"
        workspace = _Workspace(
            handle=handle, repo=repo, root=root, base=base, base_sha=base_sha
        )
        self._workspaces[handle] = workspace
        LOGGER.info("workspace opened repo=%s base=%s from=%s", repo, base, started_from)
        return {
            "handle": handle,
            "repo": repo,
            "base": base,
            "baseSha": base_sha,
            "startedFrom": started_from,
        }

    def _remote_branch_exists(self, root: Path, branch: str) -> bool:
        """Whether `origin/<branch>` is a ref this clone has.

        Fully qualified under `refs/remotes/`, so a branch sharing a name with a
        tag -- or one called `HEAD` -- cannot resolve to something else.
        """
        result = self._git(
            root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/remotes/origin/{branch}",
            check=False,
        )
        return result.returncode == 0

    def _origin_head(self, root: Path) -> str:
        result = self._git(
            root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
            check=False,
        )
        ref = (result.stdout or "").strip()
        if result.returncode == 0 and ref:
            return ref.split("/", 1)[1] if ref.startswith("origin/") else ref
        return "main"

    def read(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self._resolve(payload.get("handle"))
        path = validate_path(payload.get("path"))
        target = workspace.root / path
        if target.is_symlink() or not target.is_file():
            raise WorkspaceError(f"{path} is not a readable file in this repository", 404)
        data = target.read_bytes()
        if len(data) > self.max_file_bytes:
            raise WorkspaceError(
                f"{path} is {len(data)} bytes, over the {self.max_file_bytes}-byte "
                "per-file ceiling",
                status=413,
            )
        return {
            "path": path,
            "contentBase64": base64.b64encode(data).decode("ascii"),
            "size": len(data),
        }

    def list(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self._resolve(payload.get("handle"))
        prefix = payload.get("prefix")
        prefix = validate_path(prefix) if prefix else ""
        # `git ls-files` rather than a filesystem walk: it answers with tracked
        # names, which is what the agent is entitled to know, and it cannot
        # surface `.git` because git does not track its own directory.
        args = ["ls-files", "-z"]
        if prefix:
            args += ["--", prefix]
        raw = self._git(workspace, *args).stdout
        entries = []
        for name in filter(None, raw.split("\0")):
            candidate = workspace.root / name
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            entries.append({"path": name, "size": size})
            if len(entries) >= self.max_entries:
                break
        return {"entries": entries}

    def commit(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self._resolve(payload.get("handle"))
        branch = validate_branch(payload.get("branch"))
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            raise WorkspaceError("message must be a non-empty string")
        changes = self._validate_changes(payload.get("changes"))
        expected = payload.get("expectedBaseSha")
        if expected is not None and not isinstance(expected, str):
            raise WorkspaceError("expectedBaseSha must be a string")

        self._git(workspace, "fetch", "--quiet", "--prune", "origin")
        head = self._git(
            workspace, "rev-parse", f"origin/{workspace.base}"
        ).stdout.strip()
        if expected and head != expected:
            collisions = self._collisions(workspace, expected, head, changes)
            if collisions:
                raise WorkspaceError(
                    "the base branch moved under files this commit also writes",
                    status=409,
                    code="BASE_MOVED",
                    paths=collisions,
                )
        workspace.base_sha = head

        # Continue the branch when the remote already has it; only cut a new one
        # from the base when it does not. Always starting from the base is the
        # data loss this skill has already shipped once: a second round of
        # review feedback would replace every reviewed commit with one commit
        # that no longer contained them, and `--force-with-lease` cannot object
        # because the fetch above moved the very ref it compares against.
        start = (
            f"origin/{branch}"
            if self._remote_branch_exists(workspace.root, branch)
            else f"origin/{workspace.base}"
        )
        self._git(workspace, "checkout", "--force", "-B", branch, start)
        self._apply(workspace, changes)
        # --literal-pathspecs is a git-global option and has to precede the
        # subcommand; after it, git exits 129 on a usage error rather than
        # doing something surprising, which is how a test caught this.
        self._git(workspace, "--literal-pathspecs", "add", "--all", "--", ".")
        staged = self._git(workspace, "diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            raise WorkspaceError("no change to commit", status=409, code="EMPTY_COMMIT")
        self._git(workspace, "commit", "--quiet", "-m", message)
        commit = self._git(workspace, "rev-parse", "HEAD").stdout.strip()
        workspace.branch = branch
        return {
            "committed": True,
            "branch": branch,
            "base": workspace.base,
            "baseSha": head,
            "startedFrom": start,
            "commit": commit,
        }

    def _collisions(
        self,
        workspace: _Workspace,
        expected: str,
        head: str,
        changes: list[dict[str, Any]],
    ) -> list[str]:
        """The files this commit writes that the base also moved.

        Refusing every commit whose base advanced would fail a ten-minute audit
        behind any unrelated merge, and most merges are unrelated. Refusing only
        on a real collision is the answer a human reviewer would give.
        """
        paths = [change["path"] for change in changes]
        if not paths:
            return []
        result = self._git(
            workspace,
            "diff",
            "--name-only",
            expected,
            head,
            "--",
            *paths,
            check=False,
        )
        if result.returncode != 0:
            # The expected sha is not an object this clone has -- it named a
            # commit from another repository, or one that has been gc'd. Treat
            # an unanswerable question as a collision rather than as consent.
            return sorted(paths)
        return sorted(filter(None, (result.stdout or "").splitlines()))

    def _validate_changes(self, raw: Any) -> list[dict[str, Any]]:
        """Every entry checked before the first byte is written.

        Fail closed means before the side effects. A payload that exceeds any
        ceiling leaves the tree exactly as it found it, because a half-applied
        commit that then fails is worse than a refusal -- the next commit on the
        same handle inherits the debris and nothing records that it is there.
        """
        if not isinstance(raw, list) or not raw:
            raise WorkspaceError("changes must be a non-empty list")
        if len(raw) > self.max_entries:
            raise WorkspaceError(
                f"{len(raw)} entries is over the {self.max_entries}-entry ceiling",
                status=413,
            )
        validated: list[dict[str, Any]] = []
        seen: set[str] = set()
        total = 0
        for entry in raw:
            if not isinstance(entry, dict):
                raise WorkspaceError("each change must be an object")
            path = validate_path(entry.get("path"))
            if path in seen:
                raise WorkspaceError(
                    f"{path} appears twice in one request; which write wins would "
                    "depend on iteration order"
                )
            seen.add(path)
            if entry.get("delete") is True:
                validated.append({"path": path, "delete": True})
                continue
            encoded = entry.get("contentBase64")
            if not isinstance(encoded, str):
                raise WorkspaceError(
                    f"{path} has no contentBase64. Content is always base64 -- one "
                    "encoding, so there is never a question about which path a "
                    "byte arrived through."
                )
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise WorkspaceError(f"{path} is not valid base64: {exc}") from exc
            if len(data) > self.max_file_bytes:
                raise WorkspaceError(
                    f"{path} is {len(data)} bytes, over the {self.max_file_bytes}-byte "
                    "per-file ceiling",
                    status=413,
                )
            total += len(data)
            if total > self.max_request_bytes:
                raise WorkspaceError(
                    f"the request totals more than the {self.max_request_bytes}-byte "
                    "ceiling",
                    status=413,
                )
            validated.append({"path": path, "content": data})
        return validated

    def _apply(self, workspace: _Workspace, changes: list[dict[str, Any]]) -> None:
        for change in changes:
            target = workspace.root / change["path"]
            if change.get("delete"):
                if target.is_symlink() or target.is_file():
                    target.unlink()
                continue
            parent = target.parent
            # A symlink anywhere on the way to the destination would write
            # outside the tree while every string in the request stayed
            # repository-relative. Refuse loudly rather than follow it.
            for ancestor in [parent, *parent.parents]:
                if ancestor == workspace.root:
                    break
                if ancestor.is_symlink():
                    raise WorkspaceError(
                        f"{change['path']} is behind a symlink; the broker does not "
                        "follow links out of the tree it owns"
                    )
            if target.is_symlink():
                target.unlink()
            parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(change["content"])

    def push(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self._resolve(payload.get("handle"))
        branch = validate_branch(payload.get("branch"))
        if workspace.branch != branch:
            raise WorkspaceError(
                f"{branch} has no commit on this handle; commit before pushing",
                status=409,
            )
        # --force-with-lease, and deliberately no fetch immediately before it.
        # Fetching first is the classic way to defeat the lease: it moves the
        # remote-tracking ref onto whatever landed, and the lease then compares
        # that value against itself.
        result = self._git(
            workspace, "push", "--force-with-lease", "origin", branch, check=False
        )
        if result.returncode != 0:
            raise WorkspaceError(
                "the remote branch moved since this workspace last saw it",
                status=409,
                code="LEASE_REJECTED",
                detail=(result.stderr or "").strip()[:2000],
            )
        commit = self._git(workspace, "rev-parse", "HEAD").stdout.strip()
        return {"pushed": True, "branch": branch, "commit": commit}

    def close(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self._resolve(payload.get("handle"))
        self._workspaces.pop(workspace.handle, None)
        _remove_tree(workspace.root)
        return {"closed": True}


def _remove_tree(path: Path) -> None:
    for entry in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if entry.is_dir() and not entry.is_symlink():
                entry.rmdir()
            else:
                entry.unlink()
        except OSError:
            pass
    try:
        path.rmdir()
    except OSError:
        pass
