#!/usr/bin/env python3
"""
Convert an input image into an emoji mosaic image.

Example:
    python emoji_maker.py --input photo.png --output result.png --columns 120
"""

from __future__ import annotations

import argparse
import tempfile
import json
import math
import os
import re
import shutil
import time
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import List, Sequence, Tuple

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFont

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ModuleNotFoundError:
    tk = None
    filedialog = None
    messagebox = None
    ttk = None


DEFAULT_PALETTE = [
    "⬛", "⬜", "🟥", "🟧", "🟨", "🟩", "🟦", "🟪", "🟫",
    "❤️", "🧡", "💛", "💚", "💙", "💜",
    "🍎", "🍊", "🍋", "🥝", "🫐", "🍇", "🍓",
    "🌸", "🌻", "🌲", "🌊", "☁️", "🌙", "⭐",
    "🔥", "⚡", "❄️", "🪨", "🥥", "🧠", "🐸",
]

COMMON_EMOJI_FONTS = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/google-noto-color-emoji-fonts/Noto-COLRv1.ttf",
    "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
    "/usr/share/fonts/TTF/NotoColorEmoji.ttf",
    "/usr/share/fonts/emoji/NotoColorEmoji.ttf",
    "/usr/local/share/fonts/NotoColorEmoji.ttf",
    str(Path.home() / ".local/share/fonts/NotoColorEmoji.ttf"),
    str(Path.home() / ".fonts/NotoColorEmoji.ttf"),
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/System/Library/Fonts/Apple Color Emoji.ttf",
    "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
    "/usr/share/fonts/truetype/ancient-scripts/Symbola.ttf",
    "C:/Windows/Fonts/seguiemj.ttf",
]

COMMON_EMOJI_FONT_FAMILIES = [
    "Noto Color Emoji",
    "Apple Color Emoji",
    "Segoe UI Emoji",
    "Twitter Color Emoji",
    "EmojiOne Color",
    "Symbola",
]

PALETTE_SPLIT_RE = re.compile(r"[\s,]+")
TWEMOJI_BASE_URL = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72"
TWEMOJI_CACHE_DIR = Path(".emoji_cache") / "twemoji" / "72x72"
RUN_HISTORY_PATH = Path(".emoji_cache") / "render_history.json"
MAX_HISTORY_ENTRIES = 200


@dataclass(frozen=True)
class PaletteEntry:
    emoji: str
    mean_rgb: np.ndarray
    brightness: float
    saturation: float
    alpha_coverage: float


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
    output_path: Path
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
    output_path: Path
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
    output_path: Path
    duration_seconds: float
    frame_count: int
    fps: int
    columns: int
    rows: int
    canvas_width: int
    canvas_height: int


class EmojiMakerError(RuntimeError):
    pass


