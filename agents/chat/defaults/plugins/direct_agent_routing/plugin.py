"""Route ``/<agent> <text>`` straight to that agent's profile, skipping the front door.

An ordinary chat message costs a Chat Agent turn to pick an assignee, a kanban
card, and a worker spawn before the specialist sees it. When the user already
knows who they want, that is three steps to reach a conclusion they had at the
start. This plugin gives them a way to say so: ``/platform scale the frontend``
runs as a real turn on the ``platform`` profile — its config, skills, tools and
credentials — inside the gateway, with no front-door turn and no card.

How it reaches another profile:

* ``pre_gateway_dispatch`` fires once per inbound user message, before the
  gateway resolves a command and before authorization.
* Its ``{"action": "rewrite", "text": …}`` return is applied with
  ``dataclasses.replace(event, text=…)``, which carries the SAME ``source``
  object through. So a ``source`` this hook mutates survives the rewrite.
* ``source.profile`` is what ``_resolve_profile_home_for_source`` consults
  first when it decides whose HERMES_HOME serves the turn.

Two consequences worth keeping in mind when editing this file:

* ``rewrite``, not ``skip``, is deliberate. The authorization check runs after
  this hook, so routing cannot be used to reach a profile the sender was not
  allowed to talk to in the first place. A ``skip`` here would take the turn
  out of the gateway's hands entirely.
* The whole mechanism is inert unless ``gateway.multiplex_profiles`` is on;
  without it Hermes ignores ``source.profile`` and the turn runs on the front
  door with the prefix quietly stripped. The operator turns both that flag and
  this plugin's gate on together, from one CR field, which is why the gate
  below is checked before anything else.
"""

import importlib.util
import logging
import os
import re
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes.plugin.direct_agent_routing")

# The operator sets this from spec.harness.experimental.directProfileRouting,
# alongside GATEWAY_MULTIPLEX_PROFILES. The plugin ships enabled and does
# nothing until it is set, so the image and the CR field move independently.
_GATE_ENV = "KUBE_AGENTS_DIRECT_ROUTING"
_TRUTHY = {"1", "true", "yes", "on"}

# "/name rest-of-the-message". Anchored, and a name alone does not match: the
# rewrite would leave an empty prompt, and a conversational reply from the Chat
# Agent is a better answer to a bare "/platform" than an empty turn on the
# Platform Agent.
_ROUTE_RE = re.compile(r"^/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,62})\s+(?P<rest>.+)$", re.DOTALL)

# A leading bot mention Slack prepends when the user @-mentions the bot on the
# same line ("<@U123> /platform scale the frontend"). Same pattern the
# legacy_slash_commands plugin strips.
_LEADING_MENTION_RE = re.compile(r"^<@[UWB][A-Z0-9]+>\s*")

# Shared with the `list_agents` MCP tool and the `agent_roster` plugin, so the
# names this accepts are exactly the names the front door tells the user about.
# It is a loose script rather than an importable package — the entrypoint copies
# /opt/defaults/scripts into $HERMES_HOME/scripts and the MCP server is launched
# by absolute path — so it is loaded by path, the way agent_roster loads it.
_MODULE_NAME = "_kube_agents_agent_roster"
_SCRIPT_NAME = "agent_roster.py"
_FALLBACK_SCRIPTS_DIR = Path("/opt/defaults/scripts")

_roster_module: Optional[ModuleType] = None


def _enabled() -> bool:
    return os.environ.get(_GATE_ENV, "").strip().lower() in _TRUTHY


def _scripts_dirs() -> list[Path]:
    data_dir = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    return [data_dir / "scripts", _FALLBACK_SCRIPTS_DIR]


def _load_roster_module() -> Optional[ModuleType]:
    """Load agent_roster.py by path. Cached across turns; the roster is not."""
    global _roster_module
    if _roster_module is not None:
        return _roster_module
    for base in _scripts_dirs():
        path = base / _SCRIPT_NAME
        try:
            # is_file() is inside the try on purpose: the scripts directory is on
            # the shared PVC, and pathlib only swallows ENOENT/ENOTDIR/EBADF/ELOOP
            # — a stat() that fails with EACCES or EIO raises for real.
            if not path.is_file():
                continue
            spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            logger.warning("Could not load the agent roster from %s: %s", path, e)
            continue
        _roster_module = module
        return module
    logger.warning("No %s found under %s; direct routing disabled.",
                   _SCRIPT_NAME, [str(b) for b in _scripts_dirs()])
    return None


def resolve_profile(name: str) -> Optional[str]:
    """Return the roster's spelling of ``name``, or ``None`` if it names no agent.

    ``discover()`` re-reads the profiles directory every call — a cluster agent
    created a moment ago has to be addressable on the next message — and returns
    ``None``, distinct from ``[]``, when the directory could not be listed at
    all. Both mean "do not route": an unreadable roster is not evidence that the
    agent does not exist, and guessing would send the turn to a profile whose
    home may not be there.

    ``default`` is never returned; ``discover()`` excludes the front door
    itself, so ``/default …`` falls through like any other unknown name.
    """
    module = _load_roster_module()
    if module is None:
        return None
    data_dir = Path(os.environ.get("HERMES_HOME", "/opt/data"))
    agents = module.discover(data_dir / "profiles")
    if not agents:
        return None
    names = [str(a.get("name", "")) for a in agents]
    if name in names:
        return name
    # Case-insensitive second pass, returning the roster's spelling: "/Platform"
    # from a phone keyboard's autocapitalise is the same request as "/platform".
    lowered = name.lower()
    for candidate in names:
        if candidate.lower() == lowered:
            return candidate
    return None


def route(text: Any) -> Optional[tuple]:
    """Return ``(profile, remaining_text)`` for a routed message, else ``None``."""
    if not isinstance(text, str) or not text:
        return None
    stripped = _LEADING_MENTION_RE.sub("", text.strip(), count=1).strip()
    match = _ROUTE_RE.match(stripped)
    if match is None:
        return None
    rest = match.group("rest").strip()
    if not rest:
        return None
    profile = resolve_profile(match.group("name"))
    if profile is None:
        # Not an agent name. Falls through unchanged, which is what keeps
        # /sethome, /hermes … and every other slash command working.
        return None
    return profile, rest


def handle_pre_gateway_dispatch(
    event: Any = None,
    gateway: Any = None,
    session_store: Any = None,
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Stamp the target profile on the event and hand back the message without its prefix."""
    if not _enabled():
        return None
    try:
        original = getattr(event, "text", None)
        routed = route(original)
        if routed is None:
            return None
        profile, rest = routed
        source = getattr(event, "source", None)
        if source is None:
            # Nothing to stamp means nothing would reach the target profile, and
            # a bare rewrite would run the stripped text on the front door as
            # though the user had never named an agent. Leave it alone.
            logger.warning("Message routed to %r has no source; leaving it unrouted.", profile)
            return None
        source.profile = profile
        logger.info("Routing message directly to profile %r", profile)
        return {"action": "rewrite", "text": rest}
    except Exception as exc:
        logger.error(
            "Error in direct_agent_routing pre_gateway_dispatch hook: %s",
            exc,
            exc_info=True,
        )
        return None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", handle_pre_gateway_dispatch)
