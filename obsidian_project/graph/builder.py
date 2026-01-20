from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class GraphBuildResult:
    nodes: list[str]
    edges: list[tuple[str, str]]
    stats: dict


def build_graph_snapshot(
    *,
    outgoing_snapshot: dict[str, list[str]],
    existing_titles: set[str],
    mode: str,
    depth: int,
    center: str | None,
) -> GraphBuildResult:
    t0 = time.perf_counter()

    title_set = set(existing_titles)
    edges_all: list[tuple[str, str]] = []

    for src, dst_list in outgoing_snapshot.items():
        title_set.add(src)
        for dst in dst_list:
            title_set.add(dst)
            if src != dst:
                edges_all.append((src, dst))

    edges_all = list(dict.fromkeys(edges_all))
    nodes_all = sorted(title_set, key=str.lower)

    nodes, edges = nodes_all, edges_all

    if mode == "local" and center:
        adj: dict[str, set[str]] = {n: set() for n in nodes_all}
        for a, b in edges_all:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)

        visited = {center}
        frontier = {center}
        for _ in range(max(1, int(depth))):
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
    return GraphBuildResult(
        nodes=nodes,
        edges=edges,
        stats={
            "mode": mode,
            "depth": int(depth),
            "nodes_all": len(nodes_all),
            "edges_all": len(edges_all),
            "time_ms": dt_ms,
        },
    )
