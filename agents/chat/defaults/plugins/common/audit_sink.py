"""The file an audit record goes to: the profile's own ``logs/audit.jsonl``.

The ``tool_call_audit`` plugin and the ``chat_message_audit`` hook build their
records with ``audit_schema.envelope`` and hand them to :func:`emit`, which
appends one JSON object per line to a file under the profile's ``logs/``
directory that nothing else writes. The fluent-bit sidecar in the gateway pod
tails that file with its JSON parser (``buildFluentBitConfigMap`` in the
operator), so the record's keys reach Cloud Logging as ``jsonPayload`` fields
without a regex over anything Hermes wrote. The emitters used to log the
record through Hermes' logger and the sidecar lifted the object back out of
the formatted line by its prefix — a prefix that was Hermes' to change, and
that any logger writing untrusted text could have imitated.

Which profile's file: the one the record is emitted under.
``hermes_constants.get_hermes_home()`` is how the running Hermes names it —
the context-local override the gateway sets around a turn it serves for
another profile, then ``HERMES_HOME``, which a kanban worker is launched with
pointing at its own profile — so the file follows the record to
``/opt/data/logs/`` for the front door and ``/opt/data/profiles/<name>/logs/``
for a named profile, the same ``logs/`` Hermes routes its own ``agent.log``
to. Outside a Hermes process (the unit tests) ``HERMES_HOME`` alone decides.

Every write opens the file itself, append-only, and closes it: the kernel
orders appends from the gateway and its worker processes without a shared
handle, and nothing is held open across a rotation. Rotation is the emitters'
own: at the cap the file is renamed to ``.1`` (``.2``, ``.3``), the numbers
Hermes uses for its ``agent.log``, so the volume holds a bounded trail and the
sidecar, which keeps a rotated file open for its ``Rotate_Wait``, loses
nothing it had already begun reading. Two processes reaching the cap together
rotate twice; the second rename can move a file the sidecar had not yet
opened, and its few records then stay on the volume in ``.1`` unshipped. That
is the one window this design accepts, and no write is ever lost from the
live file.
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

AUDIT_FILE_NAME = "audit.jsonl"
LOGS_DIR_NAME = "logs"
HERMES_HOME_ENV = "HERMES_HOME"
# What the agent image sets HERMES_HOME to, and the default every script in
# this tree falls back to when the variable is unset.
DEFAULT_HERMES_HOME = "/opt/data"
# Owner read-write, group read. The agent's uid writes the file, the sidecar
# reads it through the volume's shared group, and no other uid on the volume
# can write it.
AUDIT_FILE_MODE = 0o640
# Hermes' own defaults for agent.log (hermes_logging.setup_logging). The same
# cap also bounds what the sidecar re-reads from the head of the file after a
# restart, because its tail position lives in an emptyDir.
AUDIT_FILE_MAX_BYTES = 5 * 1024 * 1024
AUDIT_FILE_BACKUP_COUNT = 3
_OPEN_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT
_ENCODING = "utf-8"

# Serialises rotation and the write that follows it within one process. Other
# processes writing the same file are ordered by O_APPEND, not by this lock.
_write_lock = threading.Lock()


def hermes_home() -> Path:
    """The home of the profile the current record belongs to."""
    try:
        from hermes_constants import get_hermes_home  # the running Hermes' own
    except ImportError:
        get_hermes_home = None
    if get_hermes_home is not None:
        try:
            return Path(get_hermes_home())
        except Exception:
            pass
    return Path(os.environ.get(HERMES_HOME_ENV) or DEFAULT_HERMES_HOME)


def audit_file_path(home: Optional[Path] = None) -> Path:
    """``<home>/logs/audit.jsonl``, for the current profile unless ``home`` is given."""
    return (home or hermes_home()) / LOGS_DIR_NAME / AUDIT_FILE_NAME


def serialize(record: Dict[str, Any]) -> str:
    """One line: the object with sorted keys, every newline inside a value escaped."""
    return json.dumps(record, default=str, sort_keys=True)


def append_line(line: str, path: Optional[Path] = None) -> Path:
    """Append ``line`` and a newline to the audit file, creating or rotating it as needed.

    Returns the path written. Raises ``OSError`` when the file cannot be written.
    """
    path = path or audit_file_path()
    data = (line + "\n").encode(_ENCODING)
    with _write_lock:
        _rotate_if_full(path, len(data))
        fd = _open(path)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
    return path


def emit(record: Dict[str, Any], logger: logging.Logger) -> None:
    """Append ``record`` to the audit file, or hand it to ``logger`` when the file cannot take it.

    The fallback is one ERROR line naming the path and ending in the record, so
    a file that cannot be written is visible in Hermes' ``agent.log`` and the
    record still reaches Cloud Logging as text — the shape the Admin Console
    reads as "wrapped" — instead of disappearing.
    """
    line = serialize(record)
    try:
        append_line(line)
    except OSError as exc:
        logger.error("audit record not written to %s (%s); record: %s", audit_file_path(), exc, line)


def _open(path: Path) -> int:
    try:
        return os.open(path, _OPEN_FLAGS, AUDIT_FILE_MODE)
    except FileNotFoundError:
        # The first record of a profile can come before anything made its
        # logs/ directory; Hermes' own setup makes it later for agent.log.
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(path, _OPEN_FLAGS, AUDIT_FILE_MODE)


def _rotated(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def _rotate_if_full(path: Path, incoming: int) -> None:
    """Shift the backups and rename the live file aside when ``incoming`` bytes would pass the cap."""
    try:
        size = os.stat(path).st_size
    except FileNotFoundError:
        return
    if size == 0 or size + incoming <= AUDIT_FILE_MAX_BYTES:
        return
    try:
        for index in range(AUDIT_FILE_BACKUP_COUNT, 1, -1):
            older = _rotated(path, index - 1)
            if older.exists():
                os.replace(older, _rotated(path, index))
        os.replace(path, _rotated(path, 1))
    except OSError:
        # Another process rotated first, or a backup could not be moved. The
        # write goes ahead into whatever file is live rather than being lost.
        pass
