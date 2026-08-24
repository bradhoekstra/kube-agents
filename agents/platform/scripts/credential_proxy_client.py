#!/usr/bin/env python3
"""Submit a supported CLI argv vector to the paired credential proxy."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid


SUPPORTED_EXECUTABLES = ("kubectl", "gcloud", "gh", "git")

# Hostnames that mean "the proxy is in this pod", and therefore that a local
# path means the same thing on both sides of the call.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", ""})

# Only these read KUBECONFIG: kubectl to pick a context, gcloud to write one in
# `container clusters get-credentials`. `git` and `gh` ignore the variable, so
# forwarding it to them buys nothing and costs plenty — the server rejects an
# out-of-workspace path rather than ignoring it, which would turn a stray
# KUBECONFIG into a 400 on a command that has nothing to do with Kubernetes.
KUBECONFIG_AWARE = frozenset({"kubectl", "gcloud"})

# Flags whose value may be `-`, meaning "read the document from stdin". This is
# the whole list the shipped skills use: kubectl's `-f`/`--filename` and
# `--patch-file`, and gh's `--body-file`.
STDIN_FILE_FLAGS = frozenset({"-f", "--filename", "--patch-file", "--body-file"})


def reads_stdin(argv: list[str]) -> bool:
    """Whether this argv asks, explicitly, to read a document from stdin.

    The shim has never forwarded stdin, and the comment in `__main__` gives the
    reason: an MCP or other stdio-based parent may have a protocol stream on
    fd 0, and consuming it would break the parent rather than the command. That
    reason is sound and this does not overrule it -- it narrows it. Reading fd 0
    only when a flag in `STDIN_FILE_FLAGS` is followed by a bare `-` means the
    read happens when the caller wrote `kubectl apply -f -` and at no other
    time, and no MCP server is invoked that way.

    The consequence of getting this wrong is asymmetric and the narrow form errs
    the safe way: reading when we should not corrupts a parent's protocol
    stream, while not reading when we should leaves the command receiving an
    empty document -- which is exactly the behaviour today.
    """
    for index, token in enumerate(argv):
        if token in STDIN_FILE_FLAGS and index + 1 < len(argv) and argv[index + 1] == "-":
            return True
        if "=" in token:
            flag, _, value = token.partition("=")
            if flag in STDIN_FILE_FLAGS and value == "-":
                return True
    return False


def shares_filesystem_with_proxy(endpoint: str) -> bool:
    """Whether a path sent to `endpoint` names the same file the caller means.

    Both path-valued fields in the request — `cwd` and `kubeconfig` — are
    resolved by the server against its own filesystem. That was always safe
    while the proxy was a sidecar. It is wrong the moment the caller is in
    another pod: the sandbox's `/opt/data` is its own volume, and the server
    would either reject the path for being outside its workspace or, worse,
    open a same-named file of its own. So a cross-pod caller sends neither, and
    the server falls back to its own workspace.

    The cost is that `git` cannot be driven from another pod — the lease check
    it runs is a statement about a directory the proxy can see, and there is no
    such directory. See docs/designs/agent-shell-sandboxing.md, "The workspace
    check".
    """
    return (urllib.parse.urlsplit(endpoint).hostname or "") in LOOPBACK_HOSTS


def execute(
    endpoint: str,
    argv: list[str],
    stdin: str | None = None,
) -> int:
    request_payload = {
        "requestId": str(uuid.uuid4()),
        "argv": argv,
    }
    local = shares_filesystem_with_proxy(endpoint)
    if local:
        request_payload["cwd"] = os.getcwd()
    # The command runs in the proxy, so the caller's environment is not
    # inherited. KUBECONFIG is the one variable an agent legitimately needs to
    # steer: Cluster Agent profiles pin themselves to a target cluster with it
    # (see agents/cluster/config.yaml). Forward the path and let the server
    # decide whether it is acceptable — it only honours paths inside the shared
    # workspace. Whitespace is stripped because profile .env files routinely
    # carry a trailing newline.
    if local and argv and argv[0] in KUBECONFIG_AWARE:
        kubeconfig = os.environ.get("KUBECONFIG", "").strip()
        if kubeconfig:
            request_payload["kubeconfig"] = kubeconfig
    if stdin is not None:
        request_payload["stdin"] = stdin
    body = json.dumps(
        request_payload,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/exec",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        # The proxy's own errors are JSON, but the error can also come from
        # whatever sits between shim and proxy — an Envoy restarting mid-request
        # answers 503 with an HTML body, and a traceback here turns a transient
        # sidecar blip into a shim crash the agent cannot read.
        try:
            payload = json.load(exc)
        except (ValueError, TypeError):
            print(
                f"credential proxy error (HTTP {exc.code}): non-JSON response",
                file=sys.stderr,
            )
            return 1
        if payload.get("code") == "SECURITY_POLICY_BLOCKED":
            print(
                payload.get("message", "Command blocked for security reasons."),
                file=sys.stderr,
            )
            print(f"policy rule: {payload.get('rule', 'unknown')}", file=sys.stderr)
            return 126
        print(payload.get("error", str(exc)), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"credential proxy unavailable: {exc.reason}", file=sys.stderr)
        return 1

    sys.stdout.write(payload.get("stdout", ""))
    sys.stderr.write(payload.get("stderr", ""))
    if payload.get("truncated"):
        print("credential proxy output truncated", file=sys.stderr)
    return int(payload.get("exitCode", 1))


class WorkspaceUnavailable(RuntimeError):
    """The broker does not have content workspaces armed."""


class WorkspaceRequestError(RuntimeError):
    """The broker refused. `status` and `payload` carry its answer verbatim."""

    def __init__(self, status: int, payload: dict) -> None:
        super().__init__(payload.get("error", f"workspace request failed ({status})"))
        self.status = status
        self.payload = payload


class Workspace:
    """A git repository the broker owns and this process cannot see.

    There is no path anywhere in this class, which is the point. A caller says
    "write these bytes to `manifests/app.yaml` and commit them"; it never learns
    where that file lands, so it cannot be talked into reading or writing
    anything else there -- including `.git/config`, which is where a filter
    driver or a hook path would have to be defined for the sixteen known
    code-execution routes to work.

    Typical use, replacing a clone/add/commit/push sequence:

        with Workspace.open(endpoint, "acme/infra") as workspace:
            current = workspace.read_text("manifests/app.yaml")
            workspace.commit(
                branch="fix/replicas",
                message="raise replicas",
                changes={"manifests/app.yaml": patched.encode()},
            )
            workspace.push()
    """

    def __init__(self, endpoint: str, opened: dict) -> None:
        self.endpoint = endpoint
        self.handle = opened["handle"]
        self.repo = opened["repo"]
        self.base = opened["base"]
        self.base_sha = opened["baseSha"]
        self.started_from = opened.get("startedFrom", "")
        self.branch: str | None = None
        self._closed = False

    @classmethod
    def open(
        cls,
        endpoint: str,
        repo: str,
        base: str | None = None,
        branch: str | None = None,
    ) -> "Workspace":
        """`branch` names the branch this session will commit to, if known.

        Naming it decides what `read` and `list` answer with: when the branch
        already exists on the remote -- a second round of review feedback -- the
        broker checks that out rather than the base, so a file read here is the
        file as the pull request has it.
        """
        payload = {"repo": repo}
        if base:
            payload["base"] = base
        if branch:
            payload["branch"] = branch
        return cls(endpoint, _workspace_call(endpoint, "open", payload))

    def read(self, path: str) -> bytes:
        result = _workspace_call(
            self.endpoint, "read", {"handle": self.handle, "path": path}
        )
        return base64.b64decode(result["contentBase64"])

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.read(path).decode(encoding)

    def list(self, prefix: str | None = None) -> list[dict]:
        payload = {"handle": self.handle}
        if prefix:
            payload["prefix"] = prefix
        return _workspace_call(self.endpoint, "list", payload)["entries"]

    def commit(
        self,
        branch: str,
        message: str,
        changes: dict[str, bytes | None],
        expected_base_sha: str | None = None,
    ) -> dict:
        """`changes` maps a repository-relative path to bytes, or to None to delete.

        Pass `expected_base_sha` (normally `self.base_sha`) to have the broker
        refuse with 409 when the base branch has moved under a file this commit
        also writes. Leaving it out means last-writer-wins against whatever
        landed in the meantime.
        """
        entries = []
        for path, content in changes.items():
            if content is None:
                entries.append({"path": path, "delete": True})
            else:
                entries.append(
                    {
                        "path": path,
                        "contentBase64": base64.b64encode(content).decode("ascii"),
                    }
                )
        payload = {
            "handle": self.handle,
            "branch": branch,
            "message": message,
            "changes": entries,
        }
        if expected_base_sha:
            payload["expectedBaseSha"] = expected_base_sha
        result = _workspace_call(self.endpoint, "commit", payload)
        self.branch = result["branch"]
        self.base_sha = result["baseSha"]
        return result

    def push(self, branch: str | None = None) -> dict:
        branch = branch or self.branch
        if not branch:
            raise ValueError("nothing has been committed on this workspace yet")
        return _workspace_call(
            self.endpoint, "push", {"handle": self.handle, "branch": branch}
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _workspace_call(self.endpoint, "close", {"handle": self.handle})

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *_exc) -> None:
        # Best effort: a failure to clean up a broker-side tree must not mask
        # the exception that is already propagating out of the with-block.
        try:
            self.close()
        except Exception:
            pass


def workspaces_available(endpoint: str) -> bool:
    """Whether this broker has content workspaces armed.

    Both mechanisms run side by side while the skills migrate, so a caller that
    can do either asks first rather than assuming.
    """
    try:
        _workspace_call(endpoint, "open", {"repo": ""})
    except WorkspaceUnavailable:
        return False
    except WorkspaceRequestError:
        # It answered about the payload rather than about the feature, so the
        # route exists.
        return True
    except urllib.error.URLError:
        return False
    return True


def _workspace_call(endpoint: str, verb: str, payload: dict) -> dict:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint.rstrip("/") + f"/v1/workspace/{verb}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            answer = json.load(exc)
        except (ValueError, TypeError):
            raise WorkspaceRequestError(exc.code, {"error": f"HTTP {exc.code}"}) from exc
        if answer.get("code") == "CONTENT_WORKSPACES_DISABLED":
            raise WorkspaceUnavailable(answer.get("error", "not enabled")) from exc
        raise WorkspaceRequestError(exc.code, answer) from exc


def read_stdin_if_requested(argv: list[str]) -> str | None:
    """fd 0, but only for an argv that named `-` as an input file.

    Still `None` when fd 0 is a terminal: an interactive `kubectl apply -f -`
    with nothing piped in would otherwise hang the shim on a read that never
    returns, which reads to the agent as the proxy being down.
    """
    if not reads_stdin(argv):
        return None
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        return sys.stdin.read()
    except (OSError, ValueError):
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--endpoint",
        default=os.getenv("CREDENTIAL_PROXY_URL"),
        required=os.getenv("CREDENTIAL_PROXY_URL") is None,
    )
    parser.add_argument(
        "executable",
        choices=SUPPORTED_EXECUTABLES,
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    return parser.parse_args()


if __name__ == "__main__":
    invoked_as = os.path.basename(sys.argv[0])
    if invoked_as in set(SUPPORTED_EXECUTABLES):
        endpoint = os.getenv("CREDENTIAL_PROXY_URL")
        if endpoint is None:
            print("CREDENTIAL_PROXY_URL is not configured", file=sys.stderr)
            raise SystemExit(1)
        argv = [invoked_as, *sys.argv[1:]]
        stdin = read_stdin_if_requested(argv)
    else:
        args = parse_args()
        endpoint = args.endpoint
        argv = [args.executable, *args.arguments]
        stdin = read_stdin_if_requested(argv)
    raise SystemExit(
        execute(
            endpoint,
            argv,
            stdin=stdin,
        )
    )
