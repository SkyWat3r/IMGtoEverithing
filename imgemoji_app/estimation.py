from __future__ import annotations

import time
from typing import List, Sequence, Tuple

from .cache import load_render_history, save_render_history
from .models import RenderEstimate, RenderResult


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
        return RenderEstimate(seconds=baseline_fixed + (total_cells * baseline_per_cell), sample_count=0, confidence="faible")

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
    confidence = "élevée" if sample_count >= 8 else "moyenne" if sample_count >= 4 else "faible"
    return RenderEstimate(seconds=max(0.1, estimated_seconds), sample_count=sample_count, confidence=confidence)


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
