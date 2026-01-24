import uuid
import logging
from pathlib import Path
import time

from PySide6.QtCore import Qt, QTimer, QThreadPool, QSettings
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QTextEdit, QLineEdit, QFileDialog, QMessageBox, QSplitter
)

from filenames import safe_filename
from links import LinkIndex
from graph_view import GraphView
from logging_setup import APP_NAME
from graph_controller import GraphController
from filesystem import atomic_write_text, write_recovery_copy
from quick_switcher import QuickSwitcherDialog
from preview_renderer import render_preview_page
from navigation import NavigationController
from app_settings import SettingsKeys, get_int, get_str
from webview import LinkableWebView
from ui_state import UiStateStore
from ui_dialogs import ask_vault_cancel_action, build_rename_dialog
from qt_utils import blocked_signals, safe_set_setting
from note_io import note_path, ensure_note_exists, read_note_text, set_editor_text
from rename_controller import RenameRewriteController
from app_helpers import (
    AUTOSAVE_DEBOUNCE_MS,
    PREVIEW_DEBOUNCE_MS_DEFAULT,
    PREVIEW_DEBOUNCE_MS_MIN,
    PREVIEW_DEBOUNCE_MS_MAX_ADD,
    PREVIEW_DEBOUNCE_MS_CHARS_PER_STEP,
    normalize_theme,
    normalize_graph_mode,
)
from preview_timing import compute_preview_debounce_ms


log = logging.getLogger(APP_NAME)


