def compute_preview_debounce_ms(
    txt_len: int,
    *,
    min_ms: int,
    max_add_ms: int,
    chars_per_step: int,
    default_ms: int,
) -> int:
    """
    Pure helper: debounce for preview render.
    Larger notes => render less frequently.
    """
    try:
        if chars_per_step <= 0:
            return default_ms
        steps = txt_len // chars_per_step
        return min_ms + min(max_add_ms, steps)
    except Exception:
        return default_ms
