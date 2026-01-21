# app/workers/graph_build.py

from __future__ import annotations

import time
from typing import Iterable

from PySide6.QtCore import QObject, QRunnable, Signal


class GraphBuildSignals(QObject):
    finished = Signal(int, dict)   # req_id, payload
    failed = Signal(int, str)      # req_id, error


class GraphBuildWorker(QRunnable):
    """
    Background worker that builds a link graph snapshot.

    INPUT:
      - outgoing_snapshot: {src: [dst1, dst2, ...]}
      - existing_titles: set[str]

    OUTPUT:
      payload = {
        nodes: list[str],
        edges: list[tuple[str, str]],
        stats: {...},
        layout_steps: int
      }
    """

    def __init__(
        self,
        *,
        req_id: int,
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
        self.mode = mode
        self.depth = max(1, int(depth))
        self.center = center
        self.outgoing = outgoing_snapshot
        self.existing_titles = set(existing_titles)

        self.max_nodes = max(50, int(max_nodes))
        self.max_steps = max(30, int(max_steps))

        self.signals = GraphBuildSignals()

    # ───────────────────────── run ─────────────────────────

    def run(self) -> None:
        t0 = time.perf_counter()

        try:
            nodes, edges = self._build_graph()

            payload = {
                "nodes": nodes,
                "edges": edges,
                "layout_steps": min(
                    self.max_steps,
                    max(30, len(nodes) * 2),
                ),
                "stats": {
                    "mode": self.mode,
                    "depth": self.depth,
                    "nodes_all": len(nodes),
                    "edges_all": len(edges),
                    "time_ms": (time.perf_counter() - t0) * 1000.0,
                },
            }

            self.signals.finished.emit(self.req_id, payload)

        except Exception as exc:
            self.signals.failed.emit(self.req_id, str(exc))

    # ───────────────────────── internal ─────────────────────────

    def _build_graph(self) -> tuple[list[str], list[tuple[str, str]]]:
        """
        Build nodes / edges according to mode.
        """
        nodes_all, edges_all = self._build_full_graph()

        if self.mode == "global":
            nodes, edges = self._limit_global(nodes_all, edges_all)
        else:
            nodes, edges = self._build_local(nodes_all, edges_all)

        return nodes, edges

    def _build_full_graph(self) -> tuple[list[str], list[tuple[str, str]]]:
        """
        Build full graph from outgoing snapshot.
        """
        nodes = set(self.existing_titles)
        edges: list[tuple[str, str]] = []

        for src, dsts in self.outgoing.items():
            nodes.add(src)
            for dst in dsts:
                nodes.add(dst)
                if src != dst:
                    edges.append((src, dst))

        # preserve order & uniqueness
        edges = list(dict.fromkeys(edges))
        return sorted(nodes, key=str.lower), edges

    # ───────────── global mode ─────────────

    def _limit_global(
        self,
        nodes: list[str],
        edges: list[tuple[str, str]],
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """
        Limit graph size in global mode by degree.
        """
        if len(nodes) <= self.max_nodes:
            return nodes, edges

        degree = {n: 0 for n in nodes}
        for a, b in edges:
            degree[a] += 1
            degree[b] += 1

        ranked = sorted(
            nodes,
            key=lambda n: (-degree.get(n, 0), n.lower()),
        )

        keep = ranked[: self.max_nodes]

        if self.center and self.center in degree and self.center not in keep:
            keep[-1] = self.center

        keep_set = set(keep)

        filtered_edges = [
            (a, b)
            for (a, b) in edges
            if a in keep_set and b in keep_set
        ]

        return sorted(keep_set, key=str.lower), filtered_edges

    # ───────────── local mode ─────────────

    def _build_local(
        self,
        nodes: list[str],
        edges: list[tuple[str, str]],
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """
        Build local graph around center up to N hops.
        """
        if not self.center or self.center not in nodes:
            return nodes, edges

        adj = self._build_adjacency(edges)

        visited = {self.center}
        frontier = {self.center}

        for _ in range(self.depth):
            next_frontier = set()
            for node in frontier:
                next_frontier |= adj.get(node, set())
            next_frontier -= visited
            visited |= next_frontier
            frontier = next_frontier

        sub_edges = [
            (a, b)
            for (a, b) in edges
            if a in visited and b in visited
        ]

        return sorted(visited, key=str.lower), sub_edges

    @staticmethod
    def _build_adjacency(
        edges: Iterable[tuple[str, str]],
    ) -> dict[str, set[str]]:
        adj: dict[str, set[str]] = {}
        for a, b in edges:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        return adj
