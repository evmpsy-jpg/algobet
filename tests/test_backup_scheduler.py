from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from app.services.backup_scheduler import backup_interval_seconds, run_sqlite_backup_once


def test_backup_interval_seconds_uses_at_least_one_hour() -> None:
    assert backup_interval_seconds(SimpleNamespace(sqlite_backup_interval_hours=0)) == 3600
    assert backup_interval_seconds(SimpleNamespace(sqlite_backup_interval_hours=2)) == 7200


@pytest.mark.asyncio
async def test_run_sqlite_backup_once_creates_backup(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "algobet.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("create table items (id integer primary key)")

    result = await run_sqlite_backup_once(
        SimpleNamespace(
            sqlite_backup_enabled=True,
            sqlite_backup_keep=3,
            database_url=f"sqlite+aiosqlite:///{db_path}",
            data_dir=data_dir,
        )
    )

    assert result is not None
    assert result.created.path.exists()


@pytest.mark.asyncio
async def test_run_sqlite_backup_once_can_be_disabled(tmp_path) -> None:
    result = await run_sqlite_backup_once(
        SimpleNamespace(
            sqlite_backup_enabled=False,
            sqlite_backup_keep=3,
            database_url="sqlite+aiosqlite:///data/algobet.db",
            data_dir=tmp_path,
        )
    )

    assert result is None
