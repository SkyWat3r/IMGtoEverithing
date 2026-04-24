from __future__ import annotations

from pathlib import Path
from typing import List, Sequence, Tuple

import numpy as np
from PIL import Image, ImageColor

from .errors import fail
from .models import EmojiRenderSource, PaletteMatcher, ProgressCallback
from .palette import render_single_emoji_tile


def load_image(path: str) -> Image.Image:
    image_path = Path(path)
    if not image_path.is_file():
        fail(f"Input image does not exist: {image_path}")
    try:
        return Image.open(image_path).convert("RGBA")
    except OSError as exc:
        fail(f"Could not open input image '{image_path}': {exc}")
        raise AssertionError("unreachable")


def parse_background(value: str) -> Tuple[int, int, int, int]:
    if value.lower() == "transparent":
        return (0, 0, 0, 0)
    try:
        rgb = ImageColor.getrgb(value)
    except ValueError as exc:
        fail(f"Invalid background color '{value}': {exc}")
        raise AssertionError("unreachable")
    return (rgb[0], rgb[1], rgb[2], 255)


def compute_grid_size(
    width: int,
    height: int,
    columns: int | None,
    rows: int | None,
    scale: float,
    stretch: bool,
) -> Tuple[int, int]:
    if scale <= 0:
        fail("--scale must be greater than 0.")
    aspect_ratio = width / height
    if columns is not None and columns <= 0:
        fail("--columns must be greater than 0.")
    if rows is not None and rows <= 0:
        fail("--rows must be greater than 0.")
    if columns is not None and rows is not None:
        if stretch:
            return columns, rows
        fail("Using both --columns and --rows changes the aspect ratio. Add --stretch to allow that.")
    if columns is not None:
        return columns, max(1, int(round(columns / aspect_ratio)))
    if rows is not None:
        return max(1, int(round(rows * aspect_ratio))), rows
    base_columns = max(1, int(round((width / 8.0) * scale)))
    return base_columns, max(1, int(round(base_columns / aspect_ratio)))


def resize_for_grid(image: Image.Image, columns: int, rows: int) -> Image.Image:
    return image.resize((columns, rows), Image.Resampling.BOX)


def build_frame_sequence(start_columns: int, max_columns: int, step_columns: int) -> List[int]:
    if start_columns <= 0:
        fail("--video-start-columns must be greater than 0.")
    if max_columns < start_columns:
        fail("--video-max-columns must be greater than or equal to --video-start-columns.")
    if step_columns <= 0:
        fail("--video-step-columns must be greater than 0.")
    sequence = list(range(start_columns, max_columns + 1, step_columns))
    if sequence[-1] != max_columns:
        sequence.append(max_columns)
    return sequence


def pad_frame_to_size(frame: Image.Image, width: int, height: int, background: Tuple[int, int, int, int]) -> Image.Image:
    if frame.width == width and frame.height == height:
        return frame
    canvas = Image.new("RGBA", (width, height), background)
    x = (width - frame.width) // 2
    y = (height - frame.height) // 2
    canvas.alpha_composite(frame, (x, y))
    return canvas


