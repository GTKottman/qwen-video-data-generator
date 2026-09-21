# Qwen Video Host

A self-hosted web app that hosts videos for Qwen3.8-Omni-Flash: drag a video in, it's
checked against Qwen's public-URL video input limits (at most 1 hour, under 2GB, exactly
720p or 1080p), and — if it passes — hosted here with a permanent URL. A separate Qwen
client app (in `client/`, work in progress) takes that URL, runs the actual two-pass
Qwen classification, and hands back the resulting dataset.

This site itself never re-encodes or calls Qwen. It only validates and hosts.

## How it fits together

- **FastAPI** app, single container, SQLite for users/settings/hosted-file metadata.
- **ffmpeg/ffprobe** are used only to probe an upload's duration/resolution/size for
  validation and to generate a thumbnail — never to re-encode.
- Qwen's API only accepts video as a **public URL** (no reliable base64/local-file path
  for video). Since this app runs on your own domain, each accepted upload gets a
  permanent, unauthenticated `/m/<id>` link that stays live until you delete it from the
  dashboard — that's the URL you paste into the Qwen client app.
- Uploads that don't meet the limits are rejected with a specific message (wrong
  resolution, too long, too large for the given resolution) telling you how to fix it
  with `client/preconvert.py` — see below.

## Deploying with Coolify

1. Push this project to a git repository Coolify can reach (GitHub/GitLab/Gitea, or a
   private repo with a deploy key).
2. In Coolify: **New Resource → Application → your repo**. Build pack: **Dockerfile**
   (the one at the repo root). Expose port `8000`.
3. Add a **persistent volume** mounted at `/data` — this holds the SQLite DB and every
   hosted video + thumbnail. Do not skip this; without it, everything is lost on every
   redeploy.
4. Attach a domain in Coolify and let it provision TLS (Let's Encrypt via Traefik). Note
   the resulting `https://...` URL — you'll enter it as the "public base URL" during
   first-run setup, since that's what `/m/<id>` links are built from.
5. (Optional but recommended) Set `APP_SECRET` to a long random string in Coolify's
   environment variables — see `.env.example`. If you skip it, the app generates one on
   first boot and persists it in the volume, which is fine for a single instance.
6. Deploy. On first visit you'll get a 3-step setup wizard: create your admin
   username/password, a system check (ffmpeg/ffprobe present, disk space), then the
   public base URL from step 4.

## Client-side pre-conversion (required if your file doesn't already meet the limits)

The site enforces Qwen's own limits at upload time — at most 1 hour, strictly under 2GB,
and exactly 720p or 1080p — and rejects anything else with a message telling you what to
fix. It never re-encodes for you.

`client/preconvert.py` is a standalone script (stdlib only, needs `ffmpeg`/`ffprobe`
on PATH) that downscales/trims a source video toward those same limits on your own
machine before you upload:

```bash
python3 client/preconvert.py input.mkv                # writes input.preconverted.mp4
python3 client/preconvert.py input.mkv -o ready.mp4    # custom output path
python3 client/preconvert.py input.mkv --dry-run       # show the plan, don't encode
```

Then drag the resulting file onto the site as usual.

Prefer a GUI? `client/start_conversion_ui.sh` checks for python3/tkinter/ffmpeg and launches
`client/conversion_ui.py`, a small Tkinter window (pick input/output, optional dry-run, a
progress bar) that wraps the same `preconvert.py` logic:

```bash
./client/start_conversion_ui.sh
```

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATA_DIR=./data PUBLIC_BASE_URL=http://localhost:8000
uvicorn app.main:app --reload
```

Note: for local testing without a real public domain, an external Qwen client won't be
able to reach `http://localhost:8000/m/<id>` — that only works once deployed somewhere
publicly reachable (e.g. the Coolify deployment above).

## Data retention

- Hosted videos and their thumbnails are kept **indefinitely until you delete them**
  from the dashboard — there's no automatic expiry.
- Deleting a hosted video removes its file, thumbnail, and database row immediately;
  its `/m/<id>` URL then 404s.

## The Qwen client app

The two-pass classification pipeline (chapter/event extraction, dataset export) no
longer runs on this site — it's a separate desktop app, `qwen_client/`, that takes a
hosted video's `/m/<id>` URL, runs it through Qwen, and writes the resulting dataset
(JSON/JSONL/CSV) locally. It's a personal tool that *uses* this site, not part of it, so
it's gitignored here rather than committed — see `qwen38_omni_video_dataset_design.docx`
for the pipeline design it implements.

```bash
pip install -r qwen_client/requirements.txt
python3 qwen_client/classify.py --configure          # one-time: set your Qwen API key
python3 qwen_client/classify.py https://video.yourdomain.com/m/abc123
```

Or `./qwen_client/start_classify_ui.sh` for a small Tkinter GUI (paste the URL, click
Run, watch the log) instead of the CLI.
