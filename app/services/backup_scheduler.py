from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.settings import get_settings
from app.services.sqlite_backup import BackupResult, create_sqlite_backup
from app.services.admin_notifications import format_backup_error_admin_text, notify_admins

logger = logging.getLogger(__name__)


def backup_interval_seconds(settings: Any) -> int:
    return max(1, int(settings.sqlite_backup_interval_hours)) * 60 * 60


async def run_sqlite_backup_once(settings: Any | None = None) -> BackupResult | None:
    settings = settings or get_settings()
    if not settings.sqlite_backup_enabled:
        logger.info("SQLite backup is disabled")
        return None

    try:
        result = await asyncio.to_thread(
            create_sqlite_backup,
            settings.database_url,
            settings.data_dir,
            keep=settings.sqlite_backup_keep,
        )
    except ValueError:
        logger.info("SQLite backup skipped: database is not file-based SQLite")
        return None

    logger.info(
        "SQLite backup created: path=%s size=%s deleted=%s",
        result.created.path,
        result.created.size_bytes,
        len(result.deleted),
    )
    return result


async def sqlite_backup_loop(bot: Any | None = None) -> None:
    settings = get_settings()
    interval = backup_interval_seconds(settings)
    while True:
        try:
            await run_sqlite_backup_once(settings)
        except asyncio.CancelledError:
            raise
        except FileNotFoundError as exc:
            logger.warning("SQLite backup skipped: database file does not exist")
            if bot is not None:
                await notify_admins(bot, settings.admin_ids, format_backup_error_admin_text(exc))
        except Exception as exc:
            logger.exception("SQLite backup failed")
            if bot is not None:
                await notify_admins(bot, settings.admin_ids, format_backup_error_admin_text(exc))
        await asyncio.sleep(interval)
