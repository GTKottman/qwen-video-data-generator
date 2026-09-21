"""Canonical dataset models, matching qwen38_omni_video_dataset_design.docx section 4/5/8.

These are the deterministic validator/normalizer: Qwen's raw JSON responses are parsed
into these models, invalid records are quarantined rather than silently repaired, and
clean records are what gets exported to JSON/JSONL/CSV.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"

EVENT_TYPES = {
    "scene_change", "action", "dialogue", "plot_progression", "instruction_step",
    "object_state_change", "audio_event", "onscreen_text", "camera_change",
    "safety_transition",
}

SAFETY_RATINGS = {
    "safe", "suggestive", "nudity", "adult_explicit", "violence", "other_sensitive",
}

NARRATIVE_FUNCTIONS = {
    "setup", "escalation", "transition", "climax_or_peak", "aftermath", "unrelated_plot",
}

AGE_STATUSES = {"adult", "unknown"}


class SafetyRecord(BaseModel):
    rating: str = "safe"
    categories: list[str] = Field(default_factory=list)
    start_ms: int = 0
    end_ms: int = 0
    participant_count: Optional[int] = None
    configuration_change: bool = False
    configuration_label: Optional[str] = None
    setting_change: bool = False
    framing_change: bool = False
    narrative_function: Optional[str] = None
    plot_progression: Optional[str] = None
    confidence: float = 0.5
    age_status: str = "unknown"

    @field_validator("rating")
    @classmethod
    def _rating_ok(cls, v):
        return v if v in SAFETY_RATINGS else "other_sensitive"

    @field_validator("narrative_function")
    @classmethod
    def _fn_ok(cls, v):
        if v is None:
            return v
        return v if v in NARRATIVE_FUNCTIONS else None

    @field_validator("age_status")
    @classmethod
    def _age_ok(cls, v):
        return v if v in AGE_STATUSES else "unknown"

    @field_validator("confidence")
    @classmethod
    def _clamp_conf(cls, v):
        return max(0.0, min(1.0, float(v)))


class Progression(BaseModel):
    state_before: str = ""
    change: str = ""
    state_after: str = ""


class Entity(BaseModel):
    entity_id: str
    label: str
    aliases: list[str] = Field(default_factory=list)
    type: str = "person"
    first_seen_ms: int = 0


class Chapter(BaseModel):
    chapter_id: str
    start_ms: int
    end_ms: int
    title: str = ""
    summary: str = ""
    purpose: str = ""
    participants: list[str] = Field(default_factory=list)
    setting: str = ""
    progression: Progression = Field(default_factory=Progression)
    safety: SafetyRecord = Field(default_factory=SafetyRecord)
    confidence: float = 0.5
    approximate_boundary: bool = False

    @model_validator(mode="after")
    def _order(self):
        if self.end_ms <= self.start_ms:
            raise ValueError(f"chapter {self.chapter_id}: end_ms <= start_ms")
        return self


class Event(BaseModel):
    event_id: str
    chapter_id: str
    start_ms: int
    end_ms: int
    type: str
    description: str = ""
    participants: list[str] = Field(default_factory=list)
    evidence: str = ""
    confidence: float = 0.5

    @field_validator("type")
    @classmethod
    def _type_ok(cls, v):
        if v not in EVENT_TYPES:
            raise ValueError(f"unknown event type {v!r}")
        return v

    @model_validator(mode="after")
    def _order(self):
        if self.end_ms < self.start_ms:
            raise ValueError(f"event {self.event_id}: end_ms < start_ms")
        return self


class GlobalSummary(BaseModel):
    short_summary: str = ""
    long_summary: str = ""
    genre: str = ""
    setting: str = ""
    narrative_arc: str = ""


class VideoInfo(BaseModel):
    id: str
    source: str
    duration_ms: int
    title: str = ""
    language: str = "unknown"
    clipped_from_original: bool = False
    original_duration_ms: Optional[int] = None


class SafetySummary(BaseModel):
    rating: str = "safe"
    categories: list[str] = Field(default_factory=list)
    ranges: list[SafetyRecord] = Field(default_factory=list)

    @field_validator("rating")
    @classmethod
    def _rating_ok(cls, v):
        return v if v in SAFETY_RATINGS else "other_sensitive"


class ProcessingInfo(BaseModel):
    model: str
    pass_: str = Field(alias="pass")
    prompt_version: str
    generated_at: str

    model_config = {"populate_by_name": True}


class AdultProgressionEntry(BaseModel):
    """Derived, non-graphic scene-progression record for R8 — computed deterministically
    from chapter/event safety fields, never authored freeform by the model."""
    start_ms: int
    end_ms: int
    chapter_id: str
    rating: str
    participant_count: Optional[int]
    configuration_change: bool
    configuration_label: Optional[str]
    setting_change: bool
    framing_change: bool
    narrative_function: Optional[str]
    plot_progression: Optional[str]
    confidence: float


class VideoDataset(BaseModel):
    schema_version: str = SCHEMA_VERSION
    video: VideoInfo
    global_summary: GlobalSummary
    entities: list[Entity] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    safety_summary: SafetySummary
    adult_scene_progression: list[AdultProgressionEntry] = Field(default_factory=list)
    processing: list[ProcessingInfo] = Field(default_factory=list)

    def compute_adult_progression(self) -> None:
        entries: list[AdultProgressionEntry] = []
        for ch in self.chapters:
            s = ch.safety
            if s.rating in ("adult_explicit", "nudity") or s.configuration_change or s.setting_change:
                entries.append(AdultProgressionEntry(
                    start_ms=s.start_ms or ch.start_ms,
                    end_ms=s.end_ms or ch.end_ms,
                    chapter_id=ch.chapter_id,
                    rating=s.rating,
                    participant_count=s.participant_count,
                    configuration_change=s.configuration_change,
                    configuration_label=s.configuration_label,
                    setting_change=s.setting_change,
                    framing_change=s.framing_change,
                    narrative_function=s.narrative_function,
                    plot_progression=s.plot_progression,
                    confidence=s.confidence,
                ))
        entries.sort(key=lambda e: e.start_ms)
        self.adult_scene_progression = entries
