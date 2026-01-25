
from pathlib import Path
import time
import math
from PySide6.QtCore import QObject, QRunnable, Signal

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
        existing_ids: set[str],
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
        self.existing_ids = existing_ids
        self.max_nodes = max(50, int(max_nodes))
        self.max_steps = max(30, int(max_steps))
        self.signals = _GraphBuildSignals()

    def run(self):
        t0 = time.perf_counter()
        try:
            # Build from snapshot (fast, no disk IO)
            title_set = set(self.existing_ids)
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
            if self.mode == "local" and self.center and self.center in nodes_all:
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