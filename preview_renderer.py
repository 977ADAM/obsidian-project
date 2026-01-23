from __future__ import annotations

import markdown as md

from wikilinks import wikilinks_to_html
from html_sanitizer import sanitize_rendered_html


MD_EXTENSIONS = ["fenced_code", "tables", "toc"]

BASE_CSS = """
    body { font-family: sans-serif; padding: 16px; line-height: 1.5; }
    code, pre { background: #f5f5f5; }
    pre { padding: 12px; overflow-x: auto; }
    a { text-decoration: none; }
    a:hover { text-decoration: underline; }
"""


def render_markdown_to_safe_html(note_text: str) -> str:
    """
    note text -> HTML:
      1) convert [[wikilinks]] to <a>
      2) markdown -> HTML
      3) sanitize HTML
    """
    text2 = wikilinks_to_html(note_text)
    rendered = md.markdown(text2, extensions=MD_EXTENSIONS)
    return sanitize_rendered_html(rendered)


def wrap_html_page(rendered_html: str, *, css: str = BASE_CSS) -> str:
    """Wrap safe HTML into a full HTML document for WebEngine."""
    return f"""
    <html>
    <head>
        <meta charset="utf-8"/>
        <style>{css}</style>
    </head>
    <body>{rendered_html}</body>
    </html>
    """


def render_preview_page(note_text: str) -> str:
    """Convenience: note text -> full HTML page."""
    return wrap_html_page(render_markdown_to_safe_html(note_text))
