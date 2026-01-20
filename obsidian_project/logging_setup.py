from __future__ import annotations

import logging
import sys
import uuid
from logging.handlers import RotatingFileHandler

from obsidian_project.settings import APP_NAME, LOG_DIR, LOG_PATH

SESSION_ID = uuid.uuid4().hex[:8]


class EnsureSessionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "session"):
            record.session = SESSION_ID
        return True


class SessionAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra.setdefault("session", SESSION_ID)
        return msg, kwargs


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s | sid=%(session)s"
    )
    session_filter = EnsureSessionFilter()

    fh = RotatingFileHandler(
        LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    fh.addFilter(session_filter)

    ch = logging.StreamHandler(sys.stdout or sys.stderr)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    ch.addFilter(session_filter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    logger.info("Logging initialized. log_file=%s", LOG_PATH)
    return logger


_base_logger = setup_logging()
log = SessionAdapter(_base_logger, {})


def install_global_exception_hooks() -> None:
    def _excepthook(exc_type, exc, tb):
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook

    try:
        from PySide6.QtCore import qInstallMessageHandler

        def _qt_message_handler(mode, context, message):
            try:
                file = getattr(context, "file", None)
                line = getattr(context, "line", None)
                func = getattr(context, "function", None)
                where = f"{file}:{line} {func}" if file or line or func else "unknown"
            except Exception:
                where = "unknown"

            level = logging.WARNING
            try:
                m = int(mode)
                if m == 0:
                    level = logging.DEBUG
                elif m == 4:
                    level = logging.INFO
                elif m == 2:
                    level = logging.ERROR
                elif m == 3:
                    level = logging.CRITICAL
            except Exception:
                pass

            log.log(level, "Qt: %s | where=%s", message, where)

        qInstallMessageHandler(_qt_message_handler)
        log.info("Qt message handler installed")
    except Exception:
        log.exception("Failed to install Qt message handler")
