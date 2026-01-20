from __future__ import annotations

from PySide6.QtWidgets import QApplication

from obsidian_project.ui.main_window import NotesApp
from obsidian_project.logging_setup import install_global_exception_hooks, log, SESSION_ID


def main() -> int:
    install_global_exception_hooks()
    app = QApplication([])
    win = NotesApp()
    win.resize(1100, 700)
    win.show()
    log.info("Приложение запущено, SID=%s", SESSION_ID)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
