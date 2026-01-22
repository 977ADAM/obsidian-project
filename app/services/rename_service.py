# app/services/rename_service.py

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThreadPool, Slot

from app.core.filenames import safe_filename
from app.workers.rename_rewrite import RenameRewriteWorker


class RenameService(QObject):
    """
    Orchestrates note rename + wikilink rewrite.

    Responsibilities:
    - validate rename
    - manage req_id / cancel
    - start RenameRewriteWorker
    - drop stale results
    """

    def __init__(
        self,
        *,
        thread_pool: QThreadPool,
        on_progress,
        on_finished,
        on_failed,
    ):
        super().__init__()

        self._pool = thread_pool
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._on_failed = on_failed

        self._req_id = 0
        self._cancel_event: threading.Event | None = None

    # ───────────────────────── public API ─────────────────────────

    def start(
        self,
        *,
        vault_dir: Path,
        files: list[Path],
        old_title: str,
        new_title: str,
    ) -> tuple[bool, str | None]:
        """
        Start rename rewrite operation.

        Returns (ok, error_message).
        """
        old_stem = safe_filename(old_title)
        new_stem = safe_filename(new_title)

        if not old_stem or not new_stem:
            return False, "Некорректное имя заметки."

        if old_stem == new_stem:
            return False, "Имя не изменилось."

        self._req_id += 1
        req_id = self._req_id

        self._cancel_event = threading.Event()

        worker = RenameRewriteWorker(
            req_id=req_id,
            vault_dir=vault_dir,
            files=files,
            old_stem=old_stem,
            new_stem=new_stem,
            cancel_event=self._cancel_event,
        )

        worker.signals.progress.connect(
            lambda rid, done, total, fn: self._handle_progress(
                rid, done, total, fn
            )
        )
        worker.signals.finished.connect(
            lambda rid, res: self._handle_finished(
                rid, res, old_stem=old_stem, new_stem=new_stem
            )
        )
        worker.signals.failed.connect(
            lambda rid, err: self._handle_failed(rid, err)
        )

        self._pool.start(worker)
        return True, None

    def cancel(self) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()

    # ───────────────────────── internal ─────────────────────────

    @Slot(int, int, int, str)
    def _handle_progress(self, req_id: int, done: int, total: int, filename: str) -> None:
        if req_id != self._req_id:
            return
        self._on_progress(req_id, done, total, filename)

    @Slot(int, dict)
    def _handle_finished(
        self,
        req_id: int,
        result: dict,
        *,
        old_stem: str,
        new_stem: str,
    ) -> None:
        if req_id != self._req_id:
            return
        self._on_finished(req_id, result, old_stem, new_stem)

    @Slot(int, str)
    def _handle_failed(self, req_id: int, err: str) -> None:
        if req_id != self._req_id:
            return
        self._on_failed(req_id, err)
