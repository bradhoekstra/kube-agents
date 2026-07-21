#!/usr/bin/env python3
# router_server.py - Chat Agent routing MCP server.
#
# Exposes discovery + relay tools so the front-door Chat Agent (the `default`
# profile) can learn which specialist Hermes profiles exist, what each is
# responsible for, delegate a fully-contextualized request to one of them, and
# relay the response back to the user.
#
# Coordination model: unlike the platform<->cluster coordination (pointer-only
# via worklog.py, where personas never exchange context directly), the Chat
# Agent is intentionally EXEMPT from the pointer-only rule. It is the
# conversational relay, not a peer specialist, so it passes full context in and
# returns the specialist's real response out.
#
# Transport: in-pod, local profile invocation (`hermes -p <name> -z ...`), the
# same reliable mechanism used by cluster_agent_profile.py. This reaches a
# specific profile in this pod, unlike the HTTP call_agent path which only
# reaches one gateway's default profile.

import os
import re
import subprocess
import sys

from pathlib import Path
from typing import Annotated

from pydantic import Field
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Chat Router")

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
# Hermes stores each profile at $HERMES_HOME/profiles/<name> (persists on the data PVC).
PROFILES_BASE = HERMES_HOME / "profiles"
# The front door itself; never a valid delegation target.
SELF_PROFILE = "default"
MAX_NAME_LEN = 63
# Match the platform_control / call_agent 5-minute ceiling for long reasoning loops.
INVOKE_TIMEOUT = int(os.environ.get("ROUTER_INVOKE_TIMEOUT", "300"))


def log(msg: str) -> None:
    print(f"[ROUTER-MCP] {msg}", file=sys.stderr)


def _run_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Env for subprocesses: HOME -> /tmp (writable creds) and HERMES_HOME pinned."""
    return {**os.environ, "HOME": "/tmp", "HERMES_HOME": str(HERMES_HOME), **(extra or {})}


def _summarize_soul(home: Path) -> str:
    """Fallback responsibilities: the first prose line of the profile's SOUL.md."""
    soul = home / "SOUL.md"
    if not soul.is_file():
        return ""
    try:
        lines = soul.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            continue
        return s
    return ""


def _responsibilities(home: Path) -> str:
    """A one-shot description of what a profile is responsible for.

    Prefers an explicit CAPABILITIES.md (the routing contract a specialist
    advertises to the Chat Agent); falls back to the SOUL.md summary so a
    profile is still discoverable even without a capabilities file.
    """
    cap = home / "CAPABILITIES.md"
    if cap.is_file():
        try:
            text = cap.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
        if text:
            return text
    return _summarize_soul(home)


def _discover() -> list[dict[str, str]]:
    """Enumerate every routable specialist profile (all profiles except `default`)."""
    agents: list[dict[str, str]] = []
    if PROFILES_BASE.is_dir():
        for p in sorted(PROFILES_BASE.iterdir()):
            if not p.is_dir() or p.name == SELF_PROFILE:
                continue
            agents.append({"name": p.name, "responsibilities": _responsibilities(p)})
    return agents


@mcp.tool()
def list_agents() -> str:
    """Discover every specialist agent you can route to, and what each is responsible for.

    Call this before delegating so you pick the right target. The registry is dynamic: newly
    created agents (for example a per-cluster agent spun up when a cluster is onboarded) appear
    here automatically. Returns one agent per line as `- <name>: <responsibilities>`.
    """
    agents = _discover()
    if not agents:
        return "No specialist agents are currently available to route to."
    return "\n".join(
        f"- {a['name']}: {a['responsibilities'] or '(no description provided)'}" for a in agents
    )


@mcp.tool()
def ask_agent(
    target_agent: Annotated[
        str,
        Field(
            description="Name of the specialist agent to delegate to, exactly as returned by "
            "list_agents (e.g. 'platform' or a 'cluster-...' profile). Must not be 'default'."
        ),
    ],
    query: Annotated[
        str,
        Field(
            description="The fully self-contained request to send. Include ALL context the agent "
            "needs (the user's intent plus relevant details from the conversation) — unlike "
            "inter-specialist coordination, you may and should pass full context here."
        ),
    ],
) -> str:
    """Delegate a fully-contextualized request to a specialist agent and return its response.

    Returns the specialist's response so you can relay it to the user. Use list_agents first to
    choose the right target. Do not route to 'default' (that is you).

    Transparency: when you relay this response, make it clear to the user WHICH agent handled the
    request — begin your reply with an attribution line naming `target_agent` (see your SOUL.md
    relay format). Never present a delegated answer as if you produced it yourself.
    """
    name = (target_agent or "").strip()
    if not name or not re.match(r"^[a-zA-Z0-9._-]+$", name) or len(name) > MAX_NAME_LEN:
        return f"ERROR: invalid target agent name: {target_agent!r}"
    if name == SELF_PROFILE:
        return (
            "ERROR: refusing to route to 'default' (that is you, the Chat Agent). "
            "Choose a specialist agent from list_agents."
        )

    home = PROFILES_BASE / name
    if not home.is_dir():
        return f"ERROR: agent '{name}' does not exist. Call list_agents to see available agents."

    env = _run_env()
    # A cluster profile pins its own KUBECONFIG in its home; honor it if present.
    kubeconfig = home / "kubeconfig.yaml"
    if kubeconfig.is_file():
        env["KUBECONFIG"] = str(kubeconfig)

    log(f"Delegating to profile '{name}' (query {len(query)} chars).")
    try:
        result = subprocess.run(
            ["hermes", "-p", name, "-z", query],
            capture_output=True, text=True, env=env, timeout=INVOKE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: agent '{name}' timed out after {INVOKE_TIMEOUT}s."
    except Exception as e:  # noqa: BLE001 - surface any invocation failure to the model
        return f"ERROR: failed to invoke agent '{name}': {e}"

    out = (result.stdout or "").strip()
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        return f"ERROR: agent '{name}' exited {result.returncode}: {err or out or '(no output)'}"
    return out or "(agent returned no output)"


if __name__ == "__main__":
    mcp.run()
