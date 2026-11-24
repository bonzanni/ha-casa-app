"""One field of a vault item in the default vault, read through the ``op``
front, which serves both secret backends. Blocking."""
from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

OP_TIMEOUT_S = 10


def read_field(title: str, field: str) -> str | None:
    """*field* of the item *title* in the default vault, or None when it
    cannot be read. ``op`` output is never logged or returned except the
    value itself."""
    vault = os.environ.get("ONEPASSWORD_DEFAULT_VAULT", "")
    if not vault:
        return None
    try:
        result = subprocess.run(
            ["op", "item", "get", title, "--vault", vault,
             "--fields", f"label={field}", "--reveal"],
            capture_output=True, text=True, timeout=OP_TIMEOUT_S,
            stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("vault: %s of %r not read (%s)", field, title,
                    type(exc).__name__)
        return None
    if result.returncode != 0:
        logger.info("vault: %s of %r not read (op exited %d)", field, title,
                    result.returncode)
        return None
    value = (result.stdout or "").strip()
    return value or None
