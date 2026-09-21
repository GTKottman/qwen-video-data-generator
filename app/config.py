import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "app.db"
TMP_DIR = DATA_DIR / "tmp"
DATASETS_DIR = DATA_DIR / "datasets"
THUMBS_DIR = DATA_DIR / "thumbnails"
HOSTED_DIR = DATA_DIR / "hosted"
SECRET_FILE = DATA_DIR / ".app_secret"

for d in (DATA_DIR, TMP_DIR, DATASETS_DIR, THUMBS_DIR, HOSTED_DIR):
    d.mkdir(parents=True, exist_ok=True)


def _load_or_create_secret() -> str:
    env_secret = os.environ.get("APP_SECRET")
    if env_secret:
        return env_secret
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    generated = secrets.token_hex(32)
    SECRET_FILE.write_text(generated)
    SECRET_FILE.chmod(0o600)
    return generated


APP_SECRET = _load_or_create_secret()

PORT = int(os.environ.get("PORT", "8000"))

# The externally-reachable https://... origin for this deployment (e.g. what Coolify /
# your reverse proxy exposes). Required so the hosted /m/<id> video links are usable
# from outside (e.g. by the separate Qwen client app). Configurable in Settings; falls
# back to this env var.
PUBLIC_BASE_URL_ENV = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Hard limits this site enforces on upload, driven by the Qwen3.8-Omni-Flash public-URL
# video input spec: at most 1 hour, strictly under 2 GB, and exactly 720p or 1080p. The
# site only validates — it never re-encodes. Anything that doesn't already meet these
# must be fixed client-side first with client/preconvert.py.
MAX_DURATION_SECONDS = 60 * 60
MAX_UPLOAD_BYTES = 2 * 1024 ** 3
ALLOWED_HEIGHTS = {720, 1080}
