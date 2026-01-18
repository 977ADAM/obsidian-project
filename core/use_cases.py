from core.models import Note, NoteId
from core.wikilinks import extract_links
from core.dto import GraphDto

class OpenNote:
    def __init__(self, repo):
        self.repo = repo

    def execute(self, title: str) -> Note:
        note_id = NoteId(title)
        return self.repo.load(note_id)


class SaveNote:
    def __init__(self, repo):
        self.repo = repo

    def execute(self, note: Note):
        self.repo.save(note)


class BuildGraph:
    def __init__(self, repo):
        self.repo = repo

    def execute(self) -> GraphDto:
        notes = self.repo.list_notes()
        links = []

        for note_id in notes:
            note = self.repo.load(note_id)
            for target in extract_links(note.content):
                links.append((note_id.title, target))

        return GraphDto(nodes=[n.title for n in notes], edges=links)
