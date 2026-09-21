import os
import secrets
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "app.db"
TMP_DIR = DATA_DIR / "tmp"
DATASETS_DIR = DATA_DIR / "datasets"
THUMBS_DIR = DATA_DIR / "thumbnails"
SECRET_FILE = DATA_DIR / ".app_secret"

for d in (DATA_DIR, TMP_DIR, DATASETS_DIR, THUMBS_DIR):
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
# your reverse proxy exposes). Required so Qwen's servers can fetch the temporary
# /m/<token> media links. Configurable in Settings; falls back to this env var.
PUBLIC_BASE_URL_ENV = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# Hard limits driven by the Qwen3.8-Omni-Flash public-URL video input spec
# (documented: up to 2 GB, up to 2 hours). We target comfortably under the
# cap to leave headroom for container/muxing overhead.
MAX_DURATION_SECONDS = 2 * 60 * 60
TARGET_MAX_BYTES = int(1.85 * 1024 ** 3)
MIN_HEIGHT = 720

# Uploads must already be pre-converted client-side (see client/preconvert.py) toward
# the same TARGET_MAX_BYTES target before they reach this server — the server no longer
# accepts arbitrarily large raw uploads. Small grace factor above the target to avoid
# rejecting legitimate pre-converted files that land slightly over due to bitrate
# control / container overhead.
MAX_UPLOAD_BYTES = int(TARGET_MAX_BYTES * 1.05)

QWEN_BASE_URLS = {
    "international": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "china": "https://dashscope.aliyuncs.com/compatible-mode/v1",
}
DEFAULT_QWEN_REGION = "international"
QWEN_MODEL = "qwen3.8-omni-flash"

# How long a direct-fetch media link stays valid for Qwen to retrieve the file.
MEDIA_TOKEN_TTL_SECONDS = 4 * 60 * 60
