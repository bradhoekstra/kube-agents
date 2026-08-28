#!/usr/bin/env python3
"""Submit a supported CLI argv vector to the paired credential proxy."""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


SUPPORTED_EXECUTABLES = ("kubectl", "gcloud", "gh", "git")

# Hostnames that mean "the proxy is in this pod", and therefore that a local
# path means the same thing on both sides of the call.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", ""})

# How long to wait to reach the broker. Bounds the connect only — see
# BrokerConnection.
BROKER_CONNECT_TIMEOUT_SECONDS = 10.0


class BrokerConnection(http.client.HTTPConnection):
    """Bound how long we wait to reach the broker, not how long it works.

    A plain ``urlopen(request, timeout=N)`` sets one socket timeout for the
    whole exchange, which would put a ceiling on the command as well as on the
    connect. That ceiling must not exist: Envoy routes /v1/exec with
    ``timeout: 0s`` deliberately, because a proxied ``gcloud container clusters
    get-credentials`` or a large ``git clone`` legitimately runs for minutes.

    Before the split there was no need for either — the broker was on the Pod's
    own loopback, so a connect either succeeded or was refused at once. Now the
    call crosses a Service. A Pending broker still fails fast, with
    ``[Errno 111] Connection refused`` from a Service that has no endpoints;
    what hangs is a SYN that is dropped rather than rejected, which is exactly
    what a default-deny egress policy does. So: a timeout while connecting, and
    none once connected.
    """

    def connect(self) -> None:
        self.timeout = BROKER_CONNECT_TIMEOUT_SECONDS
        super().connect()
        self.sock.settimeout(None)


class _BrokerHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(BrokerConnection, req)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect out of the broker.

    urllib re-sends the Authorization header across a cross-host redirect, so
    a 302 in a broker response would hand the projected token to wherever the
    Location points. Only reachable by something that already controls the
    broker's responses, but the header is the one thing worth not leaking on
    the way out.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


# A private opener rather than urllib.request.install_opener: this module is
# imported by github_token_refresh and the two relay patches, and a global
# opener would strip the total timeouts their own urlopen calls rely on.
_BROKER_OPENER = urllib.request.build_opener(_BrokerHTTPHandler, _NoRedirect())


def open_broker_request(request: urllib.request.Request):
    """Send `request` to the broker with a bounded connect."""
    return _BROKER_OPENER.open(request)


class TokenUnavailable(Exception):
    """The configured caller token could not be read."""


def authorization_headers() -> dict[str, str]:
    """Return the credential that identifies this caller to the broker.

    Empty when CREDENTIAL_PROXY_TOKEN_FILE is unset, which is the sidecar
    deployment: there the broker is reachable only on the Pod's own loopback,
    behind a socket only its own container can open, and it asks for no
    credential. When the broker runs in its own Pod the operator projects a
    ServiceAccount token with the broker's audience into this container and
    points this variable at it.

    Read on every invocation, never cached: the kubelet rewrites a projected
    token in place as it approaches expiry, and this process is short-lived
    enough that re-reading costs nothing.
    """
    token_file = os.environ.get("CREDENTIAL_PROXY_TOKEN_FILE", "").strip()
    if not token_file:
        return {}
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TokenUnavailable(f"{token_file}: {exc.strerror or exc}") from exc
    if not token:
        raise TokenUnavailable(f"{token_file} is empty")
    return {"Authorization": f"Bearer {token}"}

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
    headers = {"Content-Type": "application/json"}
    try:
        headers.update(authorization_headers())
    except TokenUnavailable as exc:
        # Sending the request anyway would earn an undifferentiated 401 and
        # hide the real fault, which is a broken token projection.
        print(f"credential proxy token unavailable: {exc}", file=sys.stderr)
        return 1
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/exec",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with open_broker_request(request) as response:
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
        # Two spellings of the same field, because the broker has two error
        # shapes: a refusal from `ContentWorkspaceError` answers `{status,
        # code, message}`, while a malformed body answers `{error}`. Reading
        # only one of them would render half the broker's refusals as the
        # generic fallback below, which is the sentence that tells a caller
        # nothing.
        super().__init__(
            payload.get("message")
            or payload.get("error")
            or f"workspace request failed ({status})"
        )
        self.status = status
        self.payload = payload


