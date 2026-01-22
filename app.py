import uuid
import logging
import threading
from pathlib import Path
import time
from urllib.parse import unquote

from PySide6.QtCore import Qt, QTimer, Signal, QThreadPool, Slot, QSettings
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QTextEdit, QLineEdit, QFileDialog, QMessageBox, QSplitter,
    QDialog, QProgressDialog
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage

import markdown as md
from filenames import safe_filename
from wikilinks import wikilinks_to_html
from links import LinkIndex
from graph_view import GraphView
from logging_setup import APP_NAME, LOG_PATH
from rename_worker import _RenameRewriteWorker
from graph_worker import _GraphBuildWorker
from filesystem import atomic_write_text, write_recovery_copy
from quick_switcher import QuickSwitcherDialog
from html_sanitizer import sanitize_rendered_html
from navigation import NavigationController


log = logging.getLogger(APP_NAME)

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
            title = unquote(raw).strip()
            self._view.linkClicked.emit(title)
            return False  # блокируем реальную навигацию
        return super().acceptNavigationRequest(url, nav_type, isMainFrame)


class LinkableWebView(QWebEngineView):
    linkClicked = Signal(str)

    def __init__(self):
        super().__init__()
        self.setPage(_NoteInterceptPage(self))

class NotesApp(QMainWindow):
    def __init__(self):
        super().__init__()
        log.info("Приложение инициализировано")
        self.setWindowTitle("obsidian-project (Python)")

        # --- SETTINGS ---
        class SettingsKeys:
            UI_THEME = "ui/theme"
            UI_GEOMETRY = "ui/geometry"
            UI_STATE = "ui/windowState"
            UI_SPLITTER = "ui/splitter_sizes"
            UI_RIGHT_SPLITTER = "ui/right_splitter_sizes"
            VAULT_DIR = "vault/dir"
            LAST_NOTE = "nav/last_note"
            GRAPH_MODE = "graph/mode"
            GRAPH_DEPTH = "graph/depth"
        # Храним (и восстанавливаем) UI-состояние и пользовательские опции.
        # QSettings сам выберет корректное место под конкретную ОС.
        self._settings = QSettings(APP_NAME, APP_NAME)
        self._settings_save_timer = QTimer(self)
        self._settings_save_timer.setSingleShot(True)
        self._settings_save_timer.setInterval(400)
        self._settings_save_timer.timeout.connect(self._save_ui_state)

        # persisted options
        self._theme = str(self._settings.value(SettingsKeys.UI_THEME, "dark"))
        self.graph_mode = str(self._settings.value(SettingsKeys.GRAPH_MODE, "global"))
        try:
            self.graph_depth = int(self._settings.value("graph/depth", 1))
        except Exception:
            self.graph_depth = 1

        # --- GRAPH LIMITS / PERF (persisted) ---
        try:
            self.max_graph_nodes = int(self._settings.value("graph/max_nodes", 400))
        except Exception:
            self.max_graph_nodes = 400
        try:
            self.max_graph_steps = int(self._settings.value("graph/max_steps", 250))
        except Exception:
            self.max_graph_steps = 250

        # startup restore targets (vault + last note)
        self._startup_vault_dir = str(self._settings.value(SettingsKeys.VAULT_DIR, "")) or ""
        self._startup_last_note = str(self._settings.value(SettingsKeys.LAST_NOTE, "")) or ""

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
        self._restore_ui_state()
        self.splitter.splitterMoved.connect(lambda *_: self._schedule_ui_state_save())
        self.right_splitter.splitterMoved.connect(lambda *_: self._schedule_ui_state_save())

        # Autosave debounce
        self.save_timer = QTimer(self)
        self.save_timer.setInterval(600)  # мс
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self._save_current_if_needed)

        # Preview debounce (чтобы не рендерить markdown на каждый символ)
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(350)  # мс (дебаунс превью; дальше можем адаптировать)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.timeout.connect(self._render_preview_from_editor)

        self._last_preview_source_text: str | None = None

        # ---- GRAPH BUILD (background) ----
        self._graph_pool = QThreadPool.globalInstance()
        self._graph_req_id = 0  # monotonically increasing; used to drop stale results
        # Debounce graph rebuild requests (avoid rebuilding on every autosave)
        self._graph_debounce_timer = QTimer(self)
        self._graph_debounce_timer.setInterval(1200)  # ms (tune as needed)
        self._graph_debounce_timer.setSingleShot(True)
        self._graph_debounce_timer.timeout.connect(self._request_build_link_graph_now)

        # ---- RENAME REWRITE (background) ----
        self._rename_pool = QThreadPool.globalInstance()
        self._rename_req_id = 0
        self._rename_cancel_event: threading.Event | None = None
        self._rename_progress: QProgressDialog | None = None

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
            pass

    def _stop_debounced_timers(self) -> None:
        """Stop delayed autosave/preview/graph debounce timers (safe before switching context)."""
        try:
            if self.save_timer.isActive():
                self.save_timer.stop()
            if self.preview_timer.isActive():
                self.preview_timer.stop()
            if self._graph_debounce_timer.isActive():
                self._graph_debounce_timer.stop()
        except Exception:
            pass

    # ----------------- QSettings helpers -----------------
    def _restore_ui_state(self) -> None:
        try:
            geo = self._settings.value("ui/geometry")
            if geo:
                self.restoreGeometry(geo)
            else:
                # default on first run
                self.resize(1100, 700)

            st = self._settings.value("ui/windowState")
            if st:
                self.restoreState(st)

            s1 = self._settings.value("ui/splitter_sizes")
            if s1:
                try:
                    self.splitter.setSizes([int(x) for x in s1])
                except Exception:
                    pass

            s2 = self._settings.value("ui/right_splitter_sizes")
            if s2 and hasattr(self, "right_splitter"):
                try:
                    self.right_splitter.setSizes([int(x) for x in s2])
                except Exception:
                    pass
        except Exception:
            log.exception("Failed to restore UI state from QSettings")

    def _schedule_ui_state_save(self) -> None:
        try:
            self._settings_save_timer.start()
        except Exception:
            pass

    def _save_ui_state(self) -> None:
        try:
            self._settings.setValue("ui/geometry", self.saveGeometry())
            self._settings.setValue("ui/windowState", self.saveState())
            self._settings.setValue("ui/splitter_sizes", self.splitter.sizes())
            if hasattr(self, "right_splitter"):
                self._settings.setValue("ui/right_splitter_sizes", self.right_splitter.sizes())
        except Exception:
            log.exception("Failed to save UI state to QSettings")

    def _apply_theme(self, name: str, *, save: bool = True) -> None:
        name = (name or "").strip().lower()
        if name not in ("dark", "light"):
            name = "dark"

        self._theme = name

        # keep menu state if actions exist
        if hasattr(self, "_act_dark"):
            self._act_dark.blockSignals(True)
            self._act_light.blockSignals(True)
            self._act_dark.setChecked(name == "dark")
            self._act_light.setChecked(name == "light")
            self._act_dark.blockSignals(False)
            self._act_light.blockSignals(False)

        try:
            self.graph.apply_theme(name)
            # перестроим граф, чтобы ноды пересоздались с новой темой
            self.request_build_link_graph(immediate=True)
            if self.current_path:
                self.graph.highlight(self.current_path.stem)
        except Exception:
            log.exception("Failed to apply theme")

        if save:
            self._settings.setValue("ui/theme", name)

    def _apply_graph_mode(self, mode: str, depth: int = 1, *, save: bool = True) -> None:
        mode = (mode or "").strip().lower()
        if mode not in ("global", "local"):
            mode = "global"
        try:
            depth = int(depth)
        except Exception:
            depth = 1
        depth = 2 if depth >= 2 else 1

        self.graph_mode = mode
        self.graph_depth = depth

        if hasattr(self, "_act_graph_global"):
            self._act_graph_global.setChecked(mode == "global")
            self._act_graph_local1.setChecked(mode == "local" and depth == 1)
            self._act_graph_local2.setChecked(mode == "local" and depth == 2)

        try:
            self.request_build_link_graph(immediate=True)
            if self.current_path:
                self.graph.highlight(self.current_path.stem)
        except Exception:
            log.exception("Failed to apply graph mode")

        if save:
            self._settings.setValue("graph/mode", mode)
            self._settings.setValue("graph/depth", depth)

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
                p = self.vault_dir / f"{safe_filename(self._startup_last_note)}.md"
                if p.exists():
                    self._open_note_no_history(p.stem)
        except Exception:
            log.exception("Failed to open last note on startup")

    def _open_vault_at(self, vault_dir: Path, *, save: bool = True) -> None:
        self.vault_dir = Path(vault_dir)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        log.info("Vault selected: %s", self.vault_dir)

        if save:
            self._settings.setValue("vault/dir", str(self.vault_dir))

        self.current_path = None
        self.editor.blockSignals(True)
        self.editor.clear()
        self.editor.blockSignals(False)
        self._dirty = False
        self._last_saved_text = ""
        # self._nav_back.clear()
        # self._nav_forward.clear()
        self._nav.clear()

        self._rebuild_link_index()
        self.refresh_list()
        self.request_build_link_graph(immediate=True)

    def closeEvent(self, event):  # type: ignore[override]
        """
        Ensure last edits are persisted on window close.
        Protects against data loss when autosave timer hasn't fired yet.
        """
        try:
            self._stop_debounced_timers()
            self._flush_current_note_before_switch()
            self._save_ui_state()
        except Exception:
            log.exception("Failed to flush note on close")
        super().closeEvent(event)

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

        title = safe_filename(title)
        path = self.vault_dir / f"{title}.md"
        log.info("Открытая заметка (без истории): заголовок=%s путь=%s", title, path)

        # --- FIX: остановим отложенные таймеры от предыдущей заметки ---
        # Иначе таймер мог "стрельнуть" после смены current_path и сохранить/отрендерить не то.
        self._stop_debounced_timers()

        # Надёжно сохраняем предыдущую заметку перед переключением.
        # Не полагаемся только на _dirty: сравниваем текст фактически.
        self._flush_current_note_before_switch()

        if not path.exists():
            log.info("Примечание не существует, создаём: %s", path)
            atomic_write_text(path, f"# {title}\n\n", encoding="utf-8")

        self.current_path = path
        self._note_token = uuid.uuid4().hex

        # persist last opened note
        try:
            self._settings.setValue("nav/last_note", title)
        except Exception:
            pass

        text = path.read_text(encoding="utf-8")

        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)

        self._dirty = False
        self._last_saved_text = text
        self._render_preview(text)
        self._select_in_list(title)

        self.request_build_link_graph(immediate=True)
        self.graph.highlight(title)
        self.graph.center_on(title)
        self.refresh_backlinks()

    def choose_vault(self):
        log.info("Открылось диалоговое окно «Выбрать хранилище».")

        # IMPORTANT: don't lose unsaved edits when switching vaults.
        # Stop timers first (autosave/preview/graph) then flush current note safely.
        try:
            self._stop_debounced_timers()
            self._flush_current_note_before_switch()
        except Exception:
            log.exception("Failed to flush note before choosing vault")

        path = QFileDialog.getExistingDirectory(self, "Выберите папку для заметок")

        if not path:
            # Пользователь отменил выбор папки
            if self.vault_dir is None:
                # Дадим явный выбор, чтобы не создавать папки "молча"
                msg = QMessageBox(self)
                msg.setIcon(QMessageBox.Warning)
                msg.setWindowTitle("Хранилище не выбрано")
                msg.setText("Вы не выбрали папку для заметок.")
                msg.setInformativeText("Как поступить?")
                btn_retry = msg.addButton("Выбрать папку ещё раз", QMessageBox.AcceptRole)
                btn_fallback = msg.addButton("Использовать резервную папку", QMessageBox.DestructiveRole)
                btn_exit = msg.addButton("Выйти", QMessageBox.RejectRole)
                msg.setDefaultButton(btn_retry)
                msg.exec()

                clicked = msg.clickedButton()
                if clicked == btn_retry:
                    return self.choose_vault()

                if clicked == btn_exit:
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

        self.listw.blockSignals(True)
        self.listw.clear()
        for p in notes:
            if not q or q in p.stem.lower():
                self.listw.addItem(p.stem)
        self.listw.blockSignals(False)

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
        self._nav.open(title, reopen_current=current)

    def nav_back(self):
        self._nav.back()

    def nav_forward(self):
        self._nav.forward()


    def _select_in_list(self, title: str):
        # Важно: setCurrentRow триггерит itemSelectionChanged -> _on_select_note -> open_or_create...
        # Поэтому на время выделения блокируем сигналы списка.
        self.listw.blockSignals(True)
        try:
            # выделяем в списке (если есть)
            for i in range(self.listw.count()):
                if self.listw.item(i).text() == title:
                    self.listw.setCurrentRow(i)
                    return
            # если не было — добавим и выделим
            self.refresh_list()
            for i in range(self.listw.count()):
                if self.listw.item(i).text() == title:
                    self.listw.setCurrentRow(i)
                    return
        finally:
            self.listw.blockSignals(False)

    def _on_text_changed(self):
        self._dirty = True
        # remember which note the pending autosave belongs to
        self._pending_save_token = self._note_token
        # Не рендерим превью на каждый символ — дебаунсим (адаптивно под размер заметки).
        txt_len = len(self.editor.toPlainText())
        # 300..800ms: большие заметки требуют более редкого рендера
        interval = 300 + min(500, txt_len // 400)
        self.preview_timer.setInterval(interval)
        self.preview_timer.start()
        self.save_timer.start()

    def _render_preview_from_editor(self):
        """Рендер превью из текущего текста редактора (используется таймером)."""
        self._render_preview(self.editor.toPlainText())

    def _render_preview(self, text: str):
        if getattr(self, "_last_preview_source_text", None) == text:
            return
        self._last_preview_source_text = text
        # wiki links -> html links, потом markdown -> html
        text2 = wikilinks_to_html(text)
        rendered = md.markdown(
            text2,
            extensions=["fenced_code", "tables", "toc"]
        )

        # sanitize HTML (защита от <script>, onerror, javascript: и т.п.)
        rendered = sanitize_rendered_html(rendered)

        # простой стиль
        page = f"""
        <html>
        <head>
            <meta charset="utf-8"/>
            <style>
                body {{ font-family: sans-serif; padding: 16px; line-height: 1.5; }}
                code, pre {{ background: #f5f5f5; }}
                pre {{ padding: 12px; overflow-x: auto; }}
                a {{ text-decoration: none; }}
                a:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>{rendered}</body>
        </html>
        """
        self.preview.setHtml(page)

    def rename_current_note_dialog(self):
        """
        Диалог переименования текущей заметки.
        Переименовывает файл и обновляет все [[wikilinks]] по vault.
        """
        if self.vault_dir is None or self.current_path is None:
            QMessageBox.information(self, "Переименование", "Сначала откройте заметку.")
            return

        old_stem = self.current_path.stem

        dlg = QDialog(self)
        dlg.setWindowTitle("Переименовать заметку")
        dlg.setModal(True)
        dlg.resize(520, 140)

        layout = QVBoxLayout(dlg)

        info = QTextEdit()
        info.setReadOnly(True)
        info.setMaximumHeight(60)
        info.setPlainText(
            "Введите новое имя заметки.\n"
            "Будет переименован файл и обновлены ссылки вида [[...]] во всём хранилище."
        )
        layout.addWidget(info)

        inp = QLineEdit()
        inp.setPlaceholderText("Новое имя…")
        inp.setText(old_stem)
        inp.selectAll()
        layout.addWidget(inp)

        btns = QHBoxLayout()
        b_ok = QAction("OK", dlg)  # placeholder action (we'll use buttons via QMessageBox-like)

        # Use QMessageBox-style buttons for simplicity
        msg = QMessageBox(dlg)
        msg.setWindowTitle("Переименовать")
        msg.setText("Нажмите OK, чтобы применить переименование.")
        # But we want the line edit in dialog, not in QMessageBox.
        # We'll implement simple accept/reject via key events.
        #
        # Instead: use standard dialog buttons via QDialogButtonBox (not imported).
        # To avoid adding imports, we handle Enter/Esc directly.

        def do_accept():
            new_title = inp.text().strip()
            if not new_title:
                QMessageBox.warning(self, "Переименование", "Имя не может быть пустым.")
                return
            ok = self.rename_note(old_title=old_stem, new_title=new_title)
            if ok:
                dlg.accept()

        def do_reject():
            dlg.reject()

        inp.returnPressed.connect(do_accept)
        dlg.finished.connect(lambda _: None)

        # Add simple buttons using QMessageBox instance (less code than QDialogButtonBox import)
        mb = QMessageBox(dlg)
        # We won't show mb; we only reuse its buttons concept? Not possible cleanly.
        # We'll create real QPushButton without import? It's in QtWidgets; but not imported.
        # Easiest: import QPushButton and build minimal buttons.

        dlg_buttons = QHBoxLayout()
        from PySide6.QtWidgets import QPushButton
        btn_cancel = QPushButton("Отмена")
        btn_ok = QPushButton("Переименовать")
        btn_ok.setDefault(True)
        btn_cancel.clicked.connect(do_reject)
        btn_ok.clicked.connect(do_accept)
        dlg_buttons.addStretch(1)
        dlg_buttons.addWidget(btn_cancel)
        dlg_buttons.addWidget(btn_ok)
        layout.addLayout(dlg_buttons)

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
        try:
            self._stop_debounced_timers()
            self._flush_current_note_before_switch()
        except Exception:
            log.exception("Failed to flush before rename")

        log.info("Rename note: %s -> %s", old_stem, new_stem)

        # 1) Rename file on disk
        try:
            old_path.replace(new_path)  # atomic-ish rename on same filesystem
        except Exception as e:
            log.exception("Rename failed: %s -> %s", old_path, new_path)
            QMessageBox.critical(self, "Переименование", f"Не удалось переименовать файл:\n{e}")
            return False

        # 2) Update nav history stacks
        try:
            self._nav_back = [new_stem if t == old_stem else t for t in self._nav_back]
            self._nav_forward = [new_stem if t == old_stem else t for t in self._nav_forward]
        except Exception:
            pass

        # 3) Обновление ссылок по vault — ТЯЖЁЛОЕ, уводим в фон + прогресс
        # (UI обновим в колбэке по завершению)
        self._start_rewrite_links_after_rename(old_stem=old_stem, new_stem=new_stem, new_path=new_path)
        return True

    def _start_rewrite_links_after_rename(self, *, old_stem: str, new_stem: str, new_path: Path) -> None:
        """
        Запускает массовый rewrite wikilinks в фоне.
        На время операции делаем editor read-only и показываем прогресс.
        """
        if self.vault_dir is None:
            return

        # Если уже идёт операция — отменим/закроем прежний прогресс корректно.
        try:
            if self._rename_progress is not None:
                self._rename_progress.reset()
        except Exception:
            pass

        self._rename_req_id += 1
        req_id = self._rename_req_id

        self._rename_cancel_event = threading.Event()

        # Список файлов снапшотом, чтобы worker не трогал UI/state
        files = sorted(self.vault_dir.glob("*.md"), key=lambda p: p.name.lower())

        # Прогресс-диалог
        dlg = QProgressDialog("Обновляю ссылки по хранилищу…", "Отмена", 0, max(1, len(files)), self)
        dlg.setWindowTitle("Переименование: обновление ссылок")
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setMinimumDuration(200)  # показывать не сразу, если всё очень быстро
        dlg.setValue(0)

        def on_cancel():
            if self._rename_cancel_event is not None:
                self._rename_cancel_event.set()
            dlg.setLabelText("Отменяю… (дожидаюсь текущего файла)")

        dlg.canceled.connect(on_cancel)
        self._rename_progress = dlg

        # На время операции — ограничим редактирование (уменьшаем риски гонок/конфликтов).
        self._set_ui_busy(True)

        worker = _RenameRewriteWorker(
            req_id=req_id,
            vault_dir=self.vault_dir,
            files=files,
            old_stem=old_stem,
            new_stem=new_stem,
            cancel_event=self._rename_cancel_event,
        )
        worker.signals.progress.connect(self._on_rename_rewrite_progress)
        worker.signals.finished.connect(lambda rid, res: self._on_rename_rewrite_finished(rid, res, new_path=new_path))
        worker.signals.failed.connect(self._on_rename_rewrite_failed)
        self._rename_pool.start(worker)

    @Slot(int, int, int, str)
    def _on_rename_rewrite_progress(self, req_id: int, done: int, total: int, filename: str) -> None:
        if req_id != self._rename_req_id:
            return
        dlg = self._rename_progress
        if dlg is None:
            return
        try:
            dlg.setMaximum(max(1, total))
            dlg.setValue(min(done, total))
            if filename:
                dlg.setLabelText(f"Обновляю ссылки… {done}/{total}\n{filename}")
        except Exception:
            pass

    def _finish_rename_ui_cleanup(self) -> None:
        """Единая точка завершения: закрыть прогресс и вернуть UI."""
        try:
            if self._rename_progress is not None:
                self._rename_progress.setValue(self._rename_progress.maximum())
                self._rename_progress.close()
        except Exception:
            pass
        self._rename_progress = None
        self._rename_cancel_event = None
        self._set_ui_busy(False)

    def _on_rename_rewrite_finished(self, req_id: int, result: dict, *, new_path: Path) -> None:
        if req_id != self._rename_req_id:
            return
        self._finish_rename_ui_cleanup()

        changed_files = int(result.get("changed_files") or 0)
        total_files = int(result.get("total_files") or 0)
        error_files: list[str] = list(result.get("error_files") or [])
        canceled = bool(result.get("canceled"))

        log.info(
            "Rename rewrite finished: total=%d changed=%d canceled=%s errors=%d",
            total_files, changed_files, canceled, len(error_files),
        )

        # 4) Re-open renamed note in UI (without creating history churn)
        try:
            # Если текущая заметка была переименована — переключим current_path на новый файл.
            if self.current_path and self.current_path.stem == safe_filename(new_path.stem):
                self.current_path = new_path
            elif self.current_path and self.current_path.stem == safe_filename(result.get("old_stem") or ""):
                self.current_path = new_path
        except Exception:
            pass

        if self.current_path and self.current_path == new_path:
            try:
                text = new_path.read_text(encoding="utf-8")
            except Exception:
                text = self.editor.toPlainText()

            self.editor.blockSignals(True)
            self.editor.setPlainText(text)
            self.editor.blockSignals(False)
            self._dirty = False
            self._last_saved_text = text
            self._render_preview(text)

        # 5) Rebuild link index (safe after mass edits) + refresh UI
        try:
            self._rebuild_link_index()
        except Exception:
            log.exception("Failed to rebuild link index after rename rewrite")

        self.refresh_list()
        self._select_in_list(new_path.stem)
        self.request_build_link_graph(immediate=True)
        self.graph.highlight(new_path.stem)
        self.graph.center_on(new_path.stem)
        self.refresh_backlinks()

        # Уведомление пользователю (не спамим, но даём знать про проблемы)
        if canceled:
            QMessageBox.information(
                self,
                "Переименование",
                "Обновление ссылок было отменено.\n"
                "Файл заметки переименован, но ссылки могли обновиться не везде.",
            )
        elif error_files:
            # показываем только небольшую выборку, детали — в логах
            sample = "\n".join(error_files[:12])
            more = "" if len(error_files) <= 12 else f"\n… и ещё {len(error_files) - 12}"
            QMessageBox.warning(
                self,
                "Переименование",
                "Переименование выполнено, но часть файлов не удалось обновить.\n\n"
                f"Проблемные файлы:\n{sample}{more}\n\n"
                f"Детали — в логах: {LOG_PATH}",
            )

    @Slot(int, str)
    def _on_rename_rewrite_failed(self, req_id: int, err: str) -> None:
        if req_id != self._rename_req_id:
            return
        self._finish_rename_ui_cleanup()
        log.warning("Rename rewrite failed (bg): %s", err)
        QMessageBox.warning(
            self,
            "Переименование",
            "Файл был переименован, но при обновлении ссылок произошла ошибка.\n\n"
            f"{err}\n\n"
            f"Детали — в логах: {LOG_PATH}",
        )

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
        """
        Build graph off the UI thread; debounce by default to avoid rebuilding on every autosave.
        Use immediate=True when user action expects instant rebuild (theme/mode/vault/open).
        """
        if self.vault_dir is None:
            return
        if immediate:
            if self._graph_debounce_timer.isActive():
                self._graph_debounce_timer.stop()
            self._request_build_link_graph_now()
        else:
            # restart debounce window
            self._graph_debounce_timer.start()

    def _request_build_link_graph_now(self):
        """Build graph off the UI thread; apply result when ready (drops stale results)."""
        if self.vault_dir is None:
            return

        self._graph_req_id += 1
        req_id = self._graph_req_id

        # snapshot inputs (so background thread doesn't touch mutable UI state)
        vault_dir = self.vault_dir
        mode = self.graph_mode
        depth = int(self.graph_depth)
        center = self.current_path.stem if self.current_path else None

        # snapshot index so worker doesn't touch UI state
        outgoing_snapshot: dict[str, list[str]] = {
            src: sorted(dsts)
            for src, dsts in self._link_index.outgoing.items()
        }
        # real notes on disk
        existing_titles = {p.stem for p in vault_dir.glob("*.md")}

        worker = _GraphBuildWorker(
            req_id=req_id,
            vault_dir=vault_dir,
            mode=mode,
            depth=depth,
            center=center,
            outgoing_snapshot=outgoing_snapshot,
            existing_titles=existing_titles,
            max_nodes=int(self.max_graph_nodes),
            max_steps=int(self.max_graph_steps),
        )
        worker.signals.finished.connect(self._on_graph_built)
        worker.signals.failed.connect(self._on_graph_build_failed)
        self._graph_pool.start(worker)

    @Slot(int, dict)
    def _on_graph_built(self, req_id: int, payload: dict):
        # Drop stale results (user navigated/saved again while worker was running)
        if req_id != self._graph_req_id:
            return

        nodes = payload["nodes"]
        edges = payload["edges"]
        stats = payload.get("stats", {})
        # pass dynamic layout steps to GraphView (UI thread)
        try:
            self.graph._layout_steps = int(payload.get("layout_steps") or stats.get("layout_steps") or 250)
        except Exception:
            self.graph._layout_steps = 250

        self.graph.build(nodes, edges)

        # keep highlight centered on current note if any
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

    @Slot(int, str)
    def _on_graph_build_failed(self, req_id: int, err: str):
        if req_id != self._graph_req_id:
            return
        log.warning("Graph build failed (bg): %s", err)

    # Backwards-compatible alias (optional): keep old name used elsewhere
    def build_link_graph(self):
        self.request_build_link_graph(immediate=True)
