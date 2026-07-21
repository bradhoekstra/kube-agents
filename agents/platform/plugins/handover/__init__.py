"""Structured status handover — the primary cluster -> platform communication channel.

A Cluster Agent publishes typed status records (health, utilization, ...) via the
``write_handover`` tool. Records are written atomically to a fixed shared path on
the data PVC:

    /opt/data/fleet/clusters/<cluster>/<location>/<type>.json

The Platform Agent consumes them with plain file reads (no custom read tool),
honoring ``expires_at`` for staleness. See docs/designs/agent-communication.md.

Design invariants:
- ``cluster`` / ``location`` come from the profile identity (config ``cluster_identity``),
  never from tool arguments — so a cluster agent cannot write another cluster's record.
- Writes are atomic (temp file + fsync + os.replace) so the platform's plain reads
  never observe a torn file.
- Only cluster profiles register this tool (enabled via config); it is absent on the
  Platform Agent.

The core write logic (``write_record``) has no Hermes dependency so it is unit-testable;
Hermes-specific wiring lives in ``register()``.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
VALID_TYPES = ("health", "utilization", "upgrade_readiness", "drift", "inventory")
DEFAULT_TTL_SECONDS = 900  # 15m; sized for a ~5-10m producer cadence.

WRITE_HANDOVER_SCHEMA = {
    "description": (
        "Publish a structured status handover record for THIS cluster so the Platform "
        "Agent can read it. The 'cluster' and 'location' are taken from this agent's "
        "profile identity — do not pass them. Overwrites the previous record of the "
        "same type (latest-wins)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": list(VALID_TYPES),
                "description": "Record type; determines the payload shape (see the publish-status skill).",
            },
            "payload": {
                "type": "object",
                "description": "The typed status body for this record type.",
            },
            "ttl_seconds": {
                "type": "integer",
                "description": f"Freshness horizon in seconds; defaults to {DEFAULT_TTL_SECONDS}.",
            },
        },
        "required": ["type", "payload"],
    },
}


def _fleet_root() -> Path:
    # Fixed absolute path in production; FLEET_DIR override is for tests only.
    return Path(os.environ.get("FLEET_DIR", "/opt/data/fleet"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def record_path(cluster: str, location: str, rtype: str, fleet_root: Optional[Path] = None) -> Path:
    root = fleet_root if fleet_root is not None else _fleet_root()
    return root / "clusters" / cluster / location / f"{rtype}.json"


def build_envelope(
    cluster: str, location: str, rtype: str, payload: Dict[str, Any], ttl_seconds: Optional[int] = None
) -> Dict[str, Any]:
    now = _utc_now()
    ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    return {
        "schema_version": SCHEMA_VERSION,
        "cluster": cluster,
        "location": location,
        "type": rtype,
        "generated_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=ttl)),
        "payload": payload,
    }


def _atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    """Write JSON atomically (temp in same dir -> fsync -> os.replace) so concurrent
    readers never see a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_record(
    cluster: str,
    location: str,
    rtype: str,
    payload: Dict[str, Any],
    ttl_seconds: Optional[int] = None,
    fleet_root: Optional[Path] = None,
) -> Path:
    """Validate and atomically write one handover record. Returns the written path.

    Raises ValueError on invalid input.
    """
    if rtype not in VALID_TYPES:
        raise ValueError(f"invalid type {rtype!r}; expected one of: {', '.join(VALID_TYPES)}")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if not cluster or not location:
        raise ValueError("cluster and location are required (derived from profile identity)")
    envelope = build_envelope(cluster, location, rtype, payload, ttl_seconds)
    path = record_path(cluster, location, rtype, fleet_root)
    _atomic_write_json(path, envelope)
    return path


def _make_handler(cluster: str, location: str):
    """Bind the caller's identity into the tool handler so it is never an argument."""

    def handle_write_handover(args: Dict[str, Any], **_kwargs: Any) -> str:
        args = args or {}
        rtype = str(args.get("type", "")).strip()
        payload = args.get("payload")
        ttl = args.get("ttl_seconds")
        try:
            path = write_record(cluster, location, rtype, payload, ttl)
        except ValueError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:  # noqa: BLE001 - surface any write failure to the model
            return json.dumps({"error": f"failed to write handover record: {e}"})
        return json.dumps(
            {"status": "ok", "cluster": cluster, "location": location, "type": rtype, "path": str(path)}
        )

    return handle_write_handover


def register(ctx: Any) -> None:
    """Register write_handover — only when the profile carries a cluster identity.

    Cluster profiles get a ``cluster_identity`` block written into their config.yaml
    at scaffold time; the Platform Agent has none, so this registers nothing there
    (belt-and-suspenders on top of not enabling the plugin in the platform config).
    """
    try:
        from hermes_cli.config import cfg_get, load_config
    except Exception as e:  # pragma: no cover - only in the Hermes runtime
        logger.warning("handover: Hermes config unavailable; not registering write_handover: %s", e)
        return

    cfg = load_config() or {}
    cluster = cfg_get(cfg, "cluster_identity", "cluster")
    location = cfg_get(cfg, "cluster_identity", "location")
    if not cluster or not location:
        logger.info("handover: no cluster_identity in profile config; write_handover not registered.")
        return

    ctx.register_tool(
        name="write_handover",
        toolset="handover",
        schema=WRITE_HANDOVER_SCHEMA,
        handler=_make_handler(cluster, location),
        description="Publish a structured status handover record for this cluster.",
        emoji="📡",
    )
    logger.info("handover: registered write_handover for cluster=%s location=%s", cluster, location)
