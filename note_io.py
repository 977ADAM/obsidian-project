from __future__ import annotations
import re
import uuid
from pathlib import Path

from PySide6.QtWidgets import QTextEdit

from filesystem import atomic_write_text
from qt_utils import blocked_signals

_FM_RE = re.compile(r"(?s)\A---\s*\n(.*?)\n---\s*\n")
_NOTE_ID_RE = re.compile(r"(?m)^\s*note_id\s*:\s*(.+?)\s*$")
_TITLE_RE = re.compile(r"(?m)^\s*title\s*:\s*(.+?)\s*$")
_H1_RE = re.compile(r"(?m)^\s*#\s+(.+?)\s*$")

def set_note_title_in_text(text: str, *, new_title: str) -> tuple[str, bool]:
    """
    Best-effort update:
      - YAML frontmatter: title:
      - first H1: "# ..."
    Returns (new_text, changed).
    """
    new_title = (new_title or "").strip()
    if not new_title:
        return text, False
    if not text:
        # if empty, build a fresh note skeleton
        return build_new_note_text(title=new_title, note_id=generate_note_id()), True

    changed = False
    out = text

    m = _FM_RE.match(out)
    if m:
        fm = m.group(1) or ""
        if _TITLE_RE.search(fm):
            fm2 = _TITLE_RE.sub(f"title: {new_title}", fm, count=1)
        else:
            fm2 = fm.rstrip() + f"\ntitle: {new_title}\n"
        if fm2 != fm:
            out = _FM_RE.sub(lambda _: f"---\n{fm2}\n---\n", out, count=1)
            changed = True
    else:
        # no frontmatter: prepend
        nid, _ = parse_note_meta(out)
        nid2 = nid or generate_note_id()
        out = build_new_note_text(title=new_title, note_id=nid2) + out
        changed = True

    # Update first H1 if present (optional but nice)
    mh1 = _H1_RE.search(out)
    if mh1:
        out2 = _H1_RE.sub(f"# {new_title}", out, count=1)
        if out2 != out:
            out = out2
            changed = True

    return out, changed


def generate_note_id() -> str:
    # короткий, но достаточно уникальный для vault; при желании можно UUID целиком
    return uuid.uuid4().hex


def parse_note_meta(text: str) -> tuple[str | None, str | None]:
    """
    Best-effort парсинг метаданных заметки:
      - YAML frontmatter: note_id/title
      - fallback title: первая H1 строка "# ..."
    """
    if not text:
        return None, None

    m = _FM_RE.match(text)
    if m:
        fm = m.group(1) or ""
        note_id = None
        title = None
        mid = _NOTE_ID_RE.search(fm)
        if mid:
            note_id = (mid.group(1) or "").strip().strip('"').strip("'")
        mt = _TITLE_RE.search(fm)
        if mt:
            title = (mt.group(1) or "").strip().strip('"').strip("'")
        return note_id or None, title or None

    # no frontmatter → fallback title from first H1
    mh1 = _H1_RE.search(text)
    if mh1:
        return None, (mh1.group(1) or "").strip() or None
    return None, None


def build_new_note_text(*, title: str, note_id: str) -> str:
    title = (title or "").strip() or "Untitled"
    note_id = (note_id or "").strip() or generate_note_id()
    return (
        "---\n"
        f"note_id: {note_id}\n"
        f"title: {title}\n"
        "---\n\n"
        f"# {title}\n\n"
    )


def ensure_note_has_id(path: Path) -> str:
    """
    Миграция 'на лету':
    - если note_id уже есть → вернуть
    - если нет → сгенерить, дописать frontmatter (atomic), вернуть
    """
    text = read_note_text(path)
    note_id, title = parse_note_meta(text)
    if note_id:
        return note_id

    new_id = generate_note_id()
    # если уже есть H1 — используем её как title, иначе берём stem
    effective_title = title or path.stem

    # если есть frontmatter, но без note_id — аккуратно добавим (упрощённо: пересоберём)
    m = _FM_RE.match(text or "")
    if m:
        # prepend note_id line внутрь frontmatter
        fm = m.group(1) or ""
        if not _NOTE_ID_RE.search(fm):
            fm2 = f"note_id: {new_id}\n" + fm
            new_text = _FM_RE.sub(lambda _: f"---\n{fm2}\n---\n", text, count=1)
        else:
            new_text = text
    else:
        # нет frontmatter → добавляем новый
        new_text = build_new_note_text(title=effective_title, note_id=new_id) + (text or "")

    atomic_write_text(path, new_text, encoding="utf-8")
    return new_id


def note_path(vault_dir: Path, stem: str) -> Path:
    """
    LEGACY: строит путь к заметке по stem (т.е. по title/filename).
    В note_id-модели путь должен зависеть только от note_id:
      vault/_notes/<note_id>.md
    """
    raise RuntimeError(
        "note_path() is legacy. Use vault/_notes/<note_id>.md and NoteCatalog.get(note_id).path"
    )


def ensure_note_exists(path: Path, title: str) -> None:
    """Создаёт заметку на диске, если её нет."""
    if path.exists():
        return
    atomic_write_text(path, build_new_note_text(title=title, note_id=generate_note_id()), encoding="utf-8")


def ensure_note_exists_with_id(path: Path, *, note_id: str, title: str) -> None:
    """
    note_id-model:
      - если файла нет, создаём его с ЗАДАННЫМ note_id
    """
    if path.exists():
        return
    note_id = (note_id or "").strip()
    if not note_id:
        # fallback на старое поведение
        ensure_note_exists(path, title)
        return
    atomic_write_text(
        path,
        build_new_note_text(title=title, note_id=note_id),
        encoding="utf-8",
    )


def read_note_text(path: Path) -> str:
    """Единая точка чтения заметки (можно позже добавить recovery/encoding fallback)."""
    return path.read_text(encoding="utf-8")


def set_editor_text(editor: QTextEdit, text: str) -> None:
    """Установка текста в редактор без триггера textChanged."""
    with blocked_signals(editor):
        editor.setPlainText(text)
