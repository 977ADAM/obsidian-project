# app/core/filenames.py

from __future__ import annotations

import re
import unicodedata
import uuid


WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\u0000-\u001f]')
WHITESPACE_RE = re.compile(r"\s+")

MAX_FILENAME_LENGTH = 120


def safe_filename(title: str) -> str:
    """
    Convert an arbitrary note title into a filesystem-safe filename.

    Goals:
    - Cross-platform (Windows / macOS / Linux)
    - Unicode-safe
    - Deterministic
    - Resistant to empty / reserved names
    """

    if title is None:
        raise ValueError("safe_filename(): title is None")

    # 1. Unicode normalization (visual equality → binary equality)
    name = unicodedata.normalize("NFKC", str(title))

    # 2. Remove control characters
    name = "".join(
        ch for ch in name
        if unicodedata.category(ch)[0] != "C"
    )

    # 3. Trim and normalize whitespace
    name = name.strip()
    name = WHITESPACE_RE.sub(" ", name)

    # 4. Replace path separators early
    name = name.replace("/", "-").replace("\\", "-")

    # 5. Replace forbidden filesystem characters
    name = INVALID_CHARS_RE.sub("_", name)

    # 6. Windows: no trailing dot or space
    name = name.rstrip(" .")

    # 7. Empty name fallback
    if not name:
        return _generate_untitled()

    # 8. Windows reserved device names
    base = name.split(".", 1)[0].strip().lower()
    if base in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"

    # 9. Length limit
    if len(name) > MAX_FILENAME_LENGTH:
        name = name[:MAX_FILENAME_LENGTH].rstrip(" .")

    return name


def _generate_untitled() -> str:
    """Generate a safe fallback filename."""
    return f"Untitled-{uuid.uuid4().hex[:6]}"
