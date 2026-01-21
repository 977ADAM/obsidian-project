import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.links import LinkIndex


def test_single_link():
    idx = LinkIndex()
    idx.update_note("A", "Link to [[B]]")

    assert idx.outgoing == {"A": {"B"}}
    assert idx.incoming == {"B": {"A"}}


def test_backlinks():
    idx = LinkIndex()
    idx.update_note("A", "[[B]]")
    idx.update_note("C", "[[B]]")

    assert idx.backlinks_for("B") == ["A", "C"]


def test_incremental_update():
    idx = LinkIndex()
    idx.update_note("A", "[[B]]")
    changed = idx.update_note("A", "[[C]]")

    assert changed
    assert "A" not in idx.incoming.get("B", set())
    assert idx.outgoing["A"] == {"C"}


def test_self_links_ignored():
    idx = LinkIndex()
    idx.update_note("A", "[[A]]")
    assert idx.outgoing == {}