def build_emoji_grid(sampled_image: Image.Image, palette_matcher: PaletteMatcher, alpha_threshold: float) -> List[List[str | None]]:
    chunk_size = 2048
    image_array = np.asarray(sampled_image, dtype=np.uint8)
    rows, columns = image_array.shape[:2]
    alpha = image_array[..., 3].astype(np.float32) / 255.0
    coverage = (alpha > 0.01).astype(np.float32)
    alpha_coverage = np.maximum(coverage, alpha)
    rgb = image_array[..., :3].astype(np.float32)
    mean_rgb = np.divide(rgb * alpha[..., None], alpha[..., None], out=np.zeros_like(rgb), where=alpha[..., None] > 1e-6)
    brightness = (0.299 * mean_rgb[..., 0]) + (0.587 * mean_rgb[..., 1]) + (0.114 * mean_rgb[..., 2])
    rgb_norm = mean_rgb / 255.0
    max_value = np.max(rgb_norm, axis=2)
    min_value = np.min(rgb_norm, axis=2)
    saturation = np.divide(max_value - min_value, max_value, out=np.zeros_like(max_value), where=max_value > 1e-6)
    flat_rgb = mean_rgb.reshape(-1, 3)
    flat_brightness = brightness.reshape(-1)
    flat_saturation = saturation.reshape(-1)
    flat_alpha = alpha_coverage.reshape(-1)
    valid_mask = flat_alpha > alpha_threshold
    flat_grid: List[str | None] = [None] * (rows * columns)
    if np.any(valid_mask):
        valid_rgb = flat_rgb[valid_mask]
        valid_brightness = flat_brightness[valid_mask]
        valid_saturation = flat_saturation[valid_mask]
        valid_alpha = flat_alpha[valid_mask]
        valid_positions = np.flatnonzero(valid_mask)
        valid_features = np.column_stack((valid_rgb.astype(np.float32), valid_brightness.astype(np.float32), valid_saturation.astype(np.float32), valid_alpha.astype(np.float32)))
        unique_features, inverse_indices = np.unique(valid_features, axis=0, return_inverse=True)
        unique_best_indices = np.empty(unique_features.shape[0], dtype=np.int32)
        palette_rgb = palette_matcher.rgb
        palette_brightness = palette_matcher.brightness
        palette_saturation = palette_matcher.saturation
        palette_alpha = palette_matcher.alpha
        for chunk_start in range(0, unique_features.shape[0], chunk_size):
            chunk_end = min(unique_features.shape[0], chunk_start + chunk_size)
            chunk = unique_features[chunk_start:chunk_end]
            rgb_chunk = chunk[:, :3]
            brightness_chunk = chunk[:, 3:4]
            saturation_chunk = chunk[:, 4:5]
            alpha_chunk = chunk[:, 5:6]
            rgb_diff = rgb_chunk[:, None, :] - palette_rgb[None, :, :]
            color_distance = np.sqrt(np.sum(rgb_diff * rgb_diff, axis=2))
            brightness_distance = np.abs(brightness_chunk - palette_brightness[None, :])
            saturation_distance = np.abs(saturation_chunk - palette_saturation[None, :]) * 255.0
            alpha_distance = np.abs(alpha_chunk - palette_alpha[None, :]) * 100.0
            scores = color_distance + (brightness_distance * 0.45) + (saturation_distance * 0.35) + (alpha_distance * 0.25)
            unique_best_indices[chunk_start:chunk_end] = np.argmin(scores, axis=1)
        best_indices = unique_best_indices[inverse_indices]
        for position, palette_index in zip(valid_positions.tolist(), best_indices.tolist()):
            flat_grid[position] = palette_matcher.emojis[palette_index]
    return [flat_grid[row_index * columns : (row_index + 1) * columns] for row_index in range(rows)]


def render_emoji_canvas(
    emoji_grid: Sequence[Sequence[str | None]],
    emoji_size: int,
    render_source: EmojiRenderSource,
    background: Tuple[int, int, int, int],
    tile_cache: dict[str, Image.Image] | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_range: Tuple[float, float] = (0.0, 1.0),
) -> Image.Image:
    rows = len(emoji_grid)
    columns = len(emoji_grid[0]) if rows else 0
    canvas = Image.new("RGBA", (columns * emoji_size, rows * emoji_size), background)
    if tile_cache is None:
        tile_cache = {}
    total_cells = max(1, rows * columns)
    processed_cells = 0
    progress_start, progress_end = progress_range
    for row_index, row in enumerate(emoji_grid):
        for column_index, emoji in enumerate(row):
            if emoji is not None:
                if emoji not in tile_cache:
                    tile_cache[emoji] = render_single_emoji_tile(emoji, emoji_size, render_source)
                canvas.alpha_composite(tile_cache[emoji], (column_index * emoji_size, row_index * emoji_size))
            processed_cells += 1
            if progress_callback is not None and (processed_cells == total_cells or processed_cells % max(1, total_cells // 40) == 0):
                fraction = progress_start + ((progress_end - progress_start) * (processed_cells / total_cells))
                progress_callback(fraction, f"Dessin emojis {processed_cells}/{total_cells}")
    return canvas
