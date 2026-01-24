from PySide6.QtCore import Qt, QTimer, QElapsedTimer, QPointF
from PySide6.QtGui import QBrush, QPen, QColor, QPainter
from PySide6.QtWidgets import QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsLineItem, QGraphicsSimpleTextItem
import random
import math





class GraphNode(QGraphicsEllipseItem):
    def __init__(self, note_id: str, label: str, x: float, y: float, degree: int, theme: dict, r_base: float = 10.0):
        r = r_base + min(10.0, degree * 1.6)

        super().__init__(-r, -r, 2*r, 2*r)
        self.note_id = note_id
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
        # Forced state is used by GraphView.highlight() so hover doesn't destroy highlight.
        # Values: "normal" | "neighbor" | "current"
        self._forced_state = "normal"

        # label
        self.label = QGraphicsSimpleTextItem(label, self)
        self.label.setBrush(QBrush(theme["label"]))
        self.label.setPos(r + 6, -8)
        self.label.setOpacity(1.0)

    def hoverEnterEvent(self, event):
        self.setPen(self.pen_hover)
        self.setBrush(self.brush_hover)
        self.setScale(1.15)
        self.glow.setVisible(True)
        super().hoverEnterEvent(event)

    def apply_forced_state(self, state: str) -> None:
        self._forced_state = state or "normal"
        if self._forced_state == "current":
            self.setPen(self.pen_selected)
            self.setBrush(self.brush_selected)
            self.glow.setVisible(True)
        elif self._forced_state == "neighbor":
            self.setPen(self.pen_hover)
            self.setBrush(self.brush_hover)
            self.glow.setVisible(False)
        else:
            self.setPen(self.pen_default)
            self.setBrush(self.brush_default)
            self.glow.setVisible(False)

    def hoverLeaveEvent(self, event):
        # Restore highlight state set by GraphView.highlight()
        try:
            self.apply_forced_state(getattr(self, "_forced_state", "normal"))
        except Exception:
            pass
        self.setScale(1.0)
        super().hoverLeaveEvent(event)


class GraphView(QGraphicsView):
    def __init__(self, on_open_note):
        super().__init__()
        self.on_open_note = on_open_note
        self.setRenderHints(QPainter.Antialiasing)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.edge_items: dict[tuple[str, str], QGraphicsLineItem] = {}

        self.nodes: dict[str, GraphNode] = {} # note_id -> node
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

    def center_on(self, note_id: str):
        node = self.nodes.get(note_id)
        if node:
            self.centerOn(node)

    def highlight(self, current_id: str):
        if not self.nodes:
            return

        neighbors = set()
        for a, b in self.edges:
            if a == current_id:
                neighbors.add(b)
            if b == current_id:
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
            line = self.edge_items.get((current_id, nb)) or self.edge_items.get((nb, current_id))
            if line:
                line.setPen(pen_edge_hi)

        # узлы
        for nid, node in self.nodes.items():
            node.setSelected(False)  # чтобы hover/leave корректно возвращал стиль
            if nid == current_id:
                node.apply_forced_state("current")
            elif nid in neighbors:
                node.apply_forced_state("neighbor")
            else:
                node.apply_forced_state("normal")

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
                self.on_open_note(node.note_id)
                return
        super().mousePressEvent(event)

    def build(self, nodes: list[str], edges: list[tuple[str, str]], labels: dict[str, str] | None = None):
        # nodes: list[note_id]
        labels = labels or {}
        prev_pos = {nid: node.pos() for nid, node in self.nodes.items()}
        self._scene.clear()
        self.nodes.clear()
        self.edges = edges[:]
        self.edge_items.clear()

        # степень узлов
        deg = {nid: 0 for nid in nodes}
        for a, b in edges:
            if a in deg: deg[a] += 1
            if b in deg: deg[b] += 1

        rng = random.Random(42)
        pos = {nid: QPointF(rng.uniform(-250, 250), rng.uniform(-250, 250)) for nid in nodes}
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
        for nid in nodes:
            tp = target_pos[nid]
            sp = prev_pos.get(nid, tp)  # старт = старая позиция, если есть
            label = labels.get(nid) or nid
            node = GraphNode(nid, label, sp.x(), sp.y(), degree=deg.get(nid, 0), theme=self._t, r_base=10.0)
            node.setZValue(10)
            self._scene.addItem(node)
            self.nodes[nid] = node

        for a, b in edges:
            na = self.nodes.get(a)
            nb = self.nodes.get(b)
            if not na or not nb:
                continue
            p1, p2 = na.pos(), nb.pos()
            line = QGraphicsLineItem(p1.x(), p1.y(), p2.x(), p2.y())
            line.setPen(pen_edge)
            line.setZValue(-10)
            self._scene.addItem(line)

            self.edge_items[(a, b)] = line

        self._scene.setSceneRect(self._scene.itemsBoundingRect().adjusted(-120, -120, 120, 120))
        self.fitInView(self._scene.sceneRect(), Qt.KeepAspectRatio)
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