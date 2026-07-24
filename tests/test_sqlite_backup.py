from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from app.services.sqlite_backup import (
    backup_directory,
    create_sqlite_backup,
    latest_sqlite_backup,
    list_sqlite_backups,
    sqlite_database_path,
)


def test_create_sqlite_backup_copies_database_and_tracks_latest(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "algobet.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("create table users (id integer primary key, name text)")
        connection.execute("insert into users (name) values (?)", ("tester",))

    result = create_sqlite_backup(f"sqlite+aiosqlite:///{db_path}", data_dir)

    assert result.created.path.exists()
    assert result.created.size_bytes > 0
    assert result.deleted == ()
    with sqlite3.connect(result.created.path) as backup:
        row = backup.execute("select name from users").fetchone()
    assert row == ("tester",)
    assert latest_sqlite_backup(data_dir).path == result.created.path


def test_create_sqlite_backup_keeps_recent_files(tmp_path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "algobet.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("create table items (id integer primary key)")

    directory = backup_directory(data_dir)
    directory.mkdir()
    old_paths = []
    for index in range(3):
        path = directory / f"algobet-20260723-12000{index}.db"
        path.write_bytes(b"old")
        timestamp = (datetime.now() - timedelta(minutes=10 - index)).timestamp()
        path.touch()
        path.stat()
        os.utime(path, (timestamp, timestamp))
        old_paths.append(path)

    result = create_sqlite_backup(f"sqlite+aiosqlite:///{db_path}", data_dir, keep=2)

    remaining = [item.path for item in list_sqlite_backups(data_dir)]
    assert result.created.path in remaining
    assert len(remaining) == 2
    assert old_paths[0] in result.deleted
    assert not old_paths[0].exists()


def test_sqlite_backup_rejects_external_database(tmp_path) -> None:
    assert sqlite_database_path("postgresql+asyncpg://user:pass@db/algobet") is None
    with pytest.raises(ValueError):
        create_sqlite_backup("postgresql+asyncpg://user:pass@db/algobet", tmp_path)
