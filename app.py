APP_NAME = "obsidian-project"

import os
import re
import sys
import unicodedata
import uuid
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import math
import random
import time
import html
from urllib.parse import quote, unquote
from dataclasses import dataclass, field

SESSION_ID = uuid.uuid4().hex[:8]

class EnsureSessionFilter(logging.Filter):
    """Гарантирует наличие record.session, чтобы Formatter не падал."""
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "session"):
            record.session = SESSION_ID
        return True

class SessionAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = kwargs.setdefault("extra", {})
        extra.setdefault("session", SESSION_ID)
        return msg, kwargs

from PySide6.QtCore import Qt, QTimer, Signal, QPointF, QElapsedTimer, QObject, QRunnable, QThreadPool, Slot
from PySide6.QtGui import QAction, QPainter, QPen, QBrush, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QTextEdit, QLineEdit, QFileDialog, QMessageBox, QSplitter,
    QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsLineItem,
    QGraphicsSimpleTextItem, QGraphicsItem, QDialog
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEnginePage

import markdown as md

# --- HTML sanitization (for Markdown preview rendered in QWebEngine) ---
try:
    import bleach  # pip install bleach
except Exception:  # pragma: no cover
    bleach = None

# чтобы не спамить warning при отсутствии bleach
_BLEACH_MISSING_WARNED = False

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

def extract_wikilink_targets(markdown_text: str) -> set[str]:
    """
    Парсит [[target]] и [[target|alias]] и возвращает множество канонических target
    (через safe_filename).
    """
    targets: set[str] = set()
    for m in WIKILINK_RE.finditer(markdown_text or ""):
        inner = (m.group(1) or "").strip()
        if not inner:
            continue
        if "|" in inner:
            target_raw = inner.split("|", 1)[0].strip()
        else:
            target_raw = inner
        dst = safe_filename(target_raw)
        if dst:
            targets.add(dst)
    return targets


@dataclass
class LinkIndex:
    """
    Индекс ссылок:
        outgoing[src] = {dst1, dst2, ...}
        incoming[dst] = {src1, src2, ...}

    dst может быть "виртуальным" (заметка ещё не существует как файл).
    """
    outgoing: dict[str, set[str]] = field(default_factory=dict)
    incoming: dict[str, set[str]] = field(default_factory=dict)

    def clear(self) -> None:
        self.outgoing.clear()
        self.incoming.clear()

    def rebuild_from_vault(self, vault_dir: Path) -> None:
        self.clear()
        for p in vault_dir.glob("*.md"):
            src = p.stem
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            self.update_note(src, text)

    def update_note(self, src: str, markdown_text: str) -> None:
        """
        Инкрементально обновляет индекс для одной заметки src.
        """
        src = safe_filename(src)
        if not src:
            return

        new_targets = extract_wikilink_targets(markdown_text)
        # self-links не держим
        new_targets.discard(src)

        old_targets = self.outgoing.get(src, set())
        if old_targets == new_targets:
            return
        # убрать старые обратные связи
        for dst in old_targets - new_targets:
            inc = self.incoming.get(dst)
            if inc:
                inc.discard(src)
                if not inc:
                    self.incoming.pop(dst, None)

        # добавить новые обратные связи
        for dst in new_targets - old_targets:
            self.incoming.setdefault(dst, set()).add(src)

        # обновить outgoing
        if new_targets:
            self.outgoing[src] = set(new_targets)
        else:
            self.outgoing.pop(src, None)

    def backlinks_for(self, target: str) -> list[str]:
        target = safe_filename(target)
        refs = sorted(self.incoming.get(target, set()), key=str.lower)
        return refs

ALLOWED_TAGS = [
    "a", "p", "br", "hr",
    "strong", "em", "code", "pre", "blockquote",
    "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "th", "td",
    # If you want images, uncomment "img" and its attrs below.
    # "img",
]

ALLOWED_ATTRS = {
    "a": ["href", "title"],
    "th": ["align"], "td": ["align"],
    # "img": ["src", "alt", "title"],
}

ALLOWED_PROTOCOLS = ["http", "https", "mailto", "note"]

