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
import os
import re
import shutil
import time
import subprocess
import sys
import threading
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable, List, Sequence, Tuple

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

try:
    from PIL import ImageTk
except ImportError:
    ImageTk = None

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ModuleNotFoundError:
    tk = None
    filedialog = None
    messagebox = None
    ttk = None

from .cache import (
    load_gui_settings,
    load_palette_metrics_cache,
    load_render_history,
    save_gui_settings,
    save_palette_metrics_cache,
    save_render_history,
)
from .ascii_art import compute_ascii_rows, find_monospace_font, measure_glyph_cell, run_ascii_with_args
from .common import build_mp4_encode_command, format_duration, has_ffmpeg, has_ffprobe
from .constants import (
    COMMON_EMOJI_FONT_FAMILIES,
    COMMON_EMOJI_FONTS,
    DEFAULT_BANNED_EMOJIS,
    DEFAULT_PALETTE,
    GUI_SETTINGS_PATH,
    INITIAL_BROWSER_RESULTS,
    LOAD_MORE_BROWSER_RESULTS,
    MAX_HISTORY_ENTRIES,
    PALETTE_METRICS_CACHE_DIR,
    PALETTE_SPLIT_RE,
    RUN_HISTORY_PATH,
    SQUARE_FIRST_DEFAULT_PALETTE,
    TWEMOJI_BASE_URL,
    TWEMOJI_CACHE_DIR,
    UNICODE_EMOJI_TEST_CACHE_PATH,
    UNICODE_EMOJI_TEST_URL,
)
from .estimation import append_render_history, build_render_profile, estimate_duration
from .errors import EmojiMakerError, fail
from .models import (
    EmojiRenderSource,
    PaletteEntry,
    PaletteMatcher,
    ProgressCallback,
    RenderEstimate,
    RenderResult,
    VideoResult,
    VideoToVideoResult,
)


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
        "--banned-palette",
        help=(
            "Emoji list to exclude from the final palette. Accepts the same formats "
            "as --palette."
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


def parse_emoji_list(value: str | None, default: Sequence[str] | None = None) -> List[str]:
    if value is None:
        tokens = list(default or [])
    else:
        candidate_path = Path(value)
        if candidate_path.is_file():
            content = candidate_path.read_text(encoding="utf-8").strip()
            tokens = [
                token.strip()
                for token in PALETTE_SPLIT_RE.split(content)
                if token.strip()
            ]
        else:
            tokens = [
                token.strip()
                for token in PALETTE_SPLIT_RE.split(value)
                if token.strip()
            ]

    unique_palette = []
    seen = set()
    for emoji in tokens:
        if emoji not in seen:
            unique_palette.append(emoji)
            seen.add(emoji)

    return unique_palette


def build_default_palette_for_render_source(render_source_kind: str) -> List[str]:
    if render_source_kind == "twemoji":
        return get_twemoji_browser_catalog()
    return list(SQUARE_FIRST_DEFAULT_PALETTE)


def parse_palette(
    palette_arg: str | None,
    banned_arg: str | None = None,
    default_palette: Sequence[str] | None = None,
) -> List[str]:
    palette = parse_emoji_list(palette_arg, default=default_palette or DEFAULT_PALETTE)
    banned = set(parse_emoji_list(banned_arg, default=DEFAULT_BANNED_EMOJIS))
    if banned:
        palette = [emoji for emoji in palette if emoji not in banned]

    if not palette:
        fail("Palette is empty. Provide at least one emoji.")
    return palette


def encode_emoji_list(emojis: Sequence[str]) -> str:
    return " ".join(emoji for emoji in emojis if emoji)


def describe_emoji(emoji: str) -> str:
    parts: List[str] = []
    for char in emoji:
        if ord(char) == 0xFE0F:
            continue
        try:
            parts.append(unicodedata.name(char).title())
        except ValueError:
            parts.append(f"U+{ord(char):04X}")
    return " + ".join(parts) if parts else "Unknown Emoji"


def fetch_unicode_emoji_test() -> str:
    if UNICODE_EMOJI_TEST_CACHE_PATH.exists():
        try:
            return UNICODE_EMOJI_TEST_CACHE_PATH.read_text(encoding="utf-8")
        except OSError:
            pass

    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                UNICODE_EMOJI_TEST_URL,
                headers={"User-Agent": "emoji_maker/1.0"},
            ),
            timeout=30,
        ) as response:
            content = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as exc:
        fail(
            "Could not fetch the Unicode emoji catalog used by the Twemoji browser. "
            f"Last error: {exc}"
        )
    UNICODE_EMOJI_TEST_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNICODE_EMOJI_TEST_CACHE_PATH.write_text(content, encoding="utf-8")
    return content


def parse_unicode_emoji_test(content: str) -> List[str]:
    emojis: List[str] = []
    seen: set[str] = set()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        left, _sep, right = line.partition("#")
        if ";" not in left:
            continue
        _codepoints, _semi, status = left.partition(";")
        if status.strip() != "fully-qualified":
            continue
        match = re.search(r"#\s+(\S+)\s+E[0-9.]+", line)
        if not match:
            continue
        emoji = match.group(1).strip()
        if emoji and emoji not in seen:
            emojis.append(emoji)
            seen.add(emoji)
    return emojis


def get_twemoji_browser_catalog() -> List[str]:
    try:
        catalog = parse_unicode_emoji_test(fetch_unicode_emoji_test())
        if catalog:
            return catalog
    except EmojiMakerError:
        pass
    fallback = list(SQUARE_FIRST_DEFAULT_PALETTE) + list(DEFAULT_PALETTE) + list(DEFAULT_BANNED_EMOJIS)
    return parse_emoji_list(encode_emoji_list(fallback))


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
    palette_matcher: PaletteMatcher,
    emoji_size: int,
    alpha_threshold: float,
    columns: int,
    rows: int,
    progress_callback: ProgressCallback | None = None,
    progress_range: Tuple[float, float] = (0.0, 1.0),
    tile_cache: dict[str, Image.Image] | None = None,
) -> tuple[Image.Image, int]:
    progress_start, progress_end = progress_range
    if progress_callback is not None:
        progress_callback(progress_start, f"Preparation grille {columns}x{rows}")
    sampled_image = resize_for_grid(image, columns, rows)
    emoji_grid = build_emoji_grid(sampled_image, palette_matcher, alpha_threshold)
    if progress_callback is not None:
        midpoint = progress_start + ((progress_end - progress_start) * 0.35)
        progress_callback(midpoint, f"Correspondance emojis {columns}x{rows}")
    canvas = render_emoji_canvas(
        emoji_grid,
        emoji_size,
        render_source,
        background,
        tile_cache=tile_cache,
        progress_callback=progress_callback,
        progress_range=(
            progress_start + ((progress_end - progress_start) * 0.45),
            progress_end,
        ),
    )
    filled_cells = sum(1 for row in emoji_grid for emoji in row if emoji is not None)
    return canvas, filled_cells


