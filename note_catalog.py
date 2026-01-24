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
        # canonical title key (case-insensitive) -> note_id
        self.by_title: Dict[str, str] = {}
        self.by_path: Dict[Path, str] = {}  # path -> note_id

    @staticmethod
    def _title_key(title: str) -> str:
        """
        Key for resolving titles from UI/wikilinks:
        - filesystem-safe
        - case-insensitive (prevents duplicates from different casing)
        """
        canon = safe_filename(title)
        return canon.casefold() if canon else ""

    def clear(self) -> None:
        self.by_id.clear()
        self.by_title.clear()
        self.by_path.clear()

    def rebuild(self, vault_dir: Path, *, migrate_to_id_paths: bool = False) -> None:
        self.clear()
        notes_dir = Path(vault_dir) / "_notes"
        if migrate_to_id_paths:
            notes_dir.mkdir(parents=True, exist_ok=True)

        paths = list(vault_dir.rglob("*.md"))
        for path in paths:
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


            # Optional migration: make filesystem path depend ONLY on note_id.
            # Target: vault/_notes/<note_id>.md
            if migrate_to_id_paths:
                try:
                    target = notes_dir / f"{note_id}.md"
                    # Don't touch already-migrated file
                    if path.resolve() != target.resolve():
                        # Avoid overwriting if something already exists at destination
                        if not target.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            path.replace(target)
                            path = target
                except Exception:
                    # Best-effort: if migration fails, keep original path
                    pass

            effective_title = (title or path.stem).strip() or path.stem
            info = NoteInfo(note_id=note_id, title=effective_title, path=path)
            self.by_id[note_id] = info
            self.by_path[path] = note_id

            key = self._title_key(effective_title)
            if key and key not in self.by_title:
                self.by_title[key] = note_id

    def path_to_id(self, path: Path) -> Optional[str]:
        return self.by_path.get(Path(path))

    def resolve_title(self, title: str) -> Optional[str]:
        key = self._title_key(title)
        if not key:
            return None
        return self.by_title.get(key)

    def get(self, note_id: str) -> Optional[NoteInfo]:
        return self.by_id.get(note_id)
