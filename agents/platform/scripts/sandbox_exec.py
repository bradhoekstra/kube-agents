#!/usr/bin/env python3
"""Run a cluster command in the shell sandbox rather than in the agent pod.

The agent image carries no `kubectl`, `gcloud`, `gh` or `git`, in any form: not
the binaries, and not the credential-proxy shims that used to stand in for them.
`deploy/docker/Dockerfile` step 2 removed the first, the note where the symlinks
used to be removed the second, and the guard at the end of the `platform` stage
fails the build if either comes back. Agent-side code with a reason to invoke one
— the platform MCP server, the cluster-agent scripts, `gke_endpoint.py`,
`forge.py`, `resolver.py` — calls `run()` here instead of `subprocess.run`, and
the command executes in the sandbox.

Several of those files run on both sides of the boundary: `resolver.py poll` is a
subprocess of the agent pod's cron gate, while `resolver.py claim` is invoked by
the model from a shell that is already in the sandbox. One call site serves both,
because `sandbox_enabled()` is false in the sandbox — the managed config it reads
is an agent-pod file — and `run()` then executes locally.

Two things about this module are load-bearing and easy to undo by accident.

It connects as `hermes`, not as `terminal.ssh_user`. That setting is the login
Hermes gives the model's shell, and it owns its own home directory in the
sandbox; bash sources `~/.bashrc` even for a non-interactive `ssh host cmd`, so
a caller authenticating as it would run the model's startup file before its own
command and could be handed forged output as a trusted tool result. Debian's
stock non-interactive guard at the top of `.bashrc` hides this, and the model
can delete the guard. `deploy/sandbox/Dockerfile` creates the second account.

It does not build the ssh subprocess environment from `os.environ`. The agent
pod holds `API_SERVER_KEY` and `SESSION_KV_API_KEY`, and `_run_env()` in
`agent_common_server.py` — the helper most of these call sites used to pass —
is `{**os.environ, "HOME": "/tmp"}`. Nothing crosses today, because the
sandbox's `sshd_config` sets `PermitUserEnvironment no` and `AcceptEnv LANG
LC_*`, but that is the remote end declining what this end should not offer.
Variables the remote command genuinely needs go through `remote_env`, which
renders them into the command line rather than into the client's environment.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess

# The sandbox login for trusted agent-pod callers. Not terminal.ssh_user; see
# the module docstring.
SANDBOX_PRINCIPAL = "hermes"

# The model's own account, and the `principal=` argument every caller but one
# must not pass. One key authorises both logins, so this is a username on the
# command line rather than a second credential, and the separation the module
# docstring describes is the only thing keeping them apart.
#
# `kanban_workspace_gc.py` passes it because the scratch workspaces it removes
# are `agent:agent 755` to the leaves, so uid 1001 cannot unlink inside them,
# and the alternative — loosening the modes and the umask in the sandbox image
# so a shared group could — buys a wider grant than the narrower login does.
# What makes it safe there does not generalise: that caller consumes no output
# as a fact about the cluster, and a `.bashrc` that hijacked its `rm` would be
# doing to uid 1000's own files what uid 1000 can already do. A caller that
# reads a command's output and believes it must use the default.
TERMINAL_PRINCIPAL = "agent"

MANAGED_CONFIG_PATH = os.environ.get("HERMES_MANAGED_CONFIG_PATH", "/etc/hermes/config.yaml")

# ssh reserves 255 for its own failures, and a remote command is free to exit
# 255 as well. The two are told apart by what ssh says on stderr when it is the
# one failing, which is the only signal available: a wrapper that appended its
# own exit-code sentinel to stdout would corrupt the output of every command
# that returns anything but text.
_SSH_LEVEL_ERRORS = re.compile(
    r"(ssh: connect to host|Connection (refused|closed|timed out)|"
    r"Could not resolve hostname|Permission denied \(publickey|"
    r"Host key verification failed|kex_exchange_identification|"
    r"Operation timed out|No route to host)",
    re.IGNORECASE,
)

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_CONNECT_OPTIONS = (
    # No user ssh config: this connection is fully described here, and a config
    # file appearing under the agent pod's HOME must not be able to redirect it.
    "-F", "/dev/null",
    "-T",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    # Without these a connection to an evicted pod can sit half-open, and a call
    # that should have failed in seconds blocks until the caller's own timeout.
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    # get_cc_pod_diagnostics alone makes three calls. Multiplexing pays the key
    # exchange once and reuses the connection for the rest of the burst.
    "-o", "ControlMaster=auto",
    "-o", "ControlPersist=60s",
)


class SandboxUnavailable(RuntimeError):
    """ssh could not reach the sandbox, so the command never ran.

    Distinct from the command running and failing: the caller can retry this
    one, and a diagnostic that reports it as a cluster problem is wrong.
    """


def _load_terminal_config(path: str | None = None) -> dict:
    """Read the `terminal:` block from the operator-managed Hermes config.

    Returns `{}` when the file is missing or unreadable rather than raising:
    the answer that matters to every caller is `sandbox_enabled()`, and a
    missing managed config means no sandbox, not a broken agent.
    """
    config_path = path or MANAGED_CONFIG_PATH
    try:
        import yaml  # noqa: PLC0415 — optional at import time, see below

        with open(config_path, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except (OSError, ImportError):
        return {}
    except Exception:  # pragma: no cover — a malformed managed config
        return {}
    terminal = loaded.get("terminal")
    return terminal if isinstance(terminal, dict) else {}


def sandbox_enabled(path: str | None = None) -> bool:
    """True when the managed config points the shell at an SSH sandbox."""
    return _load_terminal_config(path).get("backend") == "ssh"


def _control_path_dir() -> str:
    """A short, writable directory for the multiplexing control socket.

    Short matters: a unix socket path is capped near 104 bytes and `%C` is
    already a hash, so the directory is the part with room to overflow.
    """
    base = os.environ.get("TMPDIR", "/tmp")
    directory = os.path.join(base, ".sandbox-ssh")
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError:
        return ""
    return directory


def _remote_command(argv: list[str], remote_env: dict[str, str] | None, cwd: str | None) -> str:
    """Render argv into one string for the sandbox's login shell to parse.

    ssh has no argv-preserving mode — the remote shell always re-parses — so
    every element is quoted here. This is a correctness requirement rather than
    a boundary one: the model already has a shell in the sandbox, so it gains
    nothing by injecting into this one, but a pod name containing a quote must
    not silently become a different command.
    """
    parts: list[str] = []
    if cwd:
        parts.append(f"cd {shlex.quote(cwd)} &&")
    if remote_env:
        parts.append("env")
        for name, value in remote_env.items():
            if not _ENV_NAME.match(name):
                raise ValueError(f"invalid environment variable name: {name!r}")
            parts.append(f"{name}={shlex.quote(str(value))}")
    parts.extend(shlex.quote(arg) for arg in argv)
    return " ".join(parts)


def ssh_argv(argv: list[str], *, remote_env: dict[str, str] | None = None,
             cwd: str | None = None, path: str | None = None,
             principal: str = SANDBOX_PRINCIPAL) -> list[str]:
    """Build the full ssh command line for `argv`. Exposed for tests."""
    terminal = _load_terminal_config(path)
    host = terminal.get("ssh_host")
    if not host:
        raise SandboxUnavailable("managed config names no terminal.ssh_host")

    known_hosts = os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), ".ssh", "known_hosts")
    command = ["ssh", *_CONNECT_OPTIONS,
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", f"UserKnownHostsFile={known_hosts}"]

    control_dir = _control_path_dir()
    if control_dir:
        command += ["-o", f"ControlPath={os.path.join(control_dir, '%C')}"]

    key = terminal.get("ssh_key")
    if key:
        # IdentitiesOnly stops ssh offering any agent-held key first and
        # tripping MaxAuthTries before it reaches the one that works.
        command += ["-i", str(key), "-o", "IdentitiesOnly=yes"]
    port = terminal.get("ssh_port")
    if port:
        command += ["-p", str(port)]

    if principal not in (SANDBOX_PRINCIPAL, TERMINAL_PRINCIPAL):
        raise ValueError(f"not a sandbox login: {principal!r}")
    command.append(f"{principal}@{host}")
    command.append(_remote_command(argv, remote_env, cwd))
    return command


def _client_env() -> dict[str, str]:
    """The environment for the ssh client. Deliberately not `os.environ`."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        # ssh reads HOME for a config this call already suppressed with -F, but
        # an unset HOME makes it complain rather than proceed.
        "HOME": os.environ.get("TMPDIR", "/tmp"),
    }
    for passthrough in ("LANG", "TMPDIR"):
        value = os.environ.get(passthrough)
        if value:
            env[passthrough] = value
    return env