def sanitize_rendered_html(rendered_html: str) -> str:
    """
    Sanitize HTML output from Markdown before feeding it to QWebEngine.
    Without this, raw HTML inside notes can execute in the embedded browser.
    """
    if bleach is None:
        # SAFE fallback: escape everything (loses formatting but prevents HTML/JS execution).
        global _BLEACH_MISSING_WARNED
        if not _BLEACH_MISSING_WARNED:
            logging.getLogger(APP_NAME).warning(
                "bleach is not installed; preview will be shown as plain text for safety. "
                "Install 'bleach' to enable sanitized HTML rendering."
            )
            _BLEACH_MISSING_WARNED = True
        return html.escape(rendered_html)

    cleaned = bleach.clean(
        rendered_html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    # Also strip out any JS-able URLs that might slip through.
    return cleaned

# Путь логов лучше не в cwd: он нестабилен для GUI приложений.
LOG_DIR = Path.home() / f".{APP_NAME}" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / f"{APP_NAME}.log"

def setup_logging() -> logging.Logger:
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if logger.handlers:
        # чтобы при повторном импорте/запуске не плодить хендлеры
        return logger

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s | sid=%(session)s"
    )
    session_filter = EnsureSessionFilter()

    # файл с ротацией
    fh = RotatingFileHandler(
        LOG_PATH,
        maxBytes=2 * 1024 * 1024,   # 2MB
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    fh.addFilter(session_filter)

    # консоль
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    ch.addFilter(session_filter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    logger.info("Logging initialized. log_file=%s", LOG_PATH)
    return logger

_base_logger = setup_logging()
log = SessionAdapter(_base_logger, {})

def install_global_exception_hooks() -> None:
    # python exceptions (в основном потоке)
    def _excepthook(exc_type, exc, tb):
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        # можно оставить дефолтное поведение
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook

    # Qt warnings/errors (qDebug/qWarning/etc.)
    try:
        from PySide6.QtCore import qInstallMessageHandler

        def _qt_message_handler(mode, context, message):
            # Поднимем полезный контекст: файл/строка/функция.
            try:
                file = getattr(context, "file", None)
                line = getattr(context, "line", None)
                func = getattr(context, "function", None)
                where = f"{file}:{line} {func}" if file or line or func else "unknown"
            except Exception:
                where = "unknown"

            # Примерная мапа уровней (не идеальна, но лучше чем всегда warning)
            # QtMsgType: 0=Debug, 1=Warning, 2=Critical, 3=Fatal, 4=Info (зависит от Qt)
            level = logging.WARNING
            try:
                m = int(mode)
                if m == 0:
                    level = logging.DEBUG
                elif m == 4:
                    level = logging.INFO
                elif m == 2:
                    level = logging.ERROR
                elif m == 3:
                    level = logging.CRITICAL
                else:
                    level = logging.WARNING
            except Exception:
                level = logging.WARNING

            log.log(level, "Qt: %s | where=%s", message, where)

        qInstallMessageHandler(_qt_message_handler)
        log.info("Qt message handler installed")
    except Exception:
        log.exception("Failed to install Qt message handler")

def safe_filename(title: str) -> str:
    """
    Делает безопасное имя файла из заголовка заметки.
    Цели:
      - кроссплатформенность (Windows/macOS/Linux)
      - защита от "странных" символов/управляющих кодов
      - ограничение длины
      - защита от зарезервированных имён Windows (CON, PRN, ...)
    """
    if title is None:
        return "Untitled"

    # Unicode normalize: визуально одинаковые символы -> одно представление
    s = unicodedata.normalize("NFKC", str(title))

    # удаляем управляющие символы (включая \x00..)
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")

    # режем пробелы
    s = s.strip()

    # запретные разделители путей
    s = s.replace("/", "-").replace("\\", "-")

    # символы, запрещённые в Windows именах файлов: <>:"/\|?*
    s = re.sub(r'[<>:"/\\|?*\u0000-\u001f]', "_", s)

    # схлопываем пробелы/таб/переводы строк
    s = re.sub(r"\s+", " ", s)

    # Windows: имя не может заканчиваться пробелом или точкой
    s = s.rstrip(" .")

    # если пусто — даём дефолт
    if not s:
        s = "Untitled"

    # Windows reserved device names (без учёта регистра, и даже с расширением)
    # https://learn.microsoft.com/windows/win32/fileio/naming-a-file
    base = s.split(".")[0].strip().lower()
    reserved = {"con", "prn", "aux", "nul"}
    reserved |= {f"com{i}" for i in range(1, 10)}
    reserved |= {f"lpt{i}" for i in range(1, 10)}
    if base in reserved:
        s = f"_{s}"

    # ограничим длину (безопасно для большинства FS). Расширения у нас фиксированные (.md),
    # но оставим запас на всякий случай.
    max_len = 120
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")

    return s

def wikilinks_to_html(markdown_text: str) -> str:
    """
    Заменяем wikilinks на <a href="note://...">...</a>

    Поддерживаем alias (Obsidian-style):
        - [[target]] -> label=target, href=note://safe_filename(target)
        - [[target|display]] -> label=display, href=note://safe_filename(target)

    Важно:
        - label HTML-экранируем (защита от инъекций)
        - href URL-энкодим (пробелы/юникод/спецсимволы)
        - href всегда строим по каноническому имени файла (safe_filename),
        чтобы переходы/граф/беклинки совпадали с тем, как заметки реально
        сохраняются на диске.
    """
    def repl(m: re.Match) -> str:
        inner = (m.group(1) or "").strip()
        if not inner:
            return ""

        # Alias: [[target|display]]
        # Если '|' нет — display=target.
        if "|" in inner:
            target_raw, display_raw = inner.split("|", 1)
            target_raw = target_raw.strip()
            display_raw = display_raw.strip()
        else:
            target_raw = inner
            display_raw = inner

        # label: показываем display, но безопасно для HTML
        label = html.escape(display_raw, quote=False)

        # href: КАНОН - имя файла строим из target
        target = safe_filename(target_raw)

        # URL encoding, чтобы не ломать атрибут и схему
        href = "note://" + quote(target, safe="")
        return f'<a href="{href}">{label}</a>'

    return WIKILINK_RE.sub(repl, markdown_text)


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

        self.vault_dir: Path | None = None
        self.current_path: Path | None = None

        # --- LINK INDEX ---
        self._link_index = LinkIndex()

        # ---- NAV HISTORY ----
        self._nav_back: list[str] = []
        self._nav_forward: list[str] = []
        self._nav_suppress = False  # чтобы back/forward не писали сами себя в историю

        self._dirty = False
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

        # --- FIX: остановим отложенные сохранение/превью от предыдущей заметки ---
        # Иначе таймер мог "стрельнуть" после смены current_path и сохранить/отрендерить не то.
        if self.save_timer.isActive():
            self.save_timer.stop()
        if self.preview_timer.isActive():
            self.preview_timer.stop()

        # save previous
        self._save_current_if_needed()

        if not path.exists():
            log.info("Примечание не существует, создаём: %s", path)
            path.write_text(f"# {title}\n\n", encoding="utf-8")

        self.current_path = path
        text = path.read_text(encoding="utf-8")

        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)

        self._dirty = False
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
            if self.vault_dir is None:
                tmp = Path.cwd() / "vault"
                tmp.mkdir(exist_ok=True)
                self.vault_dir = tmp
                log.warning("Хранилище не выбрано. Используется резервное хранилище=%s", tmp)
                # индекс строим сразу (пусть даже пустой)
                self._rebuild_link_index()
                self.refresh_list()
            else:
                log.info("Выбор хранилища отменён. Сохраняется хранилище=%s", self.vault_dir)
            return

        self.vault_dir = Path(path)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        log.info("Vault selected: %s", self.vault_dir)
        self.current_path = None
        self.editor.blockSignals(True)
        self.editor.clear()
        self.editor.blockSignals(False)
        self._dirty = False

        self._rebuild_link_index()
        self.refresh_list()
        self.request_build_link_graph()

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
        # Не рендерим превью на каждый символ — дебаунсим.
        self.preview_timer.start()
        self.save_timer.start()

    def _render_preview_from_editor(self):
        """Рендер превью из текущего текста редактора (используется таймером)."""
        self._render_preview(self.editor.toPlainText())

    def _render_preview(self, text: str):
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

    def _save_current_if_needed(self):
        if not self._dirty or self.current_path is None:
            return
        
        log.info("Сохранение заметки: %s", self.current_path)

        try:
            text = self.editor.toPlainText()
            self.current_path.write_text(text, encoding="utf-8")

            # --- update link index incrementally (fast) ---
            self._link_index.update_note(self.current_path.stem, text)

            self._dirty = False
            self.refresh_list()
            self.request_build_link_graph()
            self.refresh_backlinks()

        except Exception as e:
            log.exception("Сохранить не удалось: %s", self.current_path)
            QMessageBox.critical(self, "Ошибка сохранения", str(e))

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

    def request_build_link_graph(self):
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
        )
        worker.signals.finished.connect(self._on_graph_built)
        worker.signals.failed.connect(self._on_graph_build_failed)
        self._graph_pool.start(worker)

    @Slot(object, object)
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

    @Slot(object, object)
    def _on_graph_build_failed(self, req_id: int, err: str):
        if req_id != self._graph_req_id:
            return
        log.warning("Graph build failed (bg): %s", err)

    # Backwards-compatible alias (optional): keep old name used elsewhere
    def build_link_graph(self):
        self.request_build_link_graph()


