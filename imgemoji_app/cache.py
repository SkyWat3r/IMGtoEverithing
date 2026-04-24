from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Sequence

from .constants import GUI_SETTINGS_PATH, MAX_HISTORY_ENTRIES, PALETTE_METRICS_CACHE_DIR, RUN_HISTORY_PATH
from .models import EmojiRenderSource


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


def load_gui_settings() -> dict[str, object]:
    if not GUI_SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(GUI_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_gui_settings(settings: dict[str, object]) -> None:
    GUI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GUI_SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_palette_metrics_cache_path(render_source: EmojiRenderSource, emoji_size: int) -> Path:
    if render_source.kind == "twemoji":
        cache_name = f"twemoji_{emoji_size}.json"
    else:
        font_name = "font"
        if render_source.font is not None:
            font_name = Path(getattr(render_source.font, "path", "font")).stem or "font"
        safe_font_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", font_name)
        cache_name = f"font_{safe_font_name}_{emoji_size}.json"
    return PALETTE_METRICS_CACHE_DIR / cache_name


def load_palette_metrics_cache(render_source: EmojiRenderSource, emoji_size: int) -> dict[str, object]:
    cache_path = get_palette_metrics_cache_path(render_source, emoji_size)
    if not cache_path.exists():
        return {"version": 1, "entries": {}, "skipped": []}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "entries": {}, "skipped": []}
    if not isinstance(payload, dict):
        return {"version": 1, "entries": {}, "skipped": []}
    entries = payload.get("entries")
    skipped = payload.get("skipped")
    return {
        "version": 1,
        "entries": entries if isinstance(entries, dict) else {},
        "skipped": skipped if isinstance(skipped, list) else [],
    }


def save_palette_metrics_cache(
    render_source: EmojiRenderSource,
    emoji_size: int,
    cache_payload: dict[str, object],
) -> None:
    PALETTE_METRICS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = get_palette_metrics_cache_path(render_source, emoji_size)
    cache_path.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