def render_video_with_args(
    args: argparse.Namespace,
    video_output: str,
    video_fps: int,
    video_start_columns: int,
    video_max_columns: int,
    video_step_columns: int,
    progress_callback: ProgressCallback | None = None,
) -> VideoResult:
    if video_fps <= 0:
        fail("--video-fps must be greater than 0.")

    image, background, palette, _columns, _rows, render_source = prepare_render(args)
    if progress_callback is not None:
        progress_callback(0.02, "Chargement image")
    palette_entries = build_palette_entries(
        palette,
        args.emoji_size,
        render_source,
        progress_callback=progress_callback,
        progress_range=(0.04, 0.45),
    )
    palette_matcher = compile_palette_matcher(palette_entries)
    shared_tile_cache: dict[str, Image.Image] = {}
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
    total_frames = max(1, len(frame_specs))
    for frame_index, (columns, rows) in enumerate(frame_specs, start=1):
        frame_start = 0.48 + (0.40 * ((frame_index - 1) / total_frames))
        frame_end = 0.48 + (0.40 * (frame_index / total_frames))
        frame, _filled_cells = render_canvas_for_grid(
            image=image,
            background=background,
            render_source=render_source,
            palette_matcher=palette_matcher,
            emoji_size=args.emoji_size,
            alpha_threshold=args.alpha_threshold,
            columns=columns,
            rows=rows,
            progress_callback=progress_callback,
            progress_range=(frame_start, frame_end),
            tile_cache=shared_tile_cache,
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
            if progress_callback is not None:
                progress_callback(0.90, "Ecriture frames video")
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
        if progress_callback is not None:
            progress_callback(0.90, "Encodage GIF")
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
    if progress_callback is not None:
        progress_callback(1.0, "Video terminee")

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
    render_source = resolve_render_source(args.font, args.emoji_size, args.emoji_source)
    palette = parse_palette(
        args.palette,
        args.banned_palette,
        default_palette=build_default_palette_for_render_source(render_source.kind),
    )
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
    progress_callback: ProgressCallback | None = None,
) -> VideoToVideoResult:
    if not has_ffmpeg():
        fail("ffmpeg is required for video-to-video mode. Install ffmpeg and retry.")
    metadata, _profile, _estimated_frame_count = build_video_to_video_profile(video_input=video_input, args=args)
    background = parse_background(args.background)
    render_source = resolve_render_source(args.font, args.emoji_size, args.emoji_source)
    palette = parse_palette(
        args.palette,
        args.banned_palette,
        default_palette=build_default_palette_for_render_source(render_source.kind),
    )
    columns, rows = compute_grid_size(
        width=int(metadata["width"]),
        height=int(metadata["height"]),
        columns=args.columns,
        rows=args.rows,
        scale=args.scale,
        stretch=args.stretch,
    )
    output_fps = args.video_fps
    if progress_callback is not None:
        progress_callback(0.02, "Analyse video source")
    palette_entries = build_palette_entries(
        palette,
        args.emoji_size,
        render_source,
        progress_callback=progress_callback,
        progress_range=(0.04, 0.35),
    )
    palette_matcher = compile_palette_matcher(palette_entries)
    shared_tile_cache: dict[str, Image.Image] = {}
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
        if progress_callback is not None:
            progress_callback(0.40, f"Frames extraites {actual_frame_count}")

        canvas_width = columns * args.emoji_size
        canvas_height = rows * args.emoji_size
        total_frames = max(1, len(extracted_frames))
        for index, frame_path in enumerate(extracted_frames, start=1):
            frame_image = Image.open(frame_path).convert("RGBA")
            frame_progress_index = index - 1
            frame_start = 0.42 + (0.40 * (frame_progress_index / total_frames))
            frame_end = 0.42 + (0.40 * (index / total_frames))
            canvas, _filled_cells = render_canvas_for_grid(
                image=frame_image,
                background=background,
                render_source=render_source,
                palette_matcher=palette_matcher,
                emoji_size=args.emoji_size,
                alpha_threshold=args.alpha_threshold,
                columns=columns,
                rows=rows,
                progress_callback=progress_callback,
                progress_range=(frame_start, frame_end),
                tile_cache=shared_tile_cache,
            )
            canvas.save(output_frames_path / f"frame_{index:06d}.png", format="PNG")

        silent_video_path = output_frames_path / "rendered_video.mp4"
        if progress_callback is not None:
            progress_callback(0.86, "Encodage video")
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
    if progress_callback is not None:
        progress_callback(1.0, "Video terminee")

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
    progress_callback: ProgressCallback | None = None,
    progress_range: Tuple[float, float] = (0.0, 1.0),
) -> List[PaletteEntry]:
    entries: List[PaletteEntry] = []
    skipped_emojis: List[str] = []
    progress_start, progress_end = progress_range
    total_emojis = max(1, len(palette))
    cache_payload = load_palette_metrics_cache(render_source, emoji_size)
    cached_entries = cache_payload["entries"] if isinstance(cache_payload.get("entries"), dict) else {}
    cached_skipped = {
        str(item)
        for item in cache_payload.get("skipped", [])
        if isinstance(item, str)
    }
    cache_dirty = False

    for index, emoji in enumerate(palette, start=1):
        cached_entry = cached_entries.get(emoji)
        if isinstance(cached_entry, dict):
            mean_rgb = cached_entry.get("mean_rgb")
            brightness = cached_entry.get("brightness")
            saturation = cached_entry.get("saturation")
            coverage = cached_entry.get("alpha_coverage")
            if (
                isinstance(mean_rgb, list)
                and len(mean_rgb) == 3
                and all(isinstance(value, (int, float)) for value in mean_rgb)
                and isinstance(brightness, (int, float))
                and isinstance(saturation, (int, float))
                and isinstance(coverage, (int, float))
            ):
                entries.append(
                    PaletteEntry(
                        emoji=emoji,
                        mean_rgb=np.asarray(mean_rgb, dtype=np.float32),
                        brightness=float(brightness),
                        saturation=float(saturation),
                        alpha_coverage=float(coverage),
                    )
                )
                if progress_callback is not None:
                    fraction = progress_start + ((progress_end - progress_start) * (index / total_emojis))
                    progress_callback(fraction, f"Preparation palette {index}/{total_emojis}")
                continue
        if emoji in cached_skipped and render_source.kind == "twemoji":
            skipped_emojis.append(emoji)
            if progress_callback is not None:
                fraction = progress_start + ((progress_end - progress_start) * (index / total_emojis))
                progress_callback(fraction, f"Preparation palette Twemoji {index}/{total_emojis}")
            continue
        try:
            tile = render_single_emoji_tile(emoji, emoji_size, render_source)
        except EmojiMakerError:
            if render_source.kind == "twemoji":
                skipped_emojis.append(emoji)
                cached_skipped.add(emoji)
                cache_dirty = True
                if progress_callback is not None:
                    fraction = progress_start + ((progress_end - progress_start) * (index / total_emojis))
                    progress_callback(fraction, f"Preparation palette Twemoji {index}/{total_emojis}")
                continue
            raise
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
        cached_entries[emoji] = {
            "mean_rgb": [float(value) for value in mean_rgb.tolist()],
            "brightness": float(brightness),
            "saturation": float(saturation),
            "alpha_coverage": float(coverage),
        }
        cache_dirty = True
        if progress_callback is not None:
            fraction = progress_start + ((progress_end - progress_start) * (index / total_emojis))
            progress_callback(fraction, f"Preparation palette {index}/{total_emojis}")
    if not entries:
        if render_source.kind == "twemoji":
            fail(
                "No usable Twemoji assets were available for the current palette. "
                "Try banning fewer emojis, using a custom palette, or switching emoji source."
            )
        fail("Palette is empty. Provide at least one emoji.")
    if cache_dirty:
        cache_payload["entries"] = cached_entries
        cache_payload["skipped"] = sorted(cached_skipped)
        save_palette_metrics_cache(render_source, emoji_size, cache_payload)
    return entries


def compile_palette_matcher(palette_entries: Sequence[PaletteEntry]) -> PaletteMatcher:
    return PaletteMatcher(
        rgb=np.stack([entry.mean_rgb for entry in palette_entries], axis=0).astype(np.float32),
        brightness=np.asarray([entry.brightness for entry in palette_entries], dtype=np.float32),
        saturation=np.asarray([entry.saturation for entry in palette_entries], dtype=np.float32),
        alpha=np.asarray([entry.alpha_coverage for entry in palette_entries], dtype=np.float32),
        emojis=tuple(entry.emoji for entry in palette_entries),
    )


