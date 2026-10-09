"""Count every chat message that reaches the gateway, by platform, and serve the count.

Registered on `pre_gateway_dispatch`, the hook Hermes runs once per inbound
gateway event, after it has dropped its own internal events and before it
checks the sender against the allowlist and dispatches; `session_store`
records its metadata on the same hook. So an unlisted user's message counts,
and a message Hermes never hands to the hook does not. The hook rewrites
nothing and never raises: a count must not cost a turn.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# See the note in plugins/session_store/store.py: the plugin loader pins no
# sys.path entry, so a sibling import is resolved from this file's location.
_PLUGINS_DIR = str(Path(__file__).resolve().parents[1])
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

from chat_metrics import metrics  # noqa: E402

logger = logging.getLogger("hermes.plugin.chat_metrics")

REGISTRY = metrics.ChatMetrics()
_listener = None
# The module Hermes' gateway process imports before it discovers plugins, and
# which no other hermes process in the container (a CLI call, a worker) loads.
# Every hermes process runs plugin discovery, so each would otherwise try to
# bind the one port the pod has for this, and only the gateway sees messages:
# the listener belongs in the gateway process alone, and the rest log nothing.
GATEWAY_MODULE = "gateway.run"


def _platform_of(event: Any) -> str:
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", "") if source is not None else ""
    return getattr(platform, "value", None) or str(platform or "")


def count_inbound(event: Any = None, gateway: Any = None, session_store: Any = None, **kwargs: Any) -> Optional[Dict[str, str]]:
    """Count the event under its platform; rewrite nothing."""
    try:
        REGISTRY.record_inbound(_platform_of(event))
    except Exception as exc:  # noqa: BLE001 -- the count never costs a turn
        logger.error("Error in chat_metrics pre_gateway_dispatch hook: %s", exc, exc_info=True)
    return None


def in_gateway_process() -> bool:
    """Whether this process is the Hermes gateway, the one that receives messages."""
    return GATEWAY_MODULE in sys.modules


def start_listener() -> None:
    """Start the metrics listener once, in the gateway process, on the operator's port, if one is set."""
    global _listener
    if _listener is not None:
        return
    if not in_gateway_process():
        logger.debug("not the gateway process; the inbound chat count is served from the gateway alone")
        return
    port = metrics.listener_port_from_env()
    if port is None:
        logger.info("%s is not set; inbound chat messages are counted but not served", metrics.METRICS_PORT_ENV)
        return
    _listener = metrics.start_listener(port, REGISTRY)


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", count_inbound)
    start_listener()
