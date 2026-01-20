from __future__ import annotations
from pathlib import Path

APP_NAME = "obsidian-project"
LOG_DIR = Path.home() / f".{APP_NAME}" / "logs"
LOG_PATH = LOG_DIR / f"{APP_NAME}.log"
