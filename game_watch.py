#!/usr/bin/env python3
"""
Daily board-game watcher for tarsasjatekok.com product pages.

What it does:
- Loads a YAML config with one or more game pages.
- Fetches each page once per run.
- Extracts visible text, tries to identify offers, prices, and availability cues.
- Keeps a small JSON state file so the email can mention changes since the last run.
- Records a rolling best-price history and embeds a chart in the HTML email.
- Sends a report-style email via SMTP when at least one tracked price changed or no
  email was sent in the last 7 days.

Install:
    uv sync

Run:
    uv run python game_watch.py --config config.yaml

Dry run (print the report, do not send email):
    uv run python game_watch.py --config config.yaml --dry-run

Schedule daily with cron / Task Scheduler / systemd timer.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import logging
import os
import re
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import requests
import yaml
from bs4 import BeautifulSoup
from bs4.element import Tag
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


UA = "Mozilla/5.0 (compatible; GameWatch/1.0; +https://example.com)"
DEFAULT_TIMEOUT = 25
DEFAULT_PRICE_HISTORY_DAYS = 30
DEFAULT_EMAIL_HEARTBEAT_DAYS = 7
STATE_META_KEY = "_meta"


# Grouped thousands (e.g. "12 345"); avoids loose digit runs like \d[\d\s]{2,}.
_PRICE_NUM = r"\d{1,3}(?:[\s]\d{3})*"
PRICE_RE = re.compile(rf"(?P<price>{_PRICE_NUM})\s*Ft", re.IGNORECASE)
STORE_OFFER_RE = re.compile(
    r"(?P<label>.{0,150}?)"
    r"(?P<availability>raktáron|rendelhető|értesítés kérhető)"
    r"[ \t]+"
    r"(?P<condition>új|újszerű|használt)"
    r"[ \t]*"
    rf"(?P<price>{_PRICE_NUM})"
    r"\s*Ft",
    re.IGNORECASE,
)

# Lower rank = preferred when picking the displayed "best" offer (in-stock vs orderable).
# "értesítés kérhető" is listed for completeness; filtered offers exclude it before best_offer.
AVAILABILITY_RANK = {
    "raktáron": 0,
    "rendelhető": 1,
    "értesítés kérhető": 2,
}

NEW_CONDITIONS = {"új"}
AVAILABLE_STATUSES = {"raktáron", "rendelhető"}

SECTION_LABELS = {
    "highlighted_partner": "Kiemelt",
    "partner": "Partnerek",
}
NO_MATCHING_OFFERS_MSG = (
    "No Hungarian new in-stock offers in target sections."
)


@dataclass
class Offer:
    label: str
    availability: str  # "raktáron" | "rendelhető" | "értesítés kérhető" | ""
    condition: str  # "új" | "újszerű" | "használt" | ""
    price_huf: int | None
    raw: str
    # Structured fields (product-list sections); empty / False when from regex fallback.
    shop: str = ""
    title: str = ""
    language: str = ""  # e.g. "magyar" from <small>; empty if absent
    section: str = ""  # "highlighted_partner" | "partner" from data-listtype
    in_stock: bool = False
    is_new: bool = False
    url: str = ""  # shop offer link from the row anchor


@dataclass
class GameResult:
    name: str
    url: str
    page_title: str
    fetched_at: str
    best_price_huf: int | None
    best_availability: str | None
    best_condition: str | None
    offer_count: int
    new_available_count: int
    offers: list[Offer]
    matching_offers: list[Offer]
    notes: list[str]
    error: str | None = None


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError("Config must be a YAML mapping.")
    if "games" not in cfg or not isinstance(cfg["games"], list):
        raise ValueError("Config must contain a 'games' list.")
    if "smtp" not in cfg or not isinstance(cfg["smtp"], dict):
        raise ValueError("Config must contain an 'smtp' mapping.")
    for i, game in enumerate(cfg["games"]):
        if not isinstance(game, dict):
            raise ValueError(f"games[{i}] must be a mapping with at least a 'url' key.")
        if "url" not in game:
            raise ValueError(f"games[{i}] is missing required 'url' key.")
        if not isinstance(game["url"], str) or not game["url"].strip():
            raise ValueError(f"games[{i}] 'url' must be a non-empty string.")
    return cfg


def load_dotenv_for_config(config_path: Path) -> None:
    """Try .env next to the config file, then in the current working directory."""
    for candidate in (config_path.resolve().parent / ".env", Path(".env")):
        if load_dotenv(candidate, override=False):
            return


def apply_smtp_env_overrides(cfg: dict[str, Any]) -> None:
    """Apply SMTP_* environment variables to cfg['smtp']; validate login credentials."""
    smtp = cfg["smtp"]

    if host := os.environ.get("SMTP_HOST"):
        smtp["host"] = host
    if username := os.environ.get("SMTP_USERNAME"):
        smtp["username"] = username
    if password := os.environ.get("SMTP_PASSWORD"):
        smtp["password"] = password
    if from_addr := os.environ.get("SMTP_FROM"):
        smtp["from"] = from_addr
    if to_addrs := os.environ.get("SMTP_TO"):
        smtp["to"] = [addr.strip() for addr in to_addrs.split(",") if addr.strip()]

    if not smtp.get("to"):
        raise ValueError(
            "No email recipients configured. Set the SMTP_TO environment variable "
            "(comma-separated list); do not store recipient addresses in config YAML."
        )

    username = smtp.get("username")
    if username and not smtp.get("password"):
        raise ValueError(
            "SMTP login is configured (smtp.username is set) but no password was "
            "provided. Set the SMTP_PASSWORD environment variable or omit smtp.username "
            "for servers that do not require authentication."
        )


def fetch_page(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    resp = requests.get(
        url,
        headers={"User-Agent": UA, "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def text_from_html(html_text: str) -> tuple[str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    title = ""
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(" ", strip=True)
    # Prefer h1 if present
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)
    visible = soup.get_text(" ", strip=True)
    visible = collapse_ws(visible)
    return title, visible


def parse_offers(visible_text: str) -> list[Offer]:
    offers: list[Offer] = []
    # First pass: store lines "label availability condition price Ft" (zero space before price OK).
    for m in STORE_OFFER_RE.finditer(visible_text):
        raw = collapse_ws(m.group(0))
        label = collapse_ws(m.group("label"))
        availability = m.group("availability").lower()
        condition = m.group("condition").lower()
        price = int(m.group("price").replace(" ", ""))
        offers.append(
            Offer(
                label=label,
                availability=availability,
                condition=condition,
                price_huf=price,
                raw=raw,
                is_new=condition in NEW_CONDITIONS,
                in_stock=availability == "raktáron",
            )
        )

    # Second pass: price-only fallback, useful if the page formatting changes.
    if not offers:
        for m in PRICE_RE.finditer(visible_text):
            price = int(m.group("price").replace(" ", ""))
            start = max(0, m.start() - 90)
            end = min(len(visible_text), m.end() + 20)
            raw = collapse_ws(visible_text[start:end])
            offers.append(
                Offer(
                    label=raw[:90],
                    availability="",
                    condition="",
                    price_huf=price,
                    raw=raw,
                )
            )

    # De-duplicate by raw string.
    deduped: list[Offer] = []
    seen = set()
    for offer in offers:
        key = (offer.raw, offer.price_huf, offer.availability, offer.condition)
        if key not in seen:
            seen.add(key)
            deduped.append(offer)
    return deduped


def _shop_from_row(row: Tag) -> str:
    sid = row.get("data-shop")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    meta = row.find("meta", attrs={"itemprop": "seller"})
    content = meta.get("content") if meta else None
    if isinstance(content, str) and content.strip():
        return content.strip()
    return ""


def _title_and_language_from_row(row: Tag) -> tuple[str, str]:
    h3 = row.select_one("h3.shop-product-title") or row.find("h3")
    if not h3:
        return "", ""
    tspan = h3.select_one("span.title")
    title = collapse_ws(tspan.get_text(" ", strip=True)) if tspan else ""
    small = h3.find("small")
    language = small.get_text(strip=True).lower() if small else ""
    return title, language


def _price_huf_from_row(row: Tag) -> int | None:
    el = row.find(attrs={"itemprop": "price"})
    if el is None:
        return None
    content = el.get("content")
    if content is None:
        return None
    try:
        return int(str(content).strip())
    except ValueError:
        return None


def _row_structured_in_stock(row: Tag) -> bool:
    if row.get("data-stock") == "1":
        return True
    for link in row.find_all("link", attrs={"itemprop": "availability"}):
        href = (link.get("href") or "").lower()
        if href.rstrip("/").endswith("instock"):
            return True
    return False


def _legacy_availability_from_product_row(row: Tag, in_stock: bool) -> str:
    """Map schema.org / visible cues to the same strings used by regex-parsed offers."""
    if in_stock:
        return "raktáron"
    for link in row.find_all("link", attrs={"itemprop": "availability"}):
        href = (link.get("href") or "").lower()
        if "preorder" in href:
            return "értesítés kérhető"
    flag = row.select_one(".flag")
    if flag:
        flag_txt = collapse_ws(flag.get_text(" ", strip=True)).lower()
        if "értesítés kérhető" in flag_txt:
            return "értesítés kérhető"
        if "rendelhető" in flag_txt:
            return "rendelhető"
    return ""


def parse_product_list_sections(html_text: str) -> list[Offer]:
    """
    Parse Kiemelt and Partnerek offer rows only (div.product-list with data-listtype).

    Each row is an ``a.row.shop``; other page sections (e.g. árkalkulátor) are ignored.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    offers: list[Offer] = []
    containers = soup.select(
        'div.product-list[data-listtype="highlighted_partner"], '
        'div.product-list[data-listtype="partner"]'
    )
    for container in containers:
        section = (container.get("data-listtype") or "").strip()
        if section not in ("highlighted_partner", "partner"):
            continue
        for row in container.select("a.row.shop"):
            shop = _shop_from_row(row)
            title, language = _title_and_language_from_row(row)
            price_huf = _price_huf_from_row(row)
            is_new = row.get("data-new") == "1"
            in_stock = _row_structured_in_stock(row)
            availability = _legacy_availability_from_product_row(row, in_stock)
            condition = "új" if is_new else ""
            label = collapse_ws(f"{shop} {title}".strip()) or collapse_ws(
                row.get_text(" ", strip=True)[:240]
            )
            raw = collapse_ws(row.get_text(" ", strip=True))
            href = row.get("href")
            offer_url = href.strip() if isinstance(href, str) else ""
            offers.append(
                Offer(
                    label=label,
                    availability=availability,
                    condition=condition,
                    price_huf=price_huf,
                    raw=raw,
                    shop=shop,
                    title=title,
                    language=language,
                    section=section,
                    in_stock=in_stock,
                    is_new=is_new,
                    url=offer_url,
                )
            )
    return offers