class Listing(list):
    """The entries `list` returned, plus what it did not return.

    A plain list, so every existing caller keeps working, carrying the two
    fields that say whether it is the whole answer. A listing that stops at the
    broker's ceiling and looks complete is how a caller ends up asking `read`
    for a path it inferred rather than one it saw.
    """

    def __init__(self, entries, total: int = 0, truncated: bool = False) -> None:
        super().__init__(entries)
        self.total = total or len(self)
        self.truncated = truncated


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
        self.shallow = bool(opened.get("shallow", False))
        self.branch: str | None = None
        self._closed = False

    @classmethod
    def open(
        cls,
        endpoint: str,
        repo: str,
        base: str | None = None,
        branch: str | None = None,
        depth: int | None = None,
    ) -> "Workspace":
        """`branch` names the branch this session will commit to, if known.

        Naming it decides what `read` and `list` answer with: when the branch
        already exists on the remote -- a second round of review feedback -- the
        broker checks that out rather than the base, so a file read here is the
        file as the pull request has it.

        `depth` opens a shallow single-branch clone for reading. The broker
        refuses `commit` on one and refuses `depth` together with `branch`.
        """
        payload = {"repo": repo}
        if base:
            payload["base"] = base
        if branch:
            payload["branch"] = branch
        if depth:
            payload["depth"] = depth
        return cls(endpoint, _workspace_call(endpoint, "open", payload))

    def read(self, path: str) -> bytes:
        result = _workspace_call(
            self.endpoint, "read", {"handle": self.handle, "path": path}
        )
        return base64.b64decode(result["contentBase64"])

    def read_text(self, path: str, encoding: str = "utf-8") -> str:
        return self.read(path).decode(encoding)

    def read_many(self, paths: list[str]) -> tuple[dict[str, bytes], list[dict]]:
        """Several files in one round trip.

        Returns what came back and what did not, the second as the broker's own
        `{path, reason}` records. A caller that ignores the second half will
        materialise a partial tree and not know it -- `requestBudget` means ask
        again for the rest, and `tooLarge` means that file is never coming.
        """
        result = _workspace_call(
            self.endpoint, "read", {"handle": self.handle, "paths": list(paths)}
        )
        files = {
            entry["path"]: base64.b64decode(entry["contentBase64"])
            for entry in result.get("files", [])
        }
        return files, result.get("skipped", [])

    def list(self, prefix: str | None = None, after: str | None = None) -> Listing:
        """One page of tracked names. `after` is the last path of the page before.

        `total` on the result counts what is still in scope after the cursor, so
        a caller pages until `truncated` is false.
        """
        payload = {"handle": self.handle}
        if prefix:
            payload["prefix"] = prefix
        if after:
            payload["after"] = after
        result = _workspace_call(self.endpoint, "list", payload)
        return Listing(
            result.get("entries", []),
            total=result.get("total", 0),
            truncated=bool(result.get("truncated")),
        )

    def grep(
        self,
        pattern: str,
        prefix: str | None = None,
        regex: bool = False,
        ignore_case: bool = False,
    ) -> dict:
        """Search the checked-out tree. Fixed-string unless `regex` is set.

        The whole answer, `{matches, total, truncated}`, rather than the matches
        alone: a search that hit the broker's ceiling and looks complete sends a
        reader off with a wrong conclusion about the repository.
        """
        payload = {"handle": self.handle, "pattern": pattern}
        if prefix:
            payload["prefix"] = prefix
        if regex:
            payload["regex"] = True
        if ignore_case:
            payload["ignoreCase"] = True
        return _workspace_call(self.endpoint, "grep", payload)

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
        # `committed: false` is an ordinary answer rather than an error -- a
        # re-run whose fix is already on the branch has nothing to add -- and it
        # carries neither a sha nor a commit. Callers read the flag; reading
        # `result["commit"]` unconditionally is how that case turns into a
        # KeyError several frames from the decision that produced it.
        if result.get("baseSha"):
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
    headers = {"Content-Type": "application/json"}
    # The same credential and the same opener `execute` uses. These routes
    # spend the broker's GitHub token exactly as /v1/exec does, so a
    # cross-Pod call that omitted the header would earn a 401, and one that
    # went through the stock opener would carry a total socket timeout onto a
    # clone that legitimately runs for minutes.
    headers.update(authorization_headers())
    request = urllib.request.Request(
        endpoint.rstrip("/") + f"/v1/workspace/{verb}",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with open_broker_request(request) as response:
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
