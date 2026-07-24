from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.settings import get_settings
from app.services.sqlite_backup import BackupResult, create_sqlite_backup

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


async def sqlite_backup_loop() -> None:
    settings = get_settings()
    interval = backup_interval_seconds(settings)
    while True:
        try:
            await run_sqlite_backup_once(settings)
        except asyncio.CancelledError:
            raise
        except FileNotFoundError:
            logger.warning("SQLite backup skipped: database file does not exist")
        except Exception:
            logger.exception("SQLite backup failed")
        await asyncio.sleep(interval)
