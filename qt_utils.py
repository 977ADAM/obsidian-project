from __future__ import annotations

from contextlib import contextmanager
from PySide6.QtCore import QSettings


@contextmanager
def blocked_signals(obj):
    """
    Контекст-менеджер: временно выключает Qt-сигналы у объекта и гарантированно
    включает обратно.
    """
    if obj is None:
        yield
        return
    try:
        obj.blockSignals(True)
        yield
    finally:
        try:
            obj.blockSignals(False)
        except Exception:
            # В редких случаях объект уже мог быть уничтожен Qt.
            pass


def safe_set_setting(settings: QSettings, key: str, value) -> None:
    """Best-effort запись в QSettings без падений UI."""
    try:
        settings.setValue(key, value)
    except Exception:
        pass
