from __future__ import annotations

import uuid
import time

from pathlib import Path
from urllib.parse import unquote

from PySide6.QtCore import Qt, QTimer, Slot, Signal, QThreadPool
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QTextEdit, QLineEdit, QFileDialog, QMessageBox,
    QSplitter, QDialog,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage

from obsidian_project.settings import APP_NAME
from obsidian_project.logging_setup import log
from obsidian_project.core.filenames import safe_filename
from obsidian_project.core.links import LinkIndex
from obsidian_project.vault.repo import VaultRepository
from obsidian_project.services.markdown_renderer import MarkdownRenderer
from obsidian_project.graph.view import GraphView
from obsidian_project.graph.worker import GraphBuildWorker

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

class QuickSwitcherDialog(QDialog):
    def __init__(self, parent, get_titles, on_open):
        super().__init__(parent)
        self.setWindowTitle("Quick Switcher")
        self.setModal(True)
        self.resize(520, 420)

        self.get_titles = get_titles   # функция -> list[str]
        self.on_open = on_open         # функция(title)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Введите название… (Enter — открыть/создать)")
        self.listw = QListWidget()

        layout = QVBoxLayout(self)
        layout.addWidget(self.input)
        layout.addWidget(self.listw)

        self._all = []
        self._reload()

        self.input.textChanged.connect(self._filter)
        self.input.returnPressed.connect(self._open_current)
        self.listw.itemActivated.connect(lambda it: self._open_title(it.text()))

        # UX: сразу фокус в поле ввода
        self.input.setFocus()

    def _reload(self):
        self._all = sorted(self.get_titles(), key=str.lower)
        self._filter(self.input.text())

    def _filter(self, text: str):
        q = (text or "").strip().lower()
        self.listw.clear()

        if not q:
            # когда пусто — показываем первые N (как "recent" упрощенно)
            for t in self._all[:40]:
                self.listw.addItem(t)
            if self.listw.count():
                self.listw.setCurrentRow(0)
            return

        # простое fuzzy-ish: сначала contains, потом startswith, потом остальные
        contains = [t for t in self._all if q in t.lower()]
        starts = [t for t in contains if t.lower().startswith(q)]
        rest = [t for t in contains if t not in starts]
        ranked = starts + rest

        for t in ranked[:80]:
            self.listw.addItem(t)

        if self.listw.count():
            self.listw.setCurrentRow(0)

    def _open_current(self):
        text = self.input.text().strip()
        if not text:
            return

        cur = self.listw.currentItem()
        if cur:
            self._open_title(cur.text())
            return

        # если нет совпадений — создаём по введенному
        self._open_title(text)

    def _open_title(self, title: str):
        self.on_open(title)
        self.accept()

