from dataclasses import dataclass
from pathlib import Path
from typing import set

@dataclass(frozen=True)
class NoteId:
    title: str

@dataclass
class Note:
    id: NoteId
    content: str

@dataclass(frozen=True)
class Link:
    src: NoteId
    dst: NoteId
