import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from preview_timing import compute_preview_debounce_ms
from app_helpers import (
    PREVIEW_DEBOUNCE_MS_DEFAULT,
    PREVIEW_DEBOUNCE_MS_MIN,
    PREVIEW_DEBOUNCE_MS_MAX_ADD,
    PREVIEW_DEBOUNCE_MS_CHARS_PER_STEP,
)

def test_compute_preview_debounce_ms():

    assert compute_preview_debounce_ms(
        txt_len=-1,
        min_ms=PREVIEW_DEBOUNCE_MS_MIN,
        max_add_ms=PREVIEW_DEBOUNCE_MS_MAX_ADD,
        chars_per_step=PREVIEW_DEBOUNCE_MS_CHARS_PER_STEP,
        default_ms=PREVIEW_DEBOUNCE_MS_DEFAULT,
    ) == 350

    assert compute_preview_debounce_ms(
        txt_len=100,
        min_ms=PREVIEW_DEBOUNCE_MS_MIN,
        max_add_ms=PREVIEW_DEBOUNCE_MS_MAX_ADD,
        chars_per_step=PREVIEW_DEBOUNCE_MS_CHARS_PER_STEP,
        default_ms=PREVIEW_DEBOUNCE_MS_DEFAULT,
    ) == 300

    assert compute_preview_debounce_ms(
        txt_len=400,
        min_ms=PREVIEW_DEBOUNCE_MS_MIN,
        max_add_ms=PREVIEW_DEBOUNCE_MS_MAX_ADD,
        chars_per_step=PREVIEW_DEBOUNCE_MS_CHARS_PER_STEP,
        default_ms=PREVIEW_DEBOUNCE_MS_DEFAULT,
    ) == 600

    assert compute_preview_debounce_ms(
        txt_len=700,
        min_ms=PREVIEW_DEBOUNCE_MS_MIN,
        max_add_ms=PREVIEW_DEBOUNCE_MS_MAX_ADD,
        chars_per_step=PREVIEW_DEBOUNCE_MS_CHARS_PER_STEP,
        default_ms=PREVIEW_DEBOUNCE_MS_DEFAULT,
    ) == 800
