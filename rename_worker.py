from pathlib import Path
import threading
from PySide6.QtCore import QObject, QRunnable, Signal
from filenames import safe_filename
from wikilinks import rewrite_wikilinks_targets
from filesystem import atomic_write_text



class _RenameRewriteSignals(QObject):
    progress = Signal(int, int, int, str)  # req_id, done, total, filename
    finished = Signal(int, dict)           # req_id, result
    failed = Signal(int, str)              # req_id, err


class _RenameRewriteWorker(QRunnable):
    def __init__(
        self,
        *,
        req_id: int,
        vault_dir: Path,
        files: list[Path],
        old_title: str,
        new_title: str,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.req_id = req_id
        self.vault_dir = vault_dir
        self.files = files
        self.old_title = safe_filename(old_title)
        self.new_title = safe_filename(new_title)
        self.cancel_event = cancel_event
        self.signals = _RenameRewriteSignals()

    def run(self) -> None:
        try:
            changed_files = 0
            total_files = len(self.files)
            error_files: list[str] = []
            done = 0
            canceled = False

            for p in self.files:
                # Пользователь нажал "Отмена"
                if self.cancel_event.is_set():
                    canceled = True
                    break

                done += 1
                try:
                    # прогресс: имя файла
                    self.signals.progress.emit(self.req_id, done, total_files, p.name)

                    txt = p.read_text(encoding="utf-8")

                    # --- BACKUP BEFORE REWRITE ---
                    backup_path = p.with_suffix(p.suffix + ".bak")
                    if not backup_path.exists():
                        atomic_write_text(backup_path, txt, encoding="utf-8")

                    new_txt, changed = rewrite_wikilinks_targets(
                        txt,
                        old_stem=self.old_title,
                        new_stem=self.new_title,
                    )
                    if changed:
                        atomic_write_text(p, new_txt, encoding="utf-8")
                        changed_files += 1
                except Exception:
                    error_files.append(str(p))
                    # продолжаем, не валим всю операцию
                    continue

            result = {
                "old_title": self.old_title,
                "new_title": self.new_title,
                "total_files": total_files,
                "changed_files": changed_files,
                "error_files": error_files,
                "canceled": canceled,
            }
            self.signals.finished.emit(self.req_id, result)
        except Exception as e:
            self.signals.failed.emit(self.req_id, str(e))

    # NOTE: _RenameRewriteWorker должен содержать только __init__/run и сигналы.
    # Любые методы NotesApp сюда не должны попадать.