"""End-to-end orchestration: ingest -> convert -> host -> Qwen pass 1 -> pass 2 per
chapter/window -> merge/validate -> export -> delete video data. Runs synchronously in
a dedicated worker thread per job (see routes/upload.py).
"""
from __future__ import annotations

import csv
import json
import time
import traceback
from pathlib import Path

from app import app_settings, ffmpeg_utils, jobs, media_tokens, normalize
from app.config import DATASETS_DIR, THUMBS_DIR, TMP_DIR
from app.dataset_schema import (
    GlobalSummary, ProcessingInfo, SafetyRecord, SafetySummary, VideoDataset, VideoInfo,
)
from app.db import now_iso
from app.qwen_client import QwenAPIError, build_client, call_pass1, call_pass2

PROMPT_VERSION = "1.0"


def _log(job_id: str, level: str, message: str) -> None:
    jobs.add_log(job_id, level, message)


def run_pipeline(job_id: str, raw_path: Path, original_filename: str, title: str) -> None:
    dataset_dir = DATASETS_DIR / job_id
    raw_dir = dataset_dir / "raw"
    qa_dir = dataset_dir / "qa"
    raw_dir.mkdir(parents=True, exist_ok=True)
    qa_dir.mkdir(parents=True, exist_ok=True)
    converted_path = TMP_DIR / f"{job_id}.mp4"

    try:
        _run(job_id, raw_path, original_filename, title, dataset_dir, raw_dir, qa_dir, converted_path)
    except Exception as exc:  # noqa: BLE001 - top-level job guard, must never crash the worker thread
        _log(job_id, "error", f"job failed: {exc}\n{traceback.format_exc()[-3000:]}")
        jobs.update_job(job_id, status="error", error_message=str(exc)[:2000])
    finally:
        media_tokens.release_all_for_job(job_id)
        for p in (raw_path, converted_path):
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass


def _run(job_id, raw_path, original_filename, title, dataset_dir, raw_dir, qa_dir, converted_path) -> None:
    # ---- ingest ----
    jobs.set_status(job_id, "probing", "reading source video metadata")
    _log(job_id, "info", f"ingest: {original_filename}")
    probe = ffmpeg_utils.probe(raw_path)
    jobs.update_job(job_id, source_duration_ms=int(probe.duration_s * 1000))
    _log(job_id, "info", f"source: {probe.width}x{probe.height} @ {probe.fps:.2f}fps, "
                          f"{probe.duration_s/60:.1f} min, {probe.size_bytes/1e6:.0f} MB, audio={probe.has_audio}")

    plan = ffmpeg_utils.plan_conversion(probe)
    if plan.clipped:
        _log(job_id, "warning",
             f"source is {probe.duration_s/3600:.2f}h, longer than the 2h limit — "
             f"clipping to the first {plan.output_duration_s/3600:.2f}h")
        jobs.update_job(job_id, clipped=1)
    _log(job_id, "info", f"conversion plan: {plan.target_width}x{plan.target_height} @ "
                          f"{plan.target_fps}fps, {plan.video_bitrate_bps/1000:.0f}kbps video")

    # ---- convert ----
    jobs.set_status(job_id, "converting", "0%")
    last_logged = {"pct": -10}

    def on_progress(frac: float) -> None:
        pct = int(frac * 100)
        if pct - last_logged["pct"] >= 10:
            jobs.set_status(job_id, "converting", f"{pct}%")
            last_logged["pct"] = pct

    ffmpeg_utils.run_conversion(raw_path, converted_path, plan, on_progress=on_progress)
    out_size = converted_path.stat().st_size
    _log(job_id, "info", f"conversion done: {out_size/1e6:.0f} MB output")
    jobs.update_job(
        job_id,
        output_duration_ms=int(plan.output_duration_s * 1000),
        output_size_bytes=out_size,
        output_resolution=f"{plan.target_width}x{plan.target_height}",
    )
    try:
        raw_path.unlink()
    except OSError:
        pass

    thumb_path = THUMBS_DIR / f"{job_id}.jpg"
    try:
        ffmpeg_utils.generate_thumbnail(converted_path, plan.output_duration_s, thumb_path)
        jobs.update_job(job_id, thumbnail_path=str(thumb_path))
    except Exception as exc:  # noqa: BLE001 - thumbnail is best-effort
        _log(job_id, "warning", f"thumbnail generation failed: {exc}")

    # ---- Qwen setup ----
    api_key = app_settings.get_qwen_api_key()
    if not api_key:
        raise RuntimeError("no Qwen API key configured — add one in Settings")
    base_url = app_settings.get_qwen_base_url()
    public_base = app_settings.get_public_base_url()
    if not public_base:
        raise RuntimeError("no public base URL configured — set one in Settings so Qwen can fetch media")
    client = build_client(api_key, base_url)

    duration_ms = int(plan.output_duration_s * 1000)
    video_id = job_id

    # ---- Pass 1: global segmentation ----
    jobs.set_status(job_id, "analyzing_pass1", "sending full video to Qwen")
    global_summary, entities, chapters, safety_summary_raw, processing = _run_pass1(
        job_id, client, converted_path, public_base, title, duration_ms, raw_dir,
    )

    # ---- Pass 2: per-chapter/window detail ----
    jobs.set_status(job_id, "analyzing_pass2", f"0/{len(chapters)} chapters")
    id_counter = normalize.new_event_id_counter()
    all_events = []
    global_context = _global_context_text(global_summary, entities)
    for idx, chapter in enumerate(chapters):
        jobs.set_status(job_id, "analyzing_pass2", f"{idx}/{len(chapters)} chapters")
        events = _run_pass2_for_chapter(
            job_id, client, converted_path, public_base, chapter, global_context,
            duration_ms, raw_dir, idx, id_counter,
        )
        all_events.extend(events)
        processing.append(ProcessingInfo(
            model="qwen3.8-omni-flash", pass_="pass2", prompt_version=PROMPT_VERSION,
            generated_at=now_iso(),
        ))

    jobs.set_status(job_id, "merging", "deduplicating events")
    merged_events = normalize.dedupe_events(all_events)
    _log(job_id, "info", f"{len(all_events)} raw events -> {len(merged_events)} after dedupe")

    # ---- assemble + validate dataset ----
    jobs.set_status(job_id, "validate", "building canonical dataset")
    video_info = VideoInfo(
        id=video_id, source=original_filename, duration_ms=duration_ms, title=title or original_filename,
        clipped_from_original=bool(plan.clipped),
        original_duration_ms=int(probe.duration_s * 1000) if plan.clipped else None,
    )
    safety_summary = SafetySummary(
        rating=safety_summary_raw.get("rating", "safe"),
        categories=safety_summary_raw.get("categories") or [],
        ranges=[c.safety for c in chapters],
    )
    dataset = VideoDataset(
        video=video_info, global_summary=global_summary, entities=entities,
        chapters=chapters, events=merged_events, safety_summary=safety_summary,
        processing=processing,
    )
    dataset.compute_adult_progression()

    # ---- export ----
    jobs.set_status(job_id, "exporting", "writing dataset files")
    _export(dataset_dir, dataset)

    overall_rating = dataset.safety_summary.rating
    jobs.update_job(
        job_id, status="done", stage_detail="complete",
        chapters_count=len(dataset.chapters), events_count=len(dataset.events),
        safety_rating=overall_rating, dataset_dir=str(dataset_dir),
    )
    _log(job_id, "info", f"done: {len(dataset.chapters)} chapters, {len(dataset.events)} events, "
                          f"safety={overall_rating}")


