"""install_env_setting.py — read one setting out of install.env without sourcing it.

`install.sh` sources install.env with `set -a`, which runs the file. A
maintenance script that needs one value — which project, which region — should
not run a file that exports API keys into its own environment, so this scans
the text instead and answers the way bash would.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

DEFAULT_PATH = Path("install.env")
# The installer rewrites install.env on every run, so a file older than this
# describes an install that may no longer exist.
MAX_AGE_SECONDS = 24 * 60 * 60


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def read_setting(key: str, path: Path = DEFAULT_PATH) -> str | None:
    """The value of `key` as bash would resolve it, or None when it is unset."""
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, separator, value = line.partition("=")
        if not separator or name != key:
            continue
        return value.split(" #", 1)[0].rstrip()
    return None


def is_stale(path: Path = DEFAULT_PATH, now: float | None = None) -> bool:
    """Whether the file is older than `MAX_AGE_SECONDS`."""
    now = time.time() if now is None else now
    return now - path.stat().st_mtime > MAX_AGE_SECONDS


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        log("usage: install_env_setting.py KEY")
        return 2
    key = argv[1]
    value = read_setting(key)
    if value is None:
        log(f"{key} is not set in install.env; install.sh will prompt for it on the next run")
        return 1
    if is_stale():
        log(f"install.env is older than {MAX_AGE_SECONDS // 3600} hours; its {key} has been ignored")
    print(value)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