class _GraphBuildSignals(QObject):
    finished = Signal(object, object)  # (req_id:int, payload:dict)
    failed = Signal(object, object)    # (req_id:int, err:str)


class _GraphBuildWorker(QRunnable):
    def __init__(
        self,
        req_id: int,
        vault_dir: Path,
        mode: str,
        depth: int,
        center: str | None,
        outgoing_snapshot: dict[str, list[str]],
        existing_titles: set[str],
    ):
        super().__init__()
        self.req_id = req_id
        self.vault_dir = vault_dir
        self.mode = mode
        self.depth = max(1, int(depth))
        self.center = center
        self.outgoing_snapshot = outgoing_snapshot
        self.existing_titles = existing_titles
        self.signals = _GraphBuildSignals()

    def run(self):
        t0 = time.perf_counter()
        try:
            # Build from snapshot (fast, no disk IO)
            title_set = set(self.existing_titles)
            edges_all: list[tuple[str, str]] = []

            for src, dst_list in self.outgoing_snapshot.items():
                if src not in title_set:
                    title_set.add(src)  # safety: shouldn't happen, but ok
                for dst in dst_list:
                    if dst not in title_set:
                        title_set.add(dst)  # virtual node
                    if src != dst:
                        edges_all.append((src, dst))

            # unique preserve order
            edges_all = list(dict.fromkeys(edges_all))
            nodes_all = sorted(title_set, key=str.lower)

            # LOCAL graph selection (if requested and we have a center)
            nodes = nodes_all
            edges = edges_all
            if self.mode == "local" and self.center:
                adj: dict[str, set[str]] = {n: set() for n in nodes_all}
                for a, b in edges_all:
                    if a in adj:
                        adj[a].add(b)
                    if b in adj:
                        adj[b].add(a)

                visited = {self.center}
                frontier = {self.center}
                for _ in range(self.depth):
                    nxt = set()
                    for v in frontier:
                        nxt |= adj.get(v, set())
                    nxt -= visited
                    visited |= nxt
                    frontier = nxt

                nodes = sorted(visited, key=str.lower)
                node_set = set(nodes)
                edges = [(a, b) for (a, b) in edges_all if a in node_set and b in node_set]

            dt_ms = (time.perf_counter() - t0) * 1000.0
            payload = {
                "nodes": nodes,
                "edges": edges,
                "stats": {
                    "mode": self.mode,
                    "depth": self.depth,
                    "nodes_all": len(nodes_all),
                    "edges_all": len(edges_all),
                    "time_ms": dt_ms,
                },
            }
            self.signals.finished.emit(self.req_id, payload)
        except Exception as e:
            self.signals.failed.emit(self.req_id, str(e))