def _global_context_text(global_summary: GlobalSummary, entities: list) -> str:
    ent_lines = "\n".join(f"- {e.label} ({e.type}), aliases: {', '.join(e.aliases) or 'none'}" for e in entities)
    return (
        f"Summary: {global_summary.short_summary}\n"
        f"Genre: {global_summary.genre}\nSetting: {global_summary.setting}\n"
        f"Narrative arc: {global_summary.narrative_arc}\n"
        f"Known entities:\n{ent_lines or '(none identified)'}"
    )


def _run_pass1(job_id, client, converted_path, public_base, title, duration_ms, raw_dir):
    token = media_tokens.create_token(job_id, converted_path, "video/mp4")
    video_url = f"{public_base}/m/{token}"
    try:
        result = _call_with_retry(
            lambda: call_pass1(client, video_url, title, duration_ms), job_id, "pass1", retries=1,
        )
    except Exception as exc:  # noqa: BLE001
        _log(job_id, "error", f"pass1 failed after retry, falling back to fixed windows: {exc}")
        chapters = normalize.fallback_fixed_chapters(duration_ms)
        return (
            GlobalSummary(short_summary="(global segmentation unavailable; fixed-window fallback used)"),
            [], chapters, {"rating": "safe", "categories": []},
            [ProcessingInfo(model="qwen3.8-omni-flash", pass_="pass1_failed_fallback",
                             prompt_version=PROMPT_VERSION, generated_at=now_iso())],
        )
    finally:
        media_tokens.release_token(token, delete_file=False)

    (raw_dir / "pass1_response.json").write_text(json.dumps(result.parsed, indent=2))
    parsed = result.parsed

    def log(level, msg):
        _log(job_id, level, msg)

    global_summary = GlobalSummary(**(parsed.get("global_summary") or {}))
    entities = normalize.assign_entity_ids(parsed.get("entities"), log=log)
    chapters = normalize.build_chapters(parsed.get("chapters"), duration_ms, log=log)
    if not chapters:
        _log(job_id, "warning", "pass1 returned no usable chapters — using fixed-window fallback")
        chapters = normalize.fallback_fixed_chapters(duration_ms)
    safety_summary_raw = parsed.get("safety_summary") or {}
    for note in parsed.get("uncertainty_notes") or []:
        _log(job_id, "info", f"pass1 uncertainty note: {note}")

    processing = [ProcessingInfo(model="qwen3.8-omni-flash", pass_="pass1", prompt_version=PROMPT_VERSION,
                                  generated_at=now_iso())]
    return global_summary, entities, chapters, safety_summary_raw, processing