def matching_offers(offers: list[Offer]) -> list[Offer]:
    """Keep Hungarian, new, in-stock rows from Kiemelt / Partnerek sections."""
    return [
        o
        for o in offers
        if o.language == "magyar" and o.is_new and o.in_stock
    ]


def top_offers(offers: list[Offer], limit: int = 3) -> list[Offer]:
    candidates = [o for o in offers if o.price_huf is not None]
    candidates.sort(key=lambda o: o.price_huf if o.price_huf is not None else 10**18)
    return candidates[:limit]


def best_offer(offers: list[Offer]) -> tuple[int | None, str | None, str | None]:
    candidates = [o for o in offers if o.price_huf is not None]
    if not candidates:
        return None, None, None
    candidates.sort(key=lambda o: o.price_huf if o.price_huf is not None else 10**18)
    best = candidates[0]
    ba = best.availability.strip() or None
    bc = best.condition.strip() or None
    return best.price_huf, ba, bc


def _best_status_combined(availability: str | None, condition: str | None) -> str | None:
    parts = [p for p in (availability, condition) if p]
    return " ".join(parts) if parts else None


def summarize_notes(text: str) -> list[str]:
    notes: list[str] = []
    lowered = text.lower()
    for word, label in [
        (
            "értesítés kérhető",
            "at least one seller currently wants notification instead of immediate stock",
        ),
        ("raktáron", "at least one seller appears to have stock"),
        ("rendelhető", "at least one seller lists the item as orderable"),
    ]:
        if word in lowered:
            notes.append(label)
    return notes


