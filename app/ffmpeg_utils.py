"""ffprobe/ffmpeg helpers: probing, CPU-encode conversion planning + execution, thumbnails.

Conversion targets: fit under config.TARGET_MAX_BYTES, never scale below
config.MIN_HEIGHT (720p floor), clip at config.MAX_DURATION_SECONDS (2h),
and prefer trimming fps/bitrate over touching resolution when the size
budget is tight.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.config import MAX_DURATION_SECONDS, MIN_HEIGHT, TARGET_MAX_BYTES

AUDIO_BITRATE_BPS = 128_000
MIN_VIDEO_BITRATE_BPS = 300_000
MAX_VIDEO_BITRATE_BPS = 5_000_000
CONTAINER_SAFETY_FACTOR = 0.97


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


@dataclass
class ConversionPlan:
    target_width: int
    target_height: int
    target_fps: float
    video_bitrate_bps: int
    audio_bitrate_bps: int
    clip_seconds: Optional[float]
    clipped: bool
    source_duration_s: float
    output_duration_s: float


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


def plan_conversion(p: ProbeResult) -> ConversionPlan:
    clipped = p.duration_s > MAX_DURATION_SECONDS
    output_duration_s = min(p.duration_s, MAX_DURATION_SECONDS)

    # Never upscale; never go below the 720p floor once we do scale down.
    target_height = min(p.height, 720) if p.height > 0 else MIN_HEIGHT
    # even dims required by yuv420p
    if target_height % 2:
        target_height -= 1
    aspect = (p.width / p.height) if p.height else 16 / 9
    target_width = int(round(target_height * aspect))
    if target_width % 2:
        target_width += 1

    audio_bps = AUDIO_BITRATE_BPS if p.has_audio else 0

    fps_candidates = [min(p.fps, 30.0), 24.0, 20.0]
    chosen_fps = fps_candidates[0]
    chosen_bitrate = MIN_VIDEO_BITRATE_BPS

    for fps in fps_candidates:
        budget_bits = TARGET_MAX_BYTES * 8 * CONTAINER_SAFETY_FACTOR
        video_bits_budget = budget_bits - (audio_bps * output_duration_s)
        bitrate = int(video_bits_budget / output_duration_s) if output_duration_s > 0 else MIN_VIDEO_BITRATE_BPS
        bitrate = max(MIN_VIDEO_BITRATE_BPS, min(bitrate, MAX_VIDEO_BITRATE_BPS))
        chosen_fps = fps
        chosen_bitrate = bitrate
        # Once we're comfortably above the floor, stop trading away frame rate.
        if bitrate > 900_000 or fps <= 20.0:
            break

    return ConversionPlan(
        target_width=target_width,
        target_height=target_height,
        target_fps=round(chosen_fps, 3),
        video_bitrate_bps=chosen_bitrate,
        audio_bitrate_bps=audio_bps,
        clip_seconds=output_duration_s if clipped else None,
        clipped=clipped,
        source_duration_s=p.duration_s,
        output_duration_s=output_duration_s,
    )


# ffmpeg's "-progress" out_time_ms field is actually microseconds despite the name
# (long-standing ffmpeg quirk) — out_time_us reports the same value, so parse that
# instead and divide by 1e6 for seconds.
_PROGRESS_RE = re.compile(r"out_time_us=(\d+)")


def run_conversion(
    input_path: Path,
    output_path: Path,
    plan: ConversionPlan,
    on_progress: Optional[Callable[[float], None]] = None,
) -> None:
    vf = f"scale={plan.target_width}:{plan.target_height}:flags=lanczos"
    cmd = ["ffmpeg", "-y", "-i", str(input_path)]
    if plan.clip_seconds is not None:
        cmd += ["-t", f"{plan.clip_seconds:.3f}"]
    cmd += [
        "-vf", f"{vf},fps={plan.target_fps}",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-b:v", str(plan.video_bitrate_bps),
        "-maxrate", str(int(plan.video_bitrate_bps * 1.45)),
        "-bufsize", str(int(plan.video_bitrate_bps * 2.5)),
    ]
    if plan.audio_bitrate_bps:
        cmd += ["-c:a", "aac", "-b:a", str(plan.audio_bitrate_bps), "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(output_path)]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    total_us = plan.output_duration_s * 1_000_000
    stderr_tail: list[str] = []

    assert proc.stdout is not None
    for line in proc.stdout:
        m = _PROGRESS_RE.search(line)
        if m and on_progress and total_us > 0:
            frac = min(1.0, int(m.group(1)) / total_us)
            on_progress(frac)

    if proc.stderr is not None:
        stderr_tail = proc.stderr.readlines()[-40:]
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed (exit {proc.returncode}): {''.join(stderr_tail)[-2000:]}")
    if on_progress:
        on_progress(1.0)


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
