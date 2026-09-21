from __future__ import annotations

import mimetypes
import secrets
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import app_settings, ffmpeg_utils, media_store
from app.auth import current_user
from app.config import ALLOWED_HEIGHTS, HOSTED_DIR, MAX_DURATION_SECONDS, MAX_UPLOAD_BYTES, TMP_DIR, THUMBS_DIR

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

ALLOWED_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov", ".flv", ".wmv", ".webm", ".m4v"}

# Multipart framing (boundaries, the title field, headers) adds a little on top of the
# raw file size, so give the Content-Length precheck a small allowance rather than
# rejecting a borderline-legitimate upload before we've even read it.
CONTENT_LENGTH_HEADROOM_BYTES = 5 * 1024 * 1024

# We keep writing past MAX_UPLOAD_BYTES up to this hard ceiling so an oversized-but-
# plausible upload (e.g. a 2.3GB 1080p file) still finishes probing and gets the
# correct tailored rejection message, rather than being truncated mid-write and
# probed as garbage. This is just a sanity bound against runaway uploads, not the
# business rule (that's MAX_UPLOAD_BYTES, checked after the file is fully written).
HARD_ABORT_BYTES = 4 * 1024 ** 3

TOO_LARGE_MESSAGE = (
    "file is far larger than Qwen's 2GB cap — pre-convert it first with "
    "client/preconvert.py (or client/start_conversion_ui.sh for a GUI) and re-upload the result"
)


def _format_bytes(n: int) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f}GB"
    if n >= 1e6:
        return f"{n / 1e6:.1f}MB"
    return f"{n / 1e3:.0f}KB"


def _format_duration(ms: int) -> str:
    total_s = int(ms / 1000)
    m, s = divmod(total_s, 60)
    return f"{m}:{s:02d}"


def _file_context(row: dict, public_base: str) -> dict:
    return {
        **row,
        "size_str": _format_bytes(row["size_bytes"]),
        "duration_str": _format_duration(row["duration_ms"]) if row["duration_ms"] else "—",
        "resolution": f"{row['width']}x{row['height']}" if row["width"] else "—",
        "url": f"{public_base}/m/{row['id']}" if public_base else f"/m/{row['id']}",
    }


def _dashboard_context(request: Request, error: str | None = None) -> dict:
    public_base = app_settings.get_public_base_url()
    return {
        "user": current_user(request),
        "files": [_file_context(row, public_base) for row in media_store.list_all()],
        "max_upload_gb": f"{MAX_UPLOAD_BYTES / 1e9:.2f}",
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "public_base_url": public_base,
        "error": error,
    }


@router.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", _dashboard_context(request))


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    ext = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
    if ext not in ALLOWED_EXTENSIONS:
        return _reject(request, f"unsupported file type {ext}")

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > HARD_ABORT_BYTES + CONTENT_LENGTH_HEADROOM_BYTES:
        return _reject(request, TOO_LARGE_MESSAGE)

    token = secrets.token_hex(8)
    tmp_path = TMP_DIR / f"upload_{token}{ext}"
    bytes_written = 0
    too_large = False
    with tmp_path.open("wb") as out:
        while True:
            chunk = await file.read(4 * 1024 * 1024)
            if not chunk:
                break
            bytes_written += len(chunk)
            if bytes_written > HARD_ABORT_BYTES:
                too_large = True
                break
            out.write(chunk)

    if too_large:
        tmp_path.unlink(missing_ok=True)
        return _reject(request, TOO_LARGE_MESSAGE)

    try:
        probe = ffmpeg_utils.probe(tmp_path)
    except Exception as exc:  # noqa: BLE001 - surface any probe failure as a rejection, not a 500
        tmp_path.unlink(missing_ok=True)
        return _reject(request, f"couldn't read this as a video file: {exc}")

    reject_reason = _validate(probe)
    if reject_reason:
        tmp_path.unlink(missing_ok=True)
        return _reject(request, reject_reason)

    stored_path = HOSTED_DIR / f"{token}{ext}"
    tmp_path.rename(stored_path)

    thumb_path = THUMBS_DIR / f"{token}.jpg"
    try:
        ffmpeg_utils.generate_thumbnail(stored_path, probe.duration_s, thumb_path)
    except Exception:  # noqa: BLE001 - thumbnail is best-effort
        thumb_path = None

    content_type = mimetypes.guess_type(str(stored_path))[0] or "application/octet-stream"
    media_store.create(
        original_filename=file.filename or stored_path.name,
        stored_path=stored_path,
        content_type=content_type,
        size_bytes=stored_path.stat().st_size,
        duration_ms=int(probe.duration_s * 1000),
        width=probe.width,
        height=probe.height,
        thumbnail_path=thumb_path,
    )
    return RedirectResponse("/", status_code=303)


@router.get("/files/{file_id}/thumbnail")
def file_thumbnail(file_id: str):
    row = media_store.get(file_id)
    if not row or not row.get("thumbnail_path"):
        raise HTTPException(404)
    path = Path(row["thumbnail_path"])
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@router.post("/files/{file_id}/delete")
def delete_file(file_id: str):
    row = media_store.get(file_id)
    if not row:
        raise HTTPException(404)
    media_store.delete(file_id)
    return RedirectResponse("/", status_code=303)


def _validate(probe) -> str | None:
    if probe.duration_s > MAX_DURATION_SECONDS:
        minutes = probe.duration_s / 60
        return f"video is {minutes:.0f} min long — must be 1 hour or under. Trim it and re-upload."
    if probe.height not in ALLOWED_HEIGHTS:
        return (
            f"video is {probe.width}x{probe.height} — must be exactly 720p or 1080p. "
            "Re-export at one of those resolutions with client/preconvert.py and re-upload."
        )
    if probe.size_bytes >= MAX_UPLOAD_BYTES:
        gb = probe.size_bytes / 1e9
        if probe.height == 1080:
            return (
                f"1080p file is {gb:.2f}GB — must be under 2GB. Convert it to 720p and under 2GB "
                "with client/preconvert.py and re-upload."
            )
        return (
            f"720p file is {gb:.2f}GB — must be under 2GB. Shorten it or lower the bitrate "
            "with client/preconvert.py, still under 2GB, and re-upload."
        )
    return None


def _reject(request: Request, message: str):
    return templates.TemplateResponse(
        request, "dashboard.html", _dashboard_context(request, error=message), status_code=400,
    )
