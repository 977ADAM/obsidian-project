from __future__ import annotations

import html
import re
from urllib.parse import quote

from .filenames import safe_filename

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def extract_wikilink_targets(markdown_text: str) -> set[str]:
    targets: set[str] = set()
    for m in WIKILINK_RE.finditer(markdown_text or ""):
        inner = (m.group(1) or "").strip()
        if not inner:
            continue
        target_raw = inner.split("|", 1)[0].strip() if "|" in inner else inner
        dst = safe_filename(target_raw)
        if dst:
            targets.add(dst)
    return targets


def wikilinks_to_html(markdown_text: str) -> str:
    def repl(m: re.Match) -> str:
        inner = (m.group(1) or "").strip()
        if not inner:
            return ""

        if "|" in inner:
            target_raw, display_raw = inner.split("|", 1)
            target_raw = target_raw.strip()
            display_raw = display_raw.strip()
        else:
            target_raw = inner
            display_raw = inner

        label = html.escape(display_raw, quote=False)
        target = safe_filename(target_raw)
        href = "note://" + quote(target, safe="")
        return f'<a href="{href}">{label}</a>'

    return WIKILINK_RE.sub(repl, markdown_text)