def build_emoji_grid(
    sampled_image: Image.Image,
    palette_matcher: PaletteMatcher,
    alpha_threshold: float,
) -> List[List[str | None]]:
    chunk_size = 2048
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

        valid_features = np.column_stack(
            (
                valid_rgb.astype(np.float32),
                valid_brightness.astype(np.float32),
                valid_saturation.astype(np.float32),
                valid_alpha.astype(np.float32),
            )
        )
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

            scores = (
                color_distance
                + (brightness_distance * 0.45)
                + (saturation_distance * 0.35)
                + (alpha_distance * 0.25)
            )
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
    canvas = Image.new(
        "RGBA",
        (columns * emoji_size, rows * emoji_size),
        background,
    )

    # Cache pre-rendered emoji tiles so repeated emojis are pasted instead of redrawn.
    if tile_cache is None:
        tile_cache = {}
    total_cells = max(1, rows * columns)
    processed_cells = 0
    progress_start, progress_end = progress_range

    for y, row in enumerate(emoji_grid):
        for x, emoji in enumerate(row):
            if emoji is None:
                processed_cells += 1
                if progress_callback is not None and (processed_cells == total_cells or processed_cells % max(1, total_cells // 40) == 0):
                    fraction = progress_start + ((progress_end - progress_start) * (processed_cells / total_cells))
                    progress_callback(fraction, f"Dessin emojis {processed_cells}/{total_cells}")
                continue
            if emoji not in tile_cache:
                tile_cache[emoji] = render_single_emoji_tile(emoji, emoji_size, render_source)
            tile = tile_cache[emoji]
            canvas.alpha_composite(tile, (x * emoji_size, y * emoji_size))
            processed_cells += 1
            if progress_callback is not None and (processed_cells == total_cells or processed_cells % max(1, total_cells // 40) == 0):
                fraction = progress_start + ((progress_end - progress_start) * (processed_cells / total_cells))
                progress_callback(fraction, f"Dessin emojis {processed_cells}/{total_cells}")

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
        if video_input_path.suffix.lower() != ".mp4":
            fail("Video input must be a .mp4 file.")
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

    columns, rows = compute_grid_size(
        width=image.width,
        height=image.height,
        columns=args.columns,
        rows=args.rows,
        scale=args.scale,
        stretch=args.stretch,
    )

    render_source = resolve_render_source(args.font, args.emoji_size, args.emoji_source)
    palette = parse_palette(
        args.palette,
        args.banned_palette,
        default_palette=build_default_palette_for_render_source(render_source.kind),
    )
    return image, background, palette, columns, rows, render_source


def run_with_args(args: argparse.Namespace, progress_callback: ProgressCallback | None = None) -> RenderResult:
    image, background, palette, columns, rows, render_source = prepare_render(args)
    started_at = time.perf_counter()
    sampled_image = resize_for_grid(image, columns, rows)
    if progress_callback is not None:
        progress_callback(0.02, "Chargement image")
    palette_entries = build_palette_entries(
        palette,
        args.emoji_size,
        render_source,
        progress_callback=progress_callback,
        progress_range=(0.05, 0.55),
    )
    palette_matcher = compile_palette_matcher(palette_entries)
    shared_tile_cache: dict[str, Image.Image] = {}
    if progress_callback is not None:
        progress_callback(0.62, "Analyse image")
    emoji_grid = build_emoji_grid(sampled_image, palette_matcher, args.alpha_threshold)
    canvas = render_emoji_canvas(
        emoji_grid,
        args.emoji_size,
        render_source,
        background,
        tile_cache=shared_tile_cache,
        progress_callback=progress_callback,
        progress_range=(0.68, 0.97),
    )

    output_path = Path(args.output)
    try:
        canvas.save(output_path, format="PNG")
    except OSError as exc:
        fail(f"Could not save output image '{output_path}': {exc}")
    if progress_callback is not None:
        progress_callback(1.0, "Image terminee")
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
    banned_palette_value = str(values["banned_palette"]).strip()
    font_value = str(values["font"]).strip()

    return argparse.Namespace(
        input=str(values["input"]).strip(),
        output=str(values["output"]).strip(),
        ascii_output=str(values["ascii_output"]).strip() or None,
        ascii_text_output=str(values["ascii_text_output"]).strip() or None,
        ascii_charset=str(values["ascii_charset"]).strip(),
        ascii_font=str(values["ascii_font"]).strip() or None,
        ascii_font_size=int(str(values["ascii_font_size"]).strip()),
        ascii_color=bool(values["ascii_color"]),
        ascii_invert=bool(values["ascii_invert"]),
        video_to_video_input=str(values["video_to_video_input"]).strip() or None,
        video_to_video_output=str(values["video_to_video_output"]).strip() or None,
        columns=parse_optional_int("columns"),
        rows=parse_optional_int("rows"),
        emoji_size=int(str(values["emoji_size"]).strip()),
        scale=parse_float("scale"),
        palette=palette_value or None,
        banned_palette=banned_palette_value or None,
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


def estimate_ascii_for_args(args: argparse.Namespace) -> tuple[float, dict[str, int | str]]:
    image = load_image(args.input)
    columns = args.columns if args.columns is not None else 120
    font_size = getattr(args, "font_size", getattr(args, "ascii_font_size", 12))
    ascii_font = getattr(args, "font", getattr(args, "ascii_font", None))
    font = find_monospace_font(ascii_font, int(font_size))
    cell_width, cell_height = measure_glyph_cell(font)
    rows = compute_ascii_rows(image.width, image.height, columns, args.rows, args.scale, cell_width, cell_height)
    total_cells = columns * rows
    estimated_seconds = 0.15 + (total_cells * 0.00003)
    return estimated_seconds, {
        "columns": columns,
        "rows": rows,
        "total_cells": total_cells,
        "output_kind": Path(args.ascii_output or "").suffix.lower() or ".png",
    }


RECENT_MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".mp4"}
RESULT_ROOT_DIR = Path.cwd() / "result"
RESULT_SUBDIRS = {
    "image_to_image": RESULT_ROOT_DIR / "IMGemoji",
    "image_to_ascii": RESULT_ROOT_DIR / "ASCII",
    "image_to_video": RESULT_ROOT_DIR / "IMGvideo",
    "video_to_video": RESULT_ROOT_DIR / "VIDEOemoji",
}


def make_unique_output_path(path_value: str | None) -> str | None:
    if path_value is None:
        return None
    raw_value = str(path_value).strip()
    if not raw_value:
        return None
    output_path = Path(raw_value)
    if not output_path.exists():
        return str(output_path)
    stem = output_path.stem
    suffix = output_path.suffix
    parent = output_path.parent
    counter = 2
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return str(candidate)
        counter += 1


def build_default_output_path(input_path: Path, suffix: str, extension: str) -> str:
    if extension == ".txt":
        output_dir = RESULT_SUBDIRS["image_to_ascii"]
    elif extension == ".mp4":
        output_dir = RESULT_SUBDIRS["video_to_video"] if suffix == "emoji" else RESULT_SUBDIRS["image_to_video"]
    elif suffix == "ascii":
        output_dir = RESULT_SUBDIRS["image_to_ascii"]
    elif suffix == "progress":
        output_dir = RESULT_SUBDIRS["image_to_video"]
    else:
        output_dir = RESULT_SUBDIRS["image_to_image"]
    output_dir.mkdir(parents=True, exist_ok=True)
    return str(output_dir / f"{input_path.stem}_{suffix}{extension}")


def is_default_output_value(value: str, mode: str) -> bool:
    raw_value = value.strip()
    if not raw_value:
        return True
    path = Path(raw_value)
    expected_dir = RESULT_SUBDIRS[mode]
    default_names = {
        "image_to_image": "result.png",
        "image_to_ascii": "result_ascii.png",
        "image_to_video": "result_progress.gif",
        "video_to_video": "result_video_emoji.mp4",
    }
    return path == expected_dir / default_names[mode] or path.name == default_names[mode]


def discover_recent_media(limit: int = 10) -> list[Path]:
    search_root = RESULT_ROOT_DIR
    search_root.mkdir(parents=True, exist_ok=True)
    seen: set[Path] = set()
    candidates: list[Path] = []
    for candidate in search_root.rglob("*"):
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() not in RECENT_MEDIA_SUFFIXES:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        candidates.append(candidate)
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[:limit]


def build_recent_thumbnail(path: Path, size: tuple[int, int] = (160, 112)) -> Image.Image:
    frame = Image.new("RGBA", size, "#141926")
    draw = ImageDraw.Draw(frame)
    inner_box = (8, 8, size[0] - 8, size[1] - 8)
    draw.rounded_rectangle(inner_box, radius=14, fill="#1b2333", outline="#2b3750", width=1)

    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
        try:
            with Image.open(path) as source:
                preview = source.convert("RGBA")
                preview.thumbnail((size[0] - 16, size[1] - 16), Image.Resampling.LANCZOS)
                framed = ImageOps.pad(preview, (size[0] - 16, size[1] - 16), method=Image.Resampling.LANCZOS, color="#141926")
                frame.alpha_composite(framed, (8, 8))
                return frame
        except (OSError, Image.DecompressionBombError):
            label = f"{path.stem[:16]}\nAperçu désactivé\nImage trop grande"
            draw.multiline_text((18, 20), label, fill="#8ca0c8", spacing=8)
            return frame

    ext_label = path.suffix.upper().replace(".", "") or "FILE"
    label = f"{ext_label}\n{path.stem[:18]}\nAperçu indisponible"
    draw.multiline_text((18, 20), label, fill="#8ca0c8", spacing=8)
    return frame


def launch_gui() -> None:
    if tk is None or filedialog is None or messagebox is None or ttk is None:
        fail(
            "Tkinter is not available in this Python environment. "
            "Install python3-tkinter/python3-tk or run the script with CLI parameters."
        )

    root = tk.Tk()
    root.title("imgEMOJI")
    root.geometry("1380x860")
    root.minsize(1160, 720)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    shell_bg = "#0b1020"
    panel_bg = "#11182b"
    card_bg = "#172033"
    accent = "#4fb3ff"
    text_primary = "#f4f7ff"
    text_muted = "#8ea2c8"
    border = "#24324d"

    root.configure(bg=shell_bg)
    style.configure("App.TFrame", background=shell_bg)
    style.configure("Sidebar.TFrame", background=panel_bg)
    style.configure("Panel.TFrame", background=panel_bg)
    style.configure("Card.TFrame", background=card_bg)
    style.configure("Hero.TLabel", background=shell_bg, foreground=text_primary, font=("TkDefaultFont", 20, "bold"))
    style.configure("Title.TLabel", background=panel_bg, foreground=text_primary, font=("TkDefaultFont", 15, "bold"))
    style.configure("SectionTitle.TLabel", background=card_bg, foreground=text_primary, font=("TkDefaultFont", 11, "bold"))
    style.configure("Muted.TLabel", background=shell_bg, foreground=text_muted)
    style.configure("SidebarMuted.TLabel", background=panel_bg, foreground=text_muted)
    style.configure("FieldTitle.TLabel", background=card_bg, foreground=text_primary, font=("TkDefaultFont", 10, "bold"))
    style.configure("FieldHelp.TLabel", background=card_bg, foreground=text_muted)
    style.configure("Info.TLabel", background=card_bg, foreground=text_muted)
    style.configure("Status.TLabel", background=shell_bg, foreground=text_primary)
    style.configure("Section.TLabelframe", background=card_bg, bordercolor=border, relief="solid")
    style.configure("Section.TLabelframe.Label", background=card_bg, foreground=text_primary, font=("TkDefaultFont", 10, "bold"))
    style.configure("TNotebook", background=shell_bg, borderwidth=0)
    style.configure("TNotebook.Tab", background=panel_bg, foreground=text_muted, padding=(18, 12), font=("TkDefaultFont", 10, "bold"))
    style.map("TNotebook.Tab", background=[("selected", card_bg)], foreground=[("selected", text_primary)])
    style.configure("Dark.Horizontal.TProgressbar", troughcolor="#0f1526", background=accent, bordercolor=border, lightcolor=accent, darkcolor=accent)
    style.configure("Dark.TCheckbutton", background=card_bg, foreground=text_primary)
    style.map("Dark.TCheckbutton", background=[("active", card_bg)], foreground=[("disabled", "#60708f")])
    style.configure("Dark.TCombobox", fieldbackground="#0f1526", background=card_bg, foreground=text_primary, arrowcolor=text_primary)
    style.configure("Dark.TEntry", fieldbackground="#0f1526", background=card_bg, foreground=text_primary, insertcolor=text_primary)
    style.configure("Dark.Vertical.TScrollbar", background=card_bg, troughcolor=shell_bg, bordercolor=border, arrowcolor=text_muted)
    root.option_add("*TCombobox*Listbox*Background", "#0f1526")
    root.option_add("*TCombobox*Listbox*Foreground", text_primary)
    root.option_add("*TCombobox*Listbox*selectBackground", accent)
    root.option_add("*TCombobox*Listbox*selectForeground", "#09101d")
    for output_dir in RESULT_SUBDIRS.values():
        output_dir.mkdir(parents=True, exist_ok=True)

    field_vars = {
        "input": tk.StringVar(),
        "output": tk.StringVar(value=str(RESULT_SUBDIRS["image_to_image"] / "result.png")),
        "ascii_output": tk.StringVar(value=str(RESULT_SUBDIRS["image_to_ascii"] / "result_ascii.png")),
        "ascii_text_output": tk.StringVar(),
        "ascii_charset": tk.StringVar(value=" .,:;!?+=*#%@"),
        "ascii_font": tk.StringVar(),
        "ascii_font_size": tk.StringVar(value="12"),
        "video_output": tk.StringVar(value=str(RESULT_SUBDIRS["image_to_video"] / "result_progress.gif")),
        "video_to_video_input": tk.StringVar(),
        "video_to_video_output": tk.StringVar(value=str(RESULT_SUBDIRS["video_to_video"] / "result_video_emoji.mp4")),
        "columns": tk.StringVar(value="80"),
        "rows": tk.StringVar(),
        "emoji_size": tk.StringVar(value="20"),
        "scale": tk.StringVar(value="1.0"),
        "palette": tk.StringVar(),
        "banned_palette": tk.StringVar(),
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
    ascii_color_var = tk.BooleanVar(value=True)
    ascii_invert_var = tk.BooleanVar(value=False)
    status_var = tk.StringVar(value="Choisissez un mode puis configurez les paramètres communs.")
    estimate_var = tk.StringVar(value="Estimation : n/a")
    progress_detail_var = tk.StringVar(value="En attente")
    progress_var = tk.DoubleVar(value=0.0)
    is_running = {"value": False}
    current_mode = {"value": "image_to_image"}
    recent_banned_emojis: List[str] = []
    browser_state: dict[str, object] = {
        "window": None,
        "search_var": None,
        "selected_info_var": None,
        "visible_banned_limit": INITIAL_BROWSER_RESULTS,
        "visible_available_limit": INITIAL_BROWSER_RESULTS,
    }
    browser_catalog = {"emojis": []}
    restoring_settings = {"value": False}
    recent_preview_refs: list[ImageTk.PhotoImage] = []
    recent_preview_widgets: list[tk.Widget] = []
    hero_preview_refs: list[ImageTk.PhotoImage] = []

    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    outer = ttk.Frame(root, style="App.TFrame", padding=18)
    outer.grid(sticky="nsew")
    outer.columnconfigure(0, weight=0)
    outer.columnconfigure(1, weight=1)
    outer.rowconfigure(0, weight=1)

    sidebar = ttk.Frame(outer, style="Sidebar.TFrame", padding=16)
    sidebar.grid(row=0, column=0, sticky="nsw", padx=(0, 18))
    sidebar.configure(width=310)
    sidebar.grid_propagate(False)
    sidebar.columnconfigure(0, weight=1)
    sidebar.rowconfigure(2, weight=1)

    recent_header = ttk.Frame(sidebar, style="Sidebar.TFrame")
    recent_header.grid(row=0, column=0, sticky="ew")
    recent_header.columnconfigure(0, weight=1)
    ttk.Label(recent_header, text="Récents", style="Title.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(
        recent_header,
        text="Les derniers rendus apparaissent ici avec aperçu. Clique pour les réutiliser.",
        style="SidebarMuted.TLabel",
        wraplength=260,
        justify="left",
    ).grid(row=1, column=0, sticky="w", pady=(6, 0))

    recent_hint = ttk.Label(
        sidebar,
        text="Aucun rendu pour le moment.",
        style="SidebarMuted.TLabel",
        wraplength=260,
        justify="left",
    )
    recent_hint.grid(row=1, column=0, sticky="ew", pady=(14, 12))

    recent_canvas = tk.Canvas(sidebar, background=panel_bg, highlightthickness=0, bd=0)
    recent_canvas.grid(row=2, column=0, sticky="nsew")
    recent_scrollbar = tk.Scrollbar(
        sidebar,
        orient="vertical",
        command=recent_canvas.yview,
        relief="flat",
        bd=0,
        bg="#202b41",
        troughcolor="#0f1526",
        activebackground="#2c3a57",
        highlightthickness=0,
    )
    recent_scrollbar.grid(row=2, column=1, sticky="ns")
    recent_canvas.configure(yscrollcommand=recent_scrollbar.set)
    recent_gallery = ttk.Frame(recent_canvas, style="Sidebar.TFrame")
    recent_gallery.columnconfigure(0, weight=1)
    recent_gallery_window = recent_canvas.create_window((0, 0), window=recent_gallery, anchor="nw")

    workspace = ttk.Frame(outer, style="App.TFrame")
    workspace.grid(row=0, column=1, sticky="nsew")
    workspace.columnconfigure(0, weight=1)
    workspace.rowconfigure(0, weight=1)

    canvas = tk.Canvas(
        workspace,
        background=shell_bg,
        highlightthickness=0,
        bd=0,
    )
    canvas.grid(row=0, column=0, sticky="nsew")

    scrollbar = tk.Scrollbar(
        workspace,
        orient="vertical",
        command=canvas.yview,
        relief="flat",
        bd=0,
        bg="#202b41",
        troughcolor="#0f1526",
        activebackground="#2c3a57",
        highlightthickness=0,
    )
    scrollbar.grid(row=0, column=1, sticky="ns")
    canvas.configure(yscrollcommand=scrollbar.set)

    container = ttk.Frame(canvas, padding=6, style="App.TFrame")
    container.columnconfigure(0, weight=1)
    container.rowconfigure(1, weight=1)
    canvas_window = canvas.create_window((0, 0), window=container, anchor="nw")

    def update_scroll_region(_event: object | None = None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    def update_canvas_width(event: object) -> None:
        width = getattr(event, "width", None)
        if width is not None:
            canvas.itemconfigure(canvas_window, width=width)

    def update_recent_scroll_region(_event: object | None = None) -> None:
        recent_canvas.configure(scrollregion=recent_canvas.bbox("all"))

    def update_recent_canvas_width(event: object) -> None:
        width = getattr(event, "width", None)
        if width is not None:
            recent_canvas.itemconfigure(recent_gallery_window, width=width)

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
    recent_gallery.bind("<Configure>", update_recent_scroll_region)
    recent_canvas.bind("<Configure>", update_recent_canvas_width)
    canvas.bind_all("<MouseWheel>", on_mousewheel)
    canvas.bind_all("<Button-4>", on_linux_scroll_up)
    canvas.bind_all("<Button-5>", on_linux_scroll_down)

    header = ttk.Frame(container, style="App.TFrame")
    header.grid(row=0, column=0, sticky="ew")
    header.columnconfigure(0, weight=1)
    header.columnconfigure(1, weight=0)
    ttk.Label(header, text="imgEMOJI Studio", style="Hero.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(
        header,
        text="Un flux unique pour générer en emoji ou en ASCII, avec galerie récente, progression lisible et sorties jamais écrasées.",
        style="Muted.TLabel",
        wraplength=980,
        justify="left",
    ).grid(row=1, column=0, sticky="w", pady=(4, 0))

    main_frame = ttk.Frame(container, style="App.TFrame")
    main_frame.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
    main_frame.columnconfigure(0, weight=1)
    main_frame.rowconfigure(1, weight=1)

    intro_frame = ttk.Frame(main_frame, style="Panel.TFrame", padding=28)
    intro_frame.grid(row=0, column=0, sticky="nsew")
    intro_frame.columnconfigure(0, weight=1)

    intro_card = ttk.Frame(intro_frame, style="Card.TFrame", padding=28)
    intro_card.grid(row=0, column=0, sticky="ew")
    intro_card.columnconfigure(0, weight=1)
    ttk.Label(intro_card, text="Choisis un mode pour commencer", style="Title.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(
        intro_card,
        text="La galerie récente reste à gauche. Clique sur + pour ouvrir uniquement le formulaire du mode voulu, puis lance la génération.",
        style="Info.TLabel",
        wraplength=760,
        justify="left",
    ).grid(row=1, column=0, sticky="w", pady=(8, 0))
    intro_generate_button = tk.Button(
        intro_card,
        text="+ Générer",
        command=lambda: None,
        relief="flat",
        bd=0,
        padx=22,
        pady=14,
        font=("TkDefaultFont", 12, "bold"),
        bg=accent,
        fg="#08101d",
        activebackground="#7ac8ff",
        activeforeground="#08101d",
        cursor="hand2",
    )
    intro_generate_button.grid(row=2, column=0, sticky="w", pady=(18, 0))

    hero_preview_card = ttk.Frame(intro_frame, style="Card.TFrame", padding=18)
    hero_preview_card.grid(row=1, column=0, sticky="nsew", pady=(18, 0))
    hero_preview_card.columnconfigure(0, weight=1)
    ttk.Label(hero_preview_card, text="Aperçu", style="Title.TLabel").grid(row=0, column=0, sticky="w")
    hero_preview_label = tk.Label(
        hero_preview_card,
        text="Clique sur un rendu récent pour l'afficher ici.",
        justify="center",
        wraplength=760,
        bg=card_bg,
        fg=text_muted,
        padx=16,
        pady=16,
    )
    hero_preview_label.grid(row=1, column=0, sticky="nsew", pady=(12, 0))

    editor_frame = ttk.Frame(main_frame, style="App.TFrame")
    editor_frame.grid(row=1, column=0, sticky="nsew")
    editor_frame.columnconfigure(0, weight=1)
    editor_frame.rowconfigure(1, weight=1)
    editor_frame.grid_remove()

    shared_frame = ttk.LabelFrame(editor_frame, text="Configuration", padding=16, style="Section.TLabelframe")
    shared_frame.grid(row=1, column=0, sticky="nsew", pady=(14, 0))
    shared_frame.columnconfigure(0, weight=1)
    shared_frame.rowconfigure(13, weight=1)

    def create_action_button(
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        accent_button: bool = False,
        width: int | None = None,
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            relief="flat",
            bd=0,
            padx=16,
            pady=10,
            font=("TkDefaultFont", 10, "bold"),
            bg=accent if accent_button else "#202b41",
            fg="#08101d" if accent_button else text_primary,
            activebackground="#7ac8ff" if accent_button else "#2c3a57",
            activeforeground="#08101d" if accent_button else text_primary,
            highlightthickness=0,
            cursor="hand2",
        )

    shared_control_rows: dict[str, tk.Widget] = {}

    def add_row(
        parent: ttk.Frame,
        row: int,
        label: str,
        description: str,
        key: str,
        browse: str | None = None,
        button_text: str = "Parcourir",
        width: int = 18,
        stretch: bool = False,
    ) -> tk.Entry:
        row_frame = ttk.Frame(parent, style="Card.TFrame")
        row_frame.grid(row=row, column=0, sticky="ew", pady=5)
        shared_control_rows[key] = row_frame
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
        entry = tk.Entry(
            control_frame,
            textvariable=field_vars[key],
            width=width,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=accent,
            bg="#0f1526",
            fg=text_primary,
            insertbackground=text_primary,
        )
        entry.grid(row=0, column=0, sticky="ew" if stretch else "w")
        if browse == "open":
            create_action_button(
                control_frame,
                text=button_text,
                command=lambda: choose_input_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "video_input":
            create_action_button(
                control_frame,
                text=button_text,
                command=lambda: choose_video_input_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "save":
            create_action_button(
                control_frame,
                text=button_text,
                command=lambda: choose_output_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "ascii_save":
            create_action_button(
                control_frame,
                text=button_text,
                command=lambda: choose_ascii_output_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "ascii_text_save":
            create_action_button(
                control_frame,
                text=button_text,
                command=lambda: choose_ascii_text_output_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "mp4_save":
            create_action_button(
                control_frame,
                text=button_text,
                command=lambda: choose_video_to_video_output_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "font":
            create_action_button(
                control_frame,
                text=button_text,
                command=lambda: choose_font_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "ascii_font":
            create_action_button(
                control_frame,
                text=button_text,
                command=lambda: choose_ascii_font_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "video":
            create_action_button(
                control_frame,
                text=button_text,
                command=lambda: choose_video_file(),
            ).grid(row=0, column=1, sticky="w", padx=(10, 0))
        elif browse == "banned_browser":
            create_action_button(
                control_frame,
                text=button_text,
                command=lambda: open_banned_emoji_browser(),
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
        if is_default_output_value(field_vars["output"].get(), "image_to_image"):
            field_vars["output"].set(build_default_output_path(input_path, "emoji", ".png"))
        if is_default_output_value(field_vars["ascii_output"].get(), "image_to_ascii"):
            field_vars["ascii_output"].set(build_default_output_path(input_path, "ascii", ".png"))
        if is_default_output_value(field_vars["video_output"].get(), "image_to_video"):
            field_vars["video_output"].set(build_default_output_path(input_path, "progress", ".gif"))

    def choose_output_file() -> None:
        RESULT_SUBDIRS["image_to_image"].mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="Choisir le fichier de sortie",
            defaultextension=".png",
            initialdir=str(RESULT_SUBDIRS["image_to_image"]),
            filetypes=[("PNG", "*.png"), ("Tous les fichiers", "*.*")],
        )
        if path:
            field_vars["output"].set(path)

    def choose_ascii_output_file() -> None:
        RESULT_SUBDIRS["image_to_ascii"].mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="Choisir la sortie ASCII",
            defaultextension=".png",
            initialdir=str(RESULT_SUBDIRS["image_to_ascii"]),
            filetypes=[("PNG", "*.png"), ("Texte", "*.txt"), ("Tous les fichiers", "*.*")],
        )
        if path:
            field_vars["ascii_output"].set(path)

    def choose_ascii_text_output_file() -> None:
        RESULT_SUBDIRS["image_to_ascii"].mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="Choisir le fichier texte ASCII",
            defaultextension=".txt",
            initialdir=str(RESULT_SUBDIRS["image_to_ascii"]),
            filetypes=[("Texte", "*.txt"), ("Tous les fichiers", "*.*")],
        )
        if path:
            field_vars["ascii_text_output"].set(path)

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

    def choose_ascii_font_file() -> None:
        path = filedialog.askopenfilename(
            title="Choisir une police monospace ASCII",
            filetypes=[
                ("Polices", "*.ttf *.ttc *.otf"),
                ("Tous les fichiers", "*.*"),
            ],
        )
        if path:
            field_vars["ascii_font"].set(path)

    def choose_video_file() -> None:
        RESULT_SUBDIRS["image_to_video"].mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="Choisir le fichier vidéo/animation",
            defaultextension=".gif",
            initialdir=str(RESULT_SUBDIRS["image_to_video"]),
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
        if is_default_output_value(field_vars["video_to_video_output"].get(), "video_to_video"):
            field_vars["video_to_video_output"].set(build_default_output_path(input_path, "emoji", ".mp4"))

    def choose_video_to_video_output_file() -> None:
        RESULT_SUBDIRS["video_to_video"].mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="Choisir la vidéo de sortie",
            defaultextension=".mp4",
            initialdir=str(RESULT_SUBDIRS["video_to_video"]),
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
    def save_current_gui_settings(*_args: object) -> None:
        if restoring_settings["value"]:
            return
        settings = {
            "columns": field_vars["columns"].get(),
            "rows": field_vars["rows"].get(),
            "emoji_size": field_vars["emoji_size"].get(),
            "scale": field_vars["scale"].get(),
            "ascii_charset": field_vars["ascii_charset"].get(),
            "ascii_font_size": field_vars["ascii_font_size"].get(),
            "palette": field_vars["palette"].get(),
            "banned_palette": field_vars["banned_palette"].get(),
            "background": field_vars["background"].get(),
            "emoji_source": field_vars["emoji_source"].get(),
            "alpha_threshold": field_vars["alpha_threshold"].get(),
            "video_fps": field_vars["video_fps"].get(),
            "video_start_columns": field_vars["video_start_columns"].get(),
            "video_max_columns": field_vars["video_max_columns"].get(),
            "video_step_columns": field_vars["video_step_columns"].get(),
            "stretch": stretch_var.get(),
            "ascii_color": ascii_color_var.get(),
            "ascii_invert": ascii_invert_var.get(),
            "recent_banned_emojis": recent_banned_emojis,
        }
        save_gui_settings(settings)

    def restore_gui_settings() -> None:
        settings = load_gui_settings()
        if not settings:
            field_vars["banned_palette"].set(encode_emoji_list(DEFAULT_BANNED_EMOJIS))
            recent_banned_emojis[:] = list(DEFAULT_BANNED_EMOJIS)
            return
        restoring_settings["value"] = True
        try:
            for key in (
                "columns",
                "rows",
                "emoji_size",
                "scale",
                "ascii_charset",
                "ascii_font_size",
                "palette",
                "banned_palette",
                "background",
                "emoji_source",
                "alpha_threshold",
                "video_fps",
                "video_start_columns",
                "video_max_columns",
                "video_step_columns",
            ):
                value = settings.get(key)
                if isinstance(value, str):
                    field_vars[key].set(value)
            if not field_vars["banned_palette"].get().strip():
                field_vars["banned_palette"].set(encode_emoji_list(DEFAULT_BANNED_EMOJIS))
            stretch_value = settings.get("stretch")
            if isinstance(stretch_value, bool):
                stretch_var.set(stretch_value)
            ascii_color_value = settings.get("ascii_color")
            if isinstance(ascii_color_value, bool):
                ascii_color_var.set(ascii_color_value)
            ascii_invert_value = settings.get("ascii_invert")
            if isinstance(ascii_invert_value, bool):
                ascii_invert_var.set(ascii_invert_value)
            saved_recent = settings.get("recent_banned_emojis")
            if isinstance(saved_recent, list):
                recent_banned_emojis[:] = [str(item) for item in saved_recent if isinstance(item, str)]
            else:
                recent_banned_emojis[:] = list(DEFAULT_BANNED_EMOJIS)
        finally:
            restoring_settings["value"] = False

    def get_banned_emojis() -> List[str]:
        raw_value = field_vars["banned_palette"].get().strip()
        return parse_emoji_list(raw_value or None, default=DEFAULT_BANNED_EMOJIS)

    def set_banned_emojis(emojis: Sequence[str]) -> None:
        field_vars["banned_palette"].set(encode_emoji_list(parse_emoji_list(encode_emoji_list(emojis))))

    def touch_recent_emoji(emoji: str) -> None:
        if emoji in recent_banned_emojis:
            recent_banned_emojis.remove(emoji)
        recent_banned_emojis.insert(0, emoji)
        del recent_banned_emojis[16:]

    def build_browser_candidates() -> List[str]:
        palette_value = field_vars["palette"].get().strip()
        custom_palette = parse_emoji_list(palette_value or None, default=SQUARE_FIRST_DEFAULT_PALETTE)
        banned = get_banned_emojis()
        catalog = browser_catalog["emojis"] if isinstance(browser_catalog.get("emojis"), list) else []
        ordered: List[str] = []
        seen: set[str] = set()
        for emoji in recent_banned_emojis + banned + custom_palette + list(DEFAULT_BANNED_EMOJIS) + list(SQUARE_FIRST_DEFAULT_PALETTE) + list(catalog):
            if emoji and emoji not in seen:
                ordered.append(emoji)
                seen.add(emoji)
        return ordered

    def get_browser_photo_image(
        emoji: str,
        image_cache: dict[str, ImageTk.PhotoImage | None],
        tile_size: int = 36,
    ) -> ImageTk.PhotoImage | None:
        if ImageTk is None:
            return None
        if emoji in image_cache:
            return image_cache[emoji]
        try:
            tile = fetch_twemoji_tile(emoji, tile_size)
        except EmojiMakerError:
            image_cache[emoji] = None
            return None
        image_cache[emoji] = ImageTk.PhotoImage(tile)
        return image_cache[emoji]

    def open_banned_emoji_browser() -> None:
        existing_window = browser_state.get("window")
        if isinstance(existing_window, tk.Toplevel) and existing_window.winfo_exists():
            existing_window.deiconify()
            existing_window.lift()
            existing_window.focus_force()
            return

        if not browser_catalog["emojis"]:
            try:
                browser_catalog["emojis"] = get_twemoji_browser_catalog()
            except EmojiMakerError as exc:
                messagebox.showerror("Catalogue Twemoji", str(exc))
                return

        browser_window = tk.Toplevel(root)
        browser_window.title("Emojis bannis")
        browser_window.geometry("860x640")
        browser_window.minsize(720, 480)
        browser_window.configure(bg=shell_bg)
        browser_window.transient(root)
        browser_window.columnconfigure(0, weight=1)
        browser_window.rowconfigure(1, weight=1)
        browser_state["window"] = browser_window
        browser_state["search_var"] = tk.StringVar()
        browser_state["selected_info_var"] = tk.StringVar(
            value="Clique sur un emoji pour l'ajouter aux bannis ou le retirer."
        )
        browser_state["visible_banned_limit"] = INITIAL_BROWSER_RESULTS
        browser_state["visible_available_limit"] = INITIAL_BROWSER_RESULTS

        image_cache: dict[str, ImageTk.PhotoImage | None] = {}

        header_frame = ttk.Frame(browser_window, padding=16, style="App.TFrame")
        header_frame.grid(row=0, column=0, sticky="ew")
        header_frame.columnconfigure(1, weight=1)
        ttk.Label(header_frame, text="Emojis bannis", style="Hero.TLabel").grid(row=0, column=0, sticky="w")
        tk.Entry(
            header_frame,
            textvariable=browser_state["search_var"],
            width=28,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=accent,
            bg="#0f1526",
            fg=text_primary,
            insertbackground=text_primary,
        ).grid(row=0, column=1, sticky="e")
        ttk.Label(
            header_frame,
            text="Recherche par emoji, nom Unicode, ou codepoint. Les plus recents restent en haut.",
            style="Muted.TLabel",
            wraplength=780,
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        content_frame = ttk.Frame(browser_window, style="App.TFrame")
        content_frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))
        content_frame.columnconfigure(0, weight=1)
        content_frame.rowconfigure(0, weight=1)

        browser_canvas = tk.Canvas(content_frame, background=shell_bg, highlightthickness=0, bd=0)
        browser_canvas.grid(row=0, column=0, sticky="nsew")
        browser_scrollbar = tk.Scrollbar(
            content_frame,
            orient="vertical",
            command=browser_canvas.yview,
            relief="flat",
            bd=0,
            bg="#202b41",
            troughcolor="#0f1526",
            activebackground="#2c3a57",
            highlightthickness=0,
        )
        browser_scrollbar.grid(row=0, column=1, sticky="ns")
        browser_canvas.configure(yscrollcommand=browser_scrollbar.set)

        tiles_frame = ttk.Frame(browser_canvas, style="App.TFrame")
        tiles_frame.columnconfigure(0, weight=1)
        browser_canvas_window = browser_canvas.create_window((0, 0), window=tiles_frame, anchor="nw")

        def update_browser_scroll_region(_event: object | None = None) -> None:
            browser_canvas.configure(scrollregion=browser_canvas.bbox("all"))

        def update_browser_canvas_width(event: object) -> None:
            width = getattr(event, "width", None)
            if width is not None:
                browser_canvas.itemconfigure(browser_canvas_window, width=width)

        tiles_frame.bind("<Configure>", update_browser_scroll_region)
        browser_canvas.bind("<Configure>", update_browser_canvas_width)

        footer_frame = ttk.Frame(browser_window, padding=(16, 0, 16, 16), style="App.TFrame")
        footer_frame.grid(row=2, column=0, sticky="ew")
        footer_frame.columnconfigure(0, weight=1)
        ttk.Label(
            footer_frame,
            textvariable=browser_state["selected_info_var"],
            wraplength=780,
            justify="left",
            style="Status.TLabel",
        ).grid(row=0, column=0, sticky="w")

        def update_banned_field_and_recent(emoji: str, should_ban: bool) -> None:
            current_banned = get_banned_emojis()
            if should_ban and emoji not in current_banned:
                current_banned.append(emoji)
            elif not should_ban:
                current_banned = [item for item in current_banned if item != emoji]
            set_banned_emojis(current_banned)
            touch_recent_emoji(emoji)

        def render_browser(*_args: object) -> None:
            for child in tiles_frame.winfo_children():
                child.destroy()

            raw_search = str(browser_state["search_var"].get()).strip().lower()
            search_text = raw_search.replace("u+", "").replace("-", "").replace(" ", "")
            banned_emojis = get_banned_emojis()
            candidate_emojis = build_browser_candidates()
            candidate_order = {emoji: index for index, emoji in enumerate(candidate_emojis)}

            def emoji_matches(emoji: str) -> bool:
                if not raw_search:
                    return True
                codepoints = "".join(f"{ord(char):x}" for char in emoji if ord(char) != 0xFE0F)
                name = describe_emoji(emoji).lower()
                return (
                    raw_search in emoji.lower()
                    or raw_search in name
                    or search_text in codepoints
                )

            def ordered_subset(emojis: Sequence[str]) -> List[str]:
                recent_order = {emoji: index for index, emoji in enumerate(recent_banned_emojis)}
                return sorted(
                    [emoji for emoji in emojis if emoji_matches(emoji)],
                    key=lambda emoji: (recent_order.get(emoji, 9999), candidate_order.get(emoji, 9999)),
                )
            all_visible_banned = ordered_subset(banned_emojis)
            all_visible_available = ordered_subset([emoji for emoji in candidate_emojis if emoji not in banned_emojis])
            banned_limit = int(browser_state.get("visible_banned_limit", INITIAL_BROWSER_RESULTS))
            available_limit = int(browser_state.get("visible_available_limit", INITIAL_BROWSER_RESULTS))
            visible_banned = all_visible_banned[:banned_limit]
            visible_available = all_visible_available[:available_limit]
            sections = [
                ("Bannis", visible_banned, all_visible_banned, False, "visible_banned_limit"),
                ("Twemoji disponibles", visible_available, all_visible_available, True, "visible_available_limit"),
            ]

            row = 0
            visible_count = 0
            for title, emojis, full_matches, should_ban, limit_key in sections:
                section = ttk.LabelFrame(tiles_frame, text=title, padding=12, style="Section.TLabelframe")
                section.grid(row=row, column=0, sticky="ew", pady=(0, 10))
                for column in range(4):
                    section.columnconfigure(column, weight=1)
                row += 1

                tile_index = 0
                for emoji in emojis:
                    photo = get_browser_photo_image(emoji, image_cache)
                    if photo is None:
                        continue
                    visible_count += 1
                    card = ttk.Frame(section, style="Card.TFrame", padding=6)
                    card.grid(row=tile_index // 4, column=tile_index % 4, sticky="nsew", padx=6, pady=6)
                    button = ttk.Button(
                        card,
                        image=photo,
                        text=f"{emoji}\n{describe_emoji(emoji)}",
                        compound="top",
                        width=18,
                        command=lambda emoji=emoji, should_ban=should_ban: on_browser_emoji_click(emoji, should_ban),
                    )
                    button.image = photo
                    button.grid(row=0, column=0, sticky="nsew")
                    tile_index += 1

                if tile_index == 0:
                    ttk.Label(
                        section,
                        text="Aucun asset Twemoji disponible pour cette section.",
                        style="Muted.TLabel",
                        wraplength=720,
                        justify="left",
                    ).grid(row=0, column=0, sticky="w")
                elif len(full_matches) > len(emojis):
                    remaining = len(full_matches) - len(emojis)
                    ttk.Button(
                        section,
                        text=f"Charger plus ({remaining} restant)",
                        command=lambda limit_key=limit_key: on_load_more(limit_key),
                    ).grid(row=(tile_index // 4) + 1, column=0, sticky="w", padx=6, pady=(8, 0))

            if visible_count == 0:
                browser_state["selected_info_var"].set(
                    "Aucun emoji Twemoji visible avec ce filtre. Essaie un autre nom ou codepoint."
                )
            else:
                browser_state["selected_info_var"].set(
                    f"{len(all_visible_banned)} banni(s) trouves | {len(all_visible_available)} emoji(s) Twemoji trouves. "
                    f"Affichage progressif pour ouvrir la fenetre plus vite."
                )

        def on_browser_emoji_click(emoji: str, should_ban: bool) -> None:
            update_banned_field_and_recent(emoji, should_ban)
            action = "ajoute aux bannis" if should_ban else "retire des bannis"
            browser_state["selected_info_var"].set(f"{emoji} {action} | {describe_emoji(emoji)}")
            render_browser()

        def on_load_more(limit_key: str) -> None:
            current_limit = int(browser_state.get(limit_key, INITIAL_BROWSER_RESULTS))
            browser_state[limit_key] = current_limit + LOAD_MORE_BROWSER_RESULTS
            render_browser()

        def close_browser() -> None:
            browser_state["window"] = None
            browser_state["search_var"] = None
            browser_state["selected_info_var"] = None
            browser_window.destroy()

        browser_window.protocol("WM_DELETE_WINDOW", close_browser)
        def on_search_change(*_args: object) -> None:
            browser_state["visible_banned_limit"] = INITIAL_BROWSER_RESULTS
            browser_state["visible_available_limit"] = INITIAL_BROWSER_RESULTS
            render_browser()

        browser_state["search_var"].trace_add("write", on_search_change)
        render_browser()

    add_row(
        shared_frame, 6, "Emojis bannis",
        "Liste d'emojis Ã  exclure de la palette finale, ou chemin vers un fichier texte.",
        "banned_palette", browse="banned_browser", button_text="Voir", width=34,
    )
    add_row(
        shared_frame, 7, "Fond",
        "Couleur des zones vides : `transparent`, `white`, `#112233`, etc.",
        "background", width=16,
    )
    add_row(
        shared_frame, 8, "Police emoji",
        "Optionnel. À renseigner si l'auto-détection ne trouve pas de bonne police.",
        "font", browse="font", width=34,
    )
    add_row(
        shared_frame, 9, "Seuil alpha",
        "Ignore les zones presque transparentes. 0.05 est une bonne base.",
        "alpha_threshold", width=10,
    )

    source_row = ttk.Frame(shared_frame, style="Card.TFrame")
    source_row.grid(row=10, column=0, sticky="ew", pady=5)
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
    source_combo = tk.OptionMenu(source_row, field_vars["emoji_source"], "auto", "font", "twemoji")
    source_combo.configure(
        relief="flat",
        bd=0,
        highlightthickness=1,
        highlightbackground=border,
        highlightcolor=accent,
        bg="#0f1526",
        fg=text_primary,
        activebackground="#202b41",
        activeforeground=text_primary,
        width=12,
    )
    source_combo["menu"].configure(
        bg="#0f1526",
        fg=text_primary,
        activebackground=accent,
        activeforeground="#08101d",
        bd=0,
    )
    source_combo.grid(row=0, column=1, sticky="e")

    stretch_row = ttk.Frame(shared_frame, style="Card.TFrame")
    stretch_row.grid(row=11, column=0, sticky="ew", pady=(8, 4))
    stretch_row.columnconfigure(0, weight=1)
    ttk.Checkbutton(
        stretch_row,
        text="Autoriser l'étirement si colonnes + lignes sont définies",
        variable=stretch_var,
        style="Dark.TCheckbutton",
    ).grid(row=0, column=0, sticky="w")
    ttk.Label(
        stretch_row,
        text="À activer seulement si tu veux forcer une grille qui ne respecte pas les proportions d'origine.",
        style="FieldHelp.TLabel",
        wraplength=860,
        justify="left",
    ).grid(row=1, column=0, sticky="w", pady=(2, 0))

    help_text = (
        "Ces réglages s'appliquent au mode choisi. Ils restent masqués tant que tu n'as pas sélectionné un type de génération."
    )
    shared_help_label = ttk.Label(shared_frame, text=help_text, wraplength=860, justify="left", style="Info.TLabel")
    shared_help_label.grid(
        row=12, column=0, sticky="w", pady=(8, 0)
    )

    shared_mode_visibility = {
        "image_to_image": {"input", "columns", "rows", "emoji_size", "scale", "palette", "banned_palette", "background", "font", "alpha_threshold", "emoji_source", "stretch", "shared_help"},
        "image_to_ascii": {"input", "columns", "rows", "scale", "background", "shared_help"},
        "image_to_video": {"input", "columns", "rows", "emoji_size", "scale", "palette", "banned_palette", "background", "font", "alpha_threshold", "emoji_source", "stretch", "shared_help"},
        "video_to_video": {"columns", "rows", "emoji_size", "scale", "palette", "banned_palette", "background", "font", "alpha_threshold", "emoji_source", "stretch", "shared_help"},
    }

    def update_shared_fields_visibility(mode: str) -> None:
        visible_keys = shared_mode_visibility.get(mode, set())
        for key, widget in shared_control_rows.items():
            if key in visible_keys:
                widget.grid()
            else:
                widget.grid_remove()
        if "emoji_source" in visible_keys:
            source_row.grid()
        else:
            source_row.grid_remove()
        if "stretch" in visible_keys:
            stretch_row.grid()
        else:
            stretch_row.grid_remove()
        if "shared_help" in visible_keys:
            shared_help_label.grid()
        else:
            shared_help_label.grid_remove()

    mode_titles = {
        "image_to_image": "Image -> Image",
        "image_to_ascii": "Image -> ASCII",
        "image_to_video": "Image -> Vidéo",
        "video_to_video": "Vidéo -> Vidéo",
    }
    generate_labels = {
        "image_to_image": "Générer l'image",
        "image_to_ascii": "Générer l'ASCII",
        "image_to_video": "Créer la vidéo",
        "video_to_video": "Créer la vidéo emoji",
    }
    mode_summary_var = tk.StringVar(value="Mode actif : Image -> Image")

    mode_bar = ttk.Frame(editor_frame, style="Panel.TFrame", padding=16)
    mode_bar.grid(row=0, column=0, sticky="ew")
    mode_bar.columnconfigure(0, weight=1)
    ttk.Label(mode_bar, textvariable=mode_summary_var, style="Title.TLabel").grid(row=0, column=0, sticky="w")
    ttk.Label(
        mode_bar,
        text="Seul le mode actif reste affiché. Reviens à l'accueil ou change de mode avec +.",
        style="SidebarMuted.TLabel",
        wraplength=640,
        justify="left",
    ).grid(row=1, column=0, sticky="w", pady=(6, 0))

    mode_menu_button = tk.Button(
        mode_bar,
        text="+ Générer",
        command=lambda: None,
        relief="flat",
        bd=0,
        padx=18,
        pady=10,
        font=("TkDefaultFont", 11, "bold"),
        bg="#202b41",
        fg=text_primary,
        activebackground="#2c3a57",
        activeforeground=text_primary,
        cursor="hand2",
    )
    mode_menu_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(16, 12))

    home_button = create_action_button(mode_bar, "Accueil", lambda: None)
    home_button.grid(row=0, column=2, rowspan=2, sticky="e", padx=(0, 12))

    mode_content = ttk.Frame(shared_frame, style="App.TFrame")
    mode_content.grid(row=13, column=0, sticky="nsew", pady=(18, 0))
    mode_content.columnconfigure(0, weight=1)
    mode_content.rowconfigure(0, weight=1)

    image_tab = ttk.Frame(mode_content, padding=(0, 0, 0, 0), style="App.TFrame")
    ascii_tab = ttk.Frame(mode_content, padding=(0, 0, 0, 0), style="App.TFrame")
    video_tab = ttk.Frame(mode_content, padding=(0, 0, 0, 0), style="App.TFrame")
    future_tab = ttk.Frame(mode_content, padding=(0, 0, 0, 0), style="App.TFrame")
    for tab in (image_tab, ascii_tab, video_tab, future_tab):
        tab.columnconfigure(0, weight=1)
        tab.grid(row=0, column=0, sticky="nsew")
        tab.grid_remove()

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
    ascii_card = ttk.LabelFrame(ascii_tab, text="Sortie ASCII", padding=16, style="Section.TLabelframe")
    ascii_card.grid(row=0, column=0, sticky="ew")
    ascii_card.columnconfigure(0, weight=1)
    add_row(
        ascii_card, 0, "Sortie ASCII",
        "Le fichier final ASCII, en `.png` ou `.txt`.",
        "ascii_output", browse="ascii_save", width=38, stretch=True,
    )
    add_row(
        ascii_card, 1, "Texte ASCII",
        "Optionnel. Sauvegarde aussi la version texte brute dans un fichier `.txt`.",
        "ascii_text_output", browse="ascii_text_save", width=38, stretch=True,
    )
    add_row(
        ascii_card, 2, "Charset",
        "Caractères du plus clair au plus sombre. Exemple : ` .,:;!?+=*#%@`.",
        "ascii_charset", width=26,
    )
    add_row(
        ascii_card, 3, "Police ASCII",
        "Optionnel. Police monospace utilisée pour le rendu PNG ASCII.",
        "ascii_font", browse="ascii_font", width=34,
    )
    add_row(
        ascii_card, 4, "Taille police",
        "Taille de la police pour le rendu PNG ASCII.",
        "ascii_font_size", width=10,
    )
    ascii_options = ttk.Frame(ascii_card, style="Card.TFrame")
    ascii_options.grid(row=5, column=0, sticky="ew", pady=(8, 0))
    ascii_options.columnconfigure(0, weight=1)
    ttk.Checkbutton(
        ascii_options,
        text="Rendu coloré",
        variable=ascii_color_var,
        style="Dark.TCheckbutton",
    ).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(
        ascii_options,
        text="Inverser le charset",
        variable=ascii_invert_var,
        style="Dark.TCheckbutton",
    ).grid(row=0, column=1, sticky="w", padx=(18, 0))
    ttk.Label(
        ascii_card,
        text="Le mode ASCII réutilise l'image d'entrée, les colonnes, lignes, scale et le fond définis dans les paramètres communs.",
        style="Info.TLabel",
        wraplength=760,
        justify="left",
    ).grid(row=6, column=0, sticky="w", pady=(10, 0))
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
    footer = ttk.Frame(editor_frame, style="App.TFrame")
    footer.grid(row=2, column=0, sticky="ew", pady=(14, 0))
    footer.columnconfigure(0, weight=1)
    generate_button = create_action_button(footer, "Générer l'image", lambda: None, accent_button=True)
    generate_button.grid(row=0, column=1, sticky="e", pady=(0, 12))
    status_label = ttk.Label(footer, textvariable=status_var, wraplength=900, justify="left", style="Status.TLabel")
    status_label.grid(row=1, column=0, columnspan=2, sticky="ew")
    detail_label = ttk.Label(footer, textvariable=progress_detail_var, wraplength=900, justify="left", style="Muted.TLabel")
    detail_label.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
    progressbar = ttk.Progressbar(footer, variable=progress_var, maximum=100.0, style="Dark.Horizontal.TProgressbar")
    progressbar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
    estimate_label = ttk.Label(footer, textvariable=estimate_var, wraplength=900, justify="left", style="Muted.TLabel")
    estimate_label.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def walk_children(widget: tk.Misc) -> list[tk.Misc]:
        nodes = [widget]
        for child in widget.winfo_children():
            nodes.extend(walk_children(child))
        return nodes

    def set_controls_state(state: str) -> None:
        for child in walk_children(outer):
            if isinstance(child, tk.Button):
                child.configure(state="disabled" if state == "disabled" else "normal")
                continue
            if isinstance(child, tk.Menubutton):
                child.configure(state="disabled" if state == "disabled" else "normal")
                continue
            if isinstance(child, (ttk.Entry, ttk.Button, ttk.Combobox, ttk.Checkbutton)):
                try:
                    child.state([state] if state in ("disabled", "!disabled") else [])
                except tk.TclError:
                    pass
        mode_menu_button.configure(state="disabled" if state == "disabled" else "normal")
        home_button.configure(state="disabled" if state == "disabled" else "normal")
        generate_button.configure(state="disabled" if state == "disabled" else "normal")

    def collect_values() -> dict[str, str | bool]:
        return {
            "input": field_vars["input"].get(),
            "output": field_vars["output"].get(),
            "ascii_output": field_vars["ascii_output"].get(),
            "ascii_text_output": field_vars["ascii_text_output"].get(),
            "ascii_charset": field_vars["ascii_charset"].get(),
            "ascii_font": field_vars["ascii_font"].get(),
            "ascii_font_size": field_vars["ascii_font_size"].get(),
            "video_output": field_vars["video_output"].get(),
            "video_to_video_input": field_vars["video_to_video_input"].get(),
            "video_to_video_output": field_vars["video_to_video_output"].get(),
            "columns": field_vars["columns"].get(),
            "rows": field_vars["rows"].get(),
            "emoji_size": field_vars["emoji_size"].get(),
            "scale": field_vars["scale"].get(),
            "palette": field_vars["palette"].get(),
            "banned_palette": field_vars["banned_palette"].get(),
            "background": field_vars["background"].get(),
            "font": field_vars["font"].get(),
            "emoji_source": field_vars["emoji_source"].get(),
            "alpha_threshold": field_vars["alpha_threshold"].get(),
            "video_fps": field_vars["video_fps"].get(),
            "video_start_columns": field_vars["video_start_columns"].get(),
            "video_max_columns": field_vars["video_max_columns"].get(),
            "video_step_columns": field_vars["video_step_columns"].get(),
            "stretch": stretch_var.get(),
            "ascii_color": ascii_color_var.get(),
            "ascii_invert": ascii_invert_var.get(),
        }

    def get_active_mode() -> str:
        return current_mode["value"]

    def open_mode_menu(anchor_widget: tk.Widget) -> None:
        try:
            x = anchor_widget.winfo_rootx()
            y = anchor_widget.winfo_rooty() + anchor_widget.winfo_height()
            mode_menu.tk_popup(x, y)
        finally:
            mode_menu.grab_release()

    def switch_mode(mode: str) -> None:
        current_mode["value"] = mode
        intro_frame.grid_remove()
        editor_frame.grid()
        update_shared_fields_visibility(mode)
        for panel in (image_tab, ascii_tab, video_tab, future_tab):
            panel.grid_remove()
        if mode == "image_to_image":
            image_tab.grid()
        elif mode == "image_to_ascii":
            ascii_tab.grid()
        elif mode == "image_to_video":
            video_tab.grid()
        else:
            future_tab.grid()
        refresh_estimate()

    def go_home() -> None:
        current_mode["value"] = "image_to_image"
        editor_frame.grid_remove()
        intro_frame.grid()
        progress_var.set(0.0)
        progress_detail_var.set("En attente")
        status_var.set("Choisissez un mode puis configurez les paramètres.")

    def show_recent_media(path: Path) -> None:
        hero_preview_refs.clear()
        suffix = path.suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"} or ImageTk is None:
            hero_preview_label.configure(
                image="",
                text=f"{path.name}\n\nAperçu grand format indisponible pour ce type de fichier.",
                compound="center",
            )
            return
        try:
            with Image.open(path) as source:
                preview = source.convert("RGBA")
                preview.thumbnail((760, 520), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(preview)
        except (OSError, Image.DecompressionBombError):
            hero_preview_label.configure(
                image="",
                text=f"{path.name}\n\nImpossible d'afficher cette image en grand.",
                compound="center",
            )
            return
        hero_preview_refs.append(photo)
        hero_preview_label.configure(image=photo, text="")

    def refresh_recent_gallery() -> None:
        for child in recent_gallery.winfo_children():
            child.destroy()
        recent_preview_refs.clear()
        recent_preview_widgets.clear()

        recent_paths = discover_recent_media(limit=12)
        if not recent_paths:
            recent_hint.configure(text="Aucun rendu pour le moment. Tes prochains exports apparaîtront ici.")
            recent_hint.grid()
            return

        recent_hint.grid_remove()
        for index, media_path in enumerate(recent_paths):
            item_frame = ttk.Frame(recent_gallery, style="Card.TFrame", padding=8)
            item_frame.grid(row=index, column=0, sticky="ew", pady=(0, 10))
            item_frame.columnconfigure(0, weight=1)
            button_label = f"{media_path.name}\n{time.strftime('%d/%m %H:%M', time.localtime(media_path.stat().st_mtime))}"
            if ImageTk is not None:
                thumbnail = build_recent_thumbnail(media_path)
                photo = ImageTk.PhotoImage(thumbnail)
                recent_preview_refs.append(photo)
                action = tk.Button(
                    item_frame,
                    image=photo,
                    text=button_label,
                    compound="top",
                    anchor="w",
                    justify="left",
                    wraplength=240,
                    command=lambda target=media_path: show_recent_media(target),
                    relief="flat",
                    bd=0,
                    padx=8,
                    pady=8,
                    bg=card_bg,
                    fg=text_primary,
                    activebackground="#24324d",
                    activeforeground=text_primary,
                    cursor="hand2",
                )
            else:
                action = tk.Button(
                    item_frame,
                    text=button_label,
                    justify="left",
                    wraplength=240,
                    command=lambda target=media_path: show_recent_media(target),
                    relief="flat",
                    bd=0,
                    padx=12,
                    pady=12,
                    bg=card_bg,
                    fg=text_primary,
                    activebackground="#24324d",
                    activeforeground=text_primary,
                    cursor="hand2",
                )
            action.grid(row=0, column=0, sticky="ew")
            recent_preview_widgets.append(action)
        show_recent_media(recent_paths[0])

    mode_menu = tk.Menu(
        mode_menu_button,
        tearoff=False,
        bg="#202b41",
        fg=text_primary,
        activebackground=accent,
        activeforeground="#08101d",
        relief="flat",
        bd=0,
    )
    for mode_key, title in mode_titles.items():
        mode_menu.add_command(label=title, command=lambda selected_mode=mode_key: switch_mode(selected_mode))
    mode_menu_button.configure(command=lambda: open_mode_menu(mode_menu_button))
    intro_generate_button.configure(command=lambda: open_mode_menu(intro_generate_button))

    def refresh_estimate(*_args: object) -> None:
        if is_running["value"]:
            return
        current_mode["value"] = get_active_mode()
        mode_summary_var.set(f"Mode actif : {mode_titles[current_mode['value']]}")
        generate_button.configure(text=generate_labels[current_mode["value"]])
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
            elif current_mode["value"] == "image_to_ascii":
                if not args.input or not args.input.strip():
                    estimate_var.set("Estimation : choisissez d'abord une image d'entrée.")
                    return
                ascii_seconds, ascii_profile = estimate_ascii_for_args(args)
                estimate_var.set(
                    "Estimation ASCII : "
                    f"{format_duration(ascii_seconds)} pour {int(ascii_profile['total_cells'])} cellules "
                    f"({int(ascii_profile['columns'])}x{int(ascii_profile['rows'])}), "
                    f"sortie {ascii_profile['output_kind']}."
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

    def make_progress_reporter(prefix: str) -> ProgressCallback:
        def report(progress: float, detail: str) -> None:
            bounded = max(0.0, min(1.0, progress))
            percent = int(round(bounded * 100))
            def update_progress() -> None:
                progress_var.set(percent)
                progress_detail_var.set(detail)
                status_var.set(f"{prefix} {percent}%")

            root.after(0, update_progress)
        return report

    def ensure_unique_output_for_key(field_key: str) -> tuple[str | None, str | None]:
        current_value = field_vars[field_key].get().strip()
        if not current_value:
            return None, None
        unique_value = make_unique_output_path(current_value)
        if unique_value is None:
            return None, None
        if unique_value != current_value:
            field_vars[field_key].set(unique_value)
            return unique_value, f"Sortie ajustée pour éviter l'écrasement : {Path(unique_value).name}"
        return unique_value, None

    def prepare_outputs_for_active_mode(args: argparse.Namespace) -> list[str]:
        messages: list[str] = []
        active_mode = current_mode["value"]
        if active_mode == "image_to_image":
            unique_path, message = ensure_unique_output_for_key("output")
            args.output = unique_path or args.output
            if message:
                messages.append(message)
        elif active_mode == "image_to_ascii":
            unique_path, message = ensure_unique_output_for_key("ascii_output")
            args.ascii_output = unique_path or args.ascii_output
            if message:
                messages.append(message)
            if field_vars["ascii_text_output"].get().strip():
                text_unique_path, text_message = ensure_unique_output_for_key("ascii_text_output")
                args.ascii_text_output = text_unique_path or args.ascii_text_output
                if text_message:
                    messages.append(text_message)
        elif active_mode == "image_to_video":
            unique_path, message = ensure_unique_output_for_key("video_output")
            args.video_output = unique_path or args.video_output
            if message:
                messages.append(message)
        else:
            unique_path, message = ensure_unique_output_for_key("video_to_video_output")
            args.video_to_video_output = unique_path or args.video_to_video_output
            if message:
                messages.append(message)
        return messages

    def set_busy_state(summary: str, detail: str, progress_percent: float = 4.0) -> None:
        is_running["value"] = True
        set_controls_state("disabled")
        progress_var.set(progress_percent)
        progress_detail_var.set(detail)
        status_var.set(summary)

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

        rename_messages = prepare_outputs_for_active_mode(args)

        try:
            estimate, profile = estimate_for_args(args)
        except EmojiMakerError as exc:
            messagebox.showerror("Paramètres invalides", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Paramètres invalides", f"Impossible d'estimer le rendu : {exc}")
            return

        set_busy_state(
            "Rendu en cours... "
            f"Estimation {format_duration(estimate.seconds)} pour {int(profile['total_cells'])} cases.",
            "Préparation du rendu image",
        )
        if rename_messages:
            progress_detail_var.set(" | ".join(rename_messages))

        def worker() -> None:
            try:
                result = run_with_args(args, progress_callback=make_progress_reporter("Rendu"))
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
        progress_var.set(0.0)
        progress_detail_var.set("Le rendu a échoué.")
        status_var.set(message)
        messagebox.showerror("Erreur", message)

    def on_success(result: RenderResult) -> None:
        is_running["value"] = False
        set_controls_state("!disabled")
        progress_var.set(100.0)
        progress_detail_var.set("Image exportée avec succès.")
        status_var.set(
            f"Rendu terminé : {result.output_path} | "
            f"{result.filled_cells} emojis dessinés sur {result.total_cells} cases en {format_duration(result.duration_seconds)}."
        )
        refresh_estimate()
        refresh_recent_gallery()
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

    def on_ascii_render() -> None:
        if is_running["value"]:
            return

        try:
            args = build_gui_args(collect_values())
        except ValueError as exc:
            messagebox.showerror("Paramètres invalides", f"Valeur invalide : {exc}")
            return

        if not args.input or not args.input.strip():
            messagebox.showerror("Paramètres invalides", "Veuillez choisir une image d'entrée.")
            return
        if not args.ascii_output:
            messagebox.showerror("Paramètres invalides", "Veuillez choisir un fichier de sortie ASCII.")
            return

        rename_messages = prepare_outputs_for_active_mode(args)

        try:
            estimated_seconds, profile = estimate_ascii_for_args(args)
        except EmojiMakerError as exc:
            messagebox.showerror("Paramètres invalides", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Paramètres invalides", f"Impossible d'estimer l'ASCII : {exc}")
            return

        set_busy_state(
            "Rendu ASCII en cours... "
            f"Estimation {format_duration(estimated_seconds)} pour {int(profile['total_cells'])} cellules.",
            "Analyse des cellules ASCII",
        )
        if rename_messages:
            progress_detail_var.set(" | ".join(rename_messages))

        def worker() -> None:
            try:
                output_path = run_ascii_with_args(args)
            except EmojiMakerError as exc:
                root.after(0, lambda exc=exc: on_error(str(exc)))
                return
            except Exception as exc:  # pragma: no cover
                root.after(0, lambda exc=exc: on_error(str(exc)))
                return
            root.after(0, lambda: on_ascii_success(output_path))

        threading.Thread(target=worker, daemon=True).start()

    def on_ascii_success(output_path: Path) -> None:
        is_running["value"] = False
        set_controls_state("!disabled")
        progress_var.set(100.0)
        progress_detail_var.set("ASCII exporté avec succès.")
        status_var.set(f"ASCII terminé : {output_path}")
        refresh_estimate()
        refresh_recent_gallery()
        extra_text = f"\nTexte ASCII : {field_vars['ascii_text_output'].get()}" if field_vars["ascii_text_output"].get().strip() else ""
        messagebox.showinfo(
            "Succès ASCII",
            f"ASCII généré :\n{output_path}{extra_text}",
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

        rename_messages = prepare_outputs_for_active_mode(args)

        try:
            estimated_seconds, frame_count = estimate_video_for_args(args)
        except EmojiMakerError as exc:
            messagebox.showerror("Paramètres invalides", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Paramètres invalides", f"Impossible d'estimer la vidéo : {exc}")
            return

        set_busy_state(
            "Vidéo en cours... "
            f"Estimation {format_duration(estimated_seconds)} pour {frame_count} frame(s) à {args.video_fps} FPS.",
            "Préparation des frames",
        )
        if rename_messages:
            progress_detail_var.set(" | ".join(rename_messages))

        def worker() -> None:
            try:
                result = render_video_with_args(
                    args=args,
                    video_output=args.video_output,
                    video_fps=args.video_fps,
                    video_start_columns=args.video_start_columns,
                    video_max_columns=args.video_max_columns,
                    video_step_columns=args.video_step_columns,
                    progress_callback=make_progress_reporter("Video"),
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
        progress_var.set(100.0)
        progress_detail_var.set("Vidéo exportée avec succès.")
        status_var.set(
            f"Vidéo terminée : {result.output_path} | "
            f"{result.frame_count} frame(s) en {format_duration(result.duration_seconds)}."
        )
        refresh_estimate()
        refresh_recent_gallery()
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

        rename_messages = prepare_outputs_for_active_mode(args)

        try:
            estimated_seconds, frame_count, profile = estimate_video_to_video_for_args(args, args.video_to_video_input)
        except EmojiMakerError as exc:
            messagebox.showerror("Paramètres invalides", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Paramètres invalides", f"Impossible d'estimer la conversion vidéo -> vidéo : {exc}")
            return

        set_busy_state(
            "Conversion vidéo -> vidéo en cours... "
            f"Estimation {format_duration(estimated_seconds)} pour {frame_count} frame(s), "
            f"grille {int(profile['columns'])}x{int(profile['rows'])}.",
            "Analyse de la vidéo source",
        )
        if rename_messages:
            progress_detail_var.set(" | ".join(rename_messages))

        def worker() -> None:
            try:
                result = render_video_to_video_with_args(
                    args=args,
                    video_input=args.video_to_video_input,
                    video_output=args.video_to_video_output,
                    progress_callback=make_progress_reporter("Video -> video"),
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
        progress_var.set(100.0)
        progress_detail_var.set("Vidéo emoji exportée avec succès.")
        status_var.set(
            f"Vidéo -> vidéo terminée : {result.output_path} | "
            f"{result.frame_count} frame(s) en {format_duration(result.duration_seconds)}."
        )
        refresh_estimate()
        refresh_recent_gallery()
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

    def run_active_mode() -> None:
        active_mode = get_active_mode()
        if active_mode == "image_to_image":
            on_render()
        elif active_mode == "image_to_ascii":
            on_ascii_render()
        elif active_mode == "image_to_video":
            on_video_render()
        else:
            on_video_to_video_render()

    home_button.configure(command=go_home)
    generate_button.configure(command=run_active_mode)

    restore_gui_settings()
    refresh_recent_gallery()

    for key, variable in field_vars.items():
        variable.trace_add("write", refresh_estimate)
        if key not in {"input", "output", "ascii_output", "ascii_text_output", "ascii_font", "video_output", "video_to_video_input", "video_to_video_output", "font"}:
            variable.trace_add("write", save_current_gui_settings)
    stretch_var.trace_add("write", refresh_estimate)
    stretch_var.trace_add("write", save_current_gui_settings)
    ascii_color_var.trace_add("write", refresh_estimate)
    ascii_color_var.trace_add("write", save_current_gui_settings)
    ascii_invert_var.trace_add("write", refresh_estimate)
    ascii_invert_var.trace_add("write", save_current_gui_settings)
    refresh_estimate()

    def on_close() -> None:
        save_current_gui_settings()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

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
