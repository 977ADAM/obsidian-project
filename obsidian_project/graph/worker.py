from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Signal

from obsidian_project.graph.builder import build_graph_snapshot


class GraphBuildSignals(QObject):
    finished = Signal(int, dict)
    failed = Signal(int, str)


class GraphBuildWorker(QRunnable):
    def __init__(self, *, req_id: int, payload: dict):
        super().__init__()
        self.req_id = req_id
        self.payload = payload
        self.signals = GraphBuildSignals()

    def run(self):
        try:
            res = build_graph_snapshot(**self.payload)
            self.signals.finished.emit(self.req_id, {"nodes": res.nodes, "edges": res.edges, "stats": res.stats})
        except Exception as e:
            self.signals.failed.emit(self.req_id, str(e))
