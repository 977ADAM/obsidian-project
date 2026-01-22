APP_NAME = "obsidian-project"

import os
import re
import sys
import unicodedata
import uuid
import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
import math
import random
import time
import html
from urllib.parse import quote, unquote
from dataclasses import dataclass, field
from datetime import datetime

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

from PySide6.QtCore import Qt, QTimer, Signal, QPointF, QElapsedTimer, QObject, QRunnable, QThreadPool, Slot, QSettings
from PySide6.QtGui import QAction, QPainter, QPen, QBrush, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QTextEdit, QLineEdit, QFileDialog, QMessageBox, QSplitter,
    QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsLineItem,
    QGraphicsSimpleTextItem, QGraphicsItem, QDialog, QProgressDialog
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

def rewrite_wikilinks_targets(markdown_text: str, old_stem: str, new_stem: str) -> tuple[str, bool]:
    """
    Переписывает wikilinks по всему тексту:
      - [[old]] -> [[new]]
      - [[old|alias]] -> [[new|alias]]
      - [[old#Heading]] -> [[new#Heading]]
      - [[old^block]] -> [[new^block]]

    Важно:
      - Сравнение по каноническому имени файла: safe_filename(base_target) == old_stem
      - base_target — часть до # или ^ (obsidian-style heading/block)
    """
    old_stem = safe_filename(old_stem)
    new_stem = safe_filename(new_stem)
    if not old_stem or not new_stem or old_stem == new_stem:
        return markdown_text, False

    changed = False

    def repl(m: re.Match) -> str:
        nonlocal changed
        inner = (m.group(1) or "").strip()
        if not inner:
            return m.group(0)

        # split alias
        if "|" in inner:
            target_raw, display_raw = inner.split("|", 1)
            target_raw = target_raw.strip()
            display_raw = display_raw.strip()
            has_alias = True
        else:
            target_raw = inner.strip()
            display_raw = ""
            has_alias = False

        # Support [[Note#Heading]] or [[Note^block]]
        suffix = ""
        base = target_raw
        for sep in ("#", "^"):
            if sep in base:
                base, suffix = base.split(sep, 1)
                suffix = sep + suffix
                break

        base = base.strip()
        base_canon = safe_filename(base)
        if base_canon == old_stem:
            changed = True
            target_raw = f"{new_stem}{suffix}"

        if has_alias:
            return f"[[{target_raw}|{display_raw}]]"
        return f"[[{target_raw}]]"

    out = WIKILINK_RE.sub(repl, markdown_text or "")
    return out, changed

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

    def update_note(self, src: str, markdown_text: str) -> bool:
        """
        Инкрементально обновляет индекс для одной заметки src.
        Возвращает True, если набор исходящих ссылок изменился (может требоваться перестройка графа/беклинков).
        """
        src = safe_filename(src)
        if not src:
            return False

        new_targets = extract_wikilink_targets(markdown_text)
        # self-links не держим
        new_targets.discard(src)

        old_targets = set(self.outgoing.get(src, ()))
        if old_targets == new_targets:
            return False
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
        return True

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
    # Needed for markdown 'toc' extension anchors:
    "h1": ["id"], "h2": ["id"], "h3": ["id"],
    "h4": ["id"], "h5": ["id"], "h6": ["id"],
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

def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """
    Atomic-ish file write:
      - write to temp file in same directory
      - fsync
      - replace() into final path
    Helps prevent partial writes on crash/power loss.
    """
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    tmp_name = f".{path.name}.tmp-{uuid.uuid4().hex}"
    tmp_path = parent / tmp_name

    f = None
    try:
        f = open(tmp_path, "w", encoding=encoding, newline="")
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
        f.close()
        f = None
        tmp_path.replace(path)
    finally:
        try:
            if f is not None:
                f.close()
        except Exception:
            pass
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass

# Recovery copies (when save fails) go here:
RECOVERY_DIR = Path.home() / f".{APP_NAME}" / "recovery"
RECOVERY_DIR.mkdir(parents=True, exist_ok=True)

def write_recovery_copy(note_path: Path, text: str) -> Path:
    """
    Best-effort emergency save when normal save fails.
    Writes a timestamped copy into ~/.<APP_NAME>/recovery/.
    """
    note_path = Path(note_path)
    stem = note_path.stem if note_path.stem else "Untitled"
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    rec_path = RECOVERY_DIR / f"{stem}.recovery.{ts}.md"
    atomic_write_text(rec_path, text, encoding="utf-8")
    return rec_path

