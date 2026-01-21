# app/infrastructure/filesystem.py

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path

from app.core.filenames import safe_filename

# ───────────────────────── paths ─────────────────────────

APP_NAME = "obsidian-project"

RECOVERY_DIR = Path.home() / f".{APP_NAME}" / "recovery"
RECOVERY_DIR.mkdir(parents=True, exist_ok=True)


# ───────────────────────── public API ─────────────────────────

def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """
    Atomic-ish file write:
    - write to temp file in same directory
    - fsync
    - replace()

    Prevents partial writes on crash/power loss.
    """
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    tmp_name = f".{path.name}.tmp-{uuid.uuid4().hex}"
    tmp_path = parent / tmp_name

    f = None
    try:
        f = open(tmp_path, "w", encoding=encoding, newline="")
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
        f.close()
        f = None

        tmp_path.replace(path)

    finally:
        try:
            if f is not None:
                f.close()
        except Exception:
            pass

        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def write_recovery_copy(note_path: Path, text: str) -> Path:
    """
    Best-effort emergency save when normal save fails.

    Writes timestamped copy into:
      ~/.obsidian-project/recovery/
    """
    note_path = Path(note_path)

    stem = safe_filename(note_path.stem) or "Untitled"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    recovery_path = RECOVERY_DIR / f"{stem}.recovery.{ts}.md"
    atomic_write_text(recovery_path, text, encoding="utf-8")

    return recovery_path
