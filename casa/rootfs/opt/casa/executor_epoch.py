# casa/rootfs/opt/casa/executor_epoch.py
"""#215: the executor *procedural epoch* — a digest over every input that
steers an executor's PROCEDURE, computed at launch from the bytes the launch
actually consumes.

Why launch-time and not load-time (design r3): the prompt template is
re-read from disk at every launch, so a load-time value can mislabel what a
launch consumed (an edit without a reload); the epoch must be derived from
the same in-hand bytes. Why source bytes and not rendered output (design
r5): the rendered prompt/CLAUDE.md embeds {task}/{context}/{executor_memory},
which would make every epoch launch-unique — and circular, since the archive
recall this epoch filters runs before rendering.

Inputs, exhaustively: the canonical role-artifact checksum
(``defn.role_checksum``), the resolved prompt template (relative path + the
exact bytes the launch read — the ``prompt_template_file`` indirection is an
input, r2), every file under the resolved doctrine directory (sorted
relative path + content), and — claude_code driver only — the SOURCE bytes
of the workspace instruction file that provisioning will consume
(``workspace-template/CLAUDE.md.tmpl``, or the plain-copy ``CLAUDE.md``
fallback, r4/r5). The rest of ``definition.yaml`` (tools, model, plumbing)
is deliberately NOT an input: changing a tool grant does not obsolete
lessons.

Summaries retained at engagement finalize are stamped with
:func:`epoch_application_tag`; the archive injection at the NEXT launch
keeps only same-tag lessons (client-side — the backend's only content
filter is the sensitivity tags, and ``tags_match="any"`` broadens).
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
    if getattr(defn, "driver", "") == "claude_code":
        h.update(b"\x00workspace\x00")
        h.update(_workspace_instruction_source(defn))
    return h.hexdigest()


def _read_or_empty(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        logger.warning("procedural epoch: unreadable input %s", path)
        return b""


def _workspace_instruction_source(defn) -> bytes:
    """The SOURCE bytes of the workspace instruction file provisioning will
    consume: ``workspace-template/CLAUDE.md.tmpl`` when present, else the
    plain-copy ``CLAUDE.md`` fallback (r4/r5 — the fallback is consumed too,
    and rendered bytes are launch-unique so only the source may be hashed).
    Mirrors the selection in ``drivers/workspace.py``'s template render."""
    template_dir = getattr(defn, "prompt_template_path", "")
    if not template_dir:
        return b""
    template_root = Path(template_dir).parent / "workspace-template"
    tmpl = template_root / "CLAUDE.md.tmpl"
    if tmpl.is_file():
        return _read_or_empty(tmpl)
    plain = template_root / "CLAUDE.md"
    if plain.is_file():
        return _read_or_empty(plain)
    return b""


def epoch_application_tag(executor_type: str, epoch: str) -> str:
    """The application tag stamped on an executor engagement summary at
    finalize and matched at the next launch's archive injection."""
    return f"{_TAG_PREFIX}{executor_type}-{epoch[:_EPOCH_HEX]}"


def is_epoch_tag(tag: str) -> bool:
    return tag.startswith(_TAG_PREFIX)


def make_archive_epoch_filter(expected_tag: str):
    """Hit filter for the executor-archive recall (#215): keep only hits
    stamped with EXACTLY this launch's epoch tag. Mismatched-epoch and
    UNTAGGED hits are dropped — the injected "lessons learned" block exists
    to steer procedure, and a lesson that cannot prove it was learned under
    the current doctrine is exactly the poison #215 names (legacy items are
    indistinguishable from pre-change ones). Count-only logging; never
    content."""
    def _filter(hits):
        kept = tuple(
            h for h in hits
            if expected_tag in (getattr(h, "application_tags", ()) or ())
        )
        dropped = len(hits) - len(kept)
        if dropped:
            logger.info(
                "executor archive: dropped %d of %d hit(s) from another "
                "doctrine epoch", dropped, len(hits),
            )
        return kept
    return _filter
