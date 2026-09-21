from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app import jobs
from app.auth import current_user
from app.config import THUMBS_DIR

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

ALLOWED_DOWNLOAD_NAMES = {
    "manifest.json", "video.json", "entities.json", "chapters.jsonl", "events.jsonl",
    "safety_ranges.jsonl", "chapters.csv", "events.csv",
}


@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "job_detail.html", {
        "user": current_user(request),
        "job": job,
        "logs": jobs.get_logs(job_id),
        "clipped_notice": request.query_params.get("clipped") == "1",
        "downloads": sorted(ALLOWED_DOWNLOAD_NAMES) if job["status"] == "done" else [],
    })


@router.get("/jobs/{job_id}/status")
def job_status(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404)
    logs = jobs.get_logs(job_id, limit=500)
    return JSONResponse({
        "status": job["status"],
        "stage_detail": job["stage_detail"],
        "error_message": job["error_message"],
        "chapters_count": job["chapters_count"],
        "events_count": job["events_count"],
        "safety_rating": job["safety_rating"],
        "logs": [{"ts": l["ts"], "level": l["level"], "message": l["message"]} for l in logs[-100:]],
    })


@router.get("/jobs/{job_id}/thumbnail")
def job_thumbnail(job_id: str):
    job = jobs.get_job(job_id)
    if not job or not job.get("thumbnail_path"):
        raise HTTPException(404)
    path = Path(job["thumbnail_path"])
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@router.get("/jobs/{job_id}/download/{filename}")
def job_download(job_id: str, filename: str):
    if filename not in ALLOWED_DOWNLOAD_NAMES:
        raise HTTPException(400, "unknown file")
    job = jobs.get_job(job_id)
    if not job or not job.get("dataset_dir"):
        raise HTTPException(404)
    path = Path(job["dataset_dir"]) / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, filename=filename)
