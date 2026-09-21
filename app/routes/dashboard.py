from __future__ import annotations

import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import ffmpeg_utils, jobs, pipeline
from app.auth import current_user
from app.config import MAX_DURATION_SECONDS, TMP_DIR

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

ALLOWED_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov", ".flv", ".wmv", ".webm", ".m4v"}


@router.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": current_user(request),
        "jobs": jobs.list_jobs(),
    })


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), title: str = Form("")):
    job_id = uuid.uuid4().hex
    ext = Path(file.filename or "video.mp4").suffix.lower() or ".mp4"
    if ext not in ALLOWED_EXTENSIONS:
        jobs.create_job(job_id, file.filename or "upload")
        jobs.update_job(job_id, status="error", error_message=f"unsupported file type {ext}")
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    raw_path = TMP_DIR / f"{job_id}_raw{ext}"
    with raw_path.open("wb") as out:
        while True:
            chunk = await file.read(4 * 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)

    jobs.create_job(job_id, file.filename or raw_path.name)

    clipped_notice = False
    try:
        probe = ffmpeg_utils.probe(raw_path)
        clipped_notice = probe.duration_s > MAX_DURATION_SECONDS
    except Exception:
        pass  # pipeline will surface the probe error properly

    thread = threading.Thread(
        target=pipeline.run_pipeline, args=(job_id, raw_path, file.filename or raw_path.name, title.strip()),
        daemon=True,
    )
    thread.start()

    suffix = "?clipped=1" if clipped_notice else ""
    return RedirectResponse(f"/jobs/{job_id}{suffix}", status_code=303)
