AUTOSAVE_DEBOUNCE_MS = 600
PREVIEW_DEBOUNCE_MS_DEFAULT = 350
# Preview debounce becomes adaptive: 300..800ms depending on note size
PREVIEW_DEBOUNCE_MS_MIN = 300
PREVIEW_DEBOUNCE_MS_MAX_ADD = 500
PREVIEW_DEBOUNCE_MS_CHARS_PER_STEP = 400


def normalize_theme(name: str) -> str:
    name = (name or "").strip().lower()
    return name if name in ("dark", "light") else "dark"


def normalize_graph_mode(mode: str, depth: int | str) -> tuple[str, int]:
    mode = (mode or "").strip().lower()
    if mode not in ("global", "local"):
        mode = "global"
    try:
        depth_i = int(depth)
    except Exception:
        depth_i = 1
    depth_i = 2 if depth_i >= 2 else 1
    return mode, depth_i
