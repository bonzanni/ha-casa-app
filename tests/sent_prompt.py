"""The system prompt text the CLI reads for a set of SDK options.

#1570: every exec site hands the SDK the prompt as a file
(``{"type": "file", "path": ...}``) so it never rides argv; a test that
captures those options reads the text back through this helper."""
from __future__ import annotations

from pathlib import Path


def sent_prompt(options):
    prompt = options.system_prompt
    if isinstance(prompt, dict) and prompt.get("type") == "file":
        return Path(prompt["path"]).read_text(encoding="utf-8")
    return prompt
