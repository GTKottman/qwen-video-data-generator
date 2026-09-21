from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from app.config import DATA_DIR


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _bin_check(name: str, args: list[str]) -> CheckResult:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return CheckResult(name, False, "found but exited non-zero")
        first_line = (proc.stdout or proc.stderr).splitlines()[0] if (proc.stdout or proc.stderr) else ""
        return CheckResult(name, True, first_line)
    except FileNotFoundError:
        return CheckResult(name, False, "not found on PATH")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, False, str(exc))


def check_ffmpeg() -> CheckResult:
    return _bin_check("ffmpeg", ["ffmpeg", "-version"])


def check_ffprobe() -> CheckResult:
    return _bin_check("ffprobe", ["ffprobe", "-version"])


def check_disk_space(min_gb: float = 5.0) -> CheckResult:
    try:
        free_gb = shutil.disk_usage(DATA_DIR).free / 1e9
    except OSError as exc:
        return CheckResult("disk space", False, str(exc))
    ok = free_gb >= min_gb
    return CheckResult("disk space", ok, f"{free_gb:.1f} GB free at {DATA_DIR}")


def run_all() -> list[CheckResult]:
    return [check_ffmpeg(), check_ffprobe(), check_disk_space()]