class GraphNode(QGraphicsEllipseItem):
    def __init__(self, title: str, x: float, y: float, degree: int, theme: dict, r_base: float = 10.0):
        r = r_base + min(10.0, degree * 1.6)

        super().__init__(-r, -r, 2*r, 2*r)
        self.title = title
        self.r = r
        self.degree = degree
        self._theme = theme

        self.setPos(x, y)
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsEllipseItem.ItemIsSelectable, True)

        # --- glow ring (простое "свечение") ---
        glow_r = r + 10
        self.glow = QGraphicsEllipseItem(-glow_r, -glow_r, 2*glow_r, 2*glow_r, self)
        self.glow.setPen(QPen(Qt.NoPen))
        self.glow.setBrush(QBrush(theme["glow"]))
        self.glow.setZValue(-1)     # под основным кругом
        self.glow.setVisible(False) # показываем на hover/selected

        # pens/brushes from theme
        self.pen_default = QPen(theme["node_pen"]); self.pen_default.setWidth(1)
        self.pen_hover = QPen(theme["node_pen_hover"]); self.pen_hover.setWidth(2)
        self.pen_selected = QPen(theme["node_pen_selected"]); self.pen_selected.setWidth(3)

        self.brush_default = QBrush(theme["node_fill"])
        self.brush_hover = QBrush(theme["node_fill_hover"])
        self.brush_selected = QBrush(theme["node_fill_selected"])

        self.setPen(self.pen_default)
        self.setBrush(self.brush_default)

        # label
        self.label = QGraphicsSimpleTextItem(title, self)
        self.label.setBrush(QBrush(theme["label"]))
        self.label.setPos(r + 6, -8)
        self.label.setOpacity(1.0)

    def hoverEnterEvent(self, event):
        self.setPen(self.pen_hover)
        self.setBrush(self.brush_hover)
        self.setScale(1.15)
        self.glow.setVisible(True)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        if self.isSelected():
            self.setPen(self.pen_selected)
            self.setBrush(self.brush_selected)
            self.glow.setVisible(True)
        else:
            self.setPen(self.pen_default)
            self.setBrush(self.brush_default)
            self.glow.setVisible(False)
        self.setScale(1.0)
        super().hoverLeaveEvent(event)