def run(argv: list[str], *, remote_env: dict[str, str] | None = None,
        local_env: dict[str, str] | None = None,
        cwd: str | None = None, timeout: float | None = None,
        check: bool = False, path: str | None = None,
        principal: str = SANDBOX_PRINCIPAL) -> subprocess.CompletedProcess:
    """Run `argv` in the sandbox and return the finished process.

    `principal` selects the sandbox login and should be left alone; see
    `TERMINAL_PRINCIPAL` for the single caller that does not.

    `remote_env` names the variables the command itself needs; they are
    rendered into the remote command line. `local_env` replaces the environment
    of the local fallback for the one caller that has to subtract a variable
    rather than add one — see `_default_runner` in `gke_endpoint.py`, where a
    forwarded `KUBECONFIG` turns a `describe` into a guaranteed HTTP 400. It has
    no remote counterpart because the remote command inherits nothing from here.

    Falls back to running locally when no sandbox is configured. Two different
    situations reach that branch and it is right for both. In the agent pod it
    means the install turned the sandbox off, and the image carries no
    credentialed binary, so the call fails with "command not found" — the honest
    report, and why the fallback is a plain `subprocess.run` rather than an error
    raised here. In the sandbox it is the normal case: `resolver.py` and
    `forge.py` also run there, there is no managed config to read, and local is
    where the command belongs.

    Raises SandboxUnavailable when ssh itself could not connect.
    """
    if not sandbox_enabled(path):
        base = local_env if local_env is not None else {**os.environ, "HOME": "/tmp"}
        return subprocess.run(argv, capture_output=True, text=True, check=check,
                              timeout=timeout, cwd=cwd, env={**base, **(remote_env or {})})

    command = ssh_argv(argv, remote_env=remote_env, cwd=cwd, path=path,
                       principal=principal)
    completed = subprocess.run(command, capture_output=True, text=True,
                               timeout=timeout, env=_client_env())
    if completed.returncode == 255 and _SSH_LEVEL_ERRORS.search(completed.stderr or ""):
        raise SandboxUnavailable(
            f"could not reach the shell sandbox: {(completed.stderr or '').strip()}"
        )
    # The argv the caller passed, not the ssh wrapper. A CalledProcessError or a
    # log line quoting `.args` should name the command that failed rather than
    # the transport that carried it, and callers that already inspect `.args`
    # keep working unchanged. Set before the `check` raise so both paths agree.
    completed.args = argv
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode, argv, completed.stdout, completed.stderr
        )
    return completed
