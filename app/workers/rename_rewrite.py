# app/workers/rename_rewrite.py

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from app.core.filenames import safe_filename
from app.core.wikilinks import rewrite_wikilinks_targets
from app.infrastructure.filesystem import atomic_write_text


class RenameRewriteSignals(QObject):
    """
    Signals emitted by RenameRewriteWorker.

    progress(req_id, done, total, filename)
    finished(req_id, result_dict)
    failed(req_id, error_message)
    """
    progress = Signal(int, int, int, str)
    finished = Signal(int, dict)
    failed = Signal(int, str)


class RenameRewriteWorker(QRunnable):
    """
    Background worker that rewrites wikilinks across a vault
    after a note rename.

    IMPORTANT:
    - No UI code
    - No Qt widgets
    - Thread-safe
    """

    def __init__(
        self,
        *,
        req_id: int,
        vault_dir: Path,
        files: list[Path],
        old_stem: str,
        new_stem: str,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.req_id = req_id
        self.vault_dir = Path(vault_dir)
        self.files = list(files)
        self.old_stem = safe_filename(old_stem)
        self.new_stem = safe_filename(new_stem)
        self.cancel_event = cancel_event

        self.signals = RenameRewriteSignals()

    def run(self) -> None:
        try:
            result = self._run_internal()
            self.signals.finished.emit(self.req_id, result)
        except Exception as exc:
            self.signals.failed.emit(self.req_id, str(exc))

    # ───────────────────────── internal ─────────────────────────

    def _run_internal(self) -> dict:
        total = len(self.files)
        done = 0
        changed_files = 0
        error_files: list[str] = []
        canceled = False

        for path in self.files:
            if self.cancel_event.is_set():
                canceled = True
                break

            done += 1
            self.signals.progress.emit(
                self.req_id,
                done,
                total,
                path.name,
            )

            try:
                original = path.read_text(encoding="utf-8")

                # Backup once (best-effort)
                backup = path.with_suffix(path.suffix + ".bak")
                if not backup.exists():
                    atomic_write_text(backup, original, encoding="utf-8")

                rewritten, changed = rewrite_wikilinks_targets(
                    original,
                    old_stem=self.old_stem,
                    new_stem=self.new_stem,
                )

                if changed:
                    atomic_write_text(path, rewritten, encoding="utf-8")
                    changed_files += 1

            except Exception:
                error_files.append(str(path))
                # do NOT crash entire operation
                continue

        return {
            "old_stem": self.old_stem,
            "new_stem": self.new_stem,
            "total_files": total,
            "changed_files": changed_files,
            "error_files": error_files,
            "canceled": canceled,
        }
