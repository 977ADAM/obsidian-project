from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VaultRepository:
    vault_dir: Path

    def ensure(self) -> None:
        self.vault_dir.mkdir(parents=True, exist_ok=True)

    def note_path(self, title: str) -> Path:
        return self.vault_dir / f"{title}.md"

    def list_titles(self) -> list[str]:
        return sorted((p.stem for p in self.vault_dir.glob("*.md")), key=str.lower)

    def existing_titles(self) -> set[str]:
        return {p.stem for p in self.vault_dir.glob("*.md")}

    def read(self, title: str) -> str:
        return self.note_path(title).read_text(encoding="utf-8")

    def ensure_note(self, title: str, *, initial_text: str) -> bool:
        path = self.note_path(title)
        if path.exists():
            return False
        path.write_text(initial_text, encoding="utf-8")
        return True

    def write_atomic(self, title: str, text: str) -> None:
        path = self.note_path(title)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
