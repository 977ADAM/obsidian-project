from __future__ import annotations

import re
import unicodedata
import uuid


_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def safe_filename(title: str | None, *, max_len: int = 120) -> str:
    """
    Make a filesystem-safe note title (cross-platform).
    """
    if title is None:
        return f"Untitled-{uuid.uuid4().hex[:6]}"

    s = unicodedata.normalize("NFKC", str(title))
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    s = s.strip()
    s = s.replace("/", "-").replace("\\", "-")
    s = re.sub(r'[<>:"/\\|?*\u0000-\u001f]', "_", s)
    s = re.sub(r"\s+", " ", s)
    s = s.rstrip(" .")

    if not s:
        s = f"Untitled-{uuid.uuid4().hex[:6]}"

    base = s.split(".")[0].strip().lower()
    if base in _WINDOWS_RESERVED:
        s = f"_{s}"

    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")

    return s
