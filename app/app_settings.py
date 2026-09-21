from __future__ import annotations

from app.config import DEFAULT_QWEN_REGION, PUBLIC_BASE_URL_ENV, QWEN_BASE_URLS
from app.crypto import decrypt_str, encrypt_str
from app.db import get_setting, set_setting


def get_qwen_api_key() -> str | None:
    enc = get_setting("qwen_api_key_enc")
    if not enc:
        return None
    try:
        return decrypt_str(enc)
    except Exception:
        return None


def set_qwen_api_key(raw_key: str) -> None:
    set_setting("qwen_api_key_enc", encrypt_str(raw_key))


def get_qwen_region() -> str:
    return get_setting("qwen_region", DEFAULT_QWEN_REGION) or DEFAULT_QWEN_REGION


def set_qwen_region(region: str) -> None:
    if region not in QWEN_BASE_URLS:
        raise ValueError(f"unknown region {region!r}")
    set_setting("qwen_region", region)


def get_qwen_base_url() -> str:
    return QWEN_BASE_URLS[get_qwen_region()]


def get_public_base_url() -> str:
    return get_setting("public_base_url", "") or PUBLIC_BASE_URL_ENV


def set_public_base_url(url: str) -> None:
    set_setting("public_base_url", url.rstrip("/"))


def is_setup_complete() -> bool:
    return get_setting("setup_complete") == "1"


def mark_setup_complete() -> None:
    set_setting("setup_complete", "1")
