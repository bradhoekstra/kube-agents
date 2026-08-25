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
from pathlib import Path
from typing import Any, Callable

# The path rule is shared with the reader, which does not ship this module --
# see workspace_paths for why it is a file of its own. Re-exported so that
# `content_workspace.validate_path` and `content_workspace.WorkspaceError` stay
# where every caller already looks for them.
from workspace_paths import (  # noqa: F401
    WorkspaceError,
    _HFS_IGNORABLE,
    _looks_like_dotgit,
    validate_path,
)

LOGGER = logging.getLogger("credential-proxy.workspace")

# Small on purpose. This carries Kubernetes manifests and pull-request bodies,
# not build artefacts, and a ceiling sized for the former is a ceiling that
# makes the latter fail loudly instead of quietly becoming a supported use.
DEFAULT_MAX_FILE_BYTES = 1 << 20  # 1 MiB
DEFAULT_MAX_REQUEST_BYTES = 8 << 20  # 8 MiB
DEFAULT_MAX_ENTRIES = 256

# Reading is a different shape of request from writing. A commit carries a
# handful of manifests; searching a repository the agent has never seen returns
# whatever the pattern hits, and the number that matters is how many matches a
# reader can act on rather than how many bytes a tree holds.
DEFAULT_MAX_MATCHES = 200
DEFAULT_MAX_MATCH_CHARS = 512

# The broker's trees live on its own volume, which is an emptyDir sized for
# Kubernetes manifests. A clone that does not fit is refused and removed rather
# than left to fill the disk out from under every other handle. The ceiling
# bounds what stays, not what a clone transiently touches -- git has already
# written the objects by the time this is measured.
DEFAULT_MAX_CLONE_BYTES = 256 << 20  # 256 MiB

# A handle is 128 bits from os.urandom and lives only in this process's memory.
# The agent cannot fabricate one, which is the property the `.lease` file it
# replaces never had -- that was a file on a shared volume, and creating it
# unlocked every mutating verb.
_HANDLE_BYTES = 16

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

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


