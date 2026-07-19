from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_name: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    uploaded_by_telegram_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(30), default="processing")
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    parsed_matches: Mapped[int] = mapped_column(Integer, default=0)
    inserted_matches: Mapped[int] = mapped_column(Integer, default=0)
    updated_matches: Mapped[int] = mapped_column(Integer, default=0)
    missing_matches: Mapped[int] = mapped_column(Integer, default=0)
    error_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)

    snapshots: Mapped[list["MatchSnapshot"]] = relationship(back_populates="import_batch")


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(primary_key=True)
    external_match_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    external_tournament_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_url: Mapped[str] = mapped_column(String(1000))
    tournament_date: Mapped[str] = mapped_column(String(20))
    match_time: Mapped[str] = mapped_column(String(10))
    match_start_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    player_1: Mapped[str] = mapped_column(String(255))
    player_2: Mapped[str] = mapped_column(String(255))
    player_1_rating: Mapped[int | None] = mapped_column(Integer)
    player_2_rating: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[str | None] = mapped_column(String(50))
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    current_import_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    is_present_in_latest_import: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    signal: Mapped["ScheduledSignal | None"] = relationship(back_populates="match", uselist=False)


class MatchSnapshot(Base):
    __tablename__ = "match_snapshots"
    __table_args__ = (UniqueConstraint("import_batch_id", "external_match_id", name="uq_snapshot_import_match"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    external_match_id: Mapped[int] = mapped_column(BigInteger, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    import_batch: Mapped[ImportBatch] = relationship(back_populates="snapshots")


class ScheduledSignal(Base):
    __tablename__ = "scheduled_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="scheduled", index=True)
    send_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    signal_type: Mapped[str | None] = mapped_column(String(100))
    signal_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    message_text: Mapped[str | None] = mapped_column(Text)
    source_import_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    cancel_reason: Mapped[str | None] = mapped_column(String(255))
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    recalculated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)

    match: Mapped[Match] = relationship(back_populates="signal")
