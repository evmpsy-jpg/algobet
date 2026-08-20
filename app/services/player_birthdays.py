from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from io import StringIO
from datetime import date, datetime
from html import unescape
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PlayerBirthday

SPORT_LIGA_PLAYERS_URL = "https://www.sport-liga.pro/ru/table-tennis/participants/players"
_DATE_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b|\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_PLAYER_ID_RE = re.compile(r"/(?:participants/)?players/(\d+)(?:\D|$)")
_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)
_ANCHOR_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class PlayerBirthdayRow:
    full_name: str
    birth_date: date | None
    source_url: str | None = None
    external_player_id: int | None = None


@dataclass(frozen=True)
class PlayerBirthdaySyncSummary:
    parsed: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


def parse_birth_date(value: str) -> date | None:
    match = _DATE_RE.search(value or "")
    if not match:
        return None
    if match.group(1):
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    else:
        year, month, day = int(match.group(4)), int(match.group(5)), int(match.group(6))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _clean_html_text(value: str) -> str:
    cleaned = _TAG_RE.sub(" ", value)
    cleaned = unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_player_id(url: str | None) -> int | None:
    if not url:
        return None
    match = _PLAYER_ID_RE.search(url)
    return int(match.group(1)) if match else None


def _looks_like_name(value: str) -> bool:
    if not value or _DATE_RE.search(value):
        return False
    lowered = value.lower()
    if lowered in {"игрок", "дата рождения", "рейтинг", "страна", "город", "player", "birth date"}:
        return False
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", value)
    return len(letters) >= 5


def parse_player_birthdays_html(html: str, *, base_url: str = SPORT_LIGA_PLAYERS_URL) -> list[PlayerBirthdayRow]:
    if "servicepipe" in html.lower() or "js-challenge-loader" in html.lower():
        raise ValueError("Sport Liga Pro вернул защитную страницу ServicePipe вместо списка игроков")

    rows: list[PlayerBirthdayRow] = []
    seen: set[tuple[int | None, str]] = set()
    for row_html in _ROW_RE.findall(html):
        birth_date = parse_birth_date(_clean_html_text(row_html))
        if birth_date is None:
            continue
        source_url = None
        name = None
        for href, anchor_html in _ANCHOR_RE.findall(row_html):
            anchor_text = _clean_html_text(anchor_html)
            if _looks_like_name(anchor_text):
                source_url = urljoin(base_url, href)
                name = anchor_text
                break
        if name is None:
            cells = [part.strip() for part in re.split(r"\s{2,}|\|", _clean_html_text(row_html)) if part.strip()]
            candidates = [cell for cell in cells if _looks_like_name(cell)]
            if candidates:
                name = max(candidates, key=len)
        if name is None:
            continue
        if source_url is None:
            hrefs = _HREF_RE.findall(row_html)
            source_url = urljoin(base_url, hrefs[0]) if hrefs else None
        external_id = _extract_player_id(source_url)
        key = (external_id, name.lower())
        if key in seen:
            continue
        seen.add(key)
        rows.append(PlayerBirthdayRow(full_name=name, birth_date=birth_date, source_url=source_url, external_player_id=external_id))
    return rows



def parse_player_birthdays_csv(text: str) -> list[PlayerBirthdayRow]:
    sample = text.strip()
    if not sample:
        return []
    delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(StringIO(sample), delimiter=delimiter)
    rows: list[PlayerBirthdayRow] = []
    aliases = {
        "full_name": {"full_name", "name", "player", "игрок", "фио", "имя"},
        "birth_date": {"birth_date", "birthday", "date_of_birth", "дата рождения", "др"},
        "source_url": {"source_url", "url", "link", "ссылка", "источник"},
        "external_player_id": {"external_player_id", "player_id", "id", "id игрока"},
    }

    def value(row: dict[str, str], key: str) -> str:
        for field, raw in row.items():
            normalized = (field or "").strip().lower()
            if normalized in aliases[key]:
                return (raw or "").strip()
        return ""

    for raw_row in reader:
        name = value(raw_row, "full_name")
        birth_date = parse_birth_date(value(raw_row, "birth_date"))
        source_url = value(raw_row, "source_url") or None
        external_raw = value(raw_row, "external_player_id")
        external_id = int(external_raw) if external_raw.isdigit() else _extract_player_id(source_url)
        if name and birth_date:
            rows.append(PlayerBirthdayRow(name, birth_date, source_url, external_id))
    return rows


def fetch_player_birthdays_from_sport_liga(url: str = SPORT_LIGA_PLAYERS_URL, *, timeout: int = 30) -> list[PlayerBirthdayRow]:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
        content_type = response.headers.get_content_charset() or "utf-8"
    html = raw.decode(content_type, errors="replace")
    return parse_player_birthdays_html(html, base_url=url)


def short_player_name(full_name: str) -> str:
    parts = [part for part in re.split(r"\s+", full_name.strip()) if part]
    if len(parts) < 2:
        return full_name.strip()
    surname = parts[0]
    initials = " ".join(f"{part[0]}." for part in parts[1:] if part)
    return f"{surname} {initials}".strip()


async def upsert_player_birthdays(session: AsyncSession, rows: list[PlayerBirthdayRow]) -> PlayerBirthdaySyncSummary:
    inserted = 0
    updated = 0
    skipped = 0
    now = datetime.utcnow()
    for row in rows:
        full_name = row.full_name.strip()
        if not full_name or row.birth_date is None:
            skipped += 1
            continue
        conditions = []
        if row.external_player_id is not None:
            conditions.append(PlayerBirthday.external_player_id == row.external_player_id)
        if row.source_url:
            conditions.append(PlayerBirthday.source_url == row.source_url)
        conditions.append(PlayerBirthday.full_name == full_name)
        existing = await session.scalar(select(PlayerBirthday).where(or_(*conditions)).limit(1))
        if existing is None:
            session.add(PlayerBirthday(
                external_player_id=row.external_player_id,
                full_name=full_name,
                short_name=short_player_name(full_name),
                birth_date=row.birth_date,
                source_url=row.source_url,
                last_synced_at=now,
            ))
            inserted += 1
        else:
            changed = False
            for attr, value in {
                "external_player_id": row.external_player_id,
                "full_name": full_name,
                "short_name": short_player_name(full_name),
                "birth_date": row.birth_date,
                "source_url": row.source_url,
            }.items():
                if value is not None and getattr(existing, attr) != value:
                    setattr(existing, attr, value)
                    changed = True
            existing.last_synced_at = now
            if changed:
                existing.updated_at = now
            updated += 1
    await session.commit()
    return PlayerBirthdaySyncSummary(parsed=len(rows), inserted=inserted, updated=updated, skipped=skipped)