def validate_depth(raw: Any) -> int | None:
    """A positive commit count, or `None` for the full history.

    `True` is an `int` in Python and would otherwise reach git as `--depth 1`
    from a caller that meant "yes, shallow" without saying how shallow. Refuse
    it: a boolean where a count belongs is a caller whose next request will
    disagree with what it got.
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise WorkspaceError("depth must be a positive integer number of commits")
    if raw < 1:
        raise WorkspaceError("depth must be a positive integer number of commits")
    return raw


@dataclass
class _Workspace:
    handle: str
    repo: str
    root: Path
    base: str
    base_sha: str
    branch: str | None = None
    shallow: bool = False
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
        self.max_matches = _positive_int(
            "CREDENTIAL_PROXY_MAX_MATCHES", DEFAULT_MAX_MATCHES
        )
        self.max_match_chars = _positive_int(
            "CREDENTIAL_PROXY_MAX_MATCH_CHARS", DEFAULT_MAX_MATCH_CHARS
        )
        self.max_clone_bytes = _positive_int(
            "CREDENTIAL_PROXY_MAX_CLONE_BYTES", DEFAULT_MAX_CLONE_BYTES
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
        """Clone a repository into a tree only this process can name.

        `depth` makes it a shallow single-branch clone, which is what reading an
        unfamiliar repository wants: the history is not what is being analysed
        and a full clone of a large one does not fit the broker's volume. It is
        a read-only workspace — `commit` refuses on it, for the reasons in that
        method — and it is refused together with `branch`, because a
        single-branch clone cannot see whether the branch exists on the remote
        and would silently answer from the base instead.
        """
        owner, name = validate_repo(payload.get("repo"))
        repo = f"{owner}/{name}"
        depth = validate_depth(payload.get("depth"))
        requested = payload.get("branch")
        if depth is not None and requested is not None:
            raise WorkspaceError(
                "depth and branch cannot be combined: a shallow clone fetches "
                "one branch, so the check for whether the working branch "
                "already exists on the remote would always answer no"
            )
        base = payload.get("base")
        base = validate_branch(base, "base") if base is not None else None
        handle = os.urandom(_HANDLE_BYTES).hex()
        root = self.tree_root / handle
        root.mkdir(parents=True, exist_ok=False)
        url = f"https://github.com/{owner}/{name}.git"
        # --no-recurse-submodules: a .gitmodules in the remote would otherwise
        # fetch a second repository whose content nobody validated, and
        # submodule plumbing reads config keys this tree is not hardened for.
        argv = ["clone", "--quiet", "--no-recurse-submodules"]
        if depth is not None:
            argv += ["--depth", str(depth), "--single-branch"]
            if base is not None:
                argv += ["--branch", base]
        argv += [url, "."]
        try:
            self._git(root, *argv)
            if base is None:
                base = self._origin_head(root)
            self._enforce_clone_ceiling(root, repo)
            self._git(root, "checkout", "--force", "-B", base, f"origin/{base}")
            base_sha = self._git(root, "rev-parse", "HEAD").stdout.strip()
            # An optional working branch, and the reason it is worth the
            # parameter: `read` and `list` answer from the tree that is checked
            # out. Left on the base, a second round of review feedback would be
            # written against the file as `main` has it rather than as the pull
            # request has it, and the reviewed work would be silently rewritten
            # out of the file.
            started_from = f"origin/{base}"
            if requested is not None:
                head = validate_branch(requested, "branch")
                if self._remote_branch_exists(root, head):
                    self._git(root, "checkout", "--force", "-B", head, f"origin/{head}")
                    started_from = f"origin/{head}"
        except BaseException:
            # A clone that failed halfway, or one refused by the ceiling, leaves
            # objects on the broker's volume that no handle names. Nothing would
            # ever collect them: `close` works from the handle, and this request
            # is not going to return one.
            _remove_tree(root)
            raise
        workspace = _Workspace(
            handle=handle,
            repo=repo,
            root=root,
            base=base,
            base_sha=base_sha,
            shallow=depth is not None,
        )
        self._workspaces[handle] = workspace
        LOGGER.info(
            "workspace opened repo=%s base=%s from=%s depth=%s",
            repo,
            base,
            started_from,
            depth if depth is not None else "full",
        )
        return {
            "handle": handle,
            "repo": repo,
            "base": base,
            "baseSha": base_sha,
            "startedFrom": started_from,
            "shallow": depth is not None,
        }

    def _enforce_clone_ceiling(self, root: Path, repo: str) -> None:
        """Refuse a clone that will not fit beside the others.

        Measured after the fact, which is the honest description of what this
        bounds: git has already written the objects, so the ceiling stops a
        large repository from *staying* on the volume rather than from touching
        it. `open` removes the tree on the way out, so the peak is one clone.
        """
        total = 0
        for directory, _subdirectories, filenames in os.walk(root):
            for filename in filenames:
                try:
                    total += os.lstat(os.path.join(directory, filename)).st_size
                except OSError:
                    continue
                if total > self.max_clone_bytes:
                    raise WorkspaceError(
                        f"{repo} is larger than the {self.max_clone_bytes}-byte "
                        "ceiling for a broker-side clone. Reopen it with a "
                        "smaller depth.",
                        status=413,
                        code="CLONE_TOO_LARGE",
                    )

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
        # `clone --single-branch` does not always leave `origin/HEAD` behind,
        # and the branch it checked out is the remote's default by definition.
        # Guessing `main` at a repository whose trunk is `master` fails at the
        # checkout below with a message about a ref, not about a default.
        local = self._git(
            root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
        )
        head = (local.stdout or "").strip()
        if local.returncode == 0 and head:
            return head
        return "main"

    def read(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One file by `path`, or several by `paths`.

        The batched form exists because materialising a repository the agent
        wants to analyse is otherwise one round trip per file, and a reader that
        pays a request per file reads fewer files than it should. Its response
        is a different shape on purpose — a batch reports what it did not return
        alongside what it did, and folding that into the single-file shape would
        mean either a 404 that carries content or content that carries a 404.
        """
        workspace = self._resolve(payload.get("handle"))
        if payload.get("paths") is None:
            return self._read_one(workspace, validate_path(payload.get("path")))
        return self._read_many(workspace, payload.get("paths"))

    def _read_one(self, workspace: _Workspace, path: str) -> dict[str, Any]:
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

    def _read_many(self, workspace: _Workspace, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, list) or not raw:
            raise WorkspaceError("paths must be a non-empty list")
        if len(raw) > self.max_entries:
            raise WorkspaceError(
                f"{len(raw)} paths is over the {self.max_entries}-path ceiling",
                status=413,
            )
        # Every name validated before the first byte is read, on the same
        # principle as `_validate_changes`: a request that names `.git/config`
        # in its last entry is refused rather than answered for the first
        # ninety-nine.
        paths = [validate_path(entry) for entry in raw]
        files: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        total = 0
        exhausted = False
        for path in paths:
            if exhausted:
                skipped.append({"path": path, "reason": "requestBudget"})
                continue
            target = workspace.root / path
            if target.is_symlink() or not target.is_file():
                skipped.append({"path": path, "reason": "notAFile"})
                continue
            size = target.stat().st_size
            if size > self.max_file_bytes:
                skipped.append({"path": path, "reason": "tooLarge", "size": size})
                continue
            if total + size > self.max_request_bytes:
                # Not an error. A caller asking for a directory's worth of files
                # cannot know the total in advance, and the answer it can act on
                # is "here is what fits, ask again for the rest" -- which is why
                # the remainder is named rather than dropped.
                exhausted = True
                skipped.append({"path": path, "reason": "requestBudget"})
                continue
            data = target.read_bytes()
            total += len(data)
            files.append(
                {
                    "path": path,
                    "contentBase64": base64.b64encode(data).decode("ascii"),
                    "size": len(data),
                }
            )
        return {"files": files, "skipped": skipped}

    def list(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Tracked names, a page at a time.

        `after` is a cursor: the last path of the previous page, and the next
        page starts strictly after it. `git ls-files` sorts by byte value and
        UTF-8 preserves code-point order, so the comparison here agrees with the
        order the names arrive in. Paging is what makes a repository the agent
        has never seen readable at all -- a listing that stops at the ceiling
        and says `truncated` is honest, but a caller with no way to ask for the
        rest still ends up guessing at names.
        """
        workspace = self._resolve(payload.get("handle"))
        prefix = payload.get("prefix")
        prefix = validate_path(prefix) if prefix else ""
        after = payload.get("after")
        after = validate_path(after) if after else ""
        # `git ls-files` rather than a filesystem walk: it answers with tracked
        # names, which is what the agent is entitled to know, and it cannot
        # surface `.git` because git does not track its own directory.
        args = ["ls-files", "-z"]
        if prefix:
            args += ["--", prefix]
        raw = self._git(workspace, *args).stdout
        entries = []
        total = 0
        for name in filter(None, raw.split("\0")):
            if after and name <= after:
                continue
            candidate = workspace.root / name
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            total += 1
            # Counted past the ceiling rather than stopped at it. A listing that
            # silently ends at the cap is the reason `read` gets asked for paths
            # that were invented: the caller saw a complete-looking answer, did
            # not find what it wanted in it, and guessed. `total` is what remains
            # in scope, so a caller can page or narrow the prefix instead.
            if len(entries) < self.max_entries:
                entries.append({"path": name, "size": size})
        return {
            "entries": entries,
            "total": total,
            "truncated": total > len(entries),
        }

    def grep(self, payload: dict[str, Any]) -> dict[str, Any]:
        """`git grep` over the checked-out tree.

        Reading a repository nobody here has seen before starts with a search,
        and without one the alternatives are both bad: fetch files by guessing
        at their names, or fetch the whole tree to search it locally. Neither
        the pattern nor the prefix can reach outside the tree -- `git grep`
        searches tracked files, so `.git` is not in scope however the pattern is
        written, and the pattern travels as the argument of `-e` so one starting
        with `-` is a pattern rather than an option.

        Fixed-string by default. A caller that wants a regular expression asks
        for one, which keeps a `.` in a filename from quietly matching more than
        the caller meant and keeps a pathological pattern behind a deliberate
        choice.
        """
        workspace = self._resolve(payload.get("handle"))
        pattern = payload.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise WorkspaceError("pattern must be a non-empty string")
        if "\x00" in pattern or "\n" in pattern or "\r" in pattern:
            raise WorkspaceError("pattern must not contain control characters")
        prefix = payload.get("prefix")
        prefix = validate_path(prefix) if prefix else ""
        # -I skips binary files, -n numbers the lines, -z puts a NUL after the
        # file name so a name carrying a colon cannot be misread as one.
        args = ["grep", "--no-color", "-I", "-n", "-z"]
        args.append("-E" if payload.get("regex") is True else "-F")
        if payload.get("ignoreCase") is True:
            args.append("-i")
        args += ["-e", pattern]
        if prefix:
            args += ["--", prefix]
        result = self._git(workspace, *args, check=False)
        if result.returncode not in (0, 1):
            # 1 is "no match". Anything else is a pattern git would not take,
            # which is only reachable with regex=True; its stderr quotes the
            # pattern back and is not returned, for the same reason no other
            # git stderr in this module is.
            raise WorkspaceError(
                "git could not search for that pattern; check the expression",
                status=400,
                code="BAD_PATTERN",
            )
        matches: list[dict[str, Any]] = []
        total = 0
        for record in (result.stdout or "").split("\n"):
            if not record:
                continue
            path, _, remainder = record.partition("\0")
            line, _, text = remainder.partition("\0")
            total += 1
            if len(matches) >= self.max_matches:
                continue
            match: dict[str, Any] = {
                "path": path,
                "line": int(line) if line.isdigit() else 0,
                "text": text[: self.max_match_chars],
            }
            if len(text) > self.max_match_chars:
                match["truncated"] = True
            matches.append(match)
        return {
            "matches": matches,
            "total": total,
            "truncated": total > len(matches),
        }

    def commit(self, payload: dict[str, Any]) -> dict[str, Any]:
        workspace = self._resolve(payload.get("handle"))
        if workspace.shallow:
            # Refused here rather than left to fail at `push`. A shallow clone
            # has no history to answer `_collisions` from, so every commit with
            # an `expectedBaseSha` would take the unanswerable-sha branch and
            # read as a conflict; and a remote may reject the push outright.
            # Both are failures a long way from the request that caused them.
            raise WorkspaceError(
                "this workspace was opened shallow, which makes it read-only. "
                "Open the repository again without depth to commit to it.",
                status=409,
                code="SHALLOW_WORKSPACE",
            )
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
