import os
import re
from pathlib import Path
import math
import random

from PySide6.QtCore import Qt, QTimer, Signal, QPointF
from PySide6.QtGui import QAction, QPainter, QPen, QBrush
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QTextEdit, QLineEdit, QFileDialog, QMessageBox, QSplitter,
    QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsLineItem
)
from PySide6.QtWebEngineWidgets import QWebEngineView

import markdown as md


WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def safe_filename(title: str) -> str:
    # минимальная "санитизация" имени файла
    title = title.strip().replace("/", "-").replace("\\", "-")
    title = re.sub(r"\s+", " ", title)
    return title


def wikilinks_to_html(markdown_text: str) -> str:
    # заменяем [[Note]] на <a href="obsidian://Note">Note</a>
    def repl(m):
        name = m.group(1).strip()
        label = name
        href = f"note://{name}"
        return f'<a href="{href}">{label}</a>'

    return WIKILINK_RE.sub(repl, markdown_text)


class LinkableWebView(QWebEngineView):
    linkClicked = Signal(str)

    def __init__(self):
        super().__init__()
        # Перехват кликов по ссылкам
        self.page().urlChanged.connect(self._on_url_changed)

    def _on_url_changed(self, url):
        # QWebEngine по умолчанию сам "переходит" по ссылкам; мы ловим кастомную схему
        if url.scheme() == "note":
            self.linkClicked.emit(url.path().lstrip("/"))
            # возвращаемся "назад", чтобы не было пустой навигации
            self.back()


class NotesApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Mini-Obsidian (Python)")

        self.vault_dir: Path | None = None
        self.current_path: Path | None = None
        self._dirty = False

        # UI
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск… (по имени файла)")
        self.listw = QListWidget()

        self.editor = QTextEdit()
        self.preview = LinkableWebView()

        self.graph = GraphView(self.open_or_create_by_title)

        self.splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.listw)

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

    def choose_vault(self):
        path = QFileDialog.getExistingDirectory(self, "Выберите папку для заметок")
        if not path:
            # если пользователь отменил и vault ещё не выбран — создадим временную папку рядом
            if self.vault_dir is None:
                tmp = Path.cwd() / "vault"
                tmp.mkdir(exist_ok=True)
                self.vault_dir = tmp
                self.refresh_list()
            return

        self.vault_dir = Path(path)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.current_path = None
        self.editor.blockSignals(True)
        self.editor.clear()
        self.editor.blockSignals(False)
        self._dirty = False
        self.refresh_list()
        self.build_link_graph()

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
        path = self.vault_dir / f"{title}.md"

        # save previous
        self._save_current_if_needed()

        if not path.exists():
            path.write_text(f"# {title}\n\n", encoding="utf-8")

        self.current_path = path
        text = path.read_text(encoding="utf-8")

        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)

        self._dirty = False
        self._render_preview(text)
        self._select_in_list(title)
        self.build_link_graph()

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
        text = self.editor.toPlainText()
        self._render_preview(text)
        self.save_timer.start()

    def _render_preview(self, text: str):
        # wiki links -> html links, потом markdown -> html
        text2 = wikilinks_to_html(text)
        html = md.markdown(
            text2,
            extensions=["fenced_code", "tables", "toc"]
        )
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
        <body>{html}</body>
        </html>
        """
        self.preview.setHtml(page)

    def _save_current_if_needed(self):
        if not self._dirty or self.current_path is None:
            return
        try:
            self.current_path.write_text(self.editor.toPlainText(), encoding="utf-8")
            self._dirty = False
            self.refresh_list()
            self.build_link_graph()
        except Exception as e:
            QMessageBox.critical(self, "Ошибка сохранения", str(e))

    def create_note_dialog(self):
        # простой способ: используем строку поиска как ввод имени
        title = self.search.text().strip()
        if not title:
            QMessageBox.information(self, "Новая заметка", "Введите название в поле поиска и нажмите 'Новая заметка…'")
            return
        self.open_or_create_by_title(title)

    def build_link_graph(self):
        if self.vault_dir is None:
            return

        # 1) читаем все заметки
        files = list(self.vault_dir.glob("*.md"))
        titles = [p.stem for p in files]
        title_set = set(titles)

        edges: list[tuple[str, str]] = []

        for p in files:
            src = p.stem
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue

            for m in WIKILINK_RE.finditer(text):
                dst = safe_filename(m.group(1).strip())
                # Obsidian создает "виртуальные" узлы тоже — сделаем так же:
                if dst not in title_set:
                    titles.append(dst)
                    title_set.add(dst)
                if src != dst:
                    edges.append((src, dst))

        # уберем дубликаты ребер
        edges = list(dict.fromkeys(edges))

        self.graph.build(sorted(title_set, key=str.lower), edges)


class GraphNode(QGraphicsEllipseItem):
    def __init__(self, title: str, x: float, y: float, r: float = 14):
        super().__init__(-r, -r, 2*r, 2*r)
        self.title = title
        self.setPos(x, y)
        self.setBrush(QBrush())
        self.setPen(QPen())
        self.setFlag(QGraphicsEllipseItem.ItemIsSelectable, True)


class GraphView(QGraphicsView):
    def __init__(self, on_open_note):
        super().__init__()
        self.on_open_note = on_open_note
        self.setRenderHints(QPainter.Antialiasing)
        self.scene = QGraphicsScene(self)
        self.setScene(self.scene)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[tuple[str, str]] = []

    def wheelEvent(self, event):
        # zoom
        factor = 1.15 if event.angleDelta().y() > 0 else (1 / 1.15)
        self.scale(factor, factor)

    def mouseDoubleClickEvent(self, event):
        item = self.itemAt(event.position().toPoint())
        if isinstance(item, GraphNode):
            self.on_open_note(item.title)
            return
        super().mouseDoubleClickEvent(event)

    def build(self, nodes: list[str], edges: list[tuple[str, str]]):
        self.scene.clear()
        self.nodes.clear()
        self.edges = edges[:]

        # стартовые координаты
        rng = random.Random(42)
        pos = {n: QPointF(rng.uniform(-250, 250), rng.uniform(-250, 250)) for n in nodes}

        # force-directed layout (простая, но работает)
        pos = self._layout_force(nodes, edges, pos, steps=250)

        # рисуем ребра (сначала линии)
        pen = QPen()
        for a, b in edges:
            if a not in pos or b not in pos:
                continue
            p1, p2 = pos[a], pos[b]
            line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
            line.setPen(pen)
            self.scene.addItem(line)

        # рисуем узлы
        for n in nodes:
            p = pos[n]
            node = GraphNode(n, p.x(), p.y(), r=14)
            self.scene.addItem(node)
            self.nodes[n] = node

        self.scene.setSceneRect(self.scene.itemsBoundingRect().adjusted(-80, -80, 80, 80))
        self.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

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


def main():
    app = QApplication([])
    win = NotesApp()
    win.resize(1100, 700)
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
