# Qwen Video Classifier

A self-hosted web app: upload a video, it's converted/downscaled on the server with
ffmpeg, sent to Qwen3.8-Omni-Flash for two-pass classification (global segmentation +
per-chapter detail, per `qwen38_omni_video_dataset_design.docx`), and the results are
exported as JSON/JSONL/CSV. The video itself is deleted once analysis finishes — only
a thumbnail, structured logs, and the dataset files are kept.

## How it fits together

- **FastAPI** app, single container, SQLite for users/settings/job history.
- **ffmpeg** (CPU, libx264) converts uploads toward a ~1.85GB target, never below 720p,
  clips anything over 2 hours, and generates a 3x3 contact-sheet thumbnail.
- Qwen's API only accepts video as a **public URL** (no reliable base64/local-file path
  for video). Since this app runs on your own domain, it serves the converted file (and
  later, per-chapter clips) at a random, single-use `/m/<token>` link that only Qwen's
  servers ever fetch — created right before each Qwen call and deleted right after,
  success or failure.
- Two-pass pipeline: Pass 1 sends the whole video for chapter/entity/safety
  segmentation; Pass 2 sends each chapter (or 7-minute sub-windows with 10s overlap for
  long chapters) for detailed event extraction; a deterministic validator
  (`app/normalize.py`) assigns IDs, clamps timestamps, dedupes boundary-overlap events,
  and quarantines anything that fails schema validation instead of crashing the job.

## Deploying with Coolify

1. Push this project to a git repository Coolify can reach (GitHub/GitLab/Gitea, or a
   private repo with a deploy key).
2. In Coolify: **New Resource → Application → your repo**. Build pack: **Dockerfile**
   (the one at the repo root). Expose port `8000`.
3. Add a **persistent volume** mounted at `/data` — this holds the SQLite DB, thumbnails,
   and every dataset's JSON/JSONL/CSV output. Do not skip this; without it, history is
   lost on every redeploy.
4. Attach a domain in Coolify and let it provision TLS (Let's Encrypt via Traefik). Note
   the resulting `https://...` URL — you'll enter it as the "public base URL" during
   first-run setup, since Qwen needs a real HTTPS URL to fetch converted video from.
5. (Optional but recommended) Set `APP_SECRET` to a long random string in Coolify's
   environment variables — see `.env.example`. If you skip it, the app generates one on
   first boot and persists it in the volume, which is fine for a single instance.
6. Deploy. On first visit you'll get a 3-step setup wizard: create your admin
   username/password, a system check (ffmpeg present, disk space, network reachability
   to Alibaba Cloud), then your Qwen API key + region + the public base URL from step 4
   (verified live before it's saved).

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATA_DIR=./data PUBLIC_BASE_URL=http://localhost:8000
uvicorn app.main:app --reload
```

Note: for local testing without a real public domain, Qwen's servers won't be able to
reach `http://localhost:8000/m/<token>` — that only works once deployed somewhere
publicly reachable (e.g. the Coolify deployment above).

## Data retention

- Original upload: deleted immediately after conversion succeeds.
- Converted video / per-chapter clips: deleted immediately after each Qwen call that
  needed them finishes (success or failure), and never later than the media-token TTL
  (4 hours) via a background sweep.
- Kept indefinitely (until you delete them yourself): one JPEG thumbnail per job, the
  exported dataset files (`manifest.json`, `video.json`, `entities.json`,
  `chapters.jsonl`, `events.jsonl`, `safety_ranges.jsonl`, `chapters.csv`, `events.csv`)
  under `/data/datasets/<job_id>/`, the raw Qwen responses under that same directory's
  `raw/` subfolder (for debugging/reprocessing), and structured job logs in SQLite.
