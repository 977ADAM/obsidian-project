from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from filenames import safe_filename
from note_io import parse_note_meta, ensure_note_has_id, read_note_text


@dataclass(frozen=True)
class NoteInfo:
    note_id: str
    title: str
    path: Path


class NoteCatalog:
    """
    Source of truth for mapping:
      note_id -> (title, path)
      canonical_title -> note_id   (best-effort; collisions handled later)
    """

    def __init__(self) -> None:
        self.by_id: Dict[str, NoteInfo] = {}
        self.by_title: Dict[str, str] = {}  # canonical title -> note_id

    def clear(self) -> None:
        self.by_id.clear()
        self.by_title.clear()

    def rebuild(self, vault_dir: Path) -> None:
        self.clear()
        for path in vault_dir.rglob("*.md"):
            try:
                text = read_note_text(path)
            except Exception:
                continue

            note_id, title = parse_note_meta(text)
            if not note_id:
                # migration-on-scan (можно выключить, если не хотите писать на диск тут)
                try:
                    note_id = ensure_note_has_id(path)
                except Exception:
                    continue

                # перечитаем мету (title могли добавить/нормализовать)
                try:
                    text2 = read_note_text(path)
                    _, title2 = parse_note_meta(text2)
                    if title2:
                        title = title2
                except Exception:
                    pass

            if not note_id:
                continue

            effective_title = (title or path.stem).strip() or path.stem
            info = NoteInfo(note_id=note_id, title=effective_title, path=path)
            self.by_id[note_id] = info

            canon = safe_filename(effective_title)
            if canon and canon not in self.by_title:
                self.by_title[canon] = note_id

    def resolve_title(self, title: str) -> Optional[str]:
        canon = safe_filename(title)
        if not canon:
            return None
        return self.by_title.get(canon)

    def get(self, note_id: str) -> Optional[NoteInfo]:
        return self.by_id.get(note_id)
