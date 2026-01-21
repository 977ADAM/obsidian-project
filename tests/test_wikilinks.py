import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.wikilinks import (
    extract_wikilink_targets,
    rewrite_wikilinks_targets,
    wikilinks_to_html,
)


def test_extract_basic():
    text = "See [[Note A]] and [[Note B|alias]]"
    assert extract_wikilink_targets(text) == {"Note A", "Note B"}


def test_extract_suffixes():
    text = "[[Note#Heading]] [[Note^block]]"
    assert extract_wikilink_targets(text) == {"Note"}


def test_rewrite_simple():
    text = "Link to [[Old]]"
    out, changed = rewrite_wikilinks_targets(text, old_stem="Old", new_stem="New")
    assert changed
    assert out == "Link to [[New]]"


def test_rewrite_alias():
    text = "[[Old|Alias]]"
    out, _ = rewrite_wikilinks_targets(text, old_stem="Old", new_stem="New")
    assert out == "[[New|Alias]]"


def test_html():
    html_out = wikilinks_to_html("[[Note|Hello]]")
    assert 'href="note://Note"' in html_out
    assert ">Hello<" in html_out
