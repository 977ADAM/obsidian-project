# --- HTML sanitization (for Markdown preview rendered in QWebEngine) ---
try:
    import bleach  # pip install bleach
except Exception:  # pragma: no cover
    bleach = None

import html
import logging
from logging_setup import APP_NAME

# чтобы не спамить warning при отсутствии bleach
_BLEACH_MISSING_WARNED = False

ALLOWED_TAGS = [
    "a", "p", "br", "hr",
    "strong", "em", "code", "pre", "blockquote",
    "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
    # If you want images, uncomment "img" and its attrs below.
    # "img",
]

ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "th": ["align"], "td": ["align"],
    # Needed for markdown 'toc' extension anchors:
    "h1": ["id"], "h2": ["id"], "h3": ["id"],
    "h4": ["id"], "h5": ["id"], "h6": ["id"],
    # "img": ["src", "alt", "title"],
}

ALLOWED_PROTOCOLS = ["http", "https", "mailto", "note"]

def sanitize_rendered_html(rendered_html: str) -> str:
    """
    Sanitize HTML output from Markdown before feeding it to QWebEngine.
    Without this, raw HTML inside notes can execute in the embedded browser.
    """
    if bleach is None:
        # SAFE fallback: escape everything (loses formatting but prevents HTML/JS execution).
        global _BLEACH_MISSING_WARNED
        if not _BLEACH_MISSING_WARNED:
            logging.getLogger(APP_NAME).warning(
                "bleach is not installed; preview will be shown as plain text for safety. "
                "Install 'bleach' to enable sanitized HTML rendering."
            )
            _BLEACH_MISSING_WARNED = True
        return html.escape(rendered_html)

    cleaned = bleach.clean(
        rendered_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    # Also strip out any JS-able URLs that might slip through.
    return cleaned