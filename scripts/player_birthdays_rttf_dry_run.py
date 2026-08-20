from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select

from app.database.models import Match, PlayerBirthday
from app.database.session import SessionFactory
from app.services.player_birthdays import PlayerBirthdayRow, parse_birth_date, parse_birth_year, short_player_name, upsert_player_birthdays

RTTF_BASE_URL = "https://rttf.ru/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
TAG_RE = re.compile(r"<[^>]+>")
SEARCH_ROW_RE = re.compile(r"<tr>\s*<td>.*?</td>\s*<td><a href=\"players/(\d+)\">(.*?)</a></td>", re.DOTALL | re.IGNORECASE)
H1_RE = re.compile(r"<h1>(.*?)</h1>", re.DOTALL | re.IGNORECASE)
BIRTH_RE = re.compile(r"Дата рождения:\s*<strong>(.*?)</strong>", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class RttfCandidate:
    player_id: int
    list_name: str
    profile_name: str = ""
    birth_date: str = ""
    birth_year: int | None = None
    has_liga_pro: bool = False
    score: int = 0
    confidence: str = "not_found"
    status: str = "not_found"
    url: str = ""
    reason: str = ""


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(TAG_RE.sub(" ", value))).strip()


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-zа-яё ]+", " ", value.lower().replace("ё", "е")).strip()


def split_name(value: str) -> tuple[str, list[str]]:
    cleaned = normalize_name(value)
    parts = [part for part in cleaned.split() if part]
    if not parts:
        return "", []
    surname = parts[0]
    initials = [part[0] for part in parts[1:] if part]
    return surname, initials


def score_match(source_name: str, profile_name: str, *, has_liga_pro: bool, has_birth: bool) -> tuple[int, str]:
    source_surname, source_initials = split_name(source_name)
    profile_surname, profile_initials = split_name(profile_name)
    if not source_surname or not profile_surname:
        return 0, "нет фамилии"
    if source_surname != profile_surname:
        return 0, "фамилия не совпала"

    score = 55
    reason = ["фамилия совпала"]
    if source_initials:
        matched = sum(1 for left, right in zip(source_initials, profile_initials) if left == right)
        if matched == len(source_initials):
            score += 25
            reason.append("инициалы совпали")
        elif matched:
            score += 10
            reason.append("часть инициалов совпала")
        else:
            score -= 20
            reason.append("инициалы не совпали")
    if has_liga_pro:
        score += 15
        reason.append("есть Лига ПРО")
    if has_birth:
        score += 5
        reason.append("есть дата/год рождения")
    return max(0, min(score, 100)), ", ".join(reason)


def confidence_for_score(score: int, *, has_birth_date: bool, has_liga_pro: bool) -> tuple[str, str]:
    if score >= 95 and has_birth_date and has_liga_pro:
        return "high", "verified"
    if score >= 80:
        return "medium", "needs_review"
    if score > 0:
        return "low", "needs_review"
    return "not_found", "not_found"


def fetch_html(url: str, *, timeout: int = 20) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def search_rttf_candidates(player_name: str, *, max_candidates: int = 5) -> list[RttfCandidate]:
    surname, _ = split_name(player_name)
    if not surname:
        return []
    query = urlencode({"name": surname, "like": "1", "sex_m": "1", "sex_f": "1", "hand_r": "1", "hand_l": "1"})
    html = fetch_html(urljoin(RTTF_BASE_URL, f"players/?{query}"))
    candidates = []
    seen = set()
    for player_id_raw, list_name_raw in SEARCH_ROW_RE.findall(html):
        player_id = int(player_id_raw)
        if player_id in seen:
            continue
        seen.add(player_id)
        candidates.append(RttfCandidate(player_id=player_id, list_name=clean_text(list_name_raw), url=urljoin(RTTF_BASE_URL, f"players/{player_id}")))
        if len(candidates) >= max_candidates:
            break
    return candidates


def enrich_candidate(player_name: str, candidate: RttfCandidate) -> RttfCandidate:
    html = fetch_html(candidate.url)
    profile_name_match = H1_RE.search(html)
    birth_match = BIRTH_RE.search(html)
    profile_name = clean_text(profile_name_match.group(1)) if profile_name_match else candidate.list_name
    birth_text = clean_text(birth_match.group(1)) if birth_match else ""
    birth_date = parse_birth_date(birth_text)
    birth_year = parse_birth_year(birth_text)
    has_liga_pro = "Лига ПРО" in html or "Лига про" in html
    score, reason = score_match(player_name, profile_name, has_liga_pro=has_liga_pro, has_birth=bool(birth_date or birth_year))
    confidence, status = confidence_for_score(score, has_birth_date=birth_date is not None, has_liga_pro=has_liga_pro)
    return RttfCandidate(
        player_id=candidate.player_id,
        list_name=candidate.list_name,
        profile_name=profile_name,
        birth_date=birth_date.isoformat() if birth_date else "",
        birth_year=birth_year,
        has_liga_pro=has_liga_pro,
        score=score,
        confidence=confidence,
        status=status,
        url=candidate.url,
        reason=reason,
    )


def find_best_rttf_match(player_name: str, *, max_candidates: int = 5, delay: float = 0.2) -> RttfCandidate:
    candidates = search_rttf_candidates(player_name, max_candidates=max_candidates)
    enriched = []
    for candidate in candidates:
        time.sleep(delay)
        try:
            enriched.append(enrich_candidate(player_name, candidate))
        except Exception as exc:
            enriched.append(RttfCandidate(player_id=candidate.player_id, list_name=candidate.list_name, url=candidate.url, reason=f"profile error: {exc}"))
    if not enriched:
        return RttfCandidate(player_id=0, list_name="", reason="кандидатов нет")
    return sorted(enriched, key=lambda item: item.score, reverse=True)[0]


async def collect_unique_players(*, limit: int, missing_only: bool) -> list[str]:
    async with SessionFactory() as session:
        matches = list((await session.scalars(select(Match))).all())
        existing_names = set()
        if missing_only:
            existing = list((await session.scalars(select(PlayerBirthday.full_name))).all())
            existing_names = {normalize_name(name) for name in existing if name}
    names = []
    seen = set()
    for match in matches:
        for name in (match.player_1, match.player_2):
            key = normalize_name(name)
            if not key or key in seen or key in existing_names:
                continue
            seen.add(key)
            names.append(name)
            if limit and len(names) >= limit:
                return names
    return names


def write_report(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["source_name", "short_name", "rttf_name", "birth_date", "birth_year", "confidence", "score", "status", "has_liga_pro", "url", "reason"]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


async def main() -> None:
    parser = argparse.ArgumentParser(description="RTTF birthday lookup for Algobet players. Dry-run by default; --apply writes only high-confidence matches.")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--max-candidates", type=int, default=5)
    parser.add_argument("--delay", type=float, default=0.3)
    parser.add_argument("--include-existing", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--apply", action="store_true", help="Write high-confidence verified matches to player_birthdays")
    args = parser.parse_args()

    players = await collect_unique_players(limit=args.limit, missing_only=not args.include_existing)
    rows = []
    apply_rows: list[PlayerBirthdayRow] = []
    counts = {"high": 0, "medium": 0, "low": 0, "not_found": 0}
    for index, player_name in enumerate(players, start=1):
        try:
            best = find_best_rttf_match(player_name, max_candidates=args.max_candidates, delay=args.delay)
        except Exception as exc:
            best = RttfCandidate(player_id=0, list_name="", reason=f"search error: {exc}")
        counts[best.confidence] = counts.get(best.confidence, 0) + 1
        row = {
            "source_name": player_name,
            "short_name": short_player_name(player_name),
            "rttf_name": best.profile_name or best.list_name,
            "birth_date": best.birth_date,
            "birth_year": best.birth_year or "",
            "confidence": best.confidence,
            "score": best.score,
            "status": best.status,
            "has_liga_pro": "yes" if best.has_liga_pro else "no",
            "url": best.url,
            "reason": best.reason,
        }
        rows.append(row)
        if args.apply and best.confidence == "high" and best.status == "verified" and best.birth_date:
            apply_rows.append(PlayerBirthdayRow(
                full_name=player_name,
                birth_date=parse_birth_date(best.birth_date),
                source_url=best.url,
                external_player_id=None,
                birth_year=best.birth_year,
                source="rttf",
                source_name="RTTF",
                confidence=best.confidence,
                confidence_score=best.score,
                verification_status=best.status,
                notes=f"RTTF: {best.profile_name}; {best.reason}",
            ))
        mode = "APPLY" if args.apply else "DRY"
        print(f"{index:03d}/{len(players)} [{mode}] {player_name} -> {row['rttf_name'] or '-'} | {row['birth_date'] or row['birth_year'] or '-'} | {best.confidence} {best.score} | {best.reason}")
        time.sleep(args.delay)

    summary = None
    if args.apply and apply_rows:
        async with SessionFactory() as session:
            summary = await upsert_player_birthdays(session, apply_rows)

    suffix = "apply" if args.apply else "dry_run"
    out = Path(args.out) if args.out else Path("data") / f"player_birthdays_rttf_{suffix}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.csv"
    write_report(out, rows)
    print("\nSummary")
    print(f"players={len(players)} high={counts.get('high', 0)} medium={counts.get('medium', 0)} low={counts.get('low', 0)} not_found={counts.get('not_found', 0)}")
    if summary is not None:
        print(f"applied parsed={summary.parsed} inserted={summary.inserted} updated={summary.updated} skipped={summary.skipped}")
    elif args.apply:
        print("applied parsed=0 inserted=0 updated=0 skipped=0")
    print(f"report={out}")


if __name__ == "__main__":
    asyncio.run(main())
