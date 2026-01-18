obsidian-project/
│
├── core/                 # ДОМЕН (НЕ ЗНАЕТ НИЧЕГО О UI/FS)
│   ├── __init__.py
│   ├── models.py         # Note, NoteId, Link
│   ├── wikilinks.py      # парсинг [[WikiLinks]]
│   ├── dto.py            # DTO для UI
│   └── use_cases.py      # OpenNote, SaveNote, BuildGraph
│
├── infra/                # ИНФРАСТРУКТУРА
│   ├── __init__.py
│   ├── fs_repo.py        # файловый репозиторий
│   └── markdown_renderer.py
│
├── ui/                   # UI (PySide6)
│   ├── __init__.py
│   ├── main_window.py
│   ├── preview.py
│   └── graph_view.py
│
├── bootstrap.py          # wiring зависимостей
├── main.py               # entrypoint
└── README.md
