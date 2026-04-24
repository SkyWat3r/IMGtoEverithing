from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Sequence, Tuple

import numpy as np
from PIL import Image, ImageColor, ImageDraw, ImageFont

from .errors import fail
from .rendering import load_image, parse_background


DEFAULT_ASCII_CHARSET = " .,:;!?+=*#%@"
COMMON_MONO_FONTS = [
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",
    "/usr/share/fonts/google-droid-sans-mono-fonts/DroidSansMono.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]


def parse_ascii_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert an image to ASCII art.")
    parser.add_argument("--input", required=True, help="Path to the input image.")
    parser.add_argument("--output", required=True, help="Path to the output file (.txt or .png).")
    parser.add_argument("--text-output", help="Optional path to save the ASCII text output.")
    parser.add_argument("--columns", type=int, default=120, help="Number of ASCII columns. Default: 120.")
    parser.add_argument("--rows", type=int, help="Optional number of ASCII rows.")
    parser.add_argument("--scale", type=float, default=1.0, help="Scale multiplier when rows are auto-computed.")
    parser.add_argument("--charset", default=DEFAULT_ASCII_CHARSET, help="ASCII characters ordered from light to dark.")
    parser.add_argument("--invert", action="store_true", help="Invert the character mapping.")
    parser.add_argument("--color", action="store_true", help="Render a colorized PNG when output is an image.")
    parser.add_argument("--font", help="Path to a monospace font for PNG rendering.")
    parser.add_argument("--font-size", type=int, default=12, help="Font size for PNG rendering. Default: 12.")
    parser.add_argument("--background", default="black", help="Background color for PNG output. Default: black.")
    return parser.parse_args(argv)


def find_monospace_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont:
    search_paths = [font_path] if font_path else COMMON_MONO_FONTS
    last_error: Exception | None = None
    for candidate in search_paths:
        if not candidate:
            continue
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError as exc:
            last_error = exc
    if font_path:
        fail(f"Could not load monospace font: {font_path}. Last error: {last_error}")
    fail("Could not find a usable monospace font. Pass --font /path/to/font.ttf.")
    raise AssertionError("unreachable")


def measure_glyph_cell(font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    probe = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    bbox = draw.textbbox((0, 0), "M", font=font)
    cell_width = max(1, bbox[2] - bbox[0])
    cell_height = max(1, bbox[3] - bbox[1] + 2)
    return cell_width, cell_height


def compute_ascii_rows(
    width: int,
    height: int,
    columns: int,
    rows: int | None,
    scale: float,
    cell_width: int,
    cell_height: int,
) -> int:
    if rows is not None:
        return rows
    if scale <= 0:
        fail("--scale must be greater than 0.")
    effective_columns = max(1, int(round(columns * scale)))
    computed_rows = max(1, int(round((height / width) * effective_columns * (cell_width / cell_height))))
    return computed_rows


def build_ascii_charset(charset: str, invert: bool) -> str:
    unique_chars = "".join(dict.fromkeys(charset))
    if not unique_chars.strip():
        fail("ASCII charset must contain at least one visible character.")
    return unique_chars[::-1] if invert else unique_chars


def resize_for_ascii(image: Image.Image, columns: int, rows: int) -> Image.Image:
    return image.resize((columns, rows), Image.Resampling.BOX)


def image_to_ascii_cells(
    image: Image.Image,
    columns: int,
    rows: int,
    charset: str,
) -> tuple[List[str], np.ndarray]:
    sampled = resize_for_ascii(image, columns, rows)
    array = np.asarray(sampled.convert("RGBA"), dtype=np.uint8)
    rgb = array[..., :3].astype(np.float32)
    alpha = array[..., 3].astype(np.float32) / 255.0
    mean_rgb = np.divide(rgb * alpha[..., None], alpha[..., None], out=np.zeros_like(rgb), where=alpha[..., None] > 1e-6)
    luminance = (
        (0.299 * mean_rgb[..., 0])
        + (0.587 * mean_rgb[..., 1])
        + (0.114 * mean_rgb[..., 2])
    )
    normalized = np.clip(luminance / 255.0, 0.0, 1.0)
    indices = np.rint(normalized * (len(charset) - 1)).astype(np.int32)

    lines: List[str] = []
    for row_index in range(rows):
        chars = []
        for column_index in range(columns):
            if alpha[row_index, column_index] <= 0.01:
                chars.append(" ")
            else:
                chars.append(charset[indices[row_index, column_index]])
        lines.append("".join(chars))
    return lines, mean_rgb.astype(np.uint8)


def render_ascii_to_image(
    lines: Sequence[str],
    colors: np.ndarray,
    font: ImageFont.FreeTypeFont,
    background: Tuple[int, int, int, int],
    colorized: bool,
) -> Image.Image:
    if not lines:
        fail("ASCII output is empty.")
    cell_width, cell_height = measure_glyph_cell(font)
    rows = len(lines)
    columns = max(len(line) for line in lines)
    canvas = Image.new("RGBA", (columns * cell_width, rows * cell_height), background)
    draw = ImageDraw.Draw(canvas)
    for row_index, line in enumerate(lines):
        for column_index, char in enumerate(line):
            if char == " ":
                continue
            if colorized:
                fill = tuple(int(value) for value in colors[row_index, column_index]) + (255,)
            else:
                fill = (255, 255, 255, 255)
            draw.text((column_index * cell_width, row_index * cell_height), char, font=font, fill=fill)
    return canvas


def save_ascii_text(lines: Sequence[str], output_path: str) -> None:
    Path(output_path).write_text("\n".join(lines), encoding="utf-8")


def run_ascii_with_args(args: argparse.Namespace) -> Path:
    font_size = getattr(args, "font_size", getattr(args, "ascii_font_size", None))
    ascii_font = getattr(args, "font", getattr(args, "ascii_font", None))
    ascii_color = bool(getattr(args, "color", getattr(args, "ascii_color", False)))
    ascii_invert = bool(getattr(args, "invert", getattr(args, "ascii_invert", False)))
    ascii_output = getattr(args, "output", getattr(args, "ascii_output", None))
    ascii_text_output = getattr(args, "text_output", getattr(args, "ascii_text_output", None))
    ascii_charset = getattr(args, "charset", getattr(args, "ascii_charset", None))

    if args.columns <= 0:
        fail("--columns must be greater than 0.")
    if args.rows is not None and args.rows <= 0:
        fail("--rows must be greater than 0.")
    if font_size is None or int(font_size) <= 0:
        fail("--font-size must be greater than 0.")
    if not ascii_output:
        fail("ASCII output is required.")
    if ascii_charset is None or not str(ascii_charset):
        fail("ASCII charset must not be empty.")

    font = find_monospace_font(ascii_font, int(font_size))
    cell_width, cell_height = measure_glyph_cell(font)
    image = load_image(args.input)
    rows = compute_ascii_rows(image.width, image.height, args.columns, args.rows, args.scale, cell_width, cell_height)
    charset = build_ascii_charset(str(ascii_charset), ascii_invert)
    lines, colors = image_to_ascii_cells(image, args.columns, rows, charset)

    output_path = Path(str(ascii_output))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix.lower()

    if ascii_text_output:
        save_ascii_text(lines, str(ascii_text_output))

    if suffix == ".txt":
        save_ascii_text(lines, str(output_path))
        return output_path

    if suffix != ".png":
        fail("ASCII output must be either .txt or .png.")

    background = parse_background(args.background)
    canvas = render_ascii_to_image(lines, colors, font, background, ascii_color)
    canvas.save(output_path, format="PNG")
    return output_path
