"""ffprobe/ffmpeg helpers used by the upload flow: probing a video's duration/
resolution/size for validation, and generating a dashboard thumbnail. The site no
longer re-encodes video — see client/preconvert.py for that, run client-side.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ProbeError(RuntimeError):
    pass


@dataclass
class ProbeResult:
    duration_s: float
    width: int
    height: int
    fps: float
    has_audio: bool
    size_bytes: int
    video_codec: str


def probe(path: Path) -> ProbeResult:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if vstream is None:
        raise ProbeError("no video stream found")

    duration_s = float(fmt.get("duration") or vstream.get("duration") or 0.0)
    if duration_s <= 0:
        raise ProbeError("could not determine video duration")

    fps_raw = vstream.get("avg_frame_rate") or vstream.get("r_frame_rate") or "0/1"
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den) if float(den) != 0 else 0.0
    except ValueError:
        fps = float(fps_raw)

    return ProbeResult(
        duration_s=duration_s,
        width=int(vstream.get("width", 0)),
        height=int(vstream.get("height", 0)),
        fps=fps or 25.0,
        has_audio=astream is not None,
        size_bytes=int(fmt.get("size") or path.stat().st_size),
        video_codec=vstream.get("codec_name", "unknown"),
    )


def generate_thumbnail(input_path: Path, duration_s: float, out_path: Path, grid: int = 3) -> None:
    frames = grid * grid
    gap = max(duration_s / frames, 0.5)
    vf = f"fps=1/{gap:.4f},scale=320:-2,tile={grid}x{grid}"
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vf", vf, "-frames:v", "1", "-q:v", "4", str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"thumbnail generation failed: {proc.stderr.strip()[-1000:]}")
