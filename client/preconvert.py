#!/usr/bin/env python3
"""Standalone client-side pre-converter for the Qwen Video Classifier.

Run this on your own machine *before* uploading a video to the app. It shrinks
the file toward the same ~1.85GB / 720p-floor / 2-hour-clip target that the
server applies on ingest (see app/ffmpeg_utils.py), so you upload a much
smaller file instead of the original. The server still re-encodes on receipt
(it never trusts client input), but starting from a file that's already near
the target size makes that pass fast and keeps upload time/bandwidth down for
large source files (4K masters, long recordings, etc).

Requires only Python 3.8+ and `ffmpeg`/`ffprobe` on PATH — no other
dependencies, so it can be copied and run standalone without the rest of this
repo or its requirements.txt.

Usage:
    python3 preconvert.py input.mkv
    python3 preconvert.py input.mkv -o ready_to_upload.mp4
    python3 preconvert.py input.mkv --dry-run
    python3 preconvert.py input.mkv --max-bytes 1000000000 --max-duration 3600
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Mirrors app/config.py defaults. Keep in sync with the server if you change
# either — a pre-converted file that's already near the server's own target
# gets re-encoded almost losslessly; one that isn't just gets re-encoded again
# from a smaller starting point (still a net win for upload time).
DEFAULT_MAX_DURATION_SECONDS = 2 * 60 * 60
DEFAULT_TARGET_MAX_BYTES = int(1.85 * 1024**3)
MIN_HEIGHT_FLOOR = 720

AUDIO_BITRATE_BPS = 128_000
MIN_VIDEO_BITRATE_BPS = 300_000
MAX_VIDEO_BITRATE_BPS = 5_000_000
CONTAINER_SAFETY_FACTOR = 0.97

_PROGRESS_RE = re.compile(r"out_time_us=(\d+)")


class PreconvertError(RuntimeError):
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


def check_dependencies() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise PreconvertError(
            f"missing required tool(s) on PATH: {', '.join(missing)}. "
            "Install ffmpeg (which bundles ffprobe) and try again."
        )


def probe(path: Path) -> ProbeResult:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PreconvertError(f"ffprobe failed: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if vstream is None:
        raise PreconvertError("no video stream found in input file")

    duration_s = float(fmt.get("duration") or vstream.get("duration") or 0.0)
    if duration_s <= 0:
        raise PreconvertError("could not determine video duration")

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


def plan_conversion(
    p: ProbeResult,
    max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS,
    target_max_bytes: int = DEFAULT_TARGET_MAX_BYTES,
) -> ConversionPlan:
    clipped = p.duration_s > max_duration_seconds
    output_duration_s = min(p.duration_s, max_duration_seconds)

    # Never upscale; never go below the 720p floor once we do scale down.
    target_height = min(p.height, MIN_HEIGHT_FLOOR) if p.height > 0 else MIN_HEIGHT_FLOOR
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
        budget_bits = target_max_bytes * 8 * CONTAINER_SAFETY_FACTOR
        video_bits_budget = budget_bits - (audio_bps * output_duration_s)
        bitrate = int(video_bits_budget / output_duration_s) if output_duration_s > 0 else MIN_VIDEO_BITRATE_BPS
        bitrate = max(MIN_VIDEO_BITRATE_BPS, min(bitrate, MAX_VIDEO_BITRATE_BPS))
        chosen_fps = fps
        chosen_bitrate = bitrate
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


def run_conversion(
    input_path: Path,
    output_path: Path,
    plan: ConversionPlan,
    on_progress: Optional[Callable[[float], None]] = None,
) -> None:
    """Run the ffmpeg conversion. Defaults to printing a progress bar to stderr;
    pass on_progress to receive a 0..1 fraction instead (e.g. for a GUI)."""
    if on_progress is None:
        on_progress = _print_progress

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
        if m and total_us > 0:
            frac = min(1.0, int(m.group(1)) / total_us)
            on_progress(frac)

    if proc.stderr is not None:
        stderr_tail = proc.stderr.readlines()[-40:]
    proc.wait()
    if on_progress is _print_progress:
        print(file=sys.stderr)  # newline after the progress line
    if proc.returncode != 0:
        raise PreconvertError(f"ffmpeg conversion failed (exit {proc.returncode}): {''.join(stderr_tail)[-2000:]}")


def _print_progress(frac: float) -> None:
    bar_width = 30
    filled = int(bar_width * frac)
    bar = "#" * filled + "-" * (bar_width - filled)
    print(f"\r  converting [{bar}] {frac * 100:5.1f}%", end="", file=sys.stderr, flush=True)


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def describe_plan(src: ProbeResult, plan: ConversionPlan) -> str:
    lines = [
        f"source:  {src.width}x{src.height} @ {src.fps:.2f}fps, "
        f"{src.duration_s / 60:.1f} min, {human_bytes(src.size_bytes)}, "
        f"codec={src.video_codec}, audio={'yes' if src.has_audio else 'no'}",
        f"target:  {plan.target_width}x{plan.target_height} @ {plan.target_fps}fps, "
        f"~{plan.video_bitrate_bps / 1000:.0f}kbps video"
        + (f" + {plan.audio_bitrate_bps / 1000:.0f}kbps audio" if plan.audio_bitrate_bps else " (no audio)"),
    ]
    if plan.clipped:
        lines.append(
            f"clip:    source is {plan.source_duration_s / 3600:.2f}h, longer than the "
            f"{plan.output_duration_s / 3600:.2f}h limit — output will be clipped to the first "
            f"{plan.output_duration_s / 3600:.2f}h"
        )
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pre-convert a video locally before uploading it to the Qwen Video Classifier.",
    )
    parser.add_argument("input", type=Path, help="path to the source video file")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="output path (default: <input stem>.preconverted.mp4 next to the input)",
    )
    parser.add_argument(
        "--max-bytes", type=int, default=DEFAULT_TARGET_MAX_BYTES,
        help=f"target output size ceiling in bytes (default: {DEFAULT_TARGET_MAX_BYTES}, ~1.85GB)",
    )
    parser.add_argument(
        "--max-duration", type=int, default=DEFAULT_MAX_DURATION_SECONDS,
        help=f"clip output to at most this many seconds (default: {DEFAULT_MAX_DURATION_SECONDS}, 2h)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="probe the input and print the conversion plan without encoding anything",
    )
    args = parser.parse_args(argv)

    try:
        check_dependencies()
        if not args.input.exists():
            raise PreconvertError(f"input file not found: {args.input}")

        src = probe(args.input)
        plan = plan_conversion(src, args.max_duration, args.max_bytes)
        print(describe_plan(src, plan))

        if args.dry_run:
            return 0

        output_path = args.output or args.input.with_suffix("").with_suffix(".preconverted.mp4")
        if output_path.resolve() == args.input.resolve():
            raise PreconvertError("output path must differ from the input path")

        print(f"\nconverting -> {output_path}")
        run_conversion(args.input, output_path, plan)

        out_size = output_path.stat().st_size
        print(f"done: {human_bytes(out_size)} written to {output_path}")
        return 0
    except PreconvertError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
