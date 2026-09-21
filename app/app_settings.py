from __future__ import annotations

from app.config import PUBLIC_BASE_URL_ENV
from app.db import get_setting, set_setting


def get_public_base_url() -> str:
    return get_setting("public_base_url", "") or PUBLIC_BASE_URL_ENV


def set_public_base_url(url: str) -> None:
    set_setting("public_base_url", url.rstrip("/"))


def is_setup_complete() -> bool:
    return get_setting("setup_complete") == "1"


def mark_setup_complete() -> None:
    set_setting("setup_complete", "1")
