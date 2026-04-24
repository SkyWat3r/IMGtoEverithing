from __future__ import annotations

import os
import subprocess
import unicodedata
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from typing import List, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .cache import load_palette_metrics_cache, save_palette_metrics_cache
from .constants import (
    COMMON_EMOJI_FONT_FAMILIES,
    COMMON_EMOJI_FONTS,
    DEFAULT_BANNED_EMOJIS,
    DEFAULT_PALETTE,
    PALETTE_SPLIT_RE,
    SQUARE_FIRST_DEFAULT_PALETTE,
    TWEMOJI_BASE_URL,
    TWEMOJI_CACHE_DIR,
    UNICODE_EMOJI_TEST_CACHE_PATH,
    UNICODE_EMOJI_TEST_URL,
)
from .errors import EmojiMakerError, fail
from .models import EmojiRenderSource, PaletteEntry, PaletteMatcher, ProgressCallback


def parse_emoji_list(value: str | None, default: Sequence[str] | None = None) -> List[str]:
    if value is None:
        source = list(default or [])
    else:
        candidate_path = Path(value)
        if candidate_path.is_file():
            content = candidate_path.read_text(encoding="utf-8").strip()
            source = [token.strip() for token in PALETTE_SPLIT_RE.split(content) if token.strip()]
        else:
            source = [token.strip() for token in PALETTE_SPLIT_RE.split(value) if token.strip()]

    unique_items: List[str] = []
    seen = set()
    for item in source:
        if item not in seen:
            unique_items.append(item)
            seen.add(item)
    return unique_items


def build_default_palette_for_render_source(render_source_kind: str) -> List[str]:
    if render_source_kind == "twemoji":
        return list(get_twemoji_browser_catalog())
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
    return " ".join(emojis)


def describe_emoji(emoji: str) -> str:
    names = []
    for char in emoji:
        try:
            names.append(unicodedata.name(char))
        except ValueError:
            continue
    return " / ".join(names) if names else emoji


def fetch_unicode_emoji_test() -> str:
    if UNICODE_EMOJI_TEST_CACHE_PATH.exists():
        try:
            return UNICODE_EMOJI_TEST_CACHE_PATH.read_text(encoding="utf-8")
        except OSError:
            pass
    request = urllib.request.Request(UNICODE_EMOJI_TEST_URL, headers={"User-Agent": "emoji_maker/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            content = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        if UNICODE_EMOJI_TEST_CACHE_PATH.exists():
            return UNICODE_EMOJI_TEST_CACHE_PATH.read_text(encoding="utf-8")
        fail(f"Could not fetch Unicode emoji catalog: {exc}")
        raise AssertionError("unreachable")
    UNICODE_EMOJI_TEST_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNICODE_EMOJI_TEST_CACHE_PATH.write_text(content, encoding="utf-8")
    return content


def parse_unicode_emoji_test(content: str) -> List[str]:
    emojis: List[str] = []
    seen = set()
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "; fully-qualified" not in line:
            continue
        emoji = line.split("#", 1)[1].strip().split(" ", 1)[0]
        if emoji and emoji not in seen:
            emojis.append(emoji)
            seen.add(emoji)
    return emojis


def get_twemoji_browser_catalog() -> List[str]:
    fallback = list(SQUARE_FIRST_DEFAULT_PALETTE) + list(DEFAULT_PALETTE) + list(DEFAULT_BANNED_EMOJIS)
    try:
        return parse_unicode_emoji_test(fetch_unicode_emoji_test()) or fallback
    except EmojiMakerError:
        return fallback


def fontconfig_match(family: str) -> str | None:
    if shutil_which("fc-match") is None:
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


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


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
    has_non_gray = any(alpha > 0 and not (red == green == blue) for _, (red, green, blue, alpha) in colors)
    return True, has_non_gray


def find_emoji_font(font_path: str | None, size: int) -> ImageFont.FreeTypeFont:
    search_paths = build_font_search_candidates(font_path)
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
        "Could not load an emoji-capable font. Pass --font /path/to/emoji_font.ttf "
        "(for example NotoColorEmoji.ttf, Apple Color Emoji.ttc, or seguiemj.ttf)."
    )
    if not font_path:
        help_text += " On Linux, install an emoji font such as the Noto Color Emoji package and retry."
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
        request = urllib.request.Request(url, headers={"User-Agent": "emoji_maker/1.0"})
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


def resolve_render_source(font_path: str | None, emoji_size: int, emoji_source: str) -> EmojiRenderSource:
    if emoji_source in ("auto", "font"):
        font = try_find_emoji_font(font_path, size=max(12, int(emoji_size * 0.9)))
        if font is not None:
            _renders, has_color = probe_font_emoji(font)
            if emoji_source == "font" or has_color:
                return EmojiRenderSource(kind="font", font=font)
        if emoji_source == "font":
            find_emoji_font(font_path, size=max(12, int(emoji_size * 0.9)))
    return EmojiRenderSource(kind="twemoji")


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


def draw_emoji(image: Image.Image, position: Tuple[float, float], emoji: str, font: ImageFont.ImageFont) -> None:
    draw = ImageDraw.Draw(image)
    try:
        draw.text(position, emoji, font=font, embedded_color=True)
    except TypeError:
        draw.text(position, emoji, font=font, fill=(255, 255, 255, 255))


def render_single_emoji_tile(emoji: str, tile_size: int, render_source: EmojiRenderSource) -> Image.Image:
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
    progress_start, progress_end = progress_range
    total_emojis = max(1, len(palette))
    cache_payload = load_palette_metrics_cache(render_source, emoji_size)
    cached_entries = cache_payload["entries"] if isinstance(cache_payload.get("entries"), dict) else {}
    cached_skipped = {str(item) for item in cache_payload.get("skipped", []) if isinstance(item, str)}
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
            if progress_callback is not None:
                fraction = progress_start + ((progress_end - progress_start) * (index / total_emojis))
                progress_callback(fraction, f"Preparation palette Twemoji {index}/{total_emojis}")
            continue
        try:
            tile = render_single_emoji_tile(emoji, emoji_size, render_source)
        except EmojiMakerError:
            if render_source.kind == "twemoji":
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
            fail("No usable Twemoji assets were available for the current palette. Try banning fewer emojis, using a custom palette, or switching emoji source.")
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