def failed_game_result(game: dict[str, Any], exc: BaseException) -> GameResult:
    """Build a GameResult when fetch or parse fails so the run can continue."""
    url = str(game["url"])
    name = str(game.get("name") or url)
    fetched_at = datetime.now(UTC).isoformat()
    err = f"{type(exc).__name__}: {exc}"
    return GameResult(
        name=name,
        url=url,
        page_title="—",
        fetched_at=fetched_at,
        best_price_huf=None,
        best_availability=None,
        best_condition=None,
        offer_count=0,
        new_available_count=0,
        offers=[],
        matching_offers=[],
        notes=[],
        error=err,
    )


def collect_game(game: dict[str, Any]) -> GameResult:
    name = game.get("name") or game.get("url")
    url = game["url"]
    html_text = fetch_page(url)
    page_title, visible = text_from_html(html_text)
    offers = parse_product_list_sections(html_text)
    new_available = matching_offers(offers)
    best_price, best_availability, best_condition = best_offer(new_available)
    notes = summarize_notes(visible)

    # Prefer the configured name, but keep the page title for reference.
    fetched_at = datetime.now(UTC).isoformat()
    return GameResult(
        name=name,
        url=url,
        page_title=page_title,
        fetched_at=fetched_at,
        best_price_huf=best_price,
        best_availability=best_availability,
        best_condition=best_condition,
        offer_count=len(offers),
        new_available_count=len(new_available),
        offers=offers[:12],
        matching_offers=new_available,
        notes=notes,
        error=None,
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _history_cutoff_date(days: int) -> str:
    cutoff = datetime.now(UTC).date() - timedelta(days=max(days - 1, 0))
    return cutoff.isoformat()


def seed_price_history(entry: dict[str, Any]) -> list[dict[str, Any]]:
    history = list(entry.get("price_history") or [])
    if history:
        return history
    price = entry.get("best_price_huf")
    last_seen = entry.get("last_seen")
    if price is not None and isinstance(last_seen, str) and len(last_seen) >= 10:
        return [{"date": last_seen[:10], "price_huf": int(price)}]
    return []


def trim_price_history(history: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    cutoff = _history_cutoff_date(days)
    return [point for point in history if str(point.get("date", "")) >= cutoff]


def append_price_history_point(
    history: list[dict[str, Any]],
    price_huf: int | None,
    fetched_at: str,
    *,
    days: int,
) -> list[dict[str, Any]]:
    trimmed = trim_price_history(history, days)
    if price_huf is None:
        return trimmed
    day = fetched_at[:10]
    point = {"date": day, "price_huf": price_huf}
    if trimmed and trimmed[-1].get("date") == day:
        trimmed[-1] = point
    else:
        trimmed.append(point)
    return trim_price_history(trimmed, days)


def price_history_for_report(
    entry: dict[str, Any],
    result: GameResult,
    *,
    days: int,
) -> list[dict[str, Any]]:
    history = seed_price_history(entry)
    return append_price_history_point(history, result.best_price_huf, result.fetched_at, days=days)


COMBINED_CHART_CID = "chart-combined"

CHART_SERIES_COLORS = [
    ("#2563eb", "#1d4ed8"),
    ("#dc2626", "#b91c1c"),
    ("#16a34a", "#15803d"),
    ("#9333ea", "#7e22ce"),
    ("#ea580c", "#c2410c"),
    ("#0891b2", "#0e7490"),
    ("#ca8a04", "#a16207"),
    ("#db2777", "#be185d"),
]


def _format_chart_price(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _series_points_by_date(
    history: list[dict[str, Any]],
) -> dict[str, int]:
    points: dict[str, int] = {}
    for point in history:
        price = point.get("price_huf")
        date = point.get("date")
        if price is None or not date:
            continue
        points[str(date)] = int(price)
    return points


def render_combined_price_chart_png(
    series: list[tuple[str, list[dict[str, Any]]]],
    *,
    width: int = 640,
    plot_height: int = 180,
) -> bytes:
    if not series:
        raise ValueError("combined price chart requires at least one series")

    all_dates = sorted(
        {
            str(point["date"])
            for _, history in series
            for point in history
            if point.get("date") and point.get("price_huf") is not None
        }
    )
    if not all_dates:
        raise ValueError("combined price chart requires at least one point")

    all_prices = [
        int(point["price_huf"])
        for _, history in series
        for point in history
        if point.get("price_huf") is not None
    ]
    min_p = min(all_prices)
    max_p = max(all_prices)
    if min_p == max_p:
        pad = max(int(min_p * 0.05), 500)
        min_p = max(0, min_p - pad)
        max_p = max_p + pad

    legend_rows = (len(series) + 1) // 2
    legend_h = 12 + legend_rows * 16
    height = plot_height + legend_h + 34

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    margin_l, margin_r, margin_t, margin_b = 56, 12, 14, 22 + legend_h
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b
    plot_right = margin_l + plot_w
    plot_bottom = margin_t + plot_h

    def y_for_price(price: int) -> float:
        if max_p == min_p:
            return margin_t + plot_h / 2
        return margin_t + plot_h * (1 - (price - min_p) / (max_p - min_p))

    def x_for_date(date: str) -> float:
        if len(all_dates) == 1:
            return margin_l + plot_w / 2
        idx = all_dates.index(date)
        return margin_l + plot_w * idx / (len(all_dates) - 1)

    draw.rectangle([margin_l, margin_t, plot_right, plot_bottom], outline="#dddddd")

    for idx, (_name, history) in enumerate(series):
        line_color, dot_color = CHART_SERIES_COLORS[idx % len(CHART_SERIES_COLORS)]
        by_date = _series_points_by_date(history)
        ordered_dates = [day for day in all_dates if day in by_date]
        coords = [(x_for_date(day), y_for_price(by_date[day])) for day in ordered_dates]
        if len(coords) >= 2:
            draw.line(coords, fill=line_color, width=2)
        for x, y in coords:
            draw.ellipse([x - 3, y - 3, x + 3, y + 3], fill=line_color, outline=dot_color)

    draw.text((4, margin_t + plot_h - 6), _format_chart_price(min_p), fill="#666666", font=font)
    draw.text((4, margin_t - 6), _format_chart_price(max_p), fill="#666666", font=font)

    date_y = plot_bottom + 6
    draw.text((margin_l, date_y), all_dates[0][5:], fill="#666666", font=font)
    if len(all_dates) > 1:
        mid = all_dates[len(all_dates) // 2][5:]
        draw.text((margin_l + plot_w / 2 - 14, date_y), mid, fill="#666666", font=font)
        draw.text((plot_right - 36, date_y), all_dates[-1][5:], fill="#666666", font=font)

    legend_y = plot_bottom + 22
    col_w = plot_w / 2
    for idx, (name, _) in enumerate(series):
        line_color, _ = CHART_SERIES_COLORS[idx % len(CHART_SERIES_COLORS)]
        col = idx % 2
        row = idx // 2
        x = margin_l + col * col_w
        y = legend_y + row * 16
        draw.line([x, y + 5, x + 18, y + 5], fill=line_color, width=2)
        draw.text((x + 24, y), name[:28], fill="#333333", font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build_price_charts(
    results: list[GameResult],
    state: dict[str, Any],
    *,
    days: int,
) -> dict[str, bytes]:
    series: list[tuple[str, list[dict[str, Any]]]] = []
    for result in results:
        if result.error or result.best_price_huf is None:
            continue
        history = price_history_for_report(state.get(result.url, {}), result, days=days)
        if not history:
            continue
        series.append((result.name, history))
    if not series:
        return {}
    return {COMBINED_CHART_CID: render_combined_price_chart_png(series)}


def summarize_price_history(history: list[dict[str, Any]], days: int) -> str:
    prices = [int(point["price_huf"]) for point in history if point.get("price_huf") is not None]
    if not prices:
        return "no price history yet"
    low, high = min(prices), max(prices)
    if len(prices) == 1:
        return f"last {days} days: {_format_chart_price(prices[0])} Ft (1 reading)"
    change = prices[-1] - prices[0]
    if change < 0:
        trend = f"down {_format_chart_price(abs(change))} Ft"
    elif change > 0:
        trend = f"up {_format_chart_price(change)} Ft"
    else:
        trend = "unchanged"
    return (
        f"last {days} days: {_format_chart_price(low)}–{_format_chart_price(high)} Ft; "
        f"{trend} since first reading"
    )


def fmt_price(value: int | None) -> str:
    return "—" if value is None else f"{value:,}".replace(",", " ") + " Ft"


def section_label(section: str) -> str:
    return SECTION_LABELS.get(section, section or "—")


def matching_offers_by_section(matching: list[Offer]) -> dict[str, int]:
    counts = {key: 0 for key in SECTION_LABELS}
    for offer in matching:
        if offer.section in counts:
            counts[offer.section] += 1
    return counts


def format_matching_offers_summary(total_in_sections: int, matching: list[Offer]) -> str:
    by_section = matching_offers_by_section(matching)
    kiemelt = by_section["highlighted_partner"]
    partner = by_section["partner"]
    if not matching:
        return (
            f"{total_in_sections} in target sections / 0 matching "
            f"({NO_MATCHING_OFFERS_MSG})"
        )
    return (
        f"{total_in_sections} in target sections / {len(matching)} matching "
        f"({kiemelt} Kiemelt, {partner} Partnerek)"
    )


def _parse_iso_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def last_email_sent_at(state: dict[str, Any]) -> datetime | None:
    meta = state.get(STATE_META_KEY)
    if not isinstance(meta, dict):
        return None
    last_sent = meta.get("last_email_sent")
    if not isinstance(last_sent, str):
        return None
    parsed = _parse_iso_datetime(last_sent)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def record_email_sent(state: dict[str, Any], when: datetime | None = None) -> None:
    sent_at = when or datetime.now(UTC)
    meta = dict(state.get(STATE_META_KEY) or {})
    meta["last_email_sent"] = sent_at.isoformat()
    state[STATE_META_KEY] = meta


def has_price_change(results: list[GameResult], state: dict[str, Any]) -> bool:
    for result in results:
        if result.error:
            continue
        prev = state.get(result.url, {})
        if not isinstance(prev, dict):
            continue
        prev_price = prev.get("best_price_huf")
        cur_price = result.best_price_huf
        if prev_price != cur_price:
            return True
    return False


def should_send_email(
    results: list[GameResult],
    state: dict[str, Any],
    *,
    heartbeat_days: int = DEFAULT_EMAIL_HEARTBEAT_DAYS,
) -> bool:
    if has_price_change(results, state):
        return True
    last_sent = last_email_sent_at(state)
    if last_sent is None:
        return True
    return datetime.now(UTC) - last_sent >= timedelta(days=heartbeat_days)


def compare_with_previous(current: GameResult, prev: dict[str, Any]) -> str:
    if current.error:
        return "fetch failed; no comparison"
    parts: list[str] = []
    prev_price = prev.get("best_price_huf")
    prev_status = prev.get("best_status")
    cur_combined = _best_status_combined(current.best_availability, current.best_condition)

    if prev_price is not None and current.best_price_huf is not None:
        diff = current.best_price_huf - prev_price
        if diff < 0:
            parts.append(f"price down {abs(diff):,}".replace(",", " ") + " Ft")
        elif diff > 0:
            parts.append(f"price up {diff:,}".replace(",", " ") + " Ft")
        else:
            parts.append("price unchanged")

    if "best_availability" in prev or "best_condition" in prev:
        prev_pair = (prev.get("best_availability"), prev.get("best_condition"))
        cur_pair = (current.best_availability, current.best_condition)
        if prev_pair != cur_pair:
            prev_l = _best_status_combined(prev_pair[0], prev_pair[1])
            parts.append(f"best offer changed: {prev_l or '—'} → {cur_combined or '—'}")
    elif prev_status and prev_status != (cur_combined or ""):
        parts.append(f"best offer changed: {prev_status} → {cur_combined or '—'}")

    if not parts:
        return "no prior comparison data"
    return "; ".join(parts)


def _offer_link_html(offer: Offer, game_url: str) -> str:
    url = offer.url or game_url
    if not url:
        return "—"
    label = html.escape(offer.shop or "link")
    return f'<a href="{html.escape(url)}">{label}</a>'


def build_html_report(
    results: list[GameResult],
    state: dict[str, Any],
    run_at: str,
    *,
    history_days: int = DEFAULT_PRICE_HISTORY_DAYS,
    charts: dict[str, bytes] | None = None,
) -> str:
    charts = charts or {}
    rows = []
    chart_section = ""
    if COMBINED_CHART_CID in charts:
        chart_section = (
            '<div style="margin-bottom:1.2em;">'
            f'<div style="font-size:12px;color:#666;margin-bottom:6px;">'
            f"Best price trends — last {history_days} days</div>"
            f'<img src="cid:{COMBINED_CHART_CID}" alt="Combined price trends" '
            'style="display:block;max-width:100%;height:auto;border:0;">'
            "</div>"
        )
    for result in results:
        prev = state.get(result.url, {})
        delta = compare_with_previous(result, prev)
        game_cell = (
            f'<strong>{html.escape(result.name)}</strong>'
            f'<br><a href="{html.escape(result.url)}">compare</a>'
        )
        if result.error:
            rows.append(
                "<tr>"
                f'<td style="padding:6px 8px;border:1px solid #ddd;">{game_cell}</td>'
                '<td style="padding:6px 8px;border:1px solid #ddd;">—</td>'
                f'<td style="padding:6px 8px;border:1px solid #ddd;color:#b00020;" colspan="2">'
                f"{html.escape(result.error)}</td>"
                "</tr>"
            )
            continue

        offers = top_offers(result.matching_offers)
        if not offers:
            rows.append(
                "<tr>"
                f'<td style="padding:6px 8px;border:1px solid #ddd;">{game_cell}</td>'
                '<td style="padding:6px 8px;border:1px solid #ddd;">—</td>'
                f'<td style="padding:6px 8px;border:1px solid #ddd;">—</td>'
                f'<td style="padding:6px 8px;border:1px solid #ddd;color:#666;">'
                f"{html.escape(NO_MATCHING_OFFERS_MSG)}</td>"
                "</tr>"
            )
        else:
            for i, offer in enumerate(offers):
                game_col = game_cell if i == 0 else ""
                delta_col = html.escape(delta) if i == 0 else ""
                rows.append(
                    "<tr>"
                    f'<td style="padding:6px 8px;border:1px solid #ddd;vertical-align:top;">{game_col}</td>'
                    f'<td style="padding:6px 8px;border:1px solid #ddd;">{html.escape(fmt_price(offer.price_huf))}</td>'
                    f'<td style="padding:6px 8px;border:1px solid #ddd;">{_offer_link_html(offer, result.url)}</td>'
                    f'<td style="padding:6px 8px;border:1px solid #ddd;color:#666;font-size:12px;">{delta_col}</td>'
                    "</tr>"
                )

    return f"""\
<!doctype html>
<html>
  <body style="font-family:Arial,Helvetica,sans-serif;line-height:1.4;color:#222;">
    <h2 style="margin-bottom:0.2em;">Daily board-game price report</h2>
    <div style="color:#666;margin-bottom:1em;">Run at {html.escape(run_at)}</div>
    {chart_section}
    <table style="border-collapse:collapse;width:100%;font-size:13px;">
      <thead>
        <tr>
          <th style="text-align:left;padding:6px 8px;border:1px solid #ddd;">Game</th>
          <th style="text-align:left;padding:6px 8px;border:1px solid #ddd;">Price</th>
          <th style="text-align:left;padding:6px 8px;border:1px solid #ddd;">Link</th>
          <th style="text-align:left;padding:6px 8px;border:1px solid #ddd;">Change</th>
        </tr>
      </thead>
      <tbody>
        {"".join(rows)}
      </tbody>
    </table>
  </body>
</html>
"""


def build_text_report(
    results: list[GameResult],
    state: dict[str, Any],
    run_at: str,
    *,
    history_days: int = DEFAULT_PRICE_HISTORY_DAYS,
) -> str:
    lines = ["Daily board-game price report", f"Run at: {run_at}", ""]
    for result in results:
        prev = state.get(result.url, {})
        delta = compare_with_previous(result, prev)
        history = price_history_for_report(prev, result, days=history_days)
        lines.append(f"{result.name} — {result.url}")
        if result.error:
            lines.append(f"  Error: {result.error}")
            lines.append("")
            continue

        offers = top_offers(result.matching_offers)
        if not offers:
            lines.append(f"  {NO_MATCHING_OFFERS_MSG}")
        else:
            for offer in offers:
                link = offer.url or result.url
                shop = offer.shop or "shop"
                lines.append(f"  {fmt_price(offer.price_huf)} | {shop}: {link}")
        lines.append(f"  Change: {delta}")
        if history:
            lines.append(f"  History: {summarize_price_history(history, history_days)}")
        lines.append("")
    return "\n".join(lines)


def send_email(
    smtp_cfg: dict[str, Any],
    subject: str,
    text_body: str,
    html_body: str,
    *,
    embedded_images: dict[str, bytes] | None = None,
) -> None:
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(text_body, "plain", "utf-8"))
    alternative.attach(MIMEText(html_body, "html", "utf-8"))

    if embedded_images:
        msg: MIMEMultipart = MIMEMultipart("related")
        msg.attach(alternative)
        for cid, data in embedded_images.items():
            image = MIMEImage(data, _subtype="png")
            image.add_header("Content-ID", f"<{cid}>")
            image.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
            msg.attach(image)
    else:
        msg = alternative

    msg["Subject"] = subject
    msg["From"] = smtp_cfg["from"]
    msg["To"] = ", ".join(smtp_cfg["to"]) if isinstance(smtp_cfg["to"], list) else smtp_cfg["to"]

    host = smtp_cfg["host"]
    port = int(smtp_cfg.get("port", 587))
    use_tls = bool(smtp_cfg.get("use_tls", True))
    username = smtp_cfg.get("username")
    password = smtp_cfg.get("password")

    with smtplib.SMTP(host, port, timeout=30) as server:
        if use_tls:
            server.starttls()
        if username:
            server.login(username, password or "")
        server.send_message(msg)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path, help="Path to YAML config")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print report instead of sending email"
    )
    args = parser.parse_args()

    load_dotenv_for_config(args.config)
    cfg = load_config(args.config)
    apply_smtp_env_overrides(cfg)
    state_path = Path(cfg.get("state_path", "state.json"))
    history_days = int(cfg.get("price_history_days", DEFAULT_PRICE_HISTORY_DAYS))
    state = load_state(state_path)

    results: list[GameResult] = []
    new_state: dict[str, Any] = dict(state)

    for game in cfg["games"]:
        try:
            result = collect_game(game)
        except Exception as exc:  # noqa: BLE001
            result = failed_game_result(game, exc)
            logger.warning("Failed to collect %s: %s", game.get("url", game), exc)
        results.append(result)
        if result.error:
            continue
        combined = _best_status_combined(result.best_availability, result.best_condition)
        prev_entry = state.get(result.url, {})
        price_history = append_price_history_point(
            seed_price_history(prev_entry),
            result.best_price_huf,
            result.fetched_at,
            days=history_days,
        )
        new_state[result.url] = {
            "name": result.name,
            "page_title": result.page_title,
            "best_price_huf": result.best_price_huf,
            "best_availability": result.best_availability,
            "best_condition": result.best_condition,
            "best_status": combined,
            "offer_count": result.offer_count,
            "new_available_count": result.new_available_count,
            "last_seen": result.fetched_at,
            "price_history": price_history,
        }

    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    charts = build_price_charts(results, state, days=history_days)
    text_body = build_text_report(results, state, run_at, history_days=history_days)
    html_body = build_html_report(
        results,
        state,
        run_at,
        history_days=history_days,
        charts=charts,
    )

    heartbeat_days = int(cfg.get("email_heartbeat_days", DEFAULT_EMAIL_HEARTBEAT_DAYS))
    send_report = should_send_email(results, state, heartbeat_days=heartbeat_days)

    if args.dry_run:
        print(text_body)
        if not send_report:
            print(
                "\n(Email would be skipped: no price change and an email was sent "
                f"within the last {heartbeat_days} days.)"
            )
        return 0

    if send_report:
        smtp_cfg = cfg["smtp"]
        subject = cfg.get("subject", f"Daily board-game report — {run_at[:10]}")
        send_email(smtp_cfg, subject, text_body, html_body, embedded_images=charts or None)
        record_email_sent(new_state)
        logger.info("Email sent.")
    else:
        logger.info(
            "Skipping email: no price change and an email was sent within the last %s days.",
            heartbeat_days,
        )

    save_state(state_path, new_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
