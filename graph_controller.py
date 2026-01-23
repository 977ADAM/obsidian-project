from __future__ import annotations

import logging
from typing import Callable, Optional
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QThreadPool, Slot

from graph_worker import _GraphBuildWorker


class GraphController(QObject):
    """
    Оркестратор построения графа:
      - debounce запросов
      - монотонный req_id для отбрасывания устаревших результатов
      - запуск _GraphBuildWorker в QThreadPool

    UI-слой (NotesApp) остаётся ответственным за применение payload к GraphView.
    """

    def __init__(
        self,
        *,
        parent: QObject,
        pool: Optional[QThreadPool] = None,
        debounce_ms: int = 1200,
        get_context: Callable[[], Optional[dict]],
        on_built: Callable[[dict], None],
        on_failed: Callable[[str], None],
        logger: Optional[logging.Logger] = None,
    ) -> None:
        super().__init__(parent)
        self._log = logger or logging.getLogger(__name__)
        self._pool = pool or QThreadPool.globalInstance()
        self._get_context = get_context
        self._on_built_cb = on_built
        self._on_failed_cb = on_failed

        self._req_id = 0

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(int(debounce_ms))
        self._debounce.timeout.connect(self._request_now)

    def stop(self) -> None:
        """Остановить debounce-таймер (безопасно при close/switch vault)."""
        try:
            if self._debounce.isActive():
                self._debounce.stop()
        except Exception:
            self._log.exception("Failed to stop graph debounce timer")

    def request(self, *, immediate: bool = False) -> None:
        ctx = self._get_context()
        if not ctx:
            return
        if immediate:
            self.stop()
            self._request_now()
        else:
            self._debounce.start()

    def _request_now(self) -> None:
        ctx = self._get_context()
        if not ctx:
            return

        self._req_id += 1
        req_id = self._req_id

        worker = _GraphBuildWorker(
            req_id=req_id,
            vault_dir=ctx["vault_dir"],
            mode=ctx["mode"],
            depth=ctx["depth"],
            center=ctx["center"],
            outgoing_snapshot=ctx["outgoing_snapshot"],
            existing_titles=ctx["existing_titles"],
            max_nodes=ctx["max_nodes"],
            max_steps=ctx["max_steps"],
        )
        worker.signals.finished.connect(self._on_worker_finished)
        worker.signals.failed.connect(self._on_worker_failed)
        self._pool.start(worker)

    @Slot(int, dict)
    def _on_worker_finished(self, req_id: int, payload: dict) -> None:
        if req_id != self._req_id:
            return
        try:
            self._on_built_cb(payload)
        except Exception:
            self._log.exception("Failed to apply graph payload")

    @Slot(int, str)
    def _on_worker_failed(self, req_id: int, err: str) -> None:
        if req_id != self._req_id:
            return
        try:
            self._on_failed_cb(err)
        except Exception:
            self._log.exception("Failed to handle graph failure")
