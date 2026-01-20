from __future__ import annotations

import html
import logging

# optional dependency
try:
    import bleach
except Exception:  # pragma: no cover
    bleach = None

_BLEACH_MISSING_WARNED = False

ALLOWED_TAGS = [
    "a", "p", "br", "hr",
    "strong", "em", "code", "pre", "blockquote",
    "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
]
ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "th": ["align"], "td": ["align"],
}
ALLOWED_PROTOCOLS = ["http", "https", "mailto", "note"]


def sanitize_rendered_html(rendered_html: str, *, logger_name: str = "obsidian-project") -> str:
    global _BLEACH_MISSING_WARNED

    if bleach is None:
        if not _BLEACH_MISSING_WARNED:
            logging.getLogger(logger_name).warning(
                "bleach is not installed; preview will be plain text for safety."
            )
            _BLEACH_MISSING_WARNED = True
        return html.escape(rendered_html)

    return bleach.clean(
        rendered_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
