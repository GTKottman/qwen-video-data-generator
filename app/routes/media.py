"""Public, unauthenticated route — intentionally not behind login (see main.py's
auth_gate). Serves only files that a live media token points at: short-lived, random,
job-scoped links created by the pipeline solely so Qwen's servers can fetch a video/clip.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app import media_tokens

router = APIRouter()


@router.get("/m/{token}")
def fetch_media(token: str):
    row = media_tokens.resolve_token(token)
    if not row:
        raise HTTPException(404)
    path = Path(row["file_path"])
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type=row["content_type"])
