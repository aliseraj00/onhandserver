"""Create zip archives for Telegram document uploads (50 MB bot limit)."""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable

# Official Bot API limit for sendDocument (python-telegram-bot FileSizeLimit.FILESIZE_UPLOAD)
TELEGRAM_UPLOAD_LIMIT_BYTES = int(50e6)  # 50 MB

# Common important Linux logs (Debian/Ubuntu + RHEL/CentOS + popular services).
# Missing files are skipped at upload time.
IMPORTANT_LINUX_LOGS: tuple[str, ...] = (
    "/var/log/syslog",
    "/var/log/messages",
    "/var/log/auth.log",
    "/var/log/secure",
    "/var/log/kern.log",
    "/var/log/dmesg",
    "/var/log/boot.log",
    "/var/log/cron",
    "/var/log/cron.log",
    "/var/log/daemon.log",
    "/var/log/user.log",
    "/var/log/faillog",
    "/var/log/dpkg.log",
    "/var/log/apt/history.log",
    "/var/log/apt/term.log",
    "/var/log/yum.log",
    "/var/log/dnf.log",
    "/var/log/cloud-init.log",
    "/var/log/cloud-init-output.log",
    "/var/log/ufw.log",
    "/var/log/fail2ban.log",
    "/var/log/nginx/error.log",
    "/var/log/nginx/access.log",
    "/var/log/apache2/error.log",
    "/var/log/apache2/access.log",
    "/var/log/httpd/error_log",
    "/var/log/httpd/access_log",
    "/var/log/mysql/error.log",
    "/var/log/mysqld.log",
    "/var/log/mariadb/mariadb.log",
    "/var/log/postgresql/postgresql.log",
)


class BackupError(Exception):
    pass


def _add_path_to_zip(zf: zipfile.ZipFile, source: Path) -> None:
    if source.is_file():
        # Keep absolute-ish path in zip so logs stay distinguishable
        arcname = str(source).lstrip("/\\").replace("\\", "/")
        zf.write(source, arcname or source.name)
        return

    root_name = source.name or "backup"
    for item in source.rglob("*"):
        if item.is_symlink() or not item.is_file():
            continue
        try:
            relative = item.relative_to(source)
        except ValueError:
            continue
        arcname = str(Path(root_name) / relative)
        zf.write(item, arcname)


def _finalize_zip(tmp_path: Path) -> Path:
    size = tmp_path.stat().st_size
    if size <= 0:
        tmp_path.unlink(missing_ok=True)
        raise BackupError("Zip archive is empty")
    if size > TELEGRAM_UPLOAD_LIMIT_BYTES:
        tmp_path.unlink(missing_ok=True)
        mb = size / (1024 * 1024)
        limit_mb = TELEGRAM_UPLOAD_LIMIT_BYTES / (1024 * 1024)
        raise BackupError(
            f"Zip is {mb:.1f} MB — Telegram bots can only upload up to "
            f"{limit_mb:.0f} MB. Choose a smaller path or fewer log files."
        )
    return tmp_path


def _new_temp_zip(prefix: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(prefix=prefix, suffix=".zip", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()
    return tmp_path


def create_backup_zip(source_path: str | Path) -> Path:
    """Zip *source_path* into a temp file. Raises BackupError if invalid or over limit."""
    source = Path(source_path).expanduser()
    try:
        source = source.resolve(strict=True)
    except FileNotFoundError as exc:
        raise BackupError(f"Path not found: {source_path}") from exc
    except OSError as exc:
        raise BackupError(f"Cannot access path: {exc}") from exc

    if not (source.is_file() or source.is_dir()):
        raise BackupError(f"Not a file or directory: {source}")

    suffix = source.name or "backup"
    tmp_path = _new_temp_zip(f"backup_{suffix}_")

    try:
        with zipfile.ZipFile(
            tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zf:
            _add_path_to_zip(zf, source)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise BackupError(f"Failed to create zip: {exc}") from exc

    return _finalize_zip(tmp_path)


def classify_log_path(path: str | Path) -> tuple[str, str]:
    """Return (status, detail) where status is found|missing|denied|other."""
    p = Path(path)
    if not p.exists():
        return "missing", "not found"
    if not p.is_file():
        return "other", "not a file"
    if not os.access(p, os.R_OK):
        return "denied", "no read permission"
    try:
        size = p.stat().st_size
    except OSError as exc:
        return "denied", str(exc)
    return "found", format_size(size)


def list_important_linux_logs() -> list[tuple[str, str, str]]:
    """Return [(path, status, detail), ...] for IMPORTANT_LINUX_LOGS."""
    return [
        (path, *classify_log_path(path)) for path in IMPORTANT_LINUX_LOGS
    ]


def create_paths_zip(
    paths: Iterable[str | Path],
    *,
    prefix: str = "logs_",
    skip_missing: bool = True,
) -> tuple[Path, list[str]]:
    """Zip readable files from *paths*. Returns (zip_path, included_paths)."""
    included: list[str] = []
    tmp_path = _new_temp_zip(prefix)

    try:
        with zipfile.ZipFile(
            tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zf:
            for raw in paths:
                status, _detail = classify_log_path(raw)
                if status != "found":
                    if skip_missing:
                        continue
                    raise BackupError(f"Cannot include {raw}: {_detail}")
                source = Path(raw)
                try:
                    _add_path_to_zip(zf, source)
                    included.append(str(source))
                except OSError:
                    continue
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise BackupError(f"Failed to create zip: {exc}") from exc

    if not included:
        tmp_path.unlink(missing_ok=True)
        raise BackupError(
            "No readable log files found. Run the bot as a user that can "
            "read /var/log (often root), or check that logs exist on this host."
        )
    return _finalize_zip(tmp_path), included


def create_linux_logs_zip() -> tuple[Path, list[str]]:
    """Zip all existing readable files from IMPORTANT_LINUX_LOGS."""
    return create_paths_zip(IMPORTANT_LINUX_LOGS, prefix="linux_logs_")


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"