def fail(message: str) -> None:
    raise EmojiMakerError(message)


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds:.2f} s"
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes = int(seconds // 60)
    remaining_seconds = int(round(seconds - (minutes * 60)))
    return f"{minutes} min {remaining_seconds} s"


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def has_ffprobe() -> bool:
    return shutil.which("ffprobe") is not None


def build_mp4_encode_command(
    *,
    framerate: int,
    input_pattern: str,
    output_path: str,
) -> List[str]:
    return [
        "ffmpeg",
        "-y",
        "-framerate",
        str(framerate),
        "-i",
        input_pattern,
        "-vf",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        output_path,
    ]


def ensure_history_dir() -> None:
    RUN_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_render_history() -> List[dict]:
    if not RUN_HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(RUN_HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def save_render_history(history: Sequence[dict]) -> None:
    ensure_history_dir()
    trimmed_history = list(history)[-MAX_HISTORY_ENTRIES:]
    RUN_HISTORY_PATH.write_text(
        json.dumps(trimmed_history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_render_profile(
    *,
    columns: int,
    rows: int,
    palette_size: int,
    emoji_size: int,
    emoji_source: str,
) -> dict[str, int | str]:
    return {
        "columns": columns,
        "rows": rows,
        "total_cells": columns * rows,
        "palette_size": palette_size,
        "emoji_size": emoji_size,
        "emoji_source": emoji_source,
    }


def estimate_duration(profile: dict[str, int | str]) -> RenderEstimate:
    history = load_render_history()
    if not history:
        total_cells = int(profile["total_cells"])
        emoji_source = str(profile["emoji_source"])
        baseline_fixed = 0.35 if emoji_source == "font" else 0.8
        baseline_per_cell = 0.0002 if emoji_source == "font" else 0.00045
        return RenderEstimate(
            seconds=baseline_fixed + (total_cells * baseline_per_cell),
            sample_count=0,
            confidence="faible",
        )

    scored_history: List[Tuple[float, dict]] = []
    for item in history:
        if item.get("duration_seconds", 0) <= 0 or item.get("total_cells", 0) <= 0:
            continue
        distance = 0.0
        if item.get("emoji_source") != profile["emoji_source"]:
            distance += 4.0
        distance += abs(int(item.get("total_cells", 0)) - int(profile["total_cells"])) / max(1, int(profile["total_cells"]))
        distance += abs(int(item.get("palette_size", 0)) - int(profile["palette_size"])) / max(1, int(profile["palette_size"]))
        distance += abs(int(item.get("emoji_size", 0)) - int(profile["emoji_size"])) / max(1, int(profile["emoji_size"]))
        scored_history.append((distance, item))

    if not scored_history:
        total_cells = int(profile["total_cells"])
        return RenderEstimate(seconds=0.5 + (total_cells * 0.0003), sample_count=0, confidence="faible")

    scored_history.sort(key=lambda item: item[0])
    nearest = scored_history[: min(12, len(scored_history))]

    weighted_seconds_per_cell = 0.0
    total_weight = 0.0
    for distance, item in nearest:
        cells = max(1, int(item["total_cells"]))
        seconds_per_cell = float(item["duration_seconds"]) / cells
        weight = 1.0 / (0.25 + distance)
        weighted_seconds_per_cell += seconds_per_cell * weight
        total_weight += weight

    avg_seconds_per_cell = weighted_seconds_per_cell / max(total_weight, 1e-6)
    fixed_overhead = 0.15 if str(profile["emoji_source"]) == "font" else 0.35
    estimated_seconds = fixed_overhead + (int(profile["total_cells"]) * avg_seconds_per_cell)

    sample_count = len(nearest)
    if sample_count >= 8:
        confidence = "élevée"
    elif sample_count >= 4:
        confidence = "moyenne"
    else:
        confidence = "faible"

    return RenderEstimate(
        seconds=max(0.1, estimated_seconds),
        sample_count=sample_count,
        confidence=confidence,
    )


def append_render_history(result: RenderResult) -> None:
    history = load_render_history()
    history.append(
        {
            "timestamp": time.time(),
            "duration_seconds": result.duration_seconds,
            "total_cells": result.total_cells,
            "filled_cells": result.filled_cells,
            "columns": result.columns,
            "rows": result.rows,
            "palette_size": result.palette_size,
            "emoji_size": result.emoji_size,
            "emoji_source": result.emoji_source,
        }
    )
    save_render_history(history)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert an image into an emoji mosaic image."
    )
    parser.add_argument("--input", required=True, help="Path to the input image.")
    parser.add_argument("--output", required=True, help="Path to the output PNG image.")
    parser.add_argument(
        "--columns",
        type=int,
        help="Number of emoji cells horizontally. Preserves aspect ratio unless --stretch is used.",
    )
    parser.add_argument(
        "--rows",
        type=int,
        help="Number of emoji cells vertically. Preserves aspect ratio unless --stretch is used.",
    )
    parser.add_argument(
        "--emoji-size",
        type=int,
        default=20,
        help="Rendered emoji cell size in output pixels. Default: 20.",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="Multiplier for automatic grid density when --columns/--rows are omitted. Default: 1.0.",
    )
    parser.add_argument(
        "--palette",
        help=(
            "Custom emoji palette. Either a comma/space separated string like "
            "'😀,😎,🔥' or a text file path containing emojis."
        ),
    )
    parser.add_argument(
        "--background",
        default="transparent",
        help="Background color for empty areas, e.g. transparent, white, black, #112233. Default: transparent.",
    )
    parser.add_argument(
        "--font",
        help="Path to an emoji-capable font file. Recommended when auto-detection does not find one.",
    )
    parser.add_argument(
        "--emoji-source",
        choices=("auto", "font", "twemoji"),
        default="auto",
        help="Emoji rendering backend. 'auto' prefers a local font and falls back to Twemoji PNG assets. Default: auto.",
    )
    parser.add_argument(
        "--stretch",
        action="store_true",
        help="Allow non-proportional resizing when both --columns and --rows are provided.",
    )
    parser.add_argument(
        "--alpha-threshold",
        type=float,
        default=0.05,
        help="Cells with alpha coverage at or below this value are left empty. Range: 0.0 to 1.0.",
    )
    parser.add_argument("--video-output", help="Optional animated output path (.gif or .mp4).")
    parser.add_argument(
        "--video-fps",
        type=int,
        default=5,
        help="FPS for animated output. Default: 5.",
    )
    parser.add_argument(
        "--video-start-columns",
        type=int,
        default=1,
        help="Starting number of columns for the animation. Default: 1.",
    )
    parser.add_argument(
        "--video-max-columns",
        type=int,
        help="Ending number of columns for the animation. Defaults to --columns if provided, otherwise 500.",
    )
    parser.add_argument(
        "--video-step-columns",
        type=int,
        default=2,
        help="Column increment between frames for the animation. Default: 2.",
    )
    return parser.parse_args()


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


def parse_palette(palette_arg: str | None) -> List[str]:
    if palette_arg is None:
        palette = list(DEFAULT_PALETTE)
    else:
        candidate_path = Path(palette_arg)
        if candidate_path.is_file():
            content = candidate_path.read_text(encoding="utf-8").strip()
            tokens = [
                token.strip()
                for token in PALETTE_SPLIT_RE.split(content)
                if token.strip()
            ]
            palette = tokens
        else:
            tokens = [
                token.strip()
                for token in PALETTE_SPLIT_RE.split(palette_arg)
                if token.strip()
            ]
            palette = tokens

    unique_palette = []
    seen = set()
    for emoji in palette:
        if emoji not in seen:
            unique_palette.append(emoji)
            seen.add(emoji)

    if not unique_palette:
        fail("Palette is empty. Provide at least one emoji.")
    return unique_palette


def fontconfig_match(family: str) -> str | None:
    if shutil.which("fc-match") is None:
        return None

    try:
        result = subprocess.run(
            ["fc-match", "--format=%{family}\n%{file}\n", family],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    matched_family = lines[0].lower()
    matched_path = lines[1]
    matched_name = Path(matched_path).name.lower()
    if (
        "emoji" not in matched_name
        and "symbola" not in matched_name
        and "emoji" not in matched_family
        and "symbola" not in matched_family
    ):
        return None

    matched_file = Path(matched_path)
    if not matched_file.exists():
        return None
    return str(matched_file)


def build_font_search_candidates(font_path: str | None) -> List[str]:
    if font_path:
        return [font_path]

    search_candidates: List[str] = []
    seen = set()

    for candidate in COMMON_EMOJI_FONTS:
        if candidate not in seen:
            search_candidates.append(candidate)
            seen.add(candidate)

    for family in COMMON_EMOJI_FONT_FAMILIES:
        matched_path = fontconfig_match(family)
        if matched_path and matched_path not in seen:
            search_candidates.append(matched_path)
            seen.add(matched_path)
        if family not in seen:
            search_candidates.append(family)
            seen.add(family)

    return search_candidates


def probe_font_emoji(font: ImageFont.ImageFont) -> Tuple[bool, bool]:
    probe = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    test_emoji = "😀"
    try:
        draw.text((0, 0), test_emoji, font=font, embedded_color=True)
    except TypeError:
        draw.text((0, 0), test_emoji, font=font, fill=(255, 255, 255, 255))
    if probe.getbbox() is None:
        return False, False

    colors = probe.getcolors(maxcolors=4096) or []
    has_non_gray = any(
        alpha > 0 and not (red == green == blue)
        for _, (red, green, blue, alpha) in colors
    )
    return True, has_non_gray


def find_emoji_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont:
    search_paths: Sequence[str] = build_font_search_candidates(font_path)
    last_error: Exception | None = None
    attempted: List[str] = []

    for candidate in search_paths:
        if not candidate:
            continue
        try:
            if Path(candidate).exists() or os.path.sep not in candidate:
                attempted.append(candidate)
                font = ImageFont.truetype(candidate, size=size)
                renders, _has_color = probe_font_emoji(font)
                if renders:
                    return font
        except OSError as exc:
            last_error = exc
            attempted.append(candidate)

    help_text = (
        "Could not load an emoji-capable font. "
        "Pass --font /path/to/emoji_font.ttf (for example NotoColorEmoji.ttf, "
        "Apple Color Emoji.ttc, or seguiemj.ttf)."
    )
    if not font_path:
        help_text += (
            " On Linux, install an emoji font such as the Noto Color Emoji package "
            "and retry."
        )
        if attempted:
            help_text += f" Checked: {', '.join(attempted[:8])}"
    if font_path:
        fail(f"{help_text} Failed font: {font_path}")
    if last_error:
        fail(f"{help_text} Last error: {last_error}")
    fail(help_text)
    raise AssertionError("unreachable")


def try_find_emoji_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont | None:
    try:
        return find_emoji_font(font_path, size)
    except EmojiMakerError:
        return None


def emoji_codepoint_candidates(emoji: str) -> List[str]:
    codepoints = [ord(char) for char in emoji]
    exact = "-".join(f"{codepoint:x}" for codepoint in codepoints)
    without_fe0f = "-".join(f"{codepoint:x}" for codepoint in codepoints if codepoint != 0xFE0F)
    candidates = []
    for candidate in (exact, without_fe0f):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def fetch_twemoji_tile(emoji: str, tile_size: int) -> Image.Image:
    TWEMOJI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    errors: List[str] = []

    for codepoint_name in emoji_codepoint_candidates(emoji):
        cache_path = TWEMOJI_CACHE_DIR / f"{codepoint_name}.png"
        if cache_path.exists():
            try:
                tile = Image.open(cache_path).convert("RGBA")
                return tile.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
            except OSError as exc:
                errors.append(f"{cache_path}: {exc}")

        url = f"{TWEMOJI_BASE_URL}/{codepoint_name}.png"
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "emoji_maker/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                data = response.read()
        except urllib.error.URLError as exc:
            errors.append(f"{url}: {exc}")
            continue

        try:
            cache_path.write_bytes(data)
            tile = Image.open(BytesIO(data)).convert("RGBA")
            return tile.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
        except OSError as exc:
            errors.append(f"{url}: {exc}")

    fail(
        f"Could not fetch emoji asset for {emoji!r} from Twemoji. "
        f"Tried: {', '.join(emoji_codepoint_candidates(emoji))}. "
        f"Last errors: {'; '.join(errors[-2:]) if errors else 'none'}"
    )
    raise AssertionError("unreachable")


def resolve_render_source(
    font_path: str | None,
    emoji_size: int,
    emoji_source: str,
) -> EmojiRenderSource:
    if emoji_source in ("auto", "font"):
        font = try_find_emoji_font(font_path, size=max(12, int(emoji_size * 0.9)))
        if font is not None:
            _renders, has_color = probe_font_emoji(font)
            if emoji_source == "font" or has_color:
                return EmojiRenderSource(kind="font", font=font)
        if emoji_source == "font":
            find_emoji_font(font_path, size=max(12, int(emoji_size * 0.9)))
    return EmojiRenderSource(kind="twemoji")


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
        computed_rows = max(1, int(round(columns / aspect_ratio)))
        return columns, computed_rows

    if rows is not None:
        computed_columns = max(1, int(round(rows * aspect_ratio)))
        return computed_columns, rows

    base_columns = max(1, int(round((width / 8.0) * scale)))
    computed_rows = max(1, int(round(base_columns / aspect_ratio)))
    return base_columns, computed_rows


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


def get_video_metadata(video_path: str) -> dict[str, float | int]:
    candidate = Path(video_path)
    if not candidate.is_file():
        fail(f"Input video does not exist: {candidate}")
    if not has_ffprobe():
        fail("ffprobe is required for video-to-video mode. Install ffmpeg/ffprobe and retry.")

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=width,height,avg_frame_rate,nb_read_frames,duration",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(candidate),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f"ffprobe failed to inspect '{candidate}': {result.stderr.strip()}")

    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        fail(f"Could not parse ffprobe output for '{candidate}': {exc}")
        raise AssertionError("unreachable")

    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    duration_raw = stream.get("duration") or payload.get("format", {}).get("duration") or 0
    duration = float(duration_raw or 0.0)
    frame_rate_raw = str(stream.get("avg_frame_rate") or "0/0")
    try:
        numerator, denominator = frame_rate_raw.split("/", 1)
        fps = float(numerator) / float(denominator) if float(denominator) != 0 else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    frame_count_raw = stream.get("nb_read_frames")
    if frame_count_raw not in (None, "N/A"):
        frame_count = int(frame_count_raw)
    else:
        frame_count = int(round(duration * fps)) if duration > 0 and fps > 0 else 0

    if width <= 0 or height <= 0:
        fail(f"Could not determine video size for '{candidate}'.")
    if duration <= 0:
        fail(f"Could not determine duration for '{candidate}'.")

    return {
        "width": width,
        "height": height,
        "duration": duration,
        "fps": fps,
        "frame_count": max(1, frame_count),
    }


def render_canvas_for_grid(
    image: Image.Image,
    background: Tuple[int, int, int, int],
    render_source: EmojiRenderSource,
    palette_entries: Sequence[PaletteEntry],
    emoji_size: int,
    alpha_threshold: float,
    columns: int,
    rows: int,
) -> tuple[Image.Image, int]:
    sampled_image = resize_for_grid(image, columns, rows)
    emoji_grid = build_emoji_grid(sampled_image, palette_entries, alpha_threshold)
    canvas = render_emoji_canvas(emoji_grid, emoji_size, render_source, background)
    filled_cells = sum(1 for row in emoji_grid for emoji in row if emoji is not None)
    return canvas, filled_cells


def render_video_with_args(
    args: argparse.Namespace,
    video_output: str,
    video_fps: int,
    video_start_columns: int,
    video_max_columns: int,
    video_step_columns: int,
) -> VideoResult:
    if video_fps <= 0:
        fail("--video-fps must be greater than 0.")

    image, background, palette, _columns, _rows, render_source = prepare_render(args)
    palette_entries = build_palette_entries(palette, args.emoji_size, render_source)
    frame_columns = build_frame_sequence(video_start_columns, video_max_columns, video_step_columns)

    frame_specs: List[tuple[int, int]] = []
    max_width = 0
    max_height = 0
    for columns in frame_columns:
        computed_columns, computed_rows = compute_grid_size(
            width=image.width,
            height=image.height,
            columns=columns,
            rows=None,
            scale=args.scale,
            stretch=False,
        )
        frame_specs.append((computed_columns, computed_rows))
        max_width = max(max_width, computed_columns * args.emoji_size)
        max_height = max(max_height, computed_rows * args.emoji_size)

    frames: List[Image.Image] = []
    started_at = time.perf_counter()
    for columns, rows in frame_specs:
        frame, _filled_cells = render_canvas_for_grid(
            image=image,
            background=background,
            render_source=render_source,
            palette_entries=palette_entries,
            emoji_size=args.emoji_size,
            alpha_threshold=args.alpha_threshold,
            columns=columns,
            rows=rows,
        )
        frames.append(pad_frame_to_size(frame, max_width, max_height, background))

    output_path = Path(video_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    suffix = output_path.suffix.lower()
    if suffix == ".mp4":
        if not has_ffmpeg():
            fail("ffmpeg is required to export MP4. Use a .gif output or install ffmpeg.")
        with tempfile.TemporaryDirectory(prefix="emoji_video_") as temp_dir:
            temp_path = Path(temp_dir)
            for index, frame in enumerate(frames):
                frame.convert("RGBA").save(temp_path / f"frame_{index:04d}.png", format="PNG")
            cmd = build_mp4_encode_command(
                framerate=video_fps,
                input_pattern=str(temp_path / "frame_%04d.png"),
                output_path=str(output_path),
            )
            result = subprocess.run(cmd, check=False, capture_output=True, text=True)
            if result.returncode != 0:
                fail(f"ffmpeg failed to create the MP4: {result.stderr.strip()}")
    else:
        duration_ms = max(20, int(round(1000 / video_fps)))
        first_frame, *other_frames = [frame.convert("RGBA") for frame in frames]
        first_frame.save(
            output_path,
            save_all=True,
            append_images=other_frames,
            duration=duration_ms,
            loop=0,
            disposal=2,
            format="GIF",
        )

    return VideoResult(
        output_path=output_path,
        duration_seconds=time.perf_counter() - started_at,
        frame_count=len(frames),
        fps=video_fps,
        start_columns=video_start_columns,
        max_columns=video_max_columns,
        step_columns=video_step_columns,
        canvas_width=max_width,
        canvas_height=max_height,
    )


def build_video_to_video_profile(
    *,
    video_input: str,
    args: argparse.Namespace,
) -> tuple[dict[str, int | float | str], dict[str, int | str], int]:
    metadata = get_video_metadata(video_input)
    background = parse_background(args.background)
    _ = background
    palette = parse_palette(args.palette)
    render_source = resolve_render_source(args.font, args.emoji_size, args.emoji_source)
    columns, rows = compute_grid_size(
        width=int(metadata["width"]),
        height=int(metadata["height"]),
        columns=args.columns,
        rows=args.rows,
        scale=args.scale,
        stretch=args.stretch,
    )
    output_fps = args.video_fps
    if output_fps <= 0:
        fail("--video-fps must be greater than 0.")
    estimated_frame_count = max(1, int(round(float(metadata["duration"]) * output_fps)))
    profile = build_render_profile(
        columns=columns,
        rows=rows,
        palette_size=len(palette),
        emoji_size=args.emoji_size,
        emoji_source=render_source.kind,
    )
    return metadata, profile, estimated_frame_count


def estimate_video_to_video_for_args(args: argparse.Namespace, video_input: str) -> tuple[float, int, dict[str, int | str]]:
    metadata, profile, estimated_frame_count = build_video_to_video_profile(video_input=video_input, args=args)
    per_frame_estimate = estimate_duration(profile)
    total_seconds = (per_frame_estimate.seconds * estimated_frame_count) + max(1.5, estimated_frame_count * 0.02)
    _ = metadata
    return total_seconds, estimated_frame_count, profile


def render_video_to_video_with_args(
    args: argparse.Namespace,
    video_input: str,
    video_output: str,
) -> VideoToVideoResult:
    if not has_ffmpeg():
        fail("ffmpeg is required for video-to-video mode. Install ffmpeg and retry.")
    metadata, _profile, _estimated_frame_count = build_video_to_video_profile(video_input=video_input, args=args)
    background = parse_background(args.background)
    palette = parse_palette(args.palette)
    render_source = resolve_render_source(args.font, args.emoji_size, args.emoji_source)
    columns, rows = compute_grid_size(
        width=int(metadata["width"]),
        height=int(metadata["height"]),
        columns=args.columns,
        rows=args.rows,
        scale=args.scale,
        stretch=args.stretch,
    )
    output_fps = args.video_fps
    palette_entries = build_palette_entries(palette, args.emoji_size, render_source)
    output_path = Path(video_output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    started_at = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="emoji_v2v_in_") as input_dir, tempfile.TemporaryDirectory(prefix="emoji_v2v_out_") as output_dir:
        input_path = Path(input_dir)
        output_frames_path = Path(output_dir)

        extract_cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_input),
            "-vf",
            f"fps={output_fps}",
            str(input_path / "frame_%06d.png"),
        ]
        extract_result = subprocess.run(extract_cmd, check=False, capture_output=True, text=True)
        if extract_result.returncode != 0:
            fail(f"ffmpeg failed to extract frames from '{video_input}': {extract_result.stderr.strip()}")

        extracted_frames = sorted(input_path.glob("frame_*.png"))
        if not extracted_frames:
            fail(f"No frames were extracted from '{video_input}'.")
        actual_frame_count = len(extracted_frames)

        canvas_width = columns * args.emoji_size
        canvas_height = rows * args.emoji_size
        for index, frame_path in enumerate(extracted_frames):
            frame_image = Image.open(frame_path).convert("RGBA")
            canvas, _filled_cells = render_canvas_for_grid(
                image=frame_image,
                background=background,
                render_source=render_source,
                palette_entries=palette_entries,
                emoji_size=args.emoji_size,
                alpha_threshold=args.alpha_threshold,
                columns=columns,
                rows=rows,
            )
            canvas.save(output_frames_path / f"frame_{index:06d}.png", format="PNG")

        silent_video_path = output_frames_path / "rendered_video.mp4"
        encode_cmd = build_mp4_encode_command(
            framerate=output_fps,
            input_pattern=str(output_frames_path / "frame_%06d.png"),
            output_path=str(silent_video_path),
        )
        encode_result = subprocess.run(encode_cmd, check=False, capture_output=True, text=True)
        if encode_result.returncode != 0:
            fail(f"ffmpeg failed to encode the rendered frames: {encode_result.stderr.strip()}")

        mux_cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(silent_video_path),
            "-i",
            str(video_input),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        mux_result = subprocess.run(mux_cmd, check=False, capture_output=True, text=True)
        if mux_result.returncode != 0:
            shutil.copy2(silent_video_path, output_path)

    return VideoToVideoResult(
        output_path=output_path,
        duration_seconds=time.perf_counter() - started_at,
        frame_count=actual_frame_count,
        fps=output_fps,
        columns=columns,
        rows=rows,
        canvas_width=columns * args.emoji_size,
        canvas_height=rows * args.emoji_size,
    )


def rgb_to_brightness(rgb: np.ndarray) -> float:
    return float(0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2])


def rgb_to_saturation(rgb: np.ndarray) -> float:
    rgb_norm = rgb.astype(np.float32) / 255.0
    max_value = float(np.max(rgb_norm))
    min_value = float(np.min(rgb_norm))
    if max_value == 0:
        return 0.0
    return (max_value - min_value) / max_value


def compute_cell_average(cell_rgba: np.ndarray) -> Tuple[np.ndarray, float, float, float]:
    alpha = cell_rgba[..., 3].astype(np.float32) / 255.0
    coverage = float(np.mean(alpha > 0.01))
    alpha_sum = float(np.sum(alpha))

    if alpha_sum <= 1e-6:
        return np.zeros(3, dtype=np.float32), 0.0, 0.0, 0.0

    rgb = cell_rgba[..., :3].astype(np.float32)
    weighted_rgb = (rgb * alpha[..., None]).sum(axis=(0, 1)) / alpha_sum
    brightness = rgb_to_brightness(weighted_rgb)
    saturation = rgb_to_saturation(weighted_rgb)
    mean_alpha = float(np.mean(alpha))
    return weighted_rgb, brightness, saturation, max(coverage, mean_alpha)


def get_text_bbox(draw: ImageDraw.ImageDraw, emoji: str, font: ImageFont.ImageFont) -> Tuple[int, int, int, int]:
    try:
        return draw.textbbox((0, 0), emoji, font=font, embedded_color=True)
    except TypeError:
        return draw.textbbox((0, 0), emoji, font=font)


def draw_emoji(
    image: Image.Image,
    position: Tuple[float, float],
    emoji: str,
    font: ImageFont.ImageFont,
) -> None:
    draw = ImageDraw.Draw(image)
    try:
        draw.text(position, emoji, font=font, embedded_color=True)
    except TypeError:
        draw.text(position, emoji, font=font, fill=(255, 255, 255, 255))


def render_single_emoji_tile(
    emoji: str,
    tile_size: int,
    render_source: EmojiRenderSource,
) -> Image.Image:
    if render_source.kind == "twemoji":
        return fetch_twemoji_tile(emoji, tile_size)

    if render_source.font is None:
        fail("Internal error: font render source is missing a font.")
    tile = Image.new("RGBA", (tile_size, tile_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    bbox = get_text_bbox(draw, emoji, render_source.font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (tile_size - text_width) / 2.0 - bbox[0]
    y = (tile_size - text_height) / 2.0 - bbox[1]
    draw_emoji(tile, (x, y), emoji, render_source.font)
    return tile


def build_palette_entries(
    palette: Sequence[str],
    emoji_size: int,
    render_source: EmojiRenderSource,
) -> List[PaletteEntry]:
    entries: List[PaletteEntry] = []

    for emoji in palette:
        tile = render_single_emoji_tile(emoji, emoji_size, render_source)
        tile_array = np.asarray(tile, dtype=np.uint8)
        mean_rgb, brightness, saturation, coverage = compute_cell_average(tile_array)
        entries.append(
            PaletteEntry(
                emoji=emoji,
                mean_rgb=mean_rgb,
                brightness=brightness,
                saturation=saturation,
                alpha_coverage=coverage,
            )
        )
    return entries


def choose_best_emoji(
    mean_rgb: np.ndarray,
    brightness: float,
    saturation: float,
    alpha_coverage: float,
    palette_entries: Sequence[PaletteEntry],
) -> str:
    best_score = math.inf
    best_emoji = palette_entries[0].emoji

    for entry in palette_entries:
        color_distance = float(np.linalg.norm(mean_rgb - entry.mean_rgb))
        brightness_distance = abs(brightness - entry.brightness)
        saturation_distance = abs(saturation - entry.saturation) * 255.0
        alpha_distance = abs(alpha_coverage - entry.alpha_coverage) * 100.0

        score = (
            color_distance * 1.0
            + brightness_distance * 0.45
            + saturation_distance * 0.35
            + alpha_distance * 0.25
        )

        if score < best_score:
            best_score = score
            best_emoji = entry.emoji

    return best_emoji


def build_emoji_grid(
    sampled_image: Image.Image,
    palette_entries: Sequence[PaletteEntry],
    alpha_threshold: float,
) -> List[List[str | None]]:
    image_array = np.asarray(sampled_image, dtype=np.uint8)
    rows, columns = image_array.shape[:2]

    alpha = image_array[..., 3].astype(np.float32) / 255.0
    coverage = (alpha > 0.01).astype(np.float32)
    alpha_coverage = np.maximum(coverage, alpha)

    rgb = image_array[..., :3].astype(np.float32)
    mean_rgb = np.divide(
        rgb * alpha[..., None],
        alpha[..., None],
        out=np.zeros_like(rgb),
        where=alpha[..., None] > 1e-6,
    )
    brightness = (
        (0.299 * mean_rgb[..., 0])
        + (0.587 * mean_rgb[..., 1])
        + (0.114 * mean_rgb[..., 2])
    )

    rgb_norm = mean_rgb / 255.0
    max_value = np.max(rgb_norm, axis=2)
    min_value = np.min(rgb_norm, axis=2)
    saturation = np.divide(
        max_value - min_value,
        max_value,
        out=np.zeros_like(max_value),
        where=max_value > 1e-6,
    )

    palette_rgb = np.stack([entry.mean_rgb for entry in palette_entries], axis=0).astype(np.float32)
    palette_brightness = np.asarray([entry.brightness for entry in palette_entries], dtype=np.float32)
    palette_saturation = np.asarray([entry.saturation for entry in palette_entries], dtype=np.float32)
    palette_alpha = np.asarray([entry.alpha_coverage for entry in palette_entries], dtype=np.float32)
    palette_emojis = [entry.emoji for entry in palette_entries]

    flat_rgb = mean_rgb.reshape(-1, 3)
    flat_brightness = brightness.reshape(-1)
    flat_saturation = saturation.reshape(-1)
    flat_alpha = alpha_coverage.reshape(-1)
    valid_mask = flat_alpha > alpha_threshold

    flat_grid: List[str | None] = [None] * (rows * columns)
    if np.any(valid_mask):
        valid_rgb = flat_rgb[valid_mask]
        valid_brightness = flat_brightness[valid_mask][:, None]
        valid_saturation = flat_saturation[valid_mask][:, None]
        valid_alpha = flat_alpha[valid_mask][:, None]

        color_distance = np.linalg.norm(valid_rgb[:, None, :] - palette_rgb[None, :, :], axis=2)
        brightness_distance = np.abs(valid_brightness - palette_brightness[None, :])
        saturation_distance = np.abs(valid_saturation - palette_saturation[None, :]) * 255.0
        alpha_distance = np.abs(valid_alpha - palette_alpha[None, :]) * 100.0

        scores = (
            color_distance
            + (brightness_distance * 0.45)
            + (saturation_distance * 0.35)
            + (alpha_distance * 0.25)
        )
        best_indices = np.argmin(scores, axis=1)
        valid_positions = np.flatnonzero(valid_mask)
        for position, palette_index in zip(valid_positions.tolist(), best_indices.tolist()):
            flat_grid[position] = palette_emojis[palette_index]

    return [flat_grid[row_index * columns : (row_index + 1) * columns] for row_index in range(rows)]


def render_emoji_canvas(
    emoji_grid: Sequence[Sequence[str | None]],
    emoji_size: int,
    render_source: EmojiRenderSource,
    background: Tuple[int, int, int, int],
) -> Image.Image:
    rows = len(emoji_grid)
    columns = len(emoji_grid[0]) if rows else 0
    canvas = Image.new(
        "RGBA",
        (columns * emoji_size, rows * emoji_size),
        background,
    )

    # Cache pre-rendered emoji tiles so repeated emojis are pasted instead of redrawn.
    tile_cache: dict[str, Image.Image] = {}

    for y, row in enumerate(emoji_grid):
        for x, emoji in enumerate(row):
            if emoji is None:
                continue
            if emoji not in tile_cache:
                tile_cache[emoji] = render_single_emoji_tile(emoji, emoji_size, render_source)
            tile = tile_cache[emoji]
            canvas.alpha_composite(tile, (x * emoji_size, y * emoji_size))

    return canvas


def validate_args(args: argparse.Namespace) -> None:
    if args.emoji_size <= 0:
        fail("--emoji-size must be greater than 0.")
    if args.alpha_threshold < 0.0 or args.alpha_threshold > 1.0:
        fail("--alpha-threshold must be between 0.0 and 1.0.")

    output_path = Path(args.output)
    if output_path.exists() and output_path.is_dir():
        fail(f"Output path points to a directory: {output_path}")
    if output_path.parent and not output_path.parent.exists():
        fail(f"Output directory does not exist: {output_path.parent}")
    if getattr(args, "video_output", None):
        video_output_path = Path(args.video_output)
        if video_output_path.exists() and video_output_path.is_dir():
            fail(f"Video output path points to a directory: {video_output_path}")
        if video_output_path.parent and not video_output_path.parent.exists():
            fail(f"Video output directory does not exist: {video_output_path.parent}")
    if getattr(args, "video_to_video_input", None):
        video_input_path = Path(args.video_to_video_input)
        if not video_input_path.is_file():
            fail(f"Video input does not exist: {video_input_path}")
    if getattr(args, "video_to_video_output", None):
        video_to_video_output_path = Path(args.video_to_video_output)
        if video_to_video_output_path.exists() and video_to_video_output_path.is_dir():
            fail(f"Video-to-video output path points to a directory: {video_to_video_output_path}")
        if video_to_video_output_path.parent and not video_to_video_output_path.parent.exists():
            fail(f"Video-to-video output directory does not exist: {video_to_video_output_path.parent}")


def prepare_render(args: argparse.Namespace) -> tuple[Image.Image, Tuple[int, int, int, int], List[str], int, int, EmojiRenderSource]:
    validate_args(args)

    image = load_image(args.input)
    background = parse_background(args.background)
    palette = parse_palette(args.palette)

    columns, rows = compute_grid_size(
        width=image.width,
        height=image.height,
        columns=args.columns,
        rows=args.rows,
        scale=args.scale,
        stretch=args.stretch,
    )

    render_source = resolve_render_source(args.font, args.emoji_size, args.emoji_source)
    return image, background, palette, columns, rows, render_source


def run_with_args(args: argparse.Namespace) -> RenderResult:
    image, background, palette, columns, rows, render_source = prepare_render(args)
    started_at = time.perf_counter()
    sampled_image = resize_for_grid(image, columns, rows)
    palette_entries = build_palette_entries(palette, args.emoji_size, render_source)
    emoji_grid = build_emoji_grid(sampled_image, palette_entries, args.alpha_threshold)
    canvas = render_emoji_canvas(emoji_grid, args.emoji_size, render_source, background)

    output_path = Path(args.output)
    try:
        canvas.save(output_path, format="PNG")
    except OSError as exc:
        fail(f"Could not save output image '{output_path}': {exc}")
    duration_seconds = time.perf_counter() - started_at
    total_cells = columns * rows
    filled_cells = sum(1 for row in emoji_grid for emoji in row if emoji is not None)
    result = RenderResult(
        output_path=output_path,
        duration_seconds=duration_seconds,
        total_cells=total_cells,
        filled_cells=filled_cells,
        columns=columns,
        rows=rows,
        palette_size=len(palette),
        emoji_size=args.emoji_size,
        emoji_source=render_source.kind,
    )
    append_render_history(result)
    return result


def build_gui_args(values: dict[str, str | bool]) -> argparse.Namespace:
    def parse_optional_int(name: str) -> int | None:
        value = str(values[name]).strip()
        return int(value) if value else None

    def parse_float(name: str) -> float:
        value = str(values[name]).strip()
        return float(value)

    palette_value = str(values["palette"]).strip()
    font_value = str(values["font"]).strip()

    return argparse.Namespace(
        input=str(values["input"]).strip(),
        output=str(values["output"]).strip(),
        video_to_video_input=str(values["video_to_video_input"]).strip() or None,
        video_to_video_output=str(values["video_to_video_output"]).strip() or None,
        columns=parse_optional_int("columns"),
        rows=parse_optional_int("rows"),
        emoji_size=int(str(values["emoji_size"]).strip()),
        scale=parse_float("scale"),
        palette=palette_value or None,
        background=str(values["background"]).strip(),
        font=font_value or None,
        emoji_source=str(values["emoji_source"]).strip(),
        stretch=bool(values["stretch"]),
        alpha_threshold=parse_float("alpha_threshold"),
        video_output=str(values["video_output"]).strip() or None,
        video_fps=int(str(values["video_fps"]).strip()),
        video_start_columns=int(str(values["video_start_columns"]).strip()),
        video_max_columns=parse_optional_int("video_max_columns"),
        video_step_columns=int(str(values["video_step_columns"]).strip()),
    )


def estimate_for_args(args: argparse.Namespace) -> tuple[RenderEstimate, dict[str, int | str]]:
    image, _background, palette, columns, rows, render_source = prepare_render(args)
    _ = image
    profile = build_render_profile(
        columns=columns,
        rows=rows,
        palette_size=len(palette),
        emoji_size=args.emoji_size,
        emoji_source=render_source.kind,
    )
    return estimate_duration(profile), profile


def estimate_video_for_args(args: argparse.Namespace) -> tuple[float, int]:
    image, _background, palette, _columns, _rows, render_source = prepare_render(args)
    _ = image
    max_columns = args.video_max_columns if args.video_max_columns is not None else (args.columns if args.columns is not None else 500)
    frame_columns = build_frame_sequence(args.video_start_columns, max_columns, args.video_step_columns)
    total_seconds = 0.0
    for columns in frame_columns:
        computed_columns, computed_rows = compute_grid_size(
            width=image.width,
            height=image.height,
            columns=columns,
            rows=None,
            scale=args.scale,
            stretch=False,
        )
        profile = build_render_profile(
            columns=computed_columns,
            rows=computed_rows,
            palette_size=len(palette),
            emoji_size=args.emoji_size,
            emoji_source=render_source.kind,
        )
        total_seconds += estimate_duration(profile).seconds
    return total_seconds, len(frame_columns)


def launch_gui() -> None:
    if tk is None or filedialog is None or messagebox is None or ttk is None:
        fail(
            "Tkinter is not available in this Python environment. "
            "Install python3-tkinter/python3-tk or run the script with CLI parameters."
        )

    root = tk.Tk()
    root.title("imgEMOJI")
    root.geometry("980x700")
    root.minsize(820, 560)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg="#f3f6fb")
    style.configure("App.TFrame", background="#f3f6fb")
    style.configure("Hero.TLabel", background="#f3f6fb", font=("TkDefaultFont", 18, "bold"))
    style.configure("Muted.TLabel", background="#f3f6fb", foreground="#516074")
    style.configure("Section.TLabelframe", background="#ffffff")
    style.configure("Section.TLabelframe.Label", background="#ffffff", font=("TkDefaultFont", 10, "bold"))
    style.configure("Card.TFrame", background="#ffffff")
    style.configure("FieldTitle.TLabel", background="#ffffff", font=("TkDefaultFont", 10, "bold"))
    style.configure("FieldHelp.TLabel", background="#ffffff", foreground="#5b6576")
    style.configure("Info.TLabel", background="#ffffff", foreground="#435066")
    style.configure("Status.TLabel", background="#f3f6fb", foreground="#243041")
    style.configure("TNotebook", background="#f3f6fb", borderwidth=0)
    style.configure("TNotebook.Tab", padding=(16, 10), font=("TkDefaultFont", 10, "bold"))

    field_vars = {
        "input": tk.StringVar(),
        "output": tk.StringVar(value="result.png"),
        "video_output": tk.StringVar(value="result_progress.gif"),
        "video_to_video_input": tk.StringVar(),
        "video_to_video_output": tk.StringVar(value="result_video_emoji.mp4"),
        "columns": tk.StringVar(value="80"),
        "rows": tk.StringVar(),
        "emoji_size": tk.StringVar(value="20"),
        "scale": tk.StringVar(value="1.0"),
        "palette": tk.StringVar(),
        "background": tk.StringVar(value="transparent"),
        "font": tk.StringVar(),
        "emoji_source": tk.StringVar(value="auto"),
        "alpha_threshold": tk.StringVar(value="0.05"),
        "video_fps": tk.StringVar(value="5"),
        "video_start_columns": tk.StringVar(value="1"),
        "video_max_columns": tk.StringVar(value="500"),
        "video_step_columns": tk.StringVar(value="2"),
    }
    stretch_var = tk.BooleanVar(value=False)
    status_var = tk.StringVar(value="Choisissez un mode puis configurez les paramètres communs.")
    estimate_var = tk.StringVar(value="Estimation : n/a")
    is_running = {"value": False}
    current_mode = {"value": "image_to_image"}

    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    outer = ttk.Frame(root, style="App.TFrame")
    outer.grid(sticky="nsew")
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(0, weight=1)

    canvas = tk.Canvas(
        outer,
        background="#f3f6fb",
        highlightthickness=0,
        bd=0,
    )
    canvas.grid(row=0, column=0, sticky="nsew")

    scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    scrollbar.grid(row=0, column=1, sticky="ns")
    canvas.configure(yscrollcommand=scrollbar.set)

    container = ttk.Frame(canvas, padding=22, style="App.TFrame")
    container.columnconfigure(0, weight=1)
    container.columnconfigure(0, weight=1)
    container.rowconfigure(1, weight=1)
    canvas_window = canvas.create_window((0, 0), window=container, anchor="nw")

    def update_scroll_region(_event: object | None = None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def update_canvas_width(event: object) -> None:
        width = getattr(event, "width", None)
        if width is not None:
            canvas.itemconfigure(canvas_window, width=width)

    def on_mousewheel(event: object) -> None:
        delta = getattr(event, "delta", 0)
        if delta:
            canvas.yview_scroll(int(-delta / 120), "units")

    def on_linux_scroll_up(_event: object) -> None:
        canvas.yview_scroll(-3, "units")

    def on_linux_scroll_down(_event: object) -> None:
        canvas.yview_scroll(3, "units")

    container.bind("<Configure>", update_scroll_region)
    canvas.bind("<Configure>", update_canvas_width)
    canvas.bind_all("<MouseWheel>", on_mousewheel)
    canvas.bind_all("<Button-4>", on_linux_scroll_up)
    canvas.bind_all("<Button-5>", on_linux_scroll_down)

    header = ttk.Frame(container, style="App.TFrame")
    header.grid(row=0, column=0, sticky="ew")
    header.columnconfigure(0, weight=1)
    ttk.Label(header, text="imgEMOJI", style="Hero.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(
        header,
        text="Trois modes séparés, des paramètres communs, et des champs expliqués sans surcharger l'écran.",
        style="Muted.TLabel",
        wraplength=920,
        justify="left",
    ).grid(row=1, column=0, sticky="w", pady=(4, 0))

    main_frame = ttk.Frame(container, style="App.TFrame")
    main_frame.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
    main_frame.columnconfigure(0, weight=1)
    main_frame.rowconfigure(1, weight=1)

    shared_frame = ttk.LabelFrame(main_frame, text="Paramètres communs", padding=16, style="Section.TLabelframe")
    shared_frame.grid(row=0, column=0, sticky="ew")
    shared_frame.columnconfigure(0, weight=1)

    def add_row(
        parent: ttk.Frame,
        row: int,
        label: str,
        description: str,
        key: str,
        browse: str | None = None,
        width: int = 18,
        stretch: bool = False,
    ) -> ttk.Entry:
        row_frame = ttk.Frame(parent, style="Card.TFrame")
        row_frame.grid(row=row, column=0, sticky="ew", pady=5)
        row_frame.columnconfigure(0, weight=1)

        text_frame = ttk.Frame(row_frame, style="Card.TFrame")
        text_frame.grid(row=0, column=0, sticky="ew", padx=(0, 18))
        text_frame.columnconfigure(0, weight=1)
        ttk.Label(text_frame, text=label, style="FieldTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            text_frame,
            text=description,
            style="FieldHelp.TLabel",
            wraplength=560,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        control_frame = ttk.Frame(row_frame, style="Card.TFrame")
        control_frame.grid(row=0, column=1, sticky="e")
        if stretch:
            control_frame.columnconfigure(0, weight=1)
        entry = ttk.Entry(control_frame, textvariable=field_vars[key], width=width)
        entry.grid(row=0, column=0, sticky="ew" if stretch else "w")
        if browse == "open":
            ttk.Button(
                control_frame,
                text="Parcourir",
                command=lambda: choose_input_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "video_input":
            ttk.Button(
                control_frame,
                text="Parcourir",
                command=lambda: choose_video_input_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "save":
            ttk.Button(
                control_frame,
                text="Parcourir",
                command=lambda: choose_output_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "mp4_save":
            ttk.Button(
                control_frame,
                text="Parcourir",
                command=lambda: choose_video_to_video_output_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "font":
            ttk.Button(
                control_frame,
                text="Parcourir",
                command=lambda: choose_font_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "video":
            ttk.Button(
                control_frame,
                text="Parcourir",
                command=lambda: choose_video_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        return entry

    def choose_input_file() -> None:
        path = filedialog.askopenfilename(
            title="Choisir l'image d'entrée",
            filetypes=[
                ("Images", "*.png *.jpg *.jpeg *.webp *.bmp *.gif"),
                ("Tous les fichiers", "*.*"),
            ],
        )
        if not path:
            return
        field_vars["input"].set(path)
        input_path = Path(path)
        if not field_vars["output"].get().strip() or field_vars["output"].get().strip() == "result.png":
            field_vars["output"].set(str(input_path.with_name(f"{input_path.stem}_emoji.png")))
        if not field_vars["video_output"].get().strip() or field_vars["video_output"].get().strip() == "result_progress.gif":
            field_vars["video_output"].set(str(input_path.with_name(f"{input_path.stem}_progress.gif")))

    def choose_output_file() -> None:
        path = filedialog.asksaveasfilename(
            title="Choisir le fichier de sortie",
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("Tous les fichiers", "*.*")],
        )
        if path:
            field_vars["output"].set(path)

    def choose_font_file() -> None:
        path = filedialog.askopenfilename(
            title="Choisir une police emoji",
            filetypes=[
                ("Polices", "*.ttf *.ttc *.otf"),
                ("Tous les fichiers", "*.*"),
            ],
        )
        if path:
            field_vars["font"].set(path)

    def choose_video_file() -> None:
        path = filedialog.asksaveasfilename(
            title="Choisir le fichier vidéo/animation",
            defaultextension=".gif",
            filetypes=[("GIF animé", "*.gif"), ("MP4", "*.mp4"), ("Tous les fichiers", "*.*")],
        )
        if path:
            field_vars["video_output"].set(path)

    def choose_video_input_file() -> None:
        path = filedialog.askopenfilename(
            title="Choisir la vidéo d'entrée",
            filetypes=[("MP4", "*.mp4"), ("Vidéos", "*.mp4 *.mov *.mkv *.webm"), ("Tous les fichiers", "*.*")],
        )
        if not path:
            return
        field_vars["video_to_video_input"].set(path)
        input_path = Path(path)
        if not field_vars["video_to_video_output"].get().strip() or field_vars["video_to_video_output"].get().strip() == "result_video_emoji.mp4":
            field_vars["video_to_video_output"].set(str(input_path.with_name(f"{input_path.stem}_emoji.mp4")))

    def choose_video_to_video_output_file() -> None:
        path = filedialog.asksaveasfilename(
            title="Choisir la vidéo de sortie",
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4"), ("Tous les fichiers", "*.*")],
        )
        if path:
            field_vars["video_to_video_output"].set(path)

    add_row(
        shared_frame, 0, "Image d'entrée",
        "Le fichier source à transformer en mosaïque emoji.",
        "input", browse="open", width=38, stretch=True,
    )
    add_row(
        shared_frame, 1, "Colonnes",
        "Le niveau de détail horizontal. Plus c'est grand, plus le rendu est fin.",
        "columns", width=10,
    )
    add_row(
        shared_frame, 2, "Lignes",
        "Optionnel. Laisse vide pour garder automatiquement les proportions.",
        "rows", width=10,
    )
    add_row(
        shared_frame, 3, "Taille emoji",
        "La taille d'un emoji dans l'image finale, en pixels.",
        "emoji_size", width=10,
    )
    add_row(
        shared_frame, 4, "Scale auto",
        "Utilisé si colonnes et lignes sont vides. 1.0 = comportement normal.",
        "scale", width=10,
    )
    add_row(
        shared_frame, 5, "Palette",
        "Liste d'emojis séparés par des virgules, ou chemin vers un fichier texte.",
        "palette", width=34,
    )
    add_row(
        shared_frame, 6, "Fond",
        "Couleur des zones vides : `transparent`, `white`, `#112233`, etc.",
        "background", width=16,
    )
    add_row(
        shared_frame, 7, "Police emoji",
        "Optionnel. À renseigner si l'auto-détection ne trouve pas de bonne police.",
        "font", browse="font", width=34,
    )
    add_row(
        shared_frame, 8, "Seuil alpha",
        "Ignore les zones presque transparentes. 0.05 est une bonne base.",
        "alpha_threshold", width=10,
    )

    source_row = ttk.Frame(shared_frame, style="Card.TFrame")
    source_row.grid(row=9, column=0, sticky="ew", pady=5)
    source_row.columnconfigure(0, weight=1)
    source_text = ttk.Frame(source_row, style="Card.TFrame")
    source_text.grid(row=0, column=0, sticky="ew", padx=(0, 18))
    source_text.columnconfigure(0, weight=1)
    ttk.Label(source_text, text="Source emoji", style="FieldTitle.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(
        source_text,
        text="`auto` choisit seul, `font` force une police locale, `twemoji` utilise les assets Twemoji.",
        style="FieldHelp.TLabel",
        wraplength=560,
        justify="left",
    ).grid(row=1, column=0, sticky="w", pady=(2, 0))
    source_combo = ttk.Combobox(
        source_row,
        textvariable=field_vars["emoji_source"],
        values=("auto", "font", "twemoji"),
        state="readonly",
        width=14,
    )
    source_combo.grid(row=0, column=1, sticky="e")

    stretch_row = ttk.Frame(shared_frame, style="Card.TFrame")
    stretch_row.grid(row=10, column=0, sticky="ew", pady=(8, 4))
    stretch_row.columnconfigure(0, weight=1)
    ttk.Checkbutton(
        stretch_row,
        text="Autoriser l'étirement si colonnes + lignes sont définies",
        variable=stretch_var,
    ).grid(row=0, column=0, sticky="w")
    ttk.Label(
        stretch_row,
        text="À activer seulement si tu veux forcer une grille qui ne respecte pas les proportions d'origine.",
        style="FieldHelp.TLabel",
        wraplength=860,
        justify="left",
    ).grid(row=1, column=0, sticky="w", pady=(2, 0))

    help_text = (
        "Les champs ici sont partagés par `Image -> Image` et `Image -> Vidéo`. "
        "Tu règles la matière commune une fois, puis tu choisis seulement la sortie dans l'onglet voulu."
    )
    ttk.Label(shared_frame, text=help_text, wraplength=860, justify="left").grid(
        row=11, column=0, sticky="w", pady=(8, 0)
    )

    notebook = ttk.Notebook(main_frame)
    notebook.grid(row=1, column=0, sticky="nsew", pady=(14, 0))

    image_tab = ttk.Frame(notebook, padding=18, style="App.TFrame")
    video_tab = ttk.Frame(notebook, padding=18, style="App.TFrame")
    future_tab = ttk.Frame(notebook, padding=18, style="App.TFrame")
    for tab in (image_tab, video_tab, future_tab):
        tab.columnconfigure(0, weight=1)

    notebook.add(image_tab, text="Image -> Image")
    notebook.add(video_tab, text="Image -> Vidéo")
    notebook.add(future_tab, text="Vidéo -> Vidéo")

    image_card = ttk.LabelFrame(image_tab, text="Sortie image", padding=16, style="Section.TLabelframe")
    image_card.grid(row=0, column=0, sticky="ew")
    image_card.columnconfigure(0, weight=1)
    add_row(
        image_card, 0, "Image de sortie",
        "Le PNG final généré par le mode `image -> image`.",
        "output", browse="save", width=38, stretch=True,
    )
    ttk.Label(
        image_card,
        text="Tu peux utiliser ce mode pour tester rapidement une palette ou régler la densité avant de passer à une animation.",
        style="Info.TLabel",
        wraplength=760,
        justify="left",
    ).grid(row=1, column=0, sticky="w", pady=(10, 0))

    image_buttons = ttk.Frame(image_tab)
    image_buttons.grid(row=1, column=0, sticky="ew", pady=(14, 0))
    image_buttons.columnconfigure(1, weight=1)
    ttk.Button(image_buttons, text="Générer l'image", command=lambda: on_render()).grid(row=0, column=0, sticky="w")
    ttk.Button(image_buttons, text="Quitter", command=root.destroy).grid(row=0, column=2, sticky="e")

    video_card = ttk.LabelFrame(video_tab, text="Sortie vidéo", padding=16, style="Section.TLabelframe")
    video_card.grid(row=0, column=0, sticky="ew")
    video_card.columnconfigure(0, weight=1)
    add_row(
        video_card, 0, "Fichier vidéo",
        "Le GIF ou MP4 de sortie.",
        "video_output", browse="video", width=38, stretch=True,
    )
    add_row(
        video_card, 1, "FPS vidéo",
        "Nombre d'images par seconde. Plus haut = animation plus fluide.",
        "video_fps", width=10,
    )
    add_row(
        video_card, 2, "Départ colonnes",
        "La première frame démarre avec cette densité.",
        "video_start_columns", width=10,
    )
    add_row(
        video_card, 3, "Fin colonnes",
        "La dernière frame finit à cette densité. Laisse 500 si tu veux aller loin.",
        "video_max_columns", width=10,
    )
    add_row(
        video_card, 4, "Pas colonnes",
        "L'écart entre deux frames. Petit pas = progression plus douce.",
        "video_step_columns", width=10,
    )
    ttk.Label(
        video_card,
        text="Réglage conseillé : départ 1, pas 2, fin 500 pour une dépixelisation progressive simple.",
        style="Info.TLabel",
        wraplength=760,
        justify="left",
    ).grid(row=5, column=0, sticky="w", pady=(10, 0))

    video_buttons = ttk.Frame(video_tab)
    video_buttons.grid(row=1, column=0, sticky="ew", pady=(14, 0))
    video_buttons.columnconfigure(1, weight=1)
    ttk.Button(video_buttons, text="Créer la vidéo", command=lambda: on_video_render()).grid(row=0, column=0, sticky="w")
    ttk.Button(video_buttons, text="Quitter", command=root.destroy).grid(row=0, column=2, sticky="e")

    future_card = ttk.LabelFrame(future_tab, text="Vidéo -> Vidéo", padding=18, style="Section.TLabelframe")
    future_card.grid(row=0, column=0, sticky="nsew")
    future_card.columnconfigure(0, weight=1)
    add_row(
        future_card, 0, "Vidéo d'entrée",
        "Le MP4 source à transformer frame par frame en version emoji.",
        "video_to_video_input", browse="video_input", width=38, stretch=True,
    )
    add_row(
        future_card, 1, "Vidéo de sortie",
        "Le MP4 final généré par le mode `vidéo -> vidéo`.",
        "video_to_video_output", browse="mp4_save", width=38, stretch=True,
    )
    add_row(
        future_card, 2, "FPS vidéo",
        "Nombre d'images par seconde à extraire puis réencoder dans le MP4 final.",
        "video_fps", width=10,
    )
    ttk.Label(
        future_card,
        text=(
            "Les paramètres communs au-dessus s'appliquent aussi ici : colonnes, palette, taille emoji, "
            "fond, source emoji, etc."
        ),
        style="Info.TLabel",
        justify="left",
        wraplength=760,
    ).grid(row=3, column=0, sticky="w", pady=(10, 0))

    future_buttons = ttk.Frame(future_tab)
    future_buttons.grid(row=1, column=0, sticky="ew", pady=(14, 0))
    future_buttons.columnconfigure(1, weight=1)
    ttk.Button(future_buttons, text="Créer la vidéo emoji", command=lambda: on_video_to_video_render()).grid(row=0, column=0, sticky="w")
    ttk.Button(future_buttons, text="Quitter", command=root.destroy).grid(row=0, column=2, sticky="e")

    footer = ttk.Frame(main_frame, style="App.TFrame")
    footer.grid(row=2, column=0, sticky="ew", pady=(14, 0))
    footer.columnconfigure(0, weight=1)
    status_label = ttk.Label(footer, textvariable=status_var, wraplength=900, justify="left", style="Status.TLabel")
    status_label.grid(row=0, column=0, sticky="ew")
    estimate_label = ttk.Label(footer, textvariable=estimate_var, wraplength=900, justify="left", style="Muted.TLabel")
    estimate_label.grid(row=1, column=0, sticky="ew", pady=(8, 0))

    def walk_children(widget: tk.Misc) -> list[tk.Misc]:
        nodes = [widget]
        for child in widget.winfo_children():
            nodes.extend(walk_children(child))
        return nodes

    def set_controls_state(state: str) -> None:
        for child in walk_children(container):
            if isinstance(child, (ttk.Entry, ttk.Button, ttk.Combobox, ttk.Checkbutton)):
                try:
                    child.state([state] if state in ("disabled", "!disabled") else [])
                except tk.TclError:
                    pass

    def collect_values() -> dict[str, str | bool]:
        return {
            "input": field_vars["input"].get(),
            "output": field_vars["output"].get(),
            "video_output": field_vars["video_output"].get(),
            "video_to_video_input": field_vars["video_to_video_input"].get(),
            "video_to_video_output": field_vars["video_to_video_output"].get(),
            "columns": field_vars["columns"].get(),
            "rows": field_vars["rows"].get(),
            "emoji_size": field_vars["emoji_size"].get(),
            "scale": field_vars["scale"].get(),
            "palette": field_vars["palette"].get(),
            "background": field_vars["background"].get(),
            "font": field_vars["font"].get(),
            "emoji_source": field_vars["emoji_source"].get(),
            "alpha_threshold": field_vars["alpha_threshold"].get(),
            "video_fps": field_vars["video_fps"].get(),
            "video_start_columns": field_vars["video_start_columns"].get(),
            "video_max_columns": field_vars["video_max_columns"].get(),
            "video_step_columns": field_vars["video_step_columns"].get(),
            "stretch": stretch_var.get(),
        }

    def get_active_mode() -> str:
        selected_tab = notebook.select()
        if selected_tab == str(image_tab):
            return "image_to_image"
        if selected_tab == str(video_tab):
            return "image_to_video"
        return "video_to_video"

    def refresh_estimate(*_args: object) -> None:
        if is_running["value"]:
            return
        current_mode["value"] = get_active_mode()
        try:
            args = build_gui_args(collect_values())
            if current_mode["value"] == "image_to_image":
                if not args.input or not args.input.strip():
                    estimate_var.set("Estimation : choisissez d'abord une image d'entrée.")
                    return
                estimate, profile = estimate_for_args(args)
                estimate_var.set(
                    "Estimation image : "
                    f"{format_duration(estimate.seconds)} pour {int(profile['total_cells'])} cases "
                    f"({int(profile['columns'])}x{int(profile['rows'])}), "
                    f"source {profile['emoji_source']}, "
                    f"historique {estimate.sample_count} échantillon(s), confiance {estimate.confidence}."
                )
            elif current_mode["value"] == "image_to_video":
                if not args.input or not args.input.strip():
                    estimate_var.set("Estimation : choisissez d'abord une image d'entrée.")
                    return
                video_seconds, video_frames = estimate_video_for_args(args)
                estimate_var.set(
                    "Estimation vidéo : "
                    f"{format_duration(video_seconds)} pour {video_frames} frame(s) à {args.video_fps} FPS."
                )
            else:
                if not args.video_to_video_input or not args.video_to_video_input.strip():
                    estimate_var.set("Estimation : choisissez d'abord une vidéo d'entrée.")
                    return
                video_seconds, video_frames, profile = estimate_video_to_video_for_args(args, args.video_to_video_input)
                estimate_var.set(
                    "Estimation vidéo -> vidéo : "
                    f"{format_duration(video_seconds)} pour {video_frames} frame(s) à {args.video_fps} FPS, "
                    f"grille {int(profile['columns'])}x{int(profile['rows'])}."
                )
        except EmojiMakerError as exc:
            estimate_var.set(f"Estimation : {exc}")
        except Exception:
            estimate_var.set("Estimation : paramètres incomplets ou invalides.")

    def on_render() -> None:
        if is_running["value"]:
            return

        try:
            args = build_gui_args(collect_values())
        except ValueError as exc:
            messagebox.showerror("Paramètres invalides", f"Valeur invalide : {exc}")
            return

        if not args.input.strip():
            messagebox.showerror("Paramètres invalides", "Veuillez choisir une image d'entrée.")
            return
        if not args.output.strip():
            messagebox.showerror("Paramètres invalides", "Veuillez choisir un fichier de sortie.")
            return

        try:
            estimate, profile = estimate_for_args(args)
        except EmojiMakerError as exc:
            messagebox.showerror("Paramètres invalides", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Paramètres invalides", f"Impossible d'estimer le rendu : {exc}")
            return

        is_running["value"] = True
        set_controls_state("disabled")
        status_var.set(
            "Rendu en cours... "
            f"Estimation {format_duration(estimate.seconds)} pour {int(profile['total_cells'])} cases."
        )

        def worker() -> None:
            try:
                result = run_with_args(args)
            except EmojiMakerError as exc:
                root.after(0, lambda exc=exc: on_error(str(exc)))
                return
            except Exception as exc:  # pragma: no cover - defensive GUI fallback
                root.after(0, lambda exc=exc: on_error(str(exc)))
                return
            root.after(0, lambda: on_success(result))

        threading.Thread(target=worker, daemon=True).start()

    def on_error(message: str) -> None:
        is_running["value"] = False
        set_controls_state("!disabled")
        status_var.set(message)
        messagebox.showerror("Erreur", message)

    def on_success(result: RenderResult) -> None:
        is_running["value"] = False
        set_controls_state("!disabled")
        status_var.set(
            f"Rendu terminé : {result.output_path} | "
            f"{result.filled_cells} emojis dessinés sur {result.total_cells} cases en {format_duration(result.duration_seconds)}."
        )
        refresh_estimate()
        messagebox.showinfo(
            "Succès",
            "Image générée :\n"
            f"{result.output_path}\n\n"
            f"Temps réel : {format_duration(result.duration_seconds)}\n"
            f"Cases totales : {result.total_cells}\n"
            f"Emojis dessinés : {result.filled_cells}\n"
            f"Grille : {result.columns}x{result.rows}\n"
            f"Source emoji : {result.emoji_source}",
        )

    def on_video_render() -> None:
        if is_running["value"]:
            return

        try:
            args = build_gui_args(collect_values())
        except ValueError as exc:
            messagebox.showerror("Paramètres invalides", f"Valeur invalide : {exc}")
            return

        if not args.input.strip():
            messagebox.showerror("Paramètres invalides", "Veuillez choisir une image d'entrée.")
            return
        if not args.video_output:
            messagebox.showerror("Paramètres invalides", "Veuillez choisir un fichier de sortie vidéo.")
            return

        if args.video_max_columns is None:
            args.video_max_columns = args.columns if args.columns is not None else 500

        try:
            estimated_seconds, frame_count = estimate_video_for_args(args)
        except EmojiMakerError as exc:
            messagebox.showerror("Paramètres invalides", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Paramètres invalides", f"Impossible d'estimer la vidéo : {exc}")
            return

        is_running["value"] = True
        set_controls_state("disabled")
        status_var.set(
            "Vidéo en cours... "
            f"Estimation {format_duration(estimated_seconds)} pour {frame_count} frame(s) à {args.video_fps} FPS."
        )

        def worker() -> None:
            try:
                result = render_video_with_args(
                    args=args,
                    video_output=args.video_output,
                    video_fps=args.video_fps,
                    video_start_columns=args.video_start_columns,
                    video_max_columns=args.video_max_columns,
                    video_step_columns=args.video_step_columns,
                )
            except EmojiMakerError as exc:
                root.after(0, lambda exc=exc: on_error(str(exc)))
                return
            except Exception as exc:  # pragma: no cover
                root.after(0, lambda exc=exc: on_error(str(exc)))
                return
            root.after(0, lambda: on_video_success(result))

        threading.Thread(target=worker, daemon=True).start()

    def on_video_success(result: VideoResult) -> None:
        is_running["value"] = False
        set_controls_state("!disabled")
        status_var.set(
            f"Vidéo terminée : {result.output_path} | "
            f"{result.frame_count} frame(s) en {format_duration(result.duration_seconds)}."
        )
        refresh_estimate()
        messagebox.showinfo(
            "Succès vidéo",
            "Animation générée :\n"
            f"{result.output_path}\n\n"
            f"Temps réel : {format_duration(result.duration_seconds)}\n"
            f"Frames : {result.frame_count}\n"
            f"FPS : {result.fps}\n"
            f"Colonnes : {result.start_columns} -> {result.max_columns} (pas {result.step_columns})\n"
            f"Taille vidéo : {result.canvas_width}x{result.canvas_height}",
        )

    def on_video_to_video_render() -> None:
        if is_running["value"]:
            return

        try:
            args = build_gui_args(collect_values())
        except ValueError as exc:
            messagebox.showerror("Paramètres invalides", f"Valeur invalide : {exc}")
            return

        if not args.video_to_video_input:
            messagebox.showerror("Paramètres invalides", "Veuillez choisir une vidéo d'entrée.")
            return
        if not args.video_to_video_output:
            messagebox.showerror("Paramètres invalides", "Veuillez choisir une vidéo de sortie.")
            return

        try:
            estimated_seconds, frame_count, profile = estimate_video_to_video_for_args(args, args.video_to_video_input)
        except EmojiMakerError as exc:
            messagebox.showerror("Paramètres invalides", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Paramètres invalides", f"Impossible d'estimer la conversion vidéo -> vidéo : {exc}")
            return

        is_running["value"] = True
        set_controls_state("disabled")
        status_var.set(
            "Conversion vidéo -> vidéo en cours... "
            f"Estimation {format_duration(estimated_seconds)} pour {frame_count} frame(s), "
            f"grille {int(profile['columns'])}x{int(profile['rows'])}."
        )

        def worker() -> None:
            try:
                result = render_video_to_video_with_args(
                    args=args,
                    video_input=args.video_to_video_input,
                    video_output=args.video_to_video_output,
                )
            except EmojiMakerError as exc:
                root.after(0, lambda exc=exc: on_error(str(exc)))
                return
            except Exception as exc:  # pragma: no cover
                root.after(0, lambda exc=exc: on_error(str(exc)))
                return
            root.after(0, lambda: on_video_to_video_success(result))

        threading.Thread(target=worker, daemon=True).start()

    def on_video_to_video_success(result: VideoToVideoResult) -> None:
        is_running["value"] = False
        set_controls_state("!disabled")
        status_var.set(
            f"Vidéo -> vidéo terminée : {result.output_path} | "
            f"{result.frame_count} frame(s) en {format_duration(result.duration_seconds)}."
        )
        refresh_estimate()
        messagebox.showinfo(
            "Succès vidéo -> vidéo",
            "Vidéo générée :\n"
            f"{result.output_path}\n\n"
            f"Temps réel : {format_duration(result.duration_seconds)}\n"
            f"Frames : {result.frame_count}\n"
            f"FPS : {result.fps}\n"
            f"Grille : {result.columns}x{result.rows}\n"
            f"Taille vidéo : {result.canvas_width}x{result.canvas_height}",
        )

    for variable in field_vars.values():
        variable.trace_add("write", refresh_estimate)
    stretch_var.trace_add("write", refresh_estimate)
    notebook.bind("<<NotebookTabChanged>>", refresh_estimate)
    refresh_estimate()

    root.mainloop()


def main() -> None:
    if len(sys.argv) == 1:
        launch_gui()
        return

    args = parse_args()
    if args.video_output and args.video_max_columns is None:
        args.video_max_columns = args.columns if args.columns is not None else 500
    try:
        estimate, profile = estimate_for_args(args)
        print(
            "Estimate: "
            f"{format_duration(estimate.seconds)} for {int(profile['total_cells'])} cells "
            f"({int(profile['columns'])}x{int(profile['rows'])}), "
            f"source={profile['emoji_source']}, samples={estimate.sample_count}, confidence={estimate.confidence}."
        )
    except EmojiMakerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        pass

    if args.video_output:
        try:
            video_seconds, frame_count = estimate_video_for_args(args)
            print(
                "Video estimate: "
                f"{format_duration(video_seconds)} for {frame_count} frames at {args.video_fps} fps."
            )
        except EmojiMakerError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        except Exception:
            pass
        result = render_video_with_args(
            args=args,
            video_output=args.video_output,
            video_fps=args.video_fps,
            video_start_columns=args.video_start_columns,
            video_max_columns=args.video_max_columns,
            video_step_columns=args.video_step_columns,
        )
        print(
            "Video done: "
            f"{result.output_path} | time={format_duration(result.duration_seconds)} | "
            f"frames={result.frame_count} | fps={result.fps} | "
            f"columns={result.start_columns}->{result.max_columns} step={result.step_columns}"
        )
        return

    result = run_with_args(args)
    print(
        "Done: "
        f"{result.output_path} | time={format_duration(result.duration_seconds)} | "
        f"cells={result.total_cells} | emojis_drawn={result.filled_cells} | "
        f"grid={result.columns}x{result.rows} | source={result.emoji_source}"
    )


if __name__ == "__main__":
    main()
