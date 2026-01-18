# main.py
"""App entrypoint.

MVP вертикальный срез:
- список заметок из vault
- открыть/редактировать/сохранить
- превью markdown

UI сделан на tkinter (stdlib), чтобы проект запускался без внешних GUI-зависимостей.
Позже легко заменить на PySide6/PyQt.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from bootstrap import build_services
from ui.main_window import MainWindow


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Obsidian-like notes MVP")
    p.add_argument(
        "--vault",
        type=Path,
        default=Path.cwd() / "vault",
        help="Path to notes folder (vault)",
    )
    return p.parse_args()


def ensure_vault(vault: Path) -> None:
    vault.mkdir(parents=True, exist_ok=True)
    # чтобы не стартовать с пустым экраном
    if not any(vault.glob("*.md")):
        (vault / "Welcome.md").write_text(
            "# Welcome\n\nЭто первая заметка.\n\n- Редактируй слева\n- Ctrl+S чтобы сохранить\n- [[Welcome]] — пример wikilink\n",
            encoding="utf-8",
        )


def main() -> int:
    args = parse_args()
    ensure_vault(args.vault)

    services = build_services(args.vault)
    win = MainWindow(services)
    win.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
