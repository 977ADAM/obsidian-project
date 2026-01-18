from pathlib import Path
from core.models import Note, NoteId

class NotesRepository:
    def list_notes(self) -> list[NoteId]:
        raise NotImplementedError

    def load(self, note_id: NoteId) -> Note:
        raise NotImplementedError

    def save(self, note: Note) -> None:
        raise NotImplementedError

class FsNotesRepository(NotesRepository):
    def __init__(self, vault: Path):
        self.vault = vault

    def list_notes(self):
        return [
            NoteId(p.stem)
            for p in self.vault.glob("*.md")
        ]

    def load(self, note_id: NoteId) -> Note:
        path = self.vault / f"{note_id.title}.md"
        return Note(note_id, path.read_text(encoding="utf-8"))

    def save(self, note: Note) -> None:
        path = self.vault / f"{note.id.title}.md"
        path.write_text(note.content, encoding="utf-8")
