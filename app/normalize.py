"""Deterministic validator/merger described in design-doc section 8: turns raw Qwen
JSON into canonical, ID-assigned, timestamp-clamped records. Invalid records are
quarantined (dropped + logged) rather than silently repaired or allowed to crash a job.
"""
from __future__ import annotations

import difflib
import itertools
from typing import Callable, Optional

from pydantic import ValidationError

from app.dataset_schema import Chapter, Entity, Event, Progression, SafetyRecord

LogFn = Callable[[str, str], None]  # (level, message)

CHAPTER_MAX_SINGLE_MS = 10 * 60 * 1000
CHAPTER_WINDOW_MS = 7 * 60 * 1000
CHAPTER_WINDOW_OVERLAP_MS = 10_000
DEDUPE_OVERLAP_TOLERANCE_MS = 8_000


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(value, hi))


def assign_entity_ids(raw_entities: list, log: Optional[LogFn] = None) -> list[Entity]:
    entities: list[Entity] = []
    for i, e in enumerate(raw_entities or []):
        try:
            entities.append(Entity(
                entity_id=f"entity_{i + 1:03d}",
                label=str(e.get("label", "unknown")),
                aliases=[str(a) for a in (e.get("aliases") or [])],
                type=str(e.get("type", "person")),
                first_seen_ms=int(e.get("first_seen_ms") or 0),
            ))
        except (ValidationError, TypeError, ValueError) as exc:
            if log:
                log("warning", f"quarantined malformed entity #{i}: {exc}")
    return entities


def build_chapters(raw_chapters: list, duration_ms: int, log: Optional[LogFn] = None) -> list[Chapter]:
    raw_sorted = sorted(raw_chapters or [], key=lambda x: x.get("start_ms", 0))
    chapters: list[Chapter] = []
    for i, c in enumerate(raw_sorted):
        try:
            start = clamp(int(c.get("start_ms", 0)), 0, duration_ms)
            end = clamp(int(c.get("end_ms", start + 1)), start + 1, duration_ms)
            safety_raw = dict(c.get("safety") or {})
            safety_raw.setdefault("start_ms", start)
            safety_raw.setdefault("end_ms", end)
            chapter = Chapter(
                chapter_id=f"ch_{i:03d}",
                start_ms=start,
                end_ms=end,
                title=str(c.get("title", "")),
                summary=str(c.get("summary", "")),
                purpose=str(c.get("purpose", "")),
                participants=[str(p) for p in (c.get("participants") or [])],
                setting=str(c.get("setting", "")),
                progression=Progression(**(c.get("progression") or {})),
                safety=SafetyRecord(**safety_raw),
                confidence=float(c.get("confidence", 0.5)),
                approximate_boundary=bool(c.get("approximate_boundary", False)),
            )
        except (ValidationError, TypeError, ValueError) as exc:
            if log:
                log("warning", f"quarantined malformed chapter #{i}: {exc}")
            continue
        chapters.append(chapter)

    # Remove impossible overlaps: clamp each chapter's start to the previous chapter's end.
    for i in range(1, len(chapters)):
        if chapters[i].start_ms < chapters[i - 1].end_ms:
            if log:
                log("warning", f"chapter overlap fixed: {chapters[i].chapter_id} start clamped to {chapters[i-1].end_ms}")
            chapters[i].start_ms = chapters[i - 1].end_ms

    cleaned = [c for c in chapters if c.end_ms > c.start_ms]
    dropped = len(chapters) - len(cleaned)
    if dropped and log:
        log("warning", f"dropped {dropped} zero-length chapter(s) after overlap normalization")
    return cleaned


def fallback_fixed_chapters(duration_ms: int, window_ms: int = 10 * 60 * 1000) -> list[Chapter]:
    """Coarse fixed-window fallback when Pass 1 segmentation fails entirely."""
    chapters = []
    pos = 0
    i = 0
    while pos < duration_ms:
        end = min(pos + window_ms, duration_ms)
        chapters.append(Chapter(
            chapter_id=f"ch_{i:03d}",
            start_ms=pos, end_ms=end,
            title=f"Segment {i + 1}", summary="Fixed-window fallback segment (global segmentation failed).",
            approximate_boundary=True,
        ))
        pos = end
        i += 1
    return chapters


def windows_for_chapter(chapter: Chapter) -> list[tuple[int, int]]:
    dur = chapter.end_ms - chapter.start_ms
    if dur <= CHAPTER_MAX_SINGLE_MS:
        return [(chapter.start_ms, chapter.end_ms)]
    windows = []
    pos = chapter.start_ms
    while pos < chapter.end_ms:
        w_end = min(pos + CHAPTER_WINDOW_MS, chapter.end_ms)
        windows.append((pos, w_end))
        if w_end >= chapter.end_ms:
            break
        pos = w_end - CHAPTER_WINDOW_OVERLAP_MS
    return windows


def new_event_id_counter():
    return itertools.count(1)


def build_events_from_window(
    raw_events: list, chapter: Chapter, window_start_ms: int, id_counter, log: Optional[LogFn] = None,
) -> list[Event]:
    events: list[Event] = []
    for e in raw_events or []:
        try:
            local_start = int(e.get("start_ms", 0))
            local_end = int(e.get("end_ms", local_start))
            abs_start = clamp(window_start_ms + local_start, chapter.start_ms, chapter.end_ms)
            abs_end = clamp(window_start_ms + max(local_end, local_start), chapter.start_ms, chapter.end_ms)
            events.append(Event(
                event_id=f"ev_{next(id_counter):06d}",
                chapter_id=chapter.chapter_id,
                start_ms=abs_start,
                end_ms=abs_end,
                type=str(e.get("type", "action")),
                description=str(e.get("description", "")),
                participants=[str(p) for p in (e.get("participants") or [])],
                evidence=str(e.get("evidence", "")),
                confidence=float(e.get("confidence", 0.5)),
            ))
        except (ValidationError, TypeError, ValueError) as exc:
            if log:
                log("warning", f"quarantined malformed event in {chapter.chapter_id}: {exc}")
    return events


def _similar(a: str, b: str) -> bool:
    if not a or not b:
        return a == b
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.6


def dedupe_events(events: list[Event]) -> list[Event]:
    """Resolve duplicate events near window-boundary overlaps; prefer higher confidence."""
    by_chapter: dict[str, list[Event]] = {}
    for e in events:
        by_chapter.setdefault(e.chapter_id, []).append(e)

    result: list[Event] = []
    for chapter_id, evs in by_chapter.items():
        evs.sort(key=lambda e: e.start_ms)
        kept: list[Event] = []
        for e in evs:
            dup_idx = None
            for idx, r in enumerate(kept):
                if r.type != e.type:
                    continue
                if abs(r.start_ms - e.start_ms) <= DEDUPE_OVERLAP_TOLERANCE_MS and _similar(r.description, e.description):
                    dup_idx = idx
                    break
            if dup_idx is None:
                kept.append(e)
            elif e.confidence > kept[dup_idx].confidence:
                kept[dup_idx] = e
        result.extend(kept)

    return sorted(result, key=lambda e: e.start_ms)
