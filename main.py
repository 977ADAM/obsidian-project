from __future__ import annotations

from PySide6.QtWidgets import QApplication
from app import install_global_exception_hooks, NotesApp, APP_NAME, SESSION_ID, log


def main():
    install_global_exception_hooks()
    app = QApplication([])
    # Ensure QSettings uses stable org/app identifiers.
    app.setOrganizationName(APP_NAME)
    app.setApplicationName(APP_NAME)
    win = NotesApp()
    win.show()
    log.info("Приложение запущено, SID=%s", SESSION_ID)
    app.exec()


if __name__ == "__main__":
    main()