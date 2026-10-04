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
A missing ``logs/`` is made the way Hermes makes its own, through
``mkdir_under_hermes_home``, which refuses a named profile that is missing or
tombstoned, so an emitter cannot bring a pruned profile's directory back.

Every write opens the file itself, append-only, and closes it, under an
exclusive ``flock`` on ``audit.jsonl.lock`` beside it. The gateway and its
worker processes share a profile's file and no handle, and the lock is what
orders them through a rotation and the write that follows it: no two writers
rotate at once, and a live file one of them has just created is never moved
by another. Rotation is the emitters' own: at the cap the file is renamed to
``.1`` (``.2``, ``.3``), the numbers Hermes uses for its ``agent.log``, so the
volume holds a bounded trail. The sidecar keeps a rotated inode open for its
``Rotate_Wait`` (30 s) and opens the new live file on its next refresh (5 s),
so the one way a record goes unshipped is a sidecar more than 30 s behind the
file at the moment it rotates.

When the file cannot take a record — a full or read-only volume, a profile
Hermes refuses to materialise — the record is printed to this process's
stdout as the same one JSON line, and an ERROR naming the file and the error,
carrying neither the record nor its keys, goes to Hermes' logger. What that
stdout reaches depends on the process. From the gateway it is the container
log, which the GKE log agent ships to Cloud Logging as ``jsonPayload`` under
the agent container, so ``jsonPayload.audit_event:*`` and the Admin Console's
field-form query still find the record. From a kanban worker it is the
worker's captured stdout, the board's ``logs/<task>.log`` on the same volume,
and not Cloud Logging; on a full volume that write fails too, and the record
is gone with the ERROR as the only trace.
"""

import fcntl
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

AUDIT_FILE_NAME = "audit.jsonl"
# Appended to the audit file's name for the lock file beside it: outside both
# globs the sidecar tails, `audit.jsonl` and `*.log`.
AUDIT_LOCK_FILE_SUFFIX = ".lock"
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


def lock_file_path(path: Path) -> Path:
    """The lock file beside the audit file at ``path``."""
    return path.with_name(path.name + AUDIT_LOCK_FILE_SUFFIX)


def serialize(record: Dict[str, Any]) -> str:
    """One line: the object with sorted keys, every newline inside a value escaped."""
    return json.dumps(record, default=str, sort_keys=True)


def append_line(line: str, path: Optional[Path] = None) -> Path:
    """Append ``line`` and a newline to the audit file, creating or rotating it as needed.

    Returns the path written. Raises when the file cannot be written: ``OSError``
    from the volume, or Hermes' ``FileNotFoundError`` for a named profile it
    refuses to materialise.
    """
    path = path or audit_file_path()
    data = (line + "\n").encode(_ENCODING)
    # The lock file is opened first, so a missing logs/ is made here. flock is
    # held by the open file description, so two threads of one process contend
    # on it exactly as two processes do, and closing the descriptor releases it.
    lock_fd = _open(lock_file_path(path))
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _rotate_if_full(path, len(data))
        fd = _open(path)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
    finally:
        os.close(lock_fd)
    return path


def emit(record: Dict[str, Any], logger: logging.Logger) -> None:
    """Append ``record`` to the audit file, or print it to this process's stdout when the file cannot take it.

    The stdout line is the same JSON object, so from the gateway process the
    GKE log agent ships it to Cloud Logging as ``jsonPayload`` under the agent
    container and ``jsonPayload.audit_event:*`` still finds it; from a kanban
    worker it reaches the board's ``logs/<task>.log`` and not Cloud Logging.
    The ERROR that accompanies it names the file and the error and carries
    neither the record nor its keys, so the console's text-form query does
    not count it and nothing ships twice.
    """
    line = serialize(record)
    try:
        append_line(line)
        return
    except Exception as exc:
        failure = exc
    path = audit_file_path()
    try:
        print(line, file=sys.stdout, flush=True)
    except Exception as stdout_failure:
        logger.error(
            "audit record not written to %s (%s) and not printed to stdout (%s); the record is lost",
            path, failure, stdout_failure,
        )
        return
    logger.error("audit record not written to %s (%s); printed to this process's stdout instead", path, failure)


def _open(path: Path) -> int:
    try:
        return os.open(path, _OPEN_FLAGS, AUDIT_FILE_MODE)
    except FileNotFoundError:
        # The first record of a profile can come before anything made its
        # logs/ directory; Hermes' own setup makes it later for agent.log.
        _make_directory(path.parent)
        return os.open(path, _OPEN_FLAGS, AUDIT_FILE_MODE)


def _make_directory(directory: Path) -> None:
    """Make ``directory`` the way Hermes would; plainly outside Hermes.

    ``mkdir_under_hermes_home`` refuses a named profile home that is missing or
    tombstoned, so a record emitted after a profile was pruned cannot bring its
    directory back; its refusal propagates and the record takes the stdout path.
    """
    try:
        from hermes_constants import mkdir_under_hermes_home
    except ImportError:
        directory.mkdir(parents=True, exist_ok=True)
        return
    mkdir_under_hermes_home(directory)


def _rotated(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def _rotate_if_full(path: Path, incoming: int) -> None:
    """Shift the backups and rename the live file aside when ``incoming`` bytes would pass the cap.

    Called with the lock held, so the size read here is the size written to.
    """
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
        # A backup that cannot be moved must not cost the record: the write
        # goes ahead into whatever file is live.
        pass