class NotesApp(QMainWindow):
    def __init__(self):
        super().__init__()
        log.info("Приложение инициализировано")
        self.setWindowTitle("obsidian-project (Python)")

        self.repo: VaultRepository | None = None
        self.current_path: Path | None = None

        # Token that changes every time we open/switch note.
        # Used to avoid autosave race writing to the wrong file.
        self._note_token: str = uuid.uuid4().hex

        # --- LINK INDEX ---
        self._link_index = LinkIndex()

        # ---- NAV HISTORY ----
        self._nav_back: list[str] = []
        self._nav_forward: list[str] = []
        self._nav_suppress = False  # чтобы back/forward не писали сами себя в историю

        self._dirty = False
        self._pending_save_token: str = self._note_token
        # Последняя сохранённая/загруженная версия текста текущей заметки.
        # Нужна, чтобы не терять изменения при переключении (даже если _dirty "не успел").
        self._last_saved_text: str = ""
        self.graph_mode = "global"   # "global" | "local"
        self.graph_depth = 1         # 1 или 2

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
        self.renderer = MarkdownRenderer(logger_name=APP_NAME)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.listw)
        left_layout.addWidget(self.backlinks)
        self.backlinks.itemClicked.connect(lambda it: self.open_or_create_by_title(it.text()))

        right = QSplitter(Qt.Vertical)
        right.addWidget(self.editor)
        right.addWidget(self.preview)
        right.addWidget(self.graph)
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 2)
        right.setStretchFactor(2, 2)

        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)

        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addWidget(self.splitter)
        self.setCentralWidget(root)

        # Autosave debounce
        self.save_timer = QTimer(self)
        self.save_timer.setInterval(600)  # мс
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self._save_current_if_needed)

        # Preview debounce (чтобы не рендерить markdown на каждый символ)
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(200)  # мс
        self.preview_timer.setSingleShot(True)
        self.preview_timer.timeout.connect(self._render_preview_from_editor)

        # ---- GRAPH BUILD (background) ----
        self._graph_pool = QThreadPool.globalInstance()
        self._graph_req_id = 0  # monotonically increasing; used to drop stale results

        # Signals
        self.search.textChanged.connect(self.refresh_list)
        self.listw.itemSelectionChanged.connect(self._on_select_note)
        self.editor.textChanged.connect(self._on_text_changed)
        self.preview.linkClicked.connect(self.open_or_create_by_title)

        # Menu
        self._build_menu()

        # Start: ask vault
        self.choose_vault()

    def closeEvent(self, event):  # type: ignore[override]
        """
        Ensure last edits are persisted on window close.
        Protects against data loss when autosave timer hasn't fired yet.
        """
        try:
            if self.save_timer.isActive():
                self.save_timer.stop()
            if self.preview_timer.isActive():
                self.preview_timer.stop()
            self._flush_current_note_before_switch()
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
        act_save.triggered.connect(self._save_current_if_needed)

        filem.addAction(act_open_vault)
        filem.addAction(act_new)
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

        act_dark = QAction("Тема: Dark", self, checkable=True)
        act_light = QAction("Тема: Light", self, checkable=True)
        act_dark.setChecked(True)

        def set_dark():
            act_dark.setChecked(True); act_light.setChecked(False)
            self.graph.apply_theme("dark")
            # перестроим граф, чтобы ноды пересоздались с новой темой
            self.request_build_link_graph()
            if self.current_path:
                self.graph.highlight(self.current_path.stem)

        def set_light():
            act_light.setChecked(True); act_dark.setChecked(False)
            self.graph.apply_theme("light")
            self.request_build_link_graph()
            if self.current_path:
                self.graph.highlight(self.current_path.stem)

        act_dark.triggered.connect(set_dark)
        act_light.triggered.connect(set_light)

        viewm.addAction(act_dark)
        viewm.addAction(act_light)

        graphm = menubar.addMenu("Граф")

        act_global = QAction("Global", self, checkable=True)
        act_local1 = QAction("Local (1 hop)", self, checkable=True)
        act_local2 = QAction("Local (2 hops)", self, checkable=True)
        act_global.setChecked(True)

        def _set_mode(mode: str, depth: int = 1):
            self.graph_mode = mode
            self.graph_depth = depth
            act_global.setChecked(mode == "global")
            act_local1.setChecked(mode == "local" and depth == 1)
            act_local2.setChecked(mode == "local" and depth == 2)

            self.request_build_link_graph()
            if self.current_path:
                self.graph.highlight(self.current_path.stem)

        act_global.triggered.connect(lambda: _set_mode("global", 1))
        act_local1.triggered.connect(lambda: _set_mode("local", 1))
        act_local2.triggered.connect(lambda: _set_mode("local", 2))

        graphm.addAction(act_global)
        graphm.addAction(act_local1)
        graphm.addAction(act_local2)

    def open_quick_switcher(self):
        if self.repo is None:
            return

        def get_titles():
            return self.repo.list_titles()

        dlg = QuickSwitcherDialog(self, get_titles=get_titles, on_open=self.open_or_create_by_title)
        dlg.exec()

    def _open_note_no_history(self, title: str):
        """Открывает/создаёт заметку и обновляет UI, но НЕ трогает историю."""
        if self.repo is None:
            return

        title = safe_filename(title)
        path = self.repo.note_path(title)
        log.info("Открытая заметка (без истории): заголовок=%s путь=%s", title, path)

        # --- FIX: остановим отложенные сохранение/превью от предыдущей заметки ---
        # Иначе таймер мог "стрельнуть" после смены current_path и сохранить/отрендерить не то.
        if self.save_timer.isActive():
            self.save_timer.stop()
        if self.preview_timer.isActive():
            self.preview_timer.stop()

        # Надёжно сохраняем предыдущую заметку перед переключением.
        # Не полагаемся только на _dirty: сравниваем текст фактически.
        self._flush_current_note_before_switch()

        created = self.repo.ensure_note(title, initial_text=f"# {title}\n\n")
        if created:
            log.info("Примечание не существует, создаём: %s", path)

        self.current_path = path
        self._note_token = uuid.uuid4().hex
        text = self.repo.read(title)

        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)

        self._dirty = False
        self._last_saved_text = text
        self._render_preview(text)
        self._select_in_list(title)

        self.request_build_link_graph()
        self.graph.highlight(title)
        self.graph.center_on(title)
        self.refresh_backlinks()

    def choose_vault(self):
        log.info("Открылось диалоговое окно «Выбрать хранилище».")
        path = QFileDialog.getExistingDirectory(self, "Выберите папку для заметок")

        if not path:
            # если пользователь отменил и vault ещё не выбран — создадим временную папку рядом
            if self.repo is None:
                tmp = Path.home() / f".{APP_NAME}" / "vault"
                tmp.mkdir(parents=True, exist_ok=True)
                self.repo = VaultRepository(tmp)
                self.repo.ensure()
                log.warning("Хранилище не выбрано. Используется резервное хранилище=%s", tmp)
                # индекс строим сразу (пусть даже пустой)
                self._rebuild_link_index()
                self.refresh_list()
            else:
                log.info("Выбор хранилища отменён. Сохраняется хранилище=%s", self.repo)
            return

        repo = VaultRepository(Path(path))
        repo.ensure()
        self.repo = repo
        log.info("Vault selected: %s", self.repo.vault_dir)
        self.current_path = None
        self.editor.blockSignals(True)
        self.editor.clear()
        self.editor.blockSignals(False)
        self._dirty = False
        self._last_saved_text = ""

        self._rebuild_link_index()
        self.refresh_list()
        self.request_build_link_graph()

    def _rebuild_link_index(self) -> None:
        if self.repo is None:
            self._link_index.clear()
            return
        t0 = time.perf_counter()
        self._link_index.rebuild_from_vault(self.repo.vault_dir)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        log.info(
            "Link index rebuilt: notes=%d incoming_keys=%d outgoing_keys=%d time_ms=%.1f",
            len(list(self.repo.vault_dir.glob("*.md"))),
            len(self._link_index.incoming),
            len(self._link_index.outgoing),
            dt_ms,
        )

    def refresh_list(self):
        if self.repo is None:
            return
        q = self.search.text().strip().lower()
        titles = self.repo.list_titles()

        self.listw.blockSignals(True)
        self.listw.clear()
        for t in titles:
            if not q or q in t.lower():
                self.listw.addItem(t)
        self.listw.blockSignals(False)

    def _on_select_note(self):
        items = self.listw.selectedItems()
        if not items:
            return
        title = items[0].text()
        self.open_or_create_by_title(title)

    def open_or_create_by_title(self, title: str):
        if self.repo is None:
            return

        title = safe_filename(title)
        log.debug("open_or_create_by_title: заголовок=%s подавлять=%s", title, self._nav_suppress)

        # если это обычная навигация (не back/forward) — пишем историю
        if not self._nav_suppress:
            current = self.current_path.stem if self.current_path else None
            if current and current != title:
                self._nav_back.append(current)
                self._nav_forward.clear()

        self._open_note_no_history(title)

    def nav_back(self):
        if not self._nav_back:
            return
        
        log.debug("Навигация назад: стек=%s", self._nav_back)

        current = self.current_path.stem if self.current_path else None
        if current:
            self._nav_forward.append(current)

        title = self._nav_back.pop()

        self._nav_suppress = True
        try:
            self._open_note_no_history(title)
        finally:
            self._nav_suppress = False

    def nav_forward(self):
        if not self._nav_forward:
            return
        
        log.debug("Навигация вперёд: стек=%s", self._nav_forward)

        current = self.current_path.stem if self.current_path else None
        if current:
            self._nav_back.append(current)

        title = self._nav_forward.pop()

        self._nav_suppress = True
        try:
            self._open_note_no_history(title)
        finally:
            self._nav_suppress = False


    def _select_in_list(self, title: str):
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

    def _on_text_changed(self):
        self._dirty = True
        # remember which note the pending autosave belongs to
        self._pending_save_token = self._note_token
        # Не рендерим превью на каждый символ — дебаунсим.
        self.preview_timer.start()
        self.save_timer.start()

    def _render_preview_from_editor(self):
        """Рендер превью из текущего текста редактора (используется таймером)."""
        self._render_preview(self.editor.toPlainText())

    def _render_preview(self, text: str):
        self.preview.setHtml(self.renderer.render_page(text))

    def _save_current_if_needed(self):
        if not self._dirty or self.current_path is None:
            return

        # If user switched notes after typing, do not write editor content
        # into a different file.
        if getattr(self, "_pending_save_token", None) != self._note_token:
            log.debug("Autosave skipped: note switched before timer fired")
            return
        
        log.info("Сохранение заметки: %s", self.current_path)

        try:
            text = self.editor.toPlainText()
            if self.repo is not None:
                self.repo.write_atomic(self.current_path.stem, text)
            else:
                self.current_path.write_text(text, encoding="utf-8")

            # --- update link index incrementally (fast) ---
            self._link_index.update_note(self.current_path.stem, text)

            self._dirty = False
            self._last_saved_text = text
            self.refresh_list()
            self.request_build_link_graph()
            self.refresh_backlinks()

        except Exception as e:
            log.exception("Сохранить не удалось: %s", self.current_path)
            QMessageBox.critical(self, "Ошибка сохранения", str(e))

    def _flush_current_note_before_switch(self) -> None:
        """
        Гарантированно сохраняет текущую заметку перед переключением, если текст реально изменился.
        Это защищает от редких гонок/сценариев, когда _dirty не успел выставиться, а таймер уже остановили.
        """
        if self.current_path is None:
            return
        try:
            current_text = self.editor.toPlainText()
            if current_text == self._last_saved_text:
                return  # ничего не изменилось
            log.info("Flush-save before switch: %s", self.current_path)
            if self.repo is not None:
                self.repo.write_atomic(self.current_path.stem, current_text)
            else:
                self.current_path.write_text(current_text, encoding="utf-8")
            self._link_index.update_note(self.current_path.stem, current_text)
            self._dirty = False
            self._last_saved_text = current_text
        except Exception as e:
            log.exception("Flush-save failed: %s", self.current_path)
            QMessageBox.critical(self, "Ошибка сохранения", str(e))

    def refresh_backlinks(self):
        self.backlinks.clear()
        if self.repo is None or self.current_path is None:
            return

        target = self.current_path.stem
        refs = self._link_index.backlinks_for(target)
        log.debug("Обратные ссылки обновлены: цель=%s считать=%d", target, len(refs))
        for r in sorted(refs, key=str.lower):
            self.backlinks.addItem(r)

    def create_note_dialog(self):
        # простой способ: используем строку поиска как ввод имени
        title = self.search.text().strip()
        if not title:
            QMessageBox.information(self, "Новая заметка", "Введите название в поле поиска и нажмите 'Новая заметка…'")
            return
        self.open_or_create_by_title(title)

    def request_build_link_graph(self):
        """Build graph off the UI thread; apply result when ready (drops stale results)."""
        if self.repo is None:
            return

        self._graph_req_id += 1
        req_id = self._graph_req_id

        payload = {
            "outgoing_snapshot": {src: sorted(dsts) for src, dsts in self._link_index.outgoing.items()},
            "existing_titles": self.repo.existing_titles(),
            "mode": self.graph_mode,
            "depth": int(self.graph_depth),
            "center": (self.current_path.stem if self.current_path else None),
        }

        worker = GraphBuildWorker(req_id=req_id, payload=payload)
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
        self.request_build_link_graph()