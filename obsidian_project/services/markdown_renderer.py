from __future__ import annotations

import markdown as md

from obsidian_project.core.sanitize import sanitize_rendered_html
from obsidian_project.core.wikilinks import wikilinks_to_html


class MarkdownRenderer:
    def __init__(self, *, logger_name: str):
        self.logger_name = logger_name

    def render_page(self, text: str) -> str:
        text2 = wikilinks_to_html(text)
        rendered = md.markdown(text2, extensions=["fenced_code", "tables", "toc"])
        rendered = sanitize_rendered_html(rendered, logger_name=self.logger_name)

        return f"""\
<html>
<head>
  <meta charset="utf-8"/>
  <style>
    body {{ font-family: sans-serif; padding: 16px; line-height: 1.5; }}
    code, pre {{ background: #f5f5f5; }}
    pre {{ padding: 12px; overflow-x: auto; }}
    a {{ text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>{rendered}</body>
</html>
"""
