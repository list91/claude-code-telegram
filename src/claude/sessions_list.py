"""Enumerate Claude Code sessions on disk — powers the /sessions command.

Claude Code persists every session as a ``*.jsonl`` transcript under
``~/.claude/projects/<encoded-cwd>/``. The directory name is the absolute
working directory with every non-alphanumeric character replaced by ``-``
(verified against real directories, e.g. ``C:\\sts\\projects\\vault`` ->
``C--sts-projects-vault`` and ``...\\.claude-telegram-bot`` -> ``...--claude-telegram-bot``).

Listing straight from disk means multi-session support needs no extra
bookkeeping and survives bot restarts — the engine already supports many
sessions per directory and resume-by-id (``options.resume``).
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

_MAX_SCAN_LINES = 60


def encode_project_dir(path: Any) -> str:
    """Encode an absolute path the way Claude Code names its project dir."""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(path))


def _projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _extract_label(jsonl_path: Path) -> str:
    """Short preview: first real user message, else a summary line."""
    summary = None
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, raw in enumerate(fh):
                if i >= _MAX_SCAN_LINES:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                otype = obj.get("type")
                if otype == "summary" and obj.get("summary"):
                    summary = str(obj["summary"])
                    continue
                if otype != "user":
                    continue
                message = obj.get("message") or {}
                content = message.get("content")
                text = None
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text")
                            break
                if not text:
                    continue
                text = " ".join(str(text).split())
                # Skip wrapped system/command content, keep genuine prompts.
                if text and not text.startswith(("<", "Caveat:")):
                    return text[:70]
    except Exception:
        pass
    return (summary or "(no preview)")[:70]


def list_sessions_for_dir(project_path: Any, limit: int = 10) -> List[Dict[str, Any]]:
    """Return recent sessions for ``project_path``, newest first.

    Each item: ``{id, label, when, mtime, size}``.
    """
    directory = _projects_root() / encode_project_dir(project_path)
    if not directory.is_dir():
        return []

    files = [p for p in directory.glob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    out: List[Dict[str, Any]] = []
    for path in files[:limit]:
        stat = path.stat()
        out.append(
            {
                "id": path.stem,
                "label": _extract_label(path),
                "when": datetime.fromtimestamp(stat.st_mtime).strftime("%d %b %H:%M"),
                "mtime": stat.st_mtime,
                "size": stat.st_size,
            }
        )
    return out