# Путь логов лучше не в cwd: он нестабилен для GUI приложений.
LOG_DIR = Path.home() / f".{APP_NAME}" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / f"{APP_NAME}.log"

def setup_logging() -> logging.Logger:
    logger = logging.getLogger(APP_NAME)

    # Log skipped autosaves explicitly (useful for race debugging)
    def log_autosave_skip():
        logger.info("Autosave skipped: note token mismatch (note was switched before timer fired)")

    logger.log_autosave_skip = log_autosave_skip  # type: ignore

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
        raise ValueError("safe_filename(): title is None")

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

    # если пусто — даём дефолт (уникальный, чтобы не перетирать файлы)
    if not s:
        s = f"Untitled-{uuid.uuid4().hex[:6]}"

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

        # ---- NAV HISTORY ----
        self._nav_back: list[str] = []
        self._nav_forward: list[str] = []
        self._nav_suppress = False  # чтобы back/forward не писали сами себя в историю

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
        self._nav_back.clear()
        self._nav_forward.clear()

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
        # avoid redundant open + history churn
        if current == title:
            return
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
        old_stem: str,
        new_stem: str,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.req_id = req_id
        self.vault_dir = vault_dir
        self.files = files
        self.old_stem = safe_filename(old_stem)
        self.new_stem = safe_filename(new_stem)
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
                        old_stem=self.old_stem,
                        new_stem=self.new_stem,
                    )
                    if changed:
                        atomic_write_text(p, new_txt, encoding="utf-8")
                        changed_files += 1
                except Exception:
                    error_files.append(str(p))
                    # продолжаем, не валим всю операцию
                    continue

            result = {
                "old_stem": self.old_stem,
                "new_stem": self.new_stem,
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

class _GraphBuildSignals(QObject):
    finished = Signal(int, dict)
    failed = Signal(int, str)


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
        max_nodes: int = 400,
        max_steps: int = 250,
    ):
        super().__init__()
        self.req_id = req_id
        self.vault_dir = vault_dir
        self.mode = mode
        self.depth = max(1, int(depth))
        self.center = center
        self.outgoing_snapshot = outgoing_snapshot
        self.existing_titles = existing_titles
        self.max_nodes = max(50, int(max_nodes))
        self.max_steps = max(30, int(max_steps))
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

            # Limit graph size in GLOBAL mode to prevent O(n^2) layout blowups.
            # Strategy: keep highest-degree nodes, always keep center (if any).
            if self.mode == "global" and len(nodes_all) > self.max_nodes:
                deg: dict[str, int] = {n: 0 for n in nodes_all}
                for a, b in edges_all:
                    if a in deg: deg[a] += 1
                    if b in deg: deg[b] += 1

                # rank by degree desc, then name
                ranked = sorted(nodes_all, key=lambda n: (-deg.get(n, 0), n.lower()))
                keep = ranked[: self.max_nodes]
                if self.center and self.center in deg and self.center not in keep:
                    keep[-1] = self.center
                node_set = set(keep)
                nodes_all = sorted(node_set, key=str.lower)
                edges_all = [(a, b) for (a, b) in edges_all if a in node_set and b in node_set]

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

            # Suggest dynamic force-layout steps based on node count (reduce CPU on larger graphs)
            # We still cap by self.max_steps.
            n = max(1, len(nodes))
            dyn_steps = int(min(self.max_steps, max(40, 20 + 10 * math.sqrt(n))))

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
                    "layout_steps": dyn_steps,
                },
                "layout_steps": dyn_steps,
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
        # steps may be injected from background worker stats, but GraphView is UI-only.
        # We'll choose a safe default; caller may override by setting self._layout_steps.
        steps = getattr(self, "_layout_steps", 250)
        try:
            steps = int(steps)
        except Exception:
            steps = 250
        target_pos = self._layout_force(nodes, edges, pos, steps=steps)

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
    # Ensure QSettings uses stable org/app identifiers.
    app.setOrganizationName(APP_NAME)
    app.setApplicationName(APP_NAME)
    win = NotesApp()
    win.show()
    log.info("Приложение запущено, SID=%s", SESSION_ID)
    app.exec()


if __name__ == "__main__":
    main()