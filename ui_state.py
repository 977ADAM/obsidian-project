import logging

from PySide6.QtCore import QTimer, QSettings
from PySide6.QtWidgets import QMainWindow, QSplitter

from app_settings import SettingsKeys


log = logging.getLogger(__name__)


class UiStateStore:
    """
    Сервис для сохранения/восстановления UI-состояния окна в QSettings.
    """
    def __init__(self, *, owner: QMainWindow, settings: QSettings, debounce_ms: int = 400):
        self._owner = owner
        self._settings = settings
        self._restoring = False
        self._timer = QTimer(owner)
        self._timer.setSingleShot(True)
        self._timer.setInterval(int(debounce_ms))
        self._timer.timeout.connect(self.save)

    def schedule_save(self) -> None:
        if self._restoring:
            return
        try:
            self._timer.start()
        except Exception:
            pass

    @staticmethod
    def _coerce_sizes(value) -> list[int] | None:
        if value is None:
            return None
        # already list-like
        if isinstance(value, (list, tuple)):
            out: list[int] = []
            for x in value:
                try:
                    out.append(int(x))
                except Exception:
                    pass
            return out or None
        # string like "200,800" or "200 800"
        if isinstance(value, str):
            parts = value.replace(",", " ").split()
            out: list[int] = []
            for p in parts:
                try:
                    out.append(int(p))
                except Exception:
                    pass
            return out or None
        return None

    def restore(self, *, splitter: QSplitter, right_splitter: QSplitter | None = None) -> None:
        self._restoring = True
        try:
            geo = self._settings.value(SettingsKeys.UI_GEOMETRY)
            if geo:
                self._owner.restoreGeometry(geo)
            else:
                self._owner.resize(1100, 700)

            st = self._settings.value(SettingsKeys.UI_STATE)
            if st:
                self._owner.restoreState(st)

            s1 = self._coerce_sizes(self._settings.value(SettingsKeys.UI_SPLITTER))
            if s1:
                try:
                    splitter.setSizes(s1)
                except Exception:
                    pass

            s2 = self._coerce_sizes(self._settings.value(SettingsKeys.UI_RIGHT_SPLITTER))
            if s2 and right_splitter is not None:
                try:
                    right_splitter.setSizes(s2)
                except Exception:
                    pass
        except Exception:
            log.exception("Failed to restore UI state from QSettings")
        finally:
            self._restoring = False

    def save(self) -> None:
        try:
            self._settings.setValue(SettingsKeys.UI_GEOMETRY, self._owner.saveGeometry())
            self._settings.setValue(SettingsKeys.UI_STATE, self._owner.saveState())

            splitter = getattr(self._owner, "splitter", None)
            if splitter is not None:
                self._settings.setValue(SettingsKeys.UI_SPLITTER, splitter.sizes())

            right_splitter = getattr(self._owner, "right_splitter", None)
            if right_splitter is not None:
                self._settings.setValue(SettingsKeys.UI_RIGHT_SPLITTER, right_splitter.sizes())
        except Exception:
            log.exception("Failed to save UI state to QSettings")
