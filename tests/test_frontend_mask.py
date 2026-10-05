"""Unit test for the pure mask-conversion helper (PIL only, no Streamlit/AWS)."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from frontend.app import strokes_to_mask_png


def _rgba_with_brushed_rect() -> np.ndarray:
    """Build a 40x30 RGBA stroke layer with a brushed (alpha>0) rectangle."""
    # Shape is (height, width, 4) to match drawable-canvas image_data.
    array = np.zeros((30, 40, 4), dtype="uint8")
    # Brush a white, fully-opaque rectangle: rows 5..15, cols 10..20.
    array[5:15, 10:20, :3] = 255
    array[5:15, 10:20, 3] = 255
    return array


def test_strokes_to_mask_png_white_where_brushed_black_elsewhere():
    canvas = _rgba_with_brushed_rect()
    size = (40, 30)  # (width, height) matching the canvas, so no scaling.
    png_bytes = strokes_to_mask_png(canvas, size)

    mask = Image.open(io.BytesIO(png_bytes))
    assert mask.mode in ("L", "1")
    assert mask.size == size

    pixels = np.asarray(mask)
    # Brushed region (PIL indexes [row, col]) is white (255 == repaint).
    assert pixels[7, 12] == 255
    # Untouched region is black (0 == keep).
    assert pixels[0, 0] == 0
    assert pixels[25, 35] == 0
    # Strictly black/white, nothing in between.
    assert set(np.unique(pixels).tolist()) <= {0, 255}


def test_strokes_to_mask_png_resizes_to_requested_size():
    canvas = _rgba_with_brushed_rect()
    target = (80, 60)  # upscaled 2x
    png_bytes = strokes_to_mask_png(canvas, target)

    mask = Image.open(io.BytesIO(png_bytes))
    assert mask.size == target
    pixels = np.asarray(mask)
    # Nearest-neighbor resize keeps the mask strictly black/white.
    assert set(np.unique(pixels).tolist()) <= {0, 255}
    # Some pixels are repaint (white) and some are keep (black).
    assert pixels.max() == 255
    assert pixels.min() == 0
