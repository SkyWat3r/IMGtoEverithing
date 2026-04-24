from __future__ import annotations

import re
from pathlib import Path


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
GUI_SETTINGS_PATH = Path(".emoji_cache") / "gui_settings.json"
UNICODE_EMOJI_TEST_CACHE_PATH = Path(".emoji_cache") / "unicode_emoji_test.txt"
UNICODE_EMOJI_TEST_URL = "https://unicode.org/Public/emoji/latest/emoji-test.txt"
PALETTE_METRICS_CACHE_DIR = Path(".emoji_cache") / "palette_metrics"
MAX_HISTORY_ENTRIES = 200
INITIAL_BROWSER_RESULTS = 48
LOAD_MORE_BROWSER_RESULTS = 48

SQUARE_FIRST_DEFAULT_PALETTE = [
    "\u2B1B",
    "\u2B1C",
    "\U0001F7E5",
    "\U0001F7E7",
    "\U0001F7E8",
    "\U0001F7E9",
    "\U0001F7E6",
    "\U0001F7EA",
    "\U0001F7EB",
    "\u2764\uFE0F",
    "\U0001F9E1",
    "\U0001F49B",
    "\U0001F49A",
    "\U0001F499",
    "\U0001F49C",
    "\U0001F34E",
    "\U0001F34A",
    "\U0001F34B",
    "\U0001F95D",
    "\U0001FADB",
    "\U0001F347",
    "\U0001F353",
    "\U0001F338",
    "\U0001F33B",
    "\U0001F332",
    "\U0001F30A",
    "\u2601\uFE0F",
    "\U0001F319",
    "\u2B50",
    "\U0001F525",
    "\u26A1",
    "\u2744\uFE0F",
    "\U0001FAA8",
    "\U0001F975",
    "\U0001F9E0",
    "\U0001F438",
]

DEFAULT_BANNED_EMOJIS: list[str] = [
    "\U0001F7EB",
    "\U0001FADB",
]