class NotesApp(QMainWindow):
    def __init__(self):
        super().__init__()
        log.info("Приложение инициализировано")
        self.setWindowTitle("obsidian-project (Python)")

        # --- SETTINGS ---
        # Храним (и восстанавливаем) UI-состояние и пользовательские опции.
        # QSettings сам выберет корректное место под конкретную ОС.
        self._settings = QSettings(APP_NAME, APP_NAME)
        self._ui_state = UiStateStore(owner=self, settings=self._settings, debounce_ms=400)

        # persisted options
        self._theme = get_str(self._settings, SettingsKeys.UI_THEME, "dark")
        self.graph_mode = get_str(self._settings, SettingsKeys.GRAPH_MODE, "global")
        self.graph_depth = get_int(self._settings, SettingsKeys.GRAPH_DEPTH, 1)

        # --- GRAPH LIMITS / PERF (persisted) ---
        self.max_graph_nodes = get_int(self._settings, SettingsKeys.GRAPH_MAX_NODES, 400)
        self.max_graph_steps = get_int(self._settings, SettingsKeys.GRAPH_MAX_STEPS, 250)

        # startup restore targets (vault + last note)
        self._startup_vault_dir = get_str(self._settings, SettingsKeys.VAULT_DIR, "") or ""
        self._startup_last_note = get_str(self._settings, SettingsKeys.LAST_NOTE, "") or ""

        self.vault_dir: Path | None = None
        self.current_path: Path | None = None

        # Token that changes every time we open/switch note.
        # Used to avoid autosave race writing to the wrong file.
        self._note_token: str = uuid.uuid4().hex

        # --- LINK INDEX ---
        self._link_index = LinkIndex()

        # ---- NAVIGATION ----
        self._nav = NavigationController(self._open_note_no_history)

        self._dirty = False
        self._pending_save_token: str = self._note_token
        # Последняя сохранённая/загруженная версия текста текущей заметки.
        # Нужна, чтобы не терять изменения при переключении (даже если _dirty "не успел").
        self._last_saved_text: str = ""

        # UI

        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск… (по имени файла)")
        self.listw = QListWidget()
        self.backlinks = QListWidget()
        self.backlinks.setMinimumHeight(120)
        self.backlinks.setToolTip("Backlinks: кто ссылается на текущую заметку")

        self.editor = QTextEdit()
        self.preview = LinkableWebView()

        self.graph = GraphView(self.open_or_create_by_title)

        self.splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.listw)
        left_layout.addWidget(self.backlinks)
        self.backlinks.itemClicked.connect(lambda it: self.open_or_create_by_title(it.text()))

        self.right_splitter = QSplitter(Qt.Vertical)
        self.right_splitter.addWidget(self.editor)
        self.right_splitter.addWidget(self.preview)
        self.right_splitter.addWidget(self.graph)
        self.right_splitter.setStretchFactor(0, 3)
        self.right_splitter.setStretchFactor(1, 2)
        self.right_splitter.setStretchFactor(2, 2)

        self.splitter.addWidget(left)
        self.splitter.addWidget(self.right_splitter)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)

        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(self.splitter)
        self.setCentralWidget(root)

        # restore UI state (window geometry/splitters) after widgets exist
        self._ui_state.restore(splitter=self.splitter, right_splitter=self.right_splitter)
        self.splitter.splitterMoved.connect(lambda *_: self._ui_state.schedule_save())
        self.right_splitter.splitterMoved.connect(lambda *_: self._ui_state.schedule_save())

        # Autosave debounce
        self.save_timer = QTimer(self)
        self.save_timer.setInterval(AUTOSAVE_DEBOUNCE_MS)  # мс
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self._save_current_if_needed)

        # Preview debounce (чтобы не рендерить markdown на каждый символ)
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(PREVIEW_DEBOUNCE_MS_DEFAULT)  # мс (дебаунс превью; дальше можем адаптировать)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.timeout.connect(self._render_preview_from_editor)

        self._last_preview_source_text: str | None = None

        # ---- GRAPH BUILD (moved to GraphController) ----
        self._graph_ctrl = GraphController(
            parent=self,
            debounce_ms=1200,
            get_context=self._graph_context_snapshot,
            on_built=self._apply_graph_payload,
            on_failed=self._on_graph_error,
            logger=log,
        )

        # ---- RENAME REWRITE (background) ----
        self._rename = RenameRewriteController(app=self, pool=QThreadPool.globalInstance())

        # Signals
        self.search.textChanged.connect(self.refresh_list)
        self.listw.itemSelectionChanged.connect(self._on_select_note)
        self.editor.textChanged.connect(self._on_text_changed)
        self.preview.linkClicked.connect(self.open_or_create_by_title)

        # Menu
        self._build_menu()

        # Apply persisted view options now that actions exist.
        self._apply_theme(self._theme, save=False)
        self._apply_graph_mode(self.graph_mode, self.graph_depth, save=False)

        # Start: try reopen last vault/note (fallback to picker).
        self._startup_open()

    def _stop_timers_and_flush(self, *, reason: str) -> None:
        """
        Единая точка: остановить дебаунс-таймеры и сохранить текущую заметку,
        чтобы не потерять изменения при смене контекста (vault/rename/close).
        """
        try:
            self._stop_debounced_timers()
            self._flush_current_note_before_switch()
        except Exception:
            log.exception("Failed to stop timers and flush (reason=%s)", reason)

    def _compute_preview_debounce_ms(self, txt_len: int) -> int:
        return compute_preview_debounce_ms(
            txt_len,
            min_ms=PREVIEW_DEBOUNCE_MS_MIN,
            max_add_ms=PREVIEW_DEBOUNCE_MS_MAX_ADD,
            chars_per_step=PREVIEW_DEBOUNCE_MS_CHARS_PER_STEP,
            default_ms=PREVIEW_DEBOUNCE_MS_DEFAULT,
        )

    def _switch_to_note(self, *, path: Path, title: str) -> None:
        """
        Единый сценарий переключения заметки (без истории):
          - стоп таймеров предыдущей заметки
          - flush предыдущих изменений
          - ensure file exists
          - load текст
          - обновить UI/state
        """
        self._stop_timers_and_flush(reason="switch_to_note")
        ensure_note_exists(path, title)

        self.current_path = path
        self._note_token = uuid.uuid4().hex

        # persist last opened note
        safe_set_setting(self._settings, SettingsKeys.LAST_NOTE, title)

        text = read_note_text(path)
        set_editor_text(self.editor, text)

        self._dirty = False
        self._last_saved_text = text

        self._render_preview(text)
        self._select_in_list(title)

        self.request_build_link_graph(immediate=True)
        self.graph.highlight(title)
        self.graph.center_on(title)
        self.refresh_backlinks()

    def _set_ui_busy(self, busy: bool) -> None:
        """
        Минимальная блокировка UI на время тяжёлых фоновых операций (mass rewrite).
        """
        try:
            self.search.setEnabled(not busy)
            self.listw.setEnabled(not busy)
            self.backlinks.setEnabled(not busy)
            # на время переписывания лучше запретить правку, чтобы не получить гонки
            self.editor.setReadOnly(busy)
            # меню оставляем, но можно расширить при желании
        except Exception:
            log.exception("Failed to toggle UI busy state (busy=%s)", busy)

    def _stop_debounced_timers(self) -> None:
        """Stop delayed autosave/preview/graph debounce timers (safe before switching context)."""
        for t in (getattr(self, "save_timer", None), getattr(self, "preview_timer", None)):
            try:
                if t is not None and t.isActive():
                    t.stop()
            except Exception:
                log.exception("Failed to stop timer: %r", t)
        # graph debounce is inside controller now
        try:
            if hasattr(self, "_graph_ctrl") and self._graph_ctrl is not None:
                self._graph_ctrl.stop()
        except Exception:
            log.exception("Failed to stop graph controller")

    def _sync_action_checks(self, mapping: dict[QAction, bool]) -> None:
        """
        Единая точка синхронизации checked-state для QAction.
        Полезно, чтобы не ловить лишние signal-циклы.
        """
        for act, checked in mapping.items():
            if act is None:
                continue
            try:
                act.blockSignals(True)
                act.setChecked(bool(checked))
            finally:
                act.blockSignals(False)

    def _apply_theme(self, name: str, *, save: bool = True) -> None:
        self._theme = normalize_theme(name)

        # keep menu state if actions exist
        if hasattr(self, "_act_dark"):
            self._sync_action_checks({
                self._act_dark: self._theme == "dark",
                self._act_light: self._theme == "light",
            })

        try:
            self.graph.apply_theme(self._theme)
            # перестроим граф, чтобы ноды пересоздались с новой темой
            self.request_build_link_graph(immediate=True)
            if self.current_path:
                self.graph.highlight(self.current_path.stem)
        except Exception:
            log.exception("Failed to apply theme")

        if save:
            safe_set_setting(self._settings, SettingsKeys.UI_THEME, self._theme)

    def _apply_graph_mode(self, mode: str, depth: int = 1, *, save: bool = True) -> None:
        mode_norm, depth_norm = normalize_graph_mode(mode, depth)
        self.graph_mode = mode_norm
        self.graph_depth = depth_norm

        if hasattr(self, "_act_graph_global"):
            self._sync_action_checks({
                self._act_graph_global: self.graph_mode == "global",
                self._act_graph_local1: (self.graph_mode == "local" and self.graph_depth == 1),
                self._act_graph_local2: (self.graph_mode == "local" and self.graph_depth == 2),
            })

        try:
            self.request_build_link_graph(immediate=True)
            if self.current_path:
                self.graph.highlight(self.current_path.stem)
        except Exception:
            log.exception("Failed to apply graph mode")

        if save:
            safe_set_setting(self._settings, SettingsKeys.GRAPH_MODE, self.graph_mode)
            safe_set_setting(self._settings, SettingsKeys.GRAPH_DEPTH, self.graph_depth)

    def _startup_open(self) -> None:
        # try reopen vault
        vault_path = Path(self._startup_vault_dir) if self._startup_vault_dir else None
        if vault_path and vault_path.exists() and vault_path.is_dir():
            self._open_vault_at(vault_path, save=True)
        else:
            self.choose_vault()

        # open last note (if exists)
        try:
            if self.vault_dir and self._startup_last_note:
                stem = safe_filename(self._startup_last_note)
                p = note_path(self.vault_dir, stem)
                if p.exists():
                    self._open_note_no_history(p.stem)
        except Exception:
            log.exception("Failed to open last note on startup")

    def _open_vault_at(self, vault_dir: Path, *, save: bool = True) -> None:
        self.vault_dir = Path(vault_dir)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        log.info("Vault selected: %s", self.vault_dir)

        if save:
            safe_set_setting(self._settings, SettingsKeys.VAULT_DIR, str(self.vault_dir))

        self.current_path = None
        with blocked_signals(self.editor):
            self.editor.clear()
        self._dirty = False
        self._last_saved_text = ""
        self._nav.clear()

        self._rebuild_link_index()
        self.refresh_list()
        self.request_build_link_graph(immediate=True)

    def closeEvent(self, event):  # type: ignore[override]
        """
        Ensure last edits are persisted on window close.
        Protects against data loss when autosave timer hasn't fired yet.
        """
        self._stop_timers_and_flush(reason="closeEvent")
        try:
            self._ui_state.save()
        except Exception:
            log.exception("Failed to save UI state on close")
        super().closeEvent(event)

    def resizeEvent(self, event):  # type: ignore[override]
        # сохраняем геометрию окна (debounced)
        try:
            self._ui_state.schedule_save()
        except Exception:
            pass
        super().resizeEvent(event)

    def moveEvent(self, event):  # type: ignore[override]
        # сохраняем позицию окна (debounced)
        try:
            self._ui_state.schedule_save()
        except Exception:
            pass
        super().moveEvent(event)

    def _build_menu(self):
        menubar = self.menuBar()
        filem = menubar.addMenu("Файл")

        act_open_vault = QAction("Открыть папку (vault)…", self)
        act_open_vault.triggered.connect(self.choose_vault)

        act_new = QAction("Новая заметка…", self)
        act_new.triggered.connect(self.create_note_dialog)

        act_save = QAction("Сохранить", self)
        act_save.setShortcut("Ctrl+S")
        act_save.triggered.connect(lambda: self.save_now(force=True))

        act_rename = QAction("Переименовать заметку…", self)
        act_rename.setShortcut("F2")
        act_rename.triggered.connect(self.rename_current_note_dialog)

        filem.addAction(act_open_vault)
        filem.addAction(act_new)
        filem.addAction(act_rename)
        filem.addSeparator()
        filem.addAction(act_save)

        act_switcher = QAction("Quick Switcher…", self)
        act_switcher.setShortcut("Ctrl+P")
        act_switcher.triggered.connect(self.open_quick_switcher)
        filem.addSeparator()
        filem.addAction(act_switcher)

        navm = menubar.addMenu("Навигация")

        act_back = QAction("Назад", self)
        act_back.setShortcut("Alt+Left")
        act_back.triggered.connect(self.nav_back)

        act_forward = QAction("Вперёд", self)
        act_forward.setShortcut("Alt+Right")
        act_forward.triggered.connect(self.nav_forward)

        navm.addAction(act_back)
        navm.addAction(act_forward)

        viewm = menubar.addMenu("Вид")

        self._act_dark = QAction("Тема: Dark", self, checkable=True)
        self._act_light = QAction("Тема: Light", self, checkable=True)
        self._act_dark.setChecked(self._theme == "dark")
        self._act_light.setChecked(self._theme == "light")

        self._act_dark.triggered.connect(lambda: self._apply_theme("dark", save=True))
        self._act_light.triggered.connect(lambda: self._apply_theme("light", save=True))

        viewm.addAction(self._act_dark)
        viewm.addAction(self._act_light)

        graphm = menubar.addMenu("Граф")

        self._act_graph_global = QAction("Global", self, checkable=True)
        self._act_graph_local1 = QAction("Local (1 hop)", self, checkable=True)
        self._act_graph_local2 = QAction("Local (2 hops)", self, checkable=True)

        # init checked state from settings
        self._act_graph_global.setChecked(self.graph_mode == "global")
        self._act_graph_local1.setChecked(self.graph_mode == "local" and self.graph_depth == 1)
        self._act_graph_local2.setChecked(self.graph_mode == "local" and self.graph_depth == 2)

        self._act_graph_global.triggered.connect(lambda: self._apply_graph_mode("global", 1, save=True))
        self._act_graph_local1.triggered.connect(lambda: self._apply_graph_mode("local", 1, save=True))
        self._act_graph_local2.triggered.connect(lambda: self._apply_graph_mode("local", 2, save=True))

        graphm.addAction(self._act_graph_global)
        graphm.addAction(self._act_graph_local1)
        graphm.addAction(self._act_graph_local2)

    def open_quick_switcher(self):
        if self.vault_dir is None:
            return

        def get_titles():
            # заметки на диске (без виртуальных)
            return [p.stem for p in self.vault_dir.glob("*.md")]

        dlg = QuickSwitcherDialog(self, get_titles=get_titles, on_open=self.open_or_create_by_title)
        dlg.exec()

    def _open_note_no_history(self, title: str):
        """Открывает/создаёт заметку и обновляет UI, но НЕ трогает историю."""
        if self.vault_dir is None:
            return

        stem = safe_filename(title)
        path = note_path(self.vault_dir, stem)
        log.info("Открытая заметка (без истории): stem=%s путь=%s", stem, path)

        self._switch_to_note(path=path, title=stem)

    def choose_vault(self):
        log.info("Открылось диалоговое окно «Выбрать хранилище».")

        # IMPORTANT: don't lose unsaved edits when switching vaults.
        self._stop_timers_and_flush(reason="choose_vault")

        path = QFileDialog.getExistingDirectory(self, "Выберите папку для заметок")

        if not path:
            # Пользователь отменил выбор папки
            if self.vault_dir is None:
                # Дадим явный выбор, чтобы не создавать папки "молча"

                action = ask_vault_cancel_action(self)
                if action == "retry":
                    return self.choose_vault()

                if action == "exit":
                    QApplication.instance().quit()
                    return

                tmp = Path.home() / f".{APP_NAME}" / "vault"
                log.warning("Хранилище не выбрано. Используется резервное хранилище=%s", tmp)
                self._open_vault_at(tmp, save=True)
            else:
                log.info("Выбор хранилища отменён. Сохраняется хранилище=%s", self.vault_dir)
            return

        self._open_vault_at(Path(path), save=True)

    def _rebuild_link_index(self) -> None:
        if self.vault_dir is None:
            self._link_index.clear()
            return
        t0 = time.perf_counter()
        self._link_index.rebuild_from_vault(self.vault_dir)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        log.info(
            "Link index rebuilt: notes=%d incoming_keys=%d outgoing_keys=%d time_ms=%.1f",
            len(list(self.vault_dir.glob("*.md"))),
            len(self._link_index.incoming),
            len(self._link_index.outgoing),
            dt_ms,
        )

    def list_notes(self) -> list[Path]:
        assert self.vault_dir is not None
        return sorted(self.vault_dir.glob("*.md"), key=lambda p: p.name.lower())

    def refresh_list(self):
        if self.vault_dir is None:
            return
        q = self.search.text().strip().lower()
        notes = self.list_notes()

        with blocked_signals(self.listw):
            self.listw.clear()
            for p in notes:
                if not q or q in p.stem.lower():
                    self.listw.addItem(p.stem)

    def _on_select_note(self):
        items = self.listw.selectedItems()
        if not items:
            return
        title = items[0].text()
        self.open_or_create_by_title(title)

    def open_or_create_by_title(self, title: str):
        if self.vault_dir is None:
            return

        title = safe_filename(title)

        current = self.current_path.stem if self.current_path else None
        if current == title:
            return
        self._nav.open(title, reopen_current=False)

    def nav_back(self):
        self._nav.back()

    def nav_forward(self):
        self._nav.forward()

    def _select_in_list(self, title: str):
        # Важно: setCurrentRow триггерит itemSelectionChanged -> _on_select_note -> open_or_create...
        # Поэтому на время выделения блокируем сигналы списка.
        def find_row() -> int:
            for i in range(self.listw.count()):
                if self.listw.item(i).text() == title:
                    return i
            return -1

        with blocked_signals(self.listw):
            row = find_row()
            if row >= 0:
                self.listw.setCurrentRow(row)
                return

            # если не было — обновим список и попробуем ещё раз
            self.refresh_list()
            row = find_row()
            if row >= 0:
                self.listw.setCurrentRow(row)

    def _on_text_changed(self):
        self._dirty = True
        # remember which note the pending autosave belongs to
        self._pending_save_token = self._note_token
        # Не рендерим превью на каждый символ — дебаунсим (адаптивно под размер заметки).
        txt_len = len(self.editor.toPlainText())
        self.preview_timer.setInterval(self._compute_preview_debounce_ms(txt_len))
        self.preview_timer.start()
        self.save_timer.start()

    def _render_preview_from_editor(self):
        """Рендер превью из текущего текста редактора (используется таймером)."""
        self._render_preview(self.editor.toPlainText())

    def _render_preview(self, text: str):
        if getattr(self, "_last_preview_source_text", None) == text:
            return
        self._last_preview_source_text = text
        self.preview.setHtml(render_preview_page(text))

    def rename_current_note_dialog(self):
        """
        Диалог переименования текущей заметки.
        Переименовывает файл и обновляет все [[wikilinks]] по vault.
        """
        if self.vault_dir is None or self.current_path is None:
            QMessageBox.information(self, "Переименование", "Сначала откройте заметку.")
            return

        old_stem = self.current_path.stem
        dlg = build_rename_dialog(
            self,
            old_stem=old_stem,
            on_rename=lambda old, new: self.rename_note(old_title=old, new_title=new),
        )
        dlg.exec()

    def rename_note(self, old_title: str, new_title: str) -> bool:
        """
        Rename note file and update all wikilinks across the vault.
        Returns True if renamed, False otherwise.
        """
        if self.vault_dir is None:
            return False

        old_stem = safe_filename(old_title)
        new_stem = safe_filename(new_title)

        if not old_stem or not new_stem:
            QMessageBox.warning(self, "Переименование", "Некорректное имя.")
            return False
        if old_stem == new_stem:
            return False

        old_path = self.vault_dir / f"{old_stem}.md"
        new_path = self.vault_dir / f"{new_stem}.md"

        if not old_path.exists():
            QMessageBox.critical(self, "Переименование", f"Файл не найден:\n{old_path}")
            return False
        if new_path.exists():
            QMessageBox.warning(
                self,
                "Переименование",
                f"Заметка с таким именем уже существует:\n{new_path.name}",
            )
            return False

        # Make sure current edits are saved (robust)
        self._stop_timers_and_flush(reason="rename_note")

        log.info("Rename note: %s -> %s", old_stem, new_stem)

        # 1) Rename file on disk
        try:
            old_path.replace(new_path)  # atomic-ish rename on same filesystem
        except Exception as e:
            log.exception("Rename failed: %s -> %s", old_path, new_path)
            QMessageBox.critical(self, "Переименование", f"Не удалось переименовать файл:\n{e}")
            return False

        # 2) Update navigation history (back/forward/current) for renamed title
        self._nav.rename_title(old_stem, new_stem)

        # 3) Обновление ссылок по vault — ТЯЖЁЛОЕ, уводим в фон + прогресс
        # (UI обновим в колбэке по завершению)
        self._rename.start(old_stem=old_stem, new_stem=new_stem, new_path=new_path)
        return True

    def save_now(
        self,
        force: bool = False,
        *,
        check_token: bool = False,
        show_errors: bool = True,
    ) -> bool:
        """
        Unified save:
          - force=False: save only if _dirty==True (fast path), and token optionally matches
          - force=True : save if editor text != _last_saved_text (robust manual save)

        check_token=True is used by autosave to avoid writing into a different note after switching.
        Returns True if something was saved successfully, False otherwise.
        """
        if self.current_path is None:
            return False

        if check_token and getattr(self, "_pending_save_token", None) != self._note_token:
            log.debug("Save skipped: note switched before timer fired")
            if hasattr(log, "log_autosave_skip"):
                log.log_autosave_skip()
            return False

        text = self.editor.toPlainText()

        # Decide whether we need to write
        if force:
            if text == self._last_saved_text:
                return False
        else:
            if not self._dirty:
                return False

        log.info("Сохранение заметки: %s (force=%s)", self.current_path, force)

        try:
            atomic_write_text(self.current_path, text, encoding="utf-8")

            # --- update link index incrementally (fast) ---
            links_changed = self._link_index.update_note(self.current_path.stem, text)

            self._dirty = False
            self._last_saved_text = text
            self.refresh_list()
            if links_changed:
                self.request_build_link_graph()  # debounced by default
            self.refresh_backlinks()
            return True

        except Exception as e:
            log.exception("Сохранить не удалось: %s", self.current_path)

            # Best-effort: write recovery copy
            rec_path = None
            try:
                rec_path = write_recovery_copy(self.current_path, text)
                log.critical("Recovery copy written: %s", rec_path)
            except Exception:
                log.exception("Failed to write recovery copy")

            if show_errors:
                msg = QMessageBox(self)
                msg.setIcon(QMessageBox.Critical)
                msg.setWindowTitle("Ошибка сохранения")
                msg.setText(f"Не удалось сохранить заметку:\n{self.current_path}\n\n{e}")
                if rec_path:
                    msg.setInformativeText(
                        "Создана recovery-копия (на случай потери данных):\n"
                        f"{rec_path}"
                    )
                msg.exec()
            return False

    def _save_current_if_needed(self):
        # Autosave: only when dirty AND token matches (avoid wrong-file writes)
        self.save_now(force=False, check_token=True, show_errors=True)

    def _flush_current_note_before_switch(self) -> None:
        """
        Гарантированно сохраняет текущую заметку перед переключением, если текст реально изменился.
        Это защищает от редких гонок/сценариев, когда _dirty не успел выставиться, а таймер уже остановили.
        """
        if self.current_path is None:
            return
        # Flush should be robust: save if text differs (not relying on _dirty).
        # We don't use token checks here because we're explicitly flushing current editor state
        # before switching context.
        saved = self.save_now(force=True, check_token=False, show_errors=True)
        if saved:
            log.info("Flush-save completed: %s", self.current_path)

    def refresh_backlinks(self):
        self.backlinks.clear()
        if self.vault_dir is None or self.current_path is None:
            return

        target = self.current_path.stem
        refs = self._link_index.backlinks_for(target)
        log.debug("Обратные ссылки обновлены: цель=%s считать=%d", target, len(refs))
        for r in refs:
            self.backlinks.addItem(r)

    def create_note_dialog(self):
        # простой способ: используем строку поиска как ввод имени
        title = self.search.text().strip()
        if not title:
            QMessageBox.information(self, "Новая заметка", "Введите название в поле поиска и нажмите 'Новая заметка…'")
            return
        self.open_or_create_by_title(title)

    def request_build_link_graph(self, immediate: bool = False):
        """Backwards-compatible API: теперь прокидываем в GraphController."""
        if self.vault_dir is None:
            return
        self._graph_ctrl.request(immediate=immediate)

    def _graph_context_snapshot(self) -> dict | None:
        """Снапшот входных данных для графа (controller вызывает это в UI потоке)."""
        if self.vault_dir is None:
            return None
        vault_dir = self.vault_dir
        center = self.current_path.stem if self.current_path else None
        outgoing_snapshot: dict[str, list[str]] = {
            src: sorted(dsts) for src, dsts in self._link_index.outgoing.items()
        }
        existing_titles = {p.stem for p in vault_dir.glob("*.md")}
        return {
            "vault_dir": vault_dir,
            "mode": self.graph_mode,
            "depth": int(self.graph_depth),
            "center": center,
            "outgoing_snapshot": outgoing_snapshot,
            "existing_titles": existing_titles,
            "max_nodes": int(self.max_graph_nodes),
            "max_steps": int(self.max_graph_steps),
        }

    def _apply_graph_payload(self, payload: dict) -> None:
        """UI-применение результата графа (остаётся в NotesApp)."""
        nodes = payload["nodes"]
        edges = payload["edges"]
        stats = payload.get("stats", {})
        try:
            self.graph._layout_steps = int(payload.get("layout_steps") or stats.get("layout_steps") or 250)
        except Exception:
            self.graph._layout_steps = 250

        self.graph.build(nodes, edges)
        if self.current_path:
            cur = self.current_path.stem
            self.graph.highlight(cur)
            self.graph.center_on(cur)

        log.debug(
            "Граф построен (bg): режим=%s глубина=%s узлы=%d края=%d time_ms=%.1f",
            stats.get("mode"), stats.get("depth"),
            stats.get("nodes_all", len(nodes)),
            stats.get("edges_all", len(edges)),
            stats.get("time_ms", -1.0),
        )

    def _on_graph_error(self, err: str) -> None:
        log.warning("Graph build failed (bg): %s", err)

    # Backwards-compatible alias (optional): keep old name used elsewhere
    def build_link_graph(self):
        self.request_build_link_graph(immediate=True)
