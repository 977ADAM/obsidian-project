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
        if txt_len < 0 or min_ms < 0 or max_add_ms < 0:
            return default_ms
        if chars_per_step <= 0:
            return default_ms

        steps = txt_len // chars_per_step
        add_ms = min(max_add_ms, steps * min_ms)
        return min_ms + add_ms
    except Exception:
        return default_ms
