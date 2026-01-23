from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSettings


@dataclass(frozen=True)
class SettingsKeys:
    UI_THEME: str = "ui/theme"
    UI_GEOMETRY: str = "ui/geometry"
    UI_STATE: str = "ui/windowState"
    UI_SPLITTER: str = "ui/splitter_sizes"
    UI_RIGHT_SPLITTER: str = "ui/right_splitter_sizes"
    VAULT_DIR: str = "vault/dir"
    LAST_NOTE: str = "nav/last_note"
    GRAPH_MODE: str = "graph/mode"
    GRAPH_DEPTH: str = "graph/depth"
    GRAPH_MAX_NODES: str = "graph/max_nodes"
    GRAPH_MAX_STEPS: str = "graph/max_steps"


def get_str(settings: QSettings, key: str, default: str) -> str:
    try:
        val = settings.value(key, default)
        return str(val) if val is not None else default
    except Exception:
        return default


def get_int(settings: QSettings, key: str, default: int) -> int:
    try:
        return int(settings.value(key, default))
    except Exception:
        return default