class GraphView(QGraphicsView):
    def __init__(self, on_open_note):
        super().__init__()
        self.on_open_note = on_open_note
        self.setRenderHints(QPainter.Antialiasing)
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.edge_items: dict[tuple[str, str], QGraphicsLineItem] = {}

        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[tuple[str, str]] = []

        # ---- THEMES ----
        self._themes = {
            "dark": {
                "bg": QColor(18, 18, 20),
                "edge": QColor(180, 180, 190, 60),
                "edge_hi": QColor(255, 255, 255, 140),
                "node_fill": QColor(80, 80, 90, 180),
                "node_fill_hover": QColor(120, 120, 130, 220),
                "node_fill_selected": QColor(170, 170, 180, 240),
                "node_pen": QColor(140, 140, 150, 180),
                "node_pen_hover": QColor(240, 240, 240, 230),
                "node_pen_selected": QColor(255, 255, 255, 255),
                "label": QColor(235, 235, 240, 220),
                "glow": QColor(255, 255, 255, 40),
            },
            "light": {
                "bg": QColor(245, 245, 248),
                "edge": QColor(60, 60, 70, 50),
                "edge_hi": QColor(40, 40, 50, 150),
                "node_fill": QColor(235, 235, 240, 255),
                "node_fill_hover": QColor(220, 220, 230, 255),
                "node_fill_selected": QColor(200, 200, 215, 255),
                "node_pen": QColor(80, 80, 90, 160),
                "node_pen_hover": QColor(20, 20, 30, 220),
                "node_pen_selected": QColor(10, 10, 20, 255),
                "label": QColor(20, 20, 30, 220),
                "glow": QColor(0, 0, 0, 25),
            },
        }
        self._theme_name = "dark"
        self.apply_theme(self._theme_name)

        # ---- ANIMATION ----
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)  # ~60 FPS
        self._anim_timer.timeout.connect(self._on_anim_tick)

        self._anim_clock = QElapsedTimer()
        self._anim_duration_ms = 380  # скорость анимации

        self._anim_start: dict[str, QPointF] = {}
        self._anim_target: dict[str, QPointF] = {}

        # ---- LABEL LOD (fade in/out by zoom) ----
        self._lod_timer = QTimer(self)
        self._lod_timer.setInterval(16)  # ~60 FPS
        self._lod_timer.timeout.connect(self._on_lod_tick)

        self._lod_current = 1.0
        self._lod_target = 1.0


    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else (1 / 1.15)

        # лимитируем масштаб
        current = self.transform().m11()
        new_scale = current * factor
        if new_scale < 0.2 or new_scale > 5.0:
            return

        self.scale(factor, factor)
        self._set_lod_target()

    def animate_to(self, target_pos: dict[str, QPointF]):
        # если уже идет анимация — остановим, чтобы не накапливать
        if self._anim_timer.isActive():
            self._anim_timer.stop()

        self._anim_target = dict(target_pos)
        self._anim_start = {t: self.nodes[t].pos() for t in self.nodes.keys() if t in self._anim_target}

        self._anim_clock.restart()
        self._anim_timer.start()

    def _ease_out_cubic(self, t: float) -> float:
        # t in [0..1]
        return 1.0 - (1.0 - t) ** 3

    def _on_anim_tick(self):
        elapsed = self._anim_clock.elapsed()
        t = min(1.0, elapsed / self._anim_duration_ms)
        k = self._ease_out_cubic(t)

        # двигаем узлы
        for title, node in self.nodes.items():
            if title not in self._anim_target:
                continue
            p0 = self._anim_start.get(title, node.pos())
            p1 = self._anim_target[title]
            x = p0.x() * (1.0 - k) + p1.x() * k
            y = p0.y() * (1.0 - k) + p1.y() * k
            node.setPos(x, y)

        # обновляем рёбра так, чтобы они тянулись за узлами
        for (a, b), line in self.edge_items.items():
            na = self.nodes.get(a)
            nb = self.nodes.get(b)
            if not na or not nb:
                continue
            p1 = na.pos()
            p2 = nb.pos()
            line.setLine(p1.x(), p1.y(), p2.x(), p2.y())

        if t >= 1.0:
            # финальный snap в точные координаты
            for title, node in self.nodes.items():
                if title in self._anim_target:
                    node.setPos(self._anim_target[title])

            for (a, b), line in self.edge_items.items():
                na = self.nodes.get(a)
                nb = self.nodes.get(b)
                if na and nb:
                    p1 = na.pos()
                    p2 = nb.pos()
                    line.setLine(p1.x(), p1.y(), p2.x(), p2.y())

            self._anim_timer.stop()

        # во время анимации тоже поддерживаем LOD (если пользователь зумит)
        self._set_lod_target()


    def center_on(self, title: str):
        node = self.nodes.get(title)
        if node:
            self.centerOn(node)

    def highlight(self, current_title: str):
        if not self.nodes:
            return

        neighbors = set()
        for a, b in self.edges:
            if a == current_title:
                neighbors.add(b)
            if b == current_title:
                neighbors.add(a)

        # сброс ребер
        pen_edge = self._pen_edge
        pen_edge.setWidth(1)
        for line in self.edge_items.values():
            line.setPen(pen_edge)

        # подсветка ребер от текущего к соседям
        pen_edge_hi = self._pen_edge_hi
        pen_edge_hi.setWidth(2)
        for nb in neighbors:
            line = self.edge_items.get((current_title, nb)) or self.edge_items.get((nb, current_title))
            if line:
                line.setPen(pen_edge_hi)

        # узлы
        for title, node in self.nodes.items():
            node.setSelected(False)  # чтобы hover/leave корректно возвращал стиль
            if title == current_title:
                node.setPen(node.pen_selected)
                node.setBrush(node.brush_selected)
                node.glow.setVisible(True)
            elif title in neighbors:
                node.setPen(node.pen_hover)
                node.setBrush(node.brush_hover)
                node.glow.setVisible(False)
            else:
                node.setPen(node.pen_default)
                node.setBrush(node.brush_default)
                node.glow.setVisible(False)

    def mousePressEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if event.button() == Qt.LeftButton and item is not None:
            # itemAt() может вернуть дочерний объект (например label: QGraphicsSimpleTextItem),
            # поэтому поднимаемся вверх по иерархии, пока не найдем GraphNode.
            node = None
            cur = item
            while cur is not None:
                if isinstance(cur, GraphNode):
                    node = cur
                    break
                cur = cur.parentItem()

            if node is not None:
                self.on_open_note(node.title)
                return
        super().mousePressEvent(event)

    def build(self, nodes: list[str], edges: list[tuple[str, str]]):
        prev_pos = {t: node.pos() for t, node in self.nodes.items()}
        self.scene.clear()
        self.nodes.clear()
        self.edges = edges[:]
        self.edge_items.clear()

        # степень узлов
        deg = {n: 0 for n in nodes}
        for a, b in edges:
            if a in deg: deg[a] += 1
            if b in deg: deg[b] += 1

        rng = random.Random(42)
        pos = {n: QPointF(rng.uniform(-250, 250), rng.uniform(-250, 250)) for n in nodes}
        target_pos = self._layout_force(nodes, edges, pos, steps=250)

        # ребра (полупрозрачные)
        pen_edge = self._pen_edge

        # узлы (создаем на "старых" позициях, если узел существовал)
        for n in nodes:
            tp = target_pos[n]
            sp = prev_pos.get(n, tp)  # старт = старая позиция, если есть

            node = GraphNode(n, sp.x(), sp.y(), degree=deg.get(n, 0), theme=self._t, r_base=10.0)
            node.setZValue(10)
            self.scene.addItem(node)
            self.nodes[n] = node

        for a, b in edges:
            na = self.nodes.get(a)
            nb = self.nodes.get(b)
            if not na or not nb:
                continue
            p1, p2 = na.pos(), nb.pos()
            line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
            line.setPen(pen_edge)
            line.setZValue(-10)
            self.scene.addItem(line)

            self.edge_items[(a, b)] = line

        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-120, -120, 120, 120))
        self.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)
        self._set_lod_target()
        self.animate_to(target_pos)

    def _layout_force(self, nodes, edges, pos, steps=200):
        # параметры (подкрутишь по вкусу)
        k_rep = 9000.0   # отталкивание
        k_att = 0.020    # притяжение
        damp = 0.85      # демпфирование

        vel = {n: QPointF(0, 0) for n in nodes}

        neighbors = {n: [] for n in nodes}
        for a, b in edges:
            if a in neighbors and b in neighbors:
                neighbors[a].append(b)
                neighbors[b].append(a)

        for _ in range(steps):
            force = {n: QPointF(0, 0) for n in nodes}

            # repulsion O(n^2) — для небольших vault ок
            for i in range(len(nodes)):
                a = nodes[i]
                pa = pos[a]
                for j in range(i + 1, len(nodes)):
                    b = nodes[j]
                    pb = pos[b]
                    dx = pa.x() - pb.x()
                    dy = pa.y() - pb.y()
                    dist2 = dx*dx + dy*dy + 0.01
                    f = k_rep / dist2
                    fx = f * dx
                    fy = f * dy
                    force[a] = force[a] + QPointF(fx, fy)
                    force[b] = force[b] + QPointF(-fx, -fy)

            # attraction по ребрам
            for a, b in edges:
                if a not in pos or b not in pos:
                    continue
                pa, pb = pos[a], pos[b]
                dx = pb.x() - pa.x()
                dy = pb.y() - pa.y()
                dist = math.sqrt(dx*dx + dy*dy) + 0.001
                fx = k_att * dx
                fy = k_att * dy
                force[a] = force[a] + QPointF(fx, fy)
                force[b] = force[b] + QPointF(-fx, -fy)

            # интеграция
            for n in nodes:
                v = vel[n] * damp + force[n] * 0.0015
                vel[n] = v
                pos[n] = pos[n] + v

        return pos

    def apply_theme(self, name: str):
        if name not in self._themes:
            return
        self._theme_name = name
        t = self._themes[name]

        # фон
        self.setBackgroundBrush(QBrush(t["bg"]))

        # перо ребер по умолчанию (используется в build/highlight)
        self._pen_edge = QPen(t["edge"])
        self._pen_edge.setWidth(1)

        self._pen_edge_hi = QPen(t["edge_hi"])
        self._pen_edge_hi.setWidth(2)

        # сохраняем, чтобы Node мог их взять
        self._t = t

    def _lod_target_from_scale(self, s: float) -> float:
        # s = текущий масштаб (1.0 примерно "нормально")
        # < 0.55  -> 0 (скрыть)
        # 0.55..1 -> плавно 0..1
        # > 1     -> 1 (показать)
        if s <= 0.55:
            return 0.0
        if s >= 1.0:
            return 1.0
        return (s - 0.55) / (1.0 - 0.55)

    def _set_lod_target(self):
        s = float(self.transform().m11())
        self._lod_target = self._lod_target_from_scale(s)
        if not self._lod_timer.isActive():
            self._lod_timer.start()

    def _on_lod_tick(self):
        # экспоненциальное приближение к цели (мягко, без рывков)
        # чем больше alpha — тем быстрее
        alpha = 0.18
        self._lod_current = self._lod_current * (1.0 - alpha) + self._lod_target * alpha

        # применяем к label у всех нод
        for node in self.nodes.values():
            node.label.setOpacity(self._lod_current)

        # стоп, когда почти достигли цели
        if abs(self._lod_current - self._lod_target) < 0.02:
            self._lod_current = self._lod_target
            for node in self.nodes.values():
                node.label.setOpacity(self._lod_current)
            self._lod_timer.stop()

def main():
    install_global_exception_hooks()
    app = QApplication([])
    win = NotesApp()
    win.resize(1100, 700)
    win.show()
    log.info("Приложение запущено, SID=%s", SESSION_ID)
    app.exec()


if __name__ == "__main__":
    main()
