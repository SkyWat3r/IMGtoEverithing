from __future__ import annotations

import shutil
from typing import List


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
