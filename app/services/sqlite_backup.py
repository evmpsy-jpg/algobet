from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


BACKUP_PREFIX = "algobet-"
BACKUP_SUFFIX = ".db"
DEFAULT_BACKUP_KEEP = 10


@dataclass(frozen=True)
class BackupInfo:
    path: Path
    size_bytes: int
    created_at: datetime


@dataclass(frozen=True)
class BackupResult:
    created: BackupInfo
    deleted: tuple[Path, ...]


@dataclass(frozen=True)
class BackupVerification:
    path: Path
    ok: bool
    message: str
    table_count: int = 0


def sqlite_database_path(database_url: str) -> Path | None:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        return None
    raw_path = database_url[len(prefix):]
    if raw_path in {":memory:", ""}:
        return None
    return Path(raw_path)


def backup_directory(data_dir: Path) -> Path:
    return data_dir / "backups"


def list_sqlite_backups(data_dir: Path) -> list[BackupInfo]:
    directory = backup_directory(data_dir)
    if not directory.exists():
        return []

    backups: list[BackupInfo] = []
    for path in directory.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"):
        if not path.is_file():
            continue
        stat = path.stat()
        backups.append(
            BackupInfo(
                path=path,
                size_bytes=stat.st_size,
                created_at=datetime.fromtimestamp(stat.st_mtime),
            )
        )
    return sorted(backups, key=lambda item: item.created_at, reverse=True)


def latest_sqlite_backup(data_dir: Path) -> BackupInfo | None:
    backups = list_sqlite_backups(data_dir)
    return backups[0] if backups else None


def create_sqlite_backup(database_url: str, data_dir: Path, *, keep: int = DEFAULT_BACKUP_KEEP) -> BackupResult:
    db_path = sqlite_database_path(database_url)
    if db_path is None:
        raise ValueError("SQLite backup is available only for a file-based SQLite database.")
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    directory = backup_directory(data_dir)
    directory.mkdir(parents=True, exist_ok=True)

    created_at = datetime.now()
    destination = directory / f"{BACKUP_PREFIX}{created_at:%Y%m%d-%H%M%S}{BACKUP_SUFFIX}"

    with sqlite3.connect(db_path, timeout=30) as source, sqlite3.connect(destination, timeout=30) as target:
        source.execute("PRAGMA busy_timeout = 30000")
        target.execute("PRAGMA busy_timeout = 30000")
        source.backup(target, pages=1000, sleep=0.05)

    deleted: list[Path] = []
    keep = max(1, keep)
    for old_backup in list_sqlite_backups(data_dir)[keep:]:
        old_backup.path.unlink(missing_ok=True)
        deleted.append(old_backup.path)

    return BackupResult(
        created=BackupInfo(
            path=destination,
            size_bytes=destination.stat().st_size,
            created_at=created_at,
        ),
        deleted=tuple(deleted),
    )


def verify_sqlite_backup(path: Path) -> BackupVerification:
    if not path.exists():
        return BackupVerification(path=path, ok=False, message="Файл backup не найден.")
    if not path.is_file():
        return BackupVerification(path=path, ok=False, message="Путь backup не является файлом.")

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            table_count = int(
                connection.execute("select count(*) from sqlite_master where type = 'table'").fetchone()[0]
            )
    except sqlite3.Error as exc:
        return BackupVerification(path=path, ok=False, message=f"SQLite ошибка: {exc}")

    result = str(integrity[0] if integrity else "unknown")
    if result.lower() != "ok":
        return BackupVerification(path=path, ok=False, message=f"Integrity check: {result}", table_count=table_count)
    return BackupVerification(path=path, ok=True, message="Backup читается, integrity_check: ok.", table_count=table_count)
