from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QTextEdit

from filesystem import atomic_write_text
from qt_utils import blocked_signals


def note_path(vault_dir: Path, stem: str) -> Path:
    """
    Строит путь к заметке по stem (без расширения в UI).
    Важно: stem уже должен быть нормализован (safe_filename).
    """
    return vault_dir / f"{stem}.md"


def ensure_note_exists(path: Path, title: str) -> None:
    """Создаёт заметку на диске, если её нет."""
    if path.exists():
        return
    atomic_write_text(path, f"# {title}\n\n", encoding="utf-8")


def read_note_text(path: Path) -> str:
    """Единая точка чтения заметки (можно позже добавить recovery/encoding fallback)."""
    return path.read_text(encoding="utf-8")


def set_editor_text(editor: QTextEdit, text: str) -> None:
    """Установка текста в редактор без триггера textChanged."""
    with blocked_signals(editor):
        editor.setPlainText(text)
