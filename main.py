from logging_setup import install_global_exception_hooks, SESSION_ID, APP_NAME, setup_logging
from app import NotesApp
from PySide6.QtWidgets import QApplication


def main():
    log = setup_logging()
    install_global_exception_hooks(log)
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