import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.core.filenames import safe_filename


def test_basic():
    assert safe_filename("Hello World") == "Hello World"


def test_slashes():
    assert safe_filename("a/b\\c") == "a-b-c"


def test_empty():
    name = safe_filename("   ")
    assert name.startswith("Untitled-")


def test_reserved_windows():
    assert safe_filename("CON").startswith("_")


def test_unicode_normalization():
    a = safe_filename("é")
    b = safe_filename("e\u0301")
    assert a == b
