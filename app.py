import os
import re
from pathlib import Path
import math
import random
import time

from PySide6.QtCore import Qt, QTimer, Signal, QPointF, QElapsedTimer
from PySide6.QtGui import QAction, QPainter, QPen, QBrush, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListWidget, QTextEdit, QLineEdit, QFileDialog, QMessageBox, QSplitter,
    QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsLineItem,
    QGraphicsSimpleTextItem, QGraphicsItem
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

        viewm = menubar.addMenu("Вид")

        act_dark = QAction("Тема: Dark", self, checkable=True)
        act_light = QAction("Тема: Light", self, checkable=True)
        act_dark.setChecked(True)

        def set_dark():
            act_dark.setChecked(True); act_light.setChecked(False)
            self.graph.apply_theme("dark")
            # перестроим граф, чтобы ноды пересоздались с новой темой
            self.build_link_graph()
            if self.current_path:
                self.graph.highlight(self.current_path.stem)

        def set_light():
            act_light.setChecked(True); act_dark.setChecked(False)
            self.graph.apply_theme("light")
            self.build_link_graph()
            if self.current_path:
                self.graph.highlight(self.current_path.stem)

        act_dark.triggered.connect(set_dark)
        act_light.triggered.connect(set_light)

        viewm.addAction(act_dark)
        viewm.addAction(act_light)

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
        self.graph.highlight(title)
        self.graph.center_on(title)

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

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else (1 / 1.15)

        # лимитируем масштаб
        current = self.transform().m11()
        new_scale = current * factor
        if new_scale < 0.2 or new_scale > 5.0:
            return

        self.scale(factor, factor)

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
        if isinstance(item, GraphNode) and event.button() == Qt.LeftButton:
            self.on_open_note(item.title)
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

def main():
    app = QApplication([])
    win = NotesApp()
    win.resize(1100, 700)
    win.show()
    app.exec()


if __name__ == "__main__":
    main()
