from __future__ import annotations

from urllib.parse import unquote

from PySide6.QtCore import Signal
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage

__all__ = ["LinkableWebView"]


class _NoteInterceptPage(QWebEnginePage):
    """
    Правильный перехват навигации: не даём QWebEngine реально "переходить"
    на note://..., а просто эмитим сигнал во view.
    """
    
    def __init__(self, view: "LinkableWebView"):
        super().__init__(view)
        self._view = view

    def acceptNavigationRequest(self, url, nav_type, isMainFrame):  # type: ignore[override]
        if isMainFrame and url.scheme() == "note":
            # Важно: для ссылок вида note://Title Qt кладёт "Title" в host(),
            # а path() может быть пустым. Для note:///Title — наоборот.
            raw = (url.path() or "").lstrip("/")
            if not raw:
                raw = url.host() or ""
            note_ref = unquote(raw).strip()
            self._view.linkClicked.emit(note_ref)
            return False  # блокируем реальную навигацию
        return super().acceptNavigationRequest(url, nav_type, isMainFrame)


class LinkableWebView(QWebEngineView):
    linkClicked = Signal(str)

    def __init__(self):
        super().__init__()
        self.setPage(_NoteInterceptPage(self))