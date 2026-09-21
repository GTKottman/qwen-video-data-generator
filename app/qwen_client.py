"""Qwen3.8-Omni-Flash integration via the DashScope OpenAI-compatible endpoint.

Mirrors the two-pass strategy from qwen38_omni_video_dataset_design.docx section 6:
Pass 1 = whole-video global segmentation, Pass 2 = per-chapter/window detail extraction.
Both passes ask for response_format=json_object (JSON Schema mode isn't listed for this
Omni model per the design doc), so callers must validate/retry locally.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from openai import OpenAI

from app.config import QWEN_MODEL

PASS1_SYSTEM = (
    "You are a video dataset analyst. Analyze only evidence present in the supplied "
    "video. Return JSON only, matching the schema described by the user. Do not invent "
    "identities, dialogue, events, or precise timestamps that cannot be supported by "
    "what is actually visible or audible."
)

PASS1_USER_TEMPLATE = """Analyze the complete video{title_clause}. Produce a single JSON object with exactly these top-level keys:

- "global_summary": {{"short_summary": str, "long_summary": str, "genre": str, "setting": str, "narrative_arc": str}}
- "entities": array of {{"label": str, "aliases": [str], "type": str, "first_seen_ms": int}} — stable participants/objects worth tracking across chapters.
- "chapters": array of {{"start_ms": int, "end_ms": int, "title": str, "summary": str, "purpose": str, "participants": [str] (entity labels), "setting": str, "progression": {{"state_before": str, "change": str, "state_after": str}}, "safety": {{"rating": one of ["safe","suggestive","nudity","adult_explicit","violence","other_sensitive"], "categories": [str], "participant_count": int or null, "configuration_change": bool, "configuration_label": str or null, "setting_change": bool, "framing_change": bool, "narrative_function": one of ["setup","escalation","transition","climax_or_peak","aftermath","unrelated_plot"] or null, "plot_progression": str or null, "confidence": float 0-1, "age_status": "adult" or "unknown"}}, "confidence": float 0-1, "approximate_boundary": bool}}
- "safety_summary": {{"rating": <overall video rating, same enum as above>, "categories": [str]}}
- "uncertainty_notes": array of str — anything ambiguous worth flagging for human review.

Boundary rule: create a new chapter when goal, setting, topic, major activity, narrative state, or content-safety state changes substantially. Prefer meaningful chapters over fixed-duration chunks. Timestamps are milliseconds from the start of THIS video file. Video duration is {duration_ms} ms.

Age gate: never infer that a person is an adult from appearance alone. If age is uncertain, set age_status to "unknown" and keep any sexual-content annotation non-graphic and classification-oriented only.
"""

PASS2_SYSTEM = (
    "Analyze only the supplied video clip, using the provided global context for names "
    "and continuity. Return JSON only, matching the schema described by the user. Do not "
    "embellish beyond observable evidence."
)

PASS2_USER_TEMPLATE = """Global context for this video (from a prior whole-video pass):
{global_context}

This clip is chapter "{chapter_title}" ({chapter_summary}), covering local time 0 to {clip_duration_ms} ms of this clip, which corresponds to {abs_start_ms}-{abs_end_ms} ms of the full video.

Extract atomic events as a JSON object with one top-level key "events": an array of objects each with:
{{"start_ms": int, "end_ms": int, "type": one of ["scene_change","action","dialogue","plot_progression","instruction_step","object_state_change","audio_event","onscreen_text","camera_change","safety_transition"], "description": str (neutral, non-graphic), "participants": [str] (entity labels from global context when applicable), "evidence": str (brief justification), "confidence": float 0-1}}

Timestamps must be LOCAL milliseconds within this clip (0 to {clip_duration_ms}), not absolute video time. Include dialogue topics, relevant visible actions, scene/camera changes, on-screen text, audio events, and plot/instruction progression. If adult NSFW content is present, classify it via a "safety_transition" event and track non-graphic progression only: configuration/setting/framing changes and narrative function, never graphic sexual description. Do not fabricate precision: omit an event rather than guessing a timestamp you cannot support.
"""


class QwenAPIError(RuntimeError):
    pass


@dataclass
class QwenCallResult:
    raw_text: str
    parsed: dict[str, Any]
    usage: Optional[dict[str, Any]]


def build_client(api_key: str, base_url: str) -> OpenAI:
    return OpenAI(api_key=api_key, base_url=base_url)


def test_connection(client: OpenAI, model: str = QWEN_MODEL) -> None:
    """Minimal, cheap call to confirm the key + base_url + model combination works."""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": [{"type": "text", "text": "Reply with the single word: ok"}]}],
            modalities=["text"],
            max_tokens=5,
        )
    except Exception as exc:  # noqa: BLE001 - surface any SDK/HTTP error to the setup wizard
        raise QwenAPIError(str(exc)) from exc
    if not resp.choices:
        raise QwenAPIError("Qwen returned no choices for the test request")


def _stream_chat(client: OpenAI, model: str, system: str, user: str, video_url: str) -> QwenCallResult:
    stream = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "video_url", "video_url": {"url": video_url}},
                {"type": "text", "text": user},
            ]},
        ],
        modalities=["text"],
        response_format={"type": "json_object"},
        stream=True,
        stream_options={"include_usage": True},
    )
    chunks: list[str] = []
    usage = None
    for event in stream:
        if event.choices:
            delta = event.choices[0].delta
            if delta and delta.content:
                chunks.append(delta.content)
        if getattr(event, "usage", None):
            usage = event.usage.model_dump() if hasattr(event.usage, "model_dump") else dict(event.usage)
    raw_text = "".join(chunks).strip()
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise QwenAPIError(f"model did not return valid JSON: {exc}") from exc
    return QwenCallResult(raw_text=raw_text, parsed=parsed, usage=usage)


def call_pass1(client: OpenAI, video_url: str, title: str, duration_ms: int, model: str = QWEN_MODEL) -> QwenCallResult:
    title_clause = f' titled "{title}"' if title else ""
    user = PASS1_USER_TEMPLATE.format(title_clause=title_clause, duration_ms=duration_ms)
    return _stream_chat(client, model, PASS1_SYSTEM, user, video_url)


def call_pass2(
    client: OpenAI,
    video_url: str,
    global_context: str,
    chapter_title: str,
    chapter_summary: str,
    abs_start_ms: int,
    abs_end_ms: int,
    clip_duration_ms: int,
    model: str = QWEN_MODEL,
) -> QwenCallResult:
    user = PASS2_USER_TEMPLATE.format(
        global_context=global_context,
        chapter_title=chapter_title or "(untitled)",
        chapter_summary=chapter_summary or "(no summary)",
        abs_start_ms=abs_start_ms,
        abs_end_ms=abs_end_ms,
        clip_duration_ms=clip_duration_ms,
    )
    return _stream_chat(client, model, PASS2_SYSTEM, user, video_url)
