from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
from PIL import ImageFont


@dataclass(frozen=True)
class PaletteEntry:
    emoji: str
    mean_rgb: np.ndarray
    brightness: float
    saturation: float
    alpha_coverage: float


@dataclass(frozen=True)
class PaletteMatcher:
    rgb: np.ndarray
    brightness: np.ndarray
    saturation: np.ndarray
    alpha: np.ndarray
    emojis: tuple[str, ...]


@dataclass(frozen=True)
class EmojiRenderSource:
    kind: str
    font: ImageFont.FreeTypeFont | None = None


@dataclass(frozen=True)
class RenderEstimate:
    seconds: float
    sample_count: int
    confidence: str


@dataclass(frozen=True)
class RenderResult:
    output_path: str
    duration_seconds: float
    total_cells: int
    filled_cells: int
    columns: int
    rows: int
    palette_size: int
    emoji_size: int
    emoji_source: str


@dataclass(frozen=True)
class VideoResult:
    output_path: str
    duration_seconds: float
    frame_count: int
    fps: int
    start_columns: int
    max_columns: int
    step_columns: int
    canvas_width: int
    canvas_height: int


@dataclass(frozen=True)
class VideoToVideoResult:
    output_path: str
    duration_seconds: float
    frame_count: int
    fps: int
    columns: int
    rows: int
    canvas_width: int
    canvas_height: int


ProgressCallback = Callable[[float, str], None]
