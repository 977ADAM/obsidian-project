# app/services/graph_service.py

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, QThreadPool, Slot

from app.workers.graph_build import GraphBuildWorker


class GraphService(QObject):
    """
    Orchestrates graph rebuild requests.

    Responsibilities:
    - debounce rebuilds
    - manage req_id (drop stale results)
    - start GraphBuildWorker
    """

    def __init__(
        self,
        *,
        thread_pool: QThreadPool,
        on_finished,
        on_failed,
        debounce_ms: int = 1200,
    ):
        super().__init__()

        self._pool = thread_pool
        self._on_finished = on_finished
        self._on_failed = on_failed

        self._req_id = 0

        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(debounce_ms)
        self._debounce_timer.timeout.connect(self._build_now)

        # last requested snapshot
        self._pending_snapshot: dict | None = None

    # ───────────────────────── public API ─────────────────────────

    def request_build(
        self,
        *,
        mode: str,
        depth: int,
        center: str | None,
        outgoing_snapshot: dict[str, list[str]],
        existing_titles: set[str],
        max_nodes: int,
        max_steps: int,
        immediate: bool = False,
    ) -> None:
        """
        Request graph rebuild.

        If immediate=False → debounced.
        If immediate=True  → build immediately.
        """
        self._pending_snapshot = {
            "mode": mode,
            "depth": depth,
            "center": center,
            "outgoing_snapshot": outgoing_snapshot,
            "existing_titles": existing_titles,
            "max_nodes": max_nodes,
            "max_steps": max_steps,
        }

        if immediate:
            if self._debounce_timer.isActive():
                self._debounce_timer.stop()
            self._build_now()
        else:
            self._debounce_timer.start()

    # ───────────────────────── internals ─────────────────────────

    def _build_now(self) -> None:
        if not self._pending_snapshot:
            return

        self._req_id += 1
        req_id = self._req_id

        snap = self._pending_snapshot
        self._pending_snapshot = None

        worker = GraphBuildWorker(
            req_id=req_id,
            mode=snap["mode"],
            depth=snap["depth"],
            center=snap["center"],
            outgoing_snapshot=snap["outgoing_snapshot"],
            existing_titles=snap["existing_titles"],
            max_nodes=snap["max_nodes"],
            max_steps=snap["max_steps"],
        )

        worker.signals.finished.connect(
            lambda rid, payload: self._handle_finished(rid, payload)
        )
        worker.signals.failed.connect(
            lambda rid, err: self._handle_failed(rid, err)
        )

        self._pool.start(worker)

    @Slot(int, dict)
    def _handle_finished(self, req_id: int, payload: dict) -> None:
        if req_id != self._req_id:
            return
        self._on_finished(req_id, payload)

    @Slot(int, str)
    def _handle_failed(self, req_id: int, error: str) -> None:
        if req_id != self._req_id:
            return
        self._on_failed(req_id, error)
