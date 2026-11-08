# casa/rootfs/opt/casa/executor_epoch.py
"""#215: the executor *procedural epoch* — a digest over every input that
steers an executor's PROCEDURE, computed at launch from the bytes the launch
actually consumes.

Why launch-time and not load-time (design r3): the prompt template is
re-read from disk at every launch, so a load-time value can mislabel what a
launch consumed (an edit without a reload); the epoch must be derived from
the same in-hand bytes. Why source bytes and not rendered output (design
r5): the rendered prompt embeds {task}/{context}/{executor_memory},
which would make every epoch launch-unique — and circular, since the archive
read this epoch filters runs before rendering.

Inputs, exhaustively: the canonical role-artifact checksum
(``defn.role_checksum``), the resolved prompt template (relative path + the
exact bytes the launch read — the ``prompt_template_file`` indirection is an
input, r2), and every file under the resolved doctrine directory (sorted
relative path + content). The rest of ``definition.yaml`` (tools, model, plumbing)
is deliberately NOT an input: changing a tool grant does not obsolete
lessons.

Summaries captured at engagement finalize carry the epoch persisted on the
engagement's record (and the copy retained to the semantic backend is
tagged with :func:`epoch_application_tag`); the archive injection at the
NEXT launch reads only summaries of the launch's own epoch.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_TAG_PREFIX = "casa-doctrine-epoch-"
_EPOCH_HEX = 12


def compute_procedural_epoch(defn, *, prompt_template: str) -> str:
    """Digest the launch's effective procedural inputs (see module docstring).

    ``prompt_template`` is the text the caller JUST read from
    ``defn.prompt_template_path`` — passed in rather than re-read so the
    epoch describes exactly the bytes the launch consumes. Doctrine and
    workspace-template files are read here (small, image-shipped trees);
    an unreadable file contributes its path with empty content rather than
    failing the launch — the epoch then still changes when the file appears.
    """
    h = hashlib.sha256()
    h.update((getattr(defn, "role_checksum", "") or "").encode())
    h.update(b"\x00prompt\x00")
    h.update(os.path.basename(
        getattr(defn, "prompt_template_path", "") or "").encode())
    h.update(b"\x00")
    h.update(prompt_template.encode())
    doctrine_dir = getattr(defn, "doctrine_dir", "") or ""
    if doctrine_dir:
        root = Path(doctrine_dir)
        if root.is_dir():
            for f in sorted(p for p in root.rglob("*") if p.is_file()):
                h.update(b"\x00doctrine\x00")
                h.update(str(f.relative_to(root)).encode())
                h.update(b"\x00")
                h.update(_read_or_empty(f))
    return h.hexdigest()


def _read_or_empty(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        logger.warning("procedural epoch: unreadable input %s", path)
        return b""


def epoch_application_tag(executor_type: str, epoch: str) -> str:
    """The application tag stamped on an executor engagement summary at
    finalize and matched at the next launch's archive injection."""
    return f"{_TAG_PREFIX}{executor_type}-{epoch[:_EPOCH_HEX]}"


def is_epoch_tag(tag: str) -> bool:
    return tag.startswith(_TAG_PREFIX)