def _run_pass2_for_chapter(job_id, client, converted_path, public_base, chapter, global_context,
                            video_duration_ms, raw_dir, chapter_idx, id_counter):
    events = []
    windows = normalize.windows_for_chapter(chapter)
    for w_idx, (w_start, w_end) in enumerate(windows):
        clip_path = TMP_DIR / f"{job_id}_ch{chapter_idx}_w{w_idx}.mp4"
        try:
            _cut_clip(converted_path, clip_path, w_start, w_end)
        except Exception as exc:  # noqa: BLE001
            _log(job_id, "warning", f"clip cut failed for {chapter.chapter_id} window {w_idx}: {exc}")
            continue

        token = media_tokens.create_token(job_id, clip_path, "video/mp4")
        video_url = f"{public_base}/m/{token}"
        try:
            result = _call_with_retry(
                lambda: call_pass2(
                    client, video_url, global_context, chapter.title, chapter.summary,
                    w_start, w_end, w_end - w_start,
                ),
                job_id, f"pass2 {chapter.chapter_id}/w{w_idx}", retries=1,
            )
            (raw_dir / f"chapter_{chapter_idx:03d}_window_{w_idx:02d}_response.json").write_text(
                json.dumps(result.parsed, indent=2)
            )
            window_events = normalize.build_events_from_window(
                result.parsed.get("events"), chapter, w_start, id_counter,
                log=lambda lvl, msg: _log(job_id, lvl, msg),
            )
            events.extend(window_events)
        except Exception as exc:  # noqa: BLE001
            _log(job_id, "warning", f"pass2 failed for {chapter.chapter_id} window {w_idx}, skipping: {exc}")
        finally:
            media_tokens.release_token(token, delete_file=True)
    return events


def _cut_clip(src: Path, dst: Path, start_ms: int, end_ms: int) -> None:
    import subprocess
    start_s = start_ms / 1000.0
    dur_s = max((end_ms - start_ms) / 1000.0, 0.5)
    cmd = [
        "ffmpeg", "-y", "-ss", f"{start_s:.3f}", "-i", str(src), "-t", f"{dur_s:.3f}",
        "-c", "copy", "-movflags", "+faststart", str(dst),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg clip cut failed: {proc.stderr.strip()[-500:]}")


def _call_with_retry(fn, job_id: str, label: str, retries: int = 1):
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except QwenAPIError as exc:
            last_exc = exc
            _log(job_id, "warning", f"{label} attempt {attempt + 1} failed: {exc}")
            time.sleep(2)
    raise last_exc  # type: ignore[misc]


def _export(dataset_dir: Path, dataset: VideoDataset) -> None:
    (dataset_dir / "video.json").write_text(dataset.model_dump_json(indent=2, by_alias=True))
    (dataset_dir / "entities.json").write_text(
        json.dumps([e.model_dump() for e in dataset.entities], indent=2)
    )

    with (dataset_dir / "chapters.jsonl").open("w") as f:
        for c in dataset.chapters:
            f.write(c.model_dump_json() + "\n")

    with (dataset_dir / "events.jsonl").open("w") as f:
        for e in dataset.events:
            f.write(e.model_dump_json() + "\n")

    with (dataset_dir / "safety_ranges.jsonl").open("w") as f:
        for s in dataset.safety_summary.ranges:
            f.write(s.model_dump_json() + "\n")

    if dataset.chapters:
        with (dataset_dir / "chapters.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["chapter_id", "start_ms", "end_ms", "title", "purpose", "safety_rating", "confidence"])
            for c in dataset.chapters:
                w.writerow([c.chapter_id, c.start_ms, c.end_ms, c.title, c.purpose, c.safety.rating, c.confidence])

    if dataset.events:
        with (dataset_dir / "events.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["event_id", "chapter_id", "start_ms", "end_ms", "type", "description", "confidence"])
            for e in dataset.events:
                w.writerow([e.event_id, e.chapter_id, e.start_ms, e.end_ms, e.type, e.description, e.confidence])

    manifest = {
        "schema_version": dataset.schema_version,
        "video_id": dataset.video.id,
        "generated_at": now_iso(),
        "files": [
            "video.json", "entities.json", "chapters.jsonl", "events.jsonl",
            "safety_ranges.jsonl", "chapters.csv", "events.csv",
        ],
        "chapters_count": len(dataset.chapters),
        "events_count": len(dataset.events),
        "safety_rating": dataset.safety_summary.rating,
    }
    (dataset_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
