#!/usr/bin/env python3
"""
Daily board-game watcher for tarsasjatekok.com product pages.

What it does:
- Loads a YAML config with one or more game pages.
- Fetches each page once per run.
- Extracts visible text, tries to identify offers, prices, and availability cues.
- Keeps a small JSON state file so the email can mention changes since the last run.
- Sends a report-style email via SMTP.

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
import json
import logging
import os
import re
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import requests
import yaml
from bs4 import BeautifulSoup
from bs4.element import Tag
from dotenv import load_dotenv

logger = logging.getLogger(__name__)


UA = "Mozilla/5.0 (compatible; GameWatch/1.0; +https://example.com)"
DEFAULT_TIMEOUT = 25


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


def build_html_report(results: list[GameResult], state: dict[str, Any], run_at: str) -> str:
    rows = []
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
            continue

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


def build_text_report(results: list[GameResult], state: dict[str, Any], run_at: str) -> str:
    lines = ["Daily board-game price report", f"Run at: {run_at}", ""]
    for result in results:
        prev = state.get(result.url, {})
        delta = compare_with_previous(result, prev)
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
        lines.append("")
    return "\n".join(lines)


def send_email(
    smtp_cfg: dict[str, Any],
    subject: str,
    text_body: str,
    html_body: str,
) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["from"]
    msg["To"] = ", ".join(smtp_cfg["to"]) if isinstance(smtp_cfg["to"], list) else smtp_cfg["to"]
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

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
        }

    run_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text_body = build_text_report(results, state, run_at)
    html_body = build_html_report(results, state, run_at)

    if args.dry_run:
        print(text_body)
        return 0

    smtp_cfg = cfg["smtp"]
    subject = cfg.get("subject", f"Daily board-game report — {run_at[:10]}")
    send_email(smtp_cfg, subject, text_body, html_body)
    save_state(state_path, new_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
