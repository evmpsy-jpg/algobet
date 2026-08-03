from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.settings import get_settings
from app.database.models import Base

settings = get_settings()
engine = create_async_engine(settings.database_url, echo=False)
SessionFactory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def _ensure_user_access_columns(connection) -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    rows = await connection.execute(text("PRAGMA table_info(user_accesses)"))
    existing = {row[1] for row in rows.fetchall()}
    columns = {
        "signals_remaining": "INTEGER",
        "plan_id": "VARCHAR(50)",
        "plan_group": "VARCHAR(30)",
        "includes_vip": "BOOLEAN DEFAULT 0",
        "includes_all_signals": "BOOLEAN DEFAULT 0",
        "includes_analytics": "BOOLEAN DEFAULT 0",
    }
    for name, definition in columns.items():
        if name not in existing:
            await connection.execute(text(f"ALTER TABLE user_accesses ADD COLUMN {name} {definition}"))


async def _ensure_web_admin_user_columns(connection) -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    rows = await connection.execute(text("PRAGMA table_info(web_admin_users)"))
    existing = {row[1] for row in rows.fetchall()}
    columns = {
        "is_super_admin": "BOOLEAN DEFAULT 0",
    }
    for name, definition in columns.items():
        if existing and name not in existing:
            await connection.execute(text(f"ALTER TABLE web_admin_users ADD COLUMN {name} {definition}"))


async def _ensure_match_analysis_request_columns(connection) -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    rows = await connection.execute(text("PRAGMA table_info(match_analysis_requests)"))
    existing = {row[1] for row in rows.fetchall()}
    columns = {
        "match_id": "INTEGER",
        "match_start_at": "DATETIME",
        "match_title": "VARCHAR(500)",
    }
    for name, definition in columns.items():
        if existing and name not in existing:
            await connection.execute(text(f"ALTER TABLE match_analysis_requests ADD COLUMN {name} {definition}"))


async def init_db() -> None:
    settings.data_dir
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await _ensure_user_access_columns(connection)
        await _ensure_web_admin_user_columns(connection)
        await _ensure_match_analysis_request_columns(connection)
