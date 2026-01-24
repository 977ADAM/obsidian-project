import logging
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, Slot
from PySide6.QtWidgets import QProgressDialog, QMessageBox

from filenames import safe_filename
from logging_setup import APP_NAME, LOG_PATH
from qt_utils import blocked_signals
from rename_worker import _RenameRewriteWorker


log = logging.getLogger(APP_NAME)


class RenameRewriteController:
    """
    Инкапсулирует flow переименования: массовый rewrite wikilinks в фоне + UI прогресс/отмена.
    """

    def __init__(self, *, app: "NotesApp", pool: QThreadPool | None = None):
        self._app = app
        self._pool = pool or QThreadPool.globalInstance()
        self._req_id = 0
        self._cancel_event: threading.Event | None = None
        self._progress: QProgressDialog | None = None

    def start(self, *, old_stem: str, new_stem: str, new_path: Path) -> None:
        app = self._app
        if app.vault_dir is None:
            return

        try:
            if self._progress is not None:
                self._progress.reset()
        except Exception:
            pass

        self._req_id += 1
        req_id = self._req_id

        self._cancel_event = threading.Event()
        files = sorted(app.vault_dir.rglob("*.md"), key=lambda p: p.name.lower())

        dlg = QProgressDialog("Обновляю ссылки по хранилищу…", "Отмена", 0, max(1, len(files)), app)
        dlg.setWindowTitle("Переименование: обновление ссылок")
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setMinimumDuration(200)
        dlg.setValue(0)

        def on_cancel():
            if self._cancel_event is not None:
                self._cancel_event.set()
            dlg.setLabelText("Отменяю… (дожидаюсь текущего файла)")

        dlg.canceled.connect(on_cancel)
        self._progress = dlg

        app._set_ui_busy(True)

        worker = _RenameRewriteWorker(
            req_id=req_id,
            vault_dir=app.vault_dir,
            files=files,
            old_stem=old_stem,
            new_stem=new_stem,
            cancel_event=self._cancel_event,
        )
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(lambda rid, res: self._on_finished(rid, res, new_path=new_path))
        worker.signals.failed.connect(self._on_failed)
        self._pool.start(worker)

    @Slot(int, int, int, str)
    def _on_progress(self, req_id: int, done: int, total: int, filename: str) -> None:
        if req_id != self._req_id:
            return
        dlg = self._progress
        if dlg is None:
            return
        try:
            dlg.setMaximum(max(1, total))
            dlg.setValue(min(done, total))
            if filename:
                dlg.setLabelText(f"Обновляю ссылки… {done}/{total}\n{filename}")
        except Exception:
            pass

    def _finish_ui_cleanup(self) -> None:
        app = self._app
        try:
            if self._progress is not None:
                self._progress.setValue(self._progress.maximum())
                self._progress.close()
        except Exception:
            pass
        self._progress = None
        self._cancel_event = None
        app._set_ui_busy(False)

    def _on_finished(self, req_id: int, result: dict, *, new_path: Path) -> None:
        if req_id != self._req_id:
            return
        app = self._app
        self._finish_ui_cleanup()

        error_files: list[str] = list(result.get("error_files") or [])
        canceled = bool(result.get("canceled"))

        log.info(
            "Rename rewrite finished: total=%s changed=%s canceled=%s errors=%d",
            result.get("total_files"), result.get("changed_files"), canceled, len(error_files),
        )

        try:
            if app.current_path and app.current_path.stem == safe_filename(new_path.stem):
                app.current_path = new_path
            elif app.current_path and app.current_path.stem == safe_filename(result.get("old_stem") or ""):
                app.current_path = new_path
        except Exception:
            pass

        if app.current_path and app.current_path == new_path:
            try:
                text = new_path.read_text(encoding="utf-8")
            except Exception:
                text = app.editor.toPlainText()

            with blocked_signals(app.editor):
                app.editor.setPlainText(text)
            app._dirty = False
            app._last_saved_text = text
            app._render_preview(text)

        try:
            app._rebuild_link_index()
        except Exception:
            log.exception("Failed to rebuild link index after rename rewrite")

        app.refresh_list()
        app._select_in_list(new_path.stem)
        app.request_build_link_graph(immediate=True)
        app.graph.highlight(new_path.stem)
        app.graph.center_on(new_path.stem)
        app.refresh_backlinks()

        if canceled:
            QMessageBox.information(
                app,
                "Переименование",
                "Обновление ссылок было отменено.\n"
                "Файл заметки переименован, но ссылки могли обновиться не везде.",
            )
        elif error_files:
            sample = "\n".join(error_files[:12])
            more = "" if len(error_files) <= 12 else f"\n… и ещё {len(error_files) - 12}"
            QMessageBox.warning(
                app,
                "Переименование",
                "Переименование выполнено, но часть файлов не удалось обновить.\n\n"
                f"Проблемные файлы:\n{sample}{more}\n\n"
                f"Детали — в логах: {LOG_PATH}",
            )

    @Slot(int, str)
    def _on_failed(self, req_id: int, err: str) -> None:
        if req_id != self._req_id:
            return
        app = self._app
        self._finish_ui_cleanup()
        log.warning("Rename rewrite failed (bg): %s", err)
        QMessageBox.warning(
            app,
            "Переименование",
            "Файл был переименован, но при обновлении ссылок произошла ошибка.\n\n"
            f"{err}\n\n"
            f"Детали — в логах: {LOG_PATH}",
        )
