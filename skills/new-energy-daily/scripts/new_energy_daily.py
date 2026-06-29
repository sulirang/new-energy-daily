from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from xml.etree import ElementTree

import httpx
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from markdown import markdown as render_markdown
from openai import OpenAI
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES_FILE = PROJECT_ROOT / "config" / "sources.yaml"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_EXA_KEYS_FILE = PROJECT_ROOT / "config" / "exa_keys.txt"
DEFAULT_EXA_KEY_STATE_FILE = PROJECT_ROOT / "state" / "exa_key_state.json"
DEFAULT_FIRECRAWL_KEY_FILE = PROJECT_ROOT / "config" / "firecrawl_key.txt"
DEFAULT_SEND_STATE_FILE = PROJECT_ROOT / "state" / "sent_reports.json"


def resolve_runtime_path(value: str | Path, default: Path) -> Path:
    raw = str(value).strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = {"spm", "from", "source", "nsukey", "fbclid", "gclid"}
GME_ZONES = (
    ("NORD", "北部"),
    ("CNOR", "中北部"),
    ("CSUD", "中南部"),
    ("SUD", "南部"),
    ("CALA", "卡拉布里亚"),
    ("SICI", "西西里"),
    ("SARD", "撒丁"),
)


@dataclass
class Candidate:
    title: str
    url: str
    source: str
    published_at: str | None = None
    summary: str | None = None
    text: str | None = None
    tags: list[str] | None = None


@dataclass
class ScoredItem:
    candidate: Candidate
    score: int
    reason: str
    topic: str
    selected: bool


@dataclass
class GmeZonalPrice:
    code: str
    name: str
    average_price_eur_mwh: float
    highest_price_eur_mwh: float
    lowest_price_eur_mwh: float
    periods: int | None = None


@dataclass
class GmePriceSnapshot:
    target_day: date
    pun_price: GmeZonalPrice | None
    zones: list[GmeZonalPrice]
    source_url: str
    missing_zones: list[str] | None = None


@dataclass
class GasPriceSnapshot:
    target_day: date
    effective_day: date
    region: str
    price_eur_mwh: float
    source_name: str
    source_url: str
    note: str | None = None


@dataclass
class DayAheadMarketSnapshot:
    target_day: date
    country: str
    market_code: str
    average_price_eur_mwh: float
    highest_price_eur_mwh: float
    lowest_price_eur_mwh: float
    periods: int
    source_url: str


@dataclass
class ExaKeyPool:
    keys: list[str]
    state_file: Path
    cursor: int = 0

    @classmethod
    def from_environment(cls) -> "ExaKeyPool":
        keys_file = resolve_runtime_path(os.environ.get("EXA_KEYS_FILE", ""), DEFAULT_EXA_KEYS_FILE)
        state_file = resolve_runtime_path(os.environ.get("EXA_KEY_STATE_FILE", ""), DEFAULT_EXA_KEY_STATE_FILE)
        keys: list[str] = []

        if keys_file.exists():
            for line in keys_file.read_text(encoding="utf-8").splitlines():
                key = line.strip()
                if key and not key.startswith("#") and key not in keys:
                    keys.append(key)

        environment_keys = re.split(r"[\s,;]+", os.environ.get("EXA_API_KEYS", "").strip())
        legacy_key = os.environ.get("EXA_API_KEY", "").strip()
        for key in [*environment_keys, legacy_key]:
            if key and key != "your_exa_api_key" and key not in keys:
                keys.append(key)

        if not keys:
            raise RuntimeError(f"No Exa API keys found in {keys_file} or environment variables")

        cursor = 0
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
            cursor = int(state.get("next_index", 0)) % len(keys)
        except (FileNotFoundError, OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            pass
        return cls(keys=keys, state_file=state_file, cursor=cursor)

    def attempts(self) -> list[tuple[int, str]]:
        return [((self.cursor + offset) % len(self.keys), self.keys[(self.cursor + offset) % len(self.keys)]) for offset in range(len(self.keys))]

    def mark_success(self, key_index: int) -> None:
        self.cursor = (key_index + 1) % len(self.keys)
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.state_file.with_name(f".{self.state_file.name}.{os.getpid()}.tmp")
            temp_file.write_text(json.dumps({"next_index": self.cursor}) + "\n", encoding="utf-8")
            os.replace(temp_file, self.state_file)
        except OSError as exc:
            print(f"warning: could not persist Exa key rotation state: {exc}", file=sys.stderr)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", BeautifulSoup(value, "lxml").get_text(" ")).strip()


def normalize_url(url: str, base_url: str | None = None) -> str:
    absolute = urljoin(base_url or "", url)
    parsed = urlparse(absolute)
    query = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key in TRACKING_QUERY_KEYS or any(key.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        query.append((key, value))
    return urlunparse(parsed._replace(fragment="", query=urlencode(query)))


def parse_datetime(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=tz)
        except Exception:
            continue
    return None


def parse_collection_cutoff(value: str) -> time:
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ValueError(f"Invalid collection cutoff time {value!r}; expected HH:MM") from exc


def collection_window(target_day: date, tz: ZoneInfo, cutoff: time) -> tuple[datetime, datetime]:
    window_end = datetime.combine(target_day, cutoff, tzinfo=tz)
    days_back = 3 if target_day.weekday() == 0 else 1
    window_start_day = target_day - timedelta(days=days_back)
    window_start = datetime.combine(window_start_day, cutoff, tzinfo=tz)
    return window_start, window_end


def is_weekend(target_day: date) -> bool:
    return target_day.weekday() >= 5


def is_in_collection_window(
    published_at: str | None,
    window_start: datetime,
    window_end: datetime,
    tz: ZoneInfo,
    allow_undated: bool,
) -> bool:
    parsed = parse_datetime(published_at, tz)
    if not parsed:
        return allow_undated
    return window_start < parsed <= window_end


def keyword_allowed(candidate: Candidate, include_keywords: list[str], exclude_keywords: list[str]) -> bool:
    text = " ".join([candidate.title, candidate.summary or "", candidate.text or ""])
    if exclude_keywords and any(keyword in text for keyword in exclude_keywords):
        return False
    if include_keywords and not any(keyword in text for keyword in include_keywords):
        return False
    return True


def fetch_rss(
    client: httpx.Client,
    source: dict[str, Any],
    tz: ZoneInfo,
    window_start: datetime,
    window_end: datetime,
    max_items: int,
) -> list[Candidate]:
    response = client.get(source["url"])
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    items = []
    elements = root.findall(".//item")
    if not elements:
        elements = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    for element in elements[: max_items * 2]:
        title = find_xml_text(element, ["title", "{http://www.w3.org/2005/Atom}title"])
        link = find_xml_text(element, ["link"])
        if not link:
            atom_link = element.find("{http://www.w3.org/2005/Atom}link")
            link = atom_link.attrib.get("href") if atom_link is not None else None
        published = find_xml_text(
            element,
            ["pubDate", "published", "updated", "{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated"],
        )
        summary = find_xml_text(
            element,
            ["description", "summary", "{http://www.w3.org/2005/Atom}summary", "{http://www.w3.org/2005/Atom}content"],
        )
        if not title or not link:
            continue
        if not is_in_collection_window(
            published,
            window_start,
            window_end,
            tz,
            bool(source.get("allow_undated", False)),
        ):
            continue
        items.append(
            Candidate(
                title=clean_text(title),
                url=normalize_url(link, source["url"]),
                source=source["name"],
                published_at=parse_datetime(published, tz).isoformat() if parse_datetime(published, tz) else None,
                summary=clean_text(summary),
                tags=source.get("tags") or [],
            )
        )
        if len(items) >= max_items:
            break
    return items


def find_xml_text(element: ElementTree.Element, names: list[str]) -> str | None:
    for name in names:
        child = element.find(name)
        if child is not None and child.text:
            return child.text
    return None


def fetch_webpage(
    client: httpx.Client,
    source: dict[str, Any],
    tz: ZoneInfo,
    window_start: datetime,
    window_end: datetime,
    max_items: int,
) -> list[Candidate]:
    response = client.get(source["list_url"])
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    items = []
    for node in soup.select(source.get("item_selector", "a")):
        title_node = node.select_one(source["title_selector"]) if source.get("title_selector") else node
        title = clean_text(title_node.get_text(" ") if title_node else "")
        href = node.get(source.get("url_attr", "href")) or (title_node.get("href") if title_node else None)
        if not title or not href:
            continue
        date_text = None
        if source.get("date_selector"):
            date_node = node.select_one(source["date_selector"])
            date_text = clean_text(date_node.get_text(" ") if date_node else "")
        if not is_in_collection_window(
            date_text,
            window_start,
            window_end,
            tz,
            bool(source.get("allow_undated", False)),
        ):
            continue
        items.append(
            Candidate(
                title=title,
                url=normalize_url(href, source["list_url"]),
                source=source["name"],
                published_at=parse_datetime(date_text, tz).isoformat() if parse_datetime(date_text, tz) else None,
                tags=source.get("tags") or [],
            )
        )
        if len(items) >= max_items:
            break
    return items


def result_value(result: Any, *names: str) -> Any:
    for name in names:
        if isinstance(result, dict) and name in result:
            return result[name]
        value = getattr(result, name, None)
        if value is not None:
            return value
    return None


def exa_error_status(exc: Exception) -> int | None:
    for value in (exc, getattr(exc, "response", None)):
        if value is None:
            continue
        status = getattr(value, "status_code", None) or getattr(value, "status", None)
        try:
            return int(status) if status is not None else None
        except (TypeError, ValueError):
            continue
    match = re.search(r"\b(401|402|403|429)\b", str(exc))
    return int(match.group(1)) if match else None


def is_exa_key_failure(exc: Exception) -> bool:
    if exa_error_status(exc) in {401, 402, 403, 429}:
        return True
    message = str(exc).lower()
    return any(
        phrase in message
        for phrase in (
            "api key",
            "unauthorized",
            "forbidden",
            "insufficient credit",
            "insufficient balance",
            "quota",
            "rate limit",
            "rate_limit",
            "exhausted",
        )
    )


def redact_exa_error(exc: Exception, keys: list[str]) -> str:
    message = str(exc)
    for key in keys:
        message = message.replace(key, "<redacted>")
    return message[:500]


def fetch_exa(
    source: dict[str, Any],
    tz: ZoneInfo,
    target_day: date,
    window_start: datetime,
    window_end: datetime,
    max_items: int,
    key_pool: ExaKeyPool | None = None,
) -> list[Candidate]:
    key_pool = key_pool or ExaKeyPool.from_environment()

    try:
        from exa_py import Exa
    except ImportError as exc:
        raise RuntimeError("exa-py is required for source type 'exa'") from exc

    configured_domains = source.get("include_domains") or source.get("domains") or []
    include_domains = []
    for configured_domain in configured_domains:
        raw_domain = str(configured_domain).strip()
        parsed_domain = urlparse(raw_domain if "://" in raw_domain else f"//{raw_domain}")
        domain = (parsed_domain.netloc or parsed_domain.path).split("/")[0].lower()
        if domain and domain not in include_domains:
            include_domains.append(domain)
    if not include_domains:
        raise ValueError("Exa source requires include_domains")

    query = source.get("query") or "新能源 光伏 风电 储能 电池 氢能 新能源汽车 电力市场 最新新闻"
    has_window_placeholder = "{window_start}" in query or "{window_end}" in query
    if "{date}" in query:
        query = query.replace("{date}", target_day.isoformat())
    query = query.replace("{window_start}", window_start.strftime("%Y-%m-%d %H:%M"))
    query = query.replace("{window_end}", window_end.strftime("%Y-%m-%d %H:%M"))
    if source.get("append_date_to_query", True) and not has_window_placeholder:
        timezone_name = getattr(tz, "key", str(tz))
        query = (
            f"{query} published between {window_start.strftime('%Y-%m-%d %H:%M')} and "
            f"{window_end.strftime('%Y-%m-%d %H:%M')} {timezone_name}"
        )
    landing_page = str(source.get("landing_page") or "").strip()
    if landing_page:
        query = f"{query} Official listing page: {landing_page}"
    result_limit = max(1, min(100, int(source.get("max_results", max_items))))
    text_limit = max(500, min(20000, int(source.get("text_max_characters", 5000))))
    start_at = window_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end_at = window_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    search_options: dict[str, Any] = {
        "type": source.get("search_type", "auto"),
        "num_results": result_limit,
        "include_domains": include_domains,
        "contents": {
            "text": {"max_characters": text_limit},
            "highlights": {"highlights_per_url": 2, "num_sentences": 2, "query": query},
        },
    }
    if source.get("use_api_date_filter", False):
        search_options["start_published_date"] = start_at
        search_options["end_published_date"] = end_at

    response = None
    key_errors = []
    for key_index, api_key in key_pool.attempts():
        try:
            response = Exa(api_key).search(query, **search_options)
        except Exception as exc:
            if not is_exa_key_failure(exc):
                raise
            safe_error = redact_exa_error(exc, key_pool.keys)
            key_errors.append(f"key {key_index + 1}: {safe_error}")
            print(
                f"warning: Exa key {key_index + 1}/{len(key_pool.keys)} unavailable; trying next key",
                file=sys.stderr,
            )
            continue
        key_pool.mark_success(key_index)
        break

    if response is None:
        details = "; ".join(key_errors)
        raise RuntimeError(f"All {len(key_pool.keys)} Exa API keys are unavailable: {details}")

    items = []
    for result in result_value(response, "results") or []:
        title = clean_text(str(result_value(result, "title") or ""))
        url = normalize_url(str(result_value(result, "url") or ""))
        published = result_value(result, "published_date", "publishedDate")
        published_text = str(published) if published is not None else None
        if not title or not url:
            continue
        if not is_in_collection_window(
            published_text,
            window_start,
            window_end,
            tz,
            bool(source.get("allow_undated", False)),
        ):
            continue

        highlights = result_value(result, "highlights") or []
        if isinstance(highlights, str):
            highlights = [highlights]
        text = clean_text(str(result_value(result, "text") or ""))[:text_limit]
        summary = clean_text(" ".join(str(item) for item in highlights)) or text[:500]
        parsed_published = parse_datetime(published_text, tz)
        items.append(
            Candidate(
                title=title,
                url=url,
                source=source["name"],
                published_at=parsed_published.isoformat() if parsed_published else None,
                summary=summary,
                text=text,
                tags=source.get("tags") or [],
            )
        )
    return items


def enrich_article_text(client: httpx.Client, candidate: Candidate, article_selector: str | None = None) -> Candidate:
    try:
        response = client.get(candidate.url)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        node = soup.select_one(article_selector) if article_selector else None
        if node is None:
            node = soup.select_one("article") or soup.select_one("main") or soup.body
        text = clean_text(node.get_text(" ") if node else "")
        candidate.text = text[:5000]
    except Exception:
        candidate.text = candidate.text or ""
    return candidate


def load_sources(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def parse_gme_daily_price(payload: Any, target_day: date) -> tuple[float, int | None]:
    if isinstance(payload, dict) and isinstance(payload.get("value"), list):
        entries = payload["value"]
    elif isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict) and "p" in payload:
        entries = [payload]
    else:
        entries = []

    expected_date = int(target_day.strftime("%Y%m%d"))
    entry = next((item for item in entries if int(item.get("df", 0)) == expected_date), None)
    if not entry or entry.get("p") is None:
        raise ValueError(f"GME returned no daily price for {target_day.isoformat()}")
    periods = int(entry["qh"]) if entry.get("qh") is not None else None
    return float(entry["p"]), periods


def parse_gme_price_stats(
    daily_payload: Any,
    interval_payload: Any,
    target_day: date,
) -> tuple[float, float, float, int]:
    average_price, _ = parse_gme_daily_price(daily_payload, target_day)
    if isinstance(interval_payload, dict) and isinstance(interval_payload.get("value"), list):
        entries = interval_payload["value"]
    elif isinstance(interval_payload, list):
        entries = interval_payload
    else:
        entries = []

    expected_date = int(target_day.strftime("%Y%m%d"))
    prices = [
        float(item["p"])
        for item in entries
        if int(item.get("df", 0)) == expected_date and item.get("p") is not None
    ]
    if not prices:
        raise ValueError(f"GME returned no 15-minute prices for {target_day.isoformat()}")
    return average_price, max(prices), min(prices), len(prices)


def load_firecrawl_api_key() -> str:
    key_file = resolve_runtime_path(os.environ.get("FIRECRAWL_KEY_FILE", ""), DEFAULT_FIRECRAWL_KEY_FILE)
    if key_file.exists():
        for line in key_file.read_text(encoding="utf-8").splitlines():
            key = line.strip()
            if key and not key.startswith("#"):
                return key

    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if key and key != "your_firecrawl_api_key":
        return key
    raise RuntimeError(f"No Firecrawl API key found in {key_file} or FIRECRAWL_API_KEY")


def firecrawl_response_json(response: httpx.Response, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.is_error or not payload.get("success", False):
        detail = payload.get("error") or payload.get("message") or response.reason_phrase
        raise RuntimeError(f"Firecrawl {operation} failed ({response.status_code}): {str(detail)[:300]}")
    return payload


def firecrawl_retry_delay(exc: Exception, attempt: int, max_attempts: int) -> float:
    if attempt >= max_attempts - 1 or "(429)" not in str(exc):
        return 0.0
    match = re.search(r"retry after\s+(\d+)s", str(exc), flags=re.IGNORECASE)
    return float(match.group(1)) + 2.0 if match else 32.0


def wait_before_firecrawl_retry(exc: Exception, attempt: int, max_attempts: int) -> None:
    delay = firecrawl_retry_delay(exc, attempt, max_attempts)
    if delay > 0:
        print(f"Firecrawl rate limited; retrying in {delay:.0f}s", file=sys.stderr)
        time_module.sleep(delay)


def run_market_fetch(key: str, fetcher: Any, config: dict[str, Any], target_day: date) -> Any:
    started = time_module.monotonic()
    print(f"market data start: {key}", flush=True)
    try:
        return fetcher(config, target_day)
    finally:
        elapsed = time_module.monotonic() - started
        print(f"market data finish: {key} ({elapsed:.1f}s)", flush=True)


def decode_firecrawl_result(value: Any) -> dict[str, Any]:
    for _ in range(3):
        if not isinstance(value, str):
            break
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("Firecrawl interact returned an invalid market-data payload")
    return value


def build_firecrawl_scrape_payload(settings: dict[str, Any], page_url: str) -> dict[str, Any]:
    return {
        "url": page_url,
        "formats": ["markdown"],
        "onlyMainContent": False,
        "maxAge": 0,
        "waitFor": int(settings.get("wait_for_ms", 8000)),
        "timeout": int(settings.get("timeout_ms", 120000)),
        "location": {
            "country": settings.get("country", "IT"),
            "languages": settings.get("languages", ["it-IT"]),
        },
        "proxy": settings.get("proxy", "auto"),
        "storeInCache": False,
    }


def run_firecrawl_interaction(
    settings: dict[str, Any],
    page_url: str,
    interaction_code: str,
    origin: str,
    operation: str,
) -> dict[str, Any]:
    api_key = load_firecrawl_api_key()
    api_base = os.environ.get("FIRECRAWL_API_BASE", settings.get("api_base", "https://api.firecrawl.dev")).rstrip("/")
    timeout_ms = int(settings.get("timeout_ms", 120000))
    interact_timeout = int(settings.get("interact_timeout_seconds", 60))
    max_attempts = max(1, int(settings.get("max_attempts", 2)))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Connection": "close",
        "User-Agent": "new-energy-daily/1.0",
    }
    last_error: Exception | None = None

    with httpx.Client(
        headers=headers,
        timeout=httpx.Timeout((timeout_ms / 1000) + 30, connect=30.0),
        limits=httpx.Limits(max_keepalive_connections=0),
    ) as client:
        for attempt in range(max_attempts):
            scrape_id = None
            try:
                scrape_response = client.post(
                    f"{api_base}/v2/scrape",
                    json=build_firecrawl_scrape_payload(settings, page_url),
                )
                scrape_payload = firecrawl_response_json(scrape_response, f"{operation} scrape")
                scrape_data = scrape_payload.get("data") or {}
                scrape_id = (scrape_data.get("metadata") or {}).get("scrapeId") or scrape_data.get("scrapeId")
                if not scrape_id:
                    raise RuntimeError(f"Firecrawl {operation} scrape did not return a scrapeId")

                interact_response = client.post(
                    f"{api_base}/v2/scrape/{scrape_id}/interact",
                    json={
                        "code": interaction_code,
                        "language": "node",
                        "timeout": interact_timeout,
                        "origin": origin,
                    },
                )
                interact_payload = firecrawl_response_json(interact_response, f"{operation} interact")
                if interact_payload.get("exitCode") not in (None, 0):
                    raise RuntimeError(
                        f"Firecrawl {operation} interact exited with code {interact_payload['exitCode']}"
                    )
                return decode_firecrawl_result(interact_payload.get("result") or interact_payload.get("stdout"))
            except Exception as exc:
                last_error = exc
                wait_before_firecrawl_retry(exc, attempt, max_attempts)
            finally:
                if scrape_id:
                    try:
                        client.delete(f"{api_base}/v2/scrape/{scrape_id}/interact")
                    except Exception:
                        pass

    raise RuntimeError(
        f"Firecrawl could not retrieve {operation} after {max_attempts} attempt(s): {last_error}"
    )


def run_firecrawl_scrape_text(
    settings: dict[str, Any],
    page_url: str,
    operation: str,
) -> str:
    api_key = load_firecrawl_api_key()
    api_base = os.environ.get("FIRECRAWL_API_BASE", settings.get("api_base", "https://api.firecrawl.dev")).rstrip("/")
    timeout_ms = int(settings.get("timeout_ms", 120000))
    max_attempts = max(1, int(settings.get("max_attempts", 2)))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Connection": "close",
        "User-Agent": "new-energy-daily/1.0",
    }
    last_error: Exception | None = None

    with httpx.Client(
        headers=headers,
        timeout=httpx.Timeout((timeout_ms / 1000) + 30, connect=30.0),
        limits=httpx.Limits(max_keepalive_connections=0),
    ) as client:
        for attempt in range(max_attempts):
            try:
                payload = build_firecrawl_scrape_payload(settings, page_url)
                payload["formats"] = ["markdown"]
                response = client.post(f"{api_base}/v2/scrape", json=payload)
                response_payload = firecrawl_response_json(response, operation)
                data = response_payload.get("data") or {}
                text = data.get("markdown") or data.get("rawHtml")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError(f"Firecrawl {operation} returned no text")
                return text
            except Exception as exc:
                last_error = exc
                wait_before_firecrawl_retry(exc, attempt, max_attempts)

    raise RuntimeError(
        f"Firecrawl could not retrieve {operation} after {max_attempts} attempt(s): {last_error}"
    )


def build_gme_firecrawl_code(target_day: date) -> str:
    browser_input = {
        "dateValue": target_day.strftime("%Y%m%d"),
        "zones": [code for code, _ in GME_ZONES],
    }
    return f"""
await page.waitForLoadState('domcontentloaded');
await page.waitForTimeout(1000);
const output = await page.evaluate(async (input) => {{
  const html = document.documentElement.innerHTML;
  const token = document.querySelector('input[name="__RequestVerificationToken"]')?.value
    || html.match(/name=["']__RequestVerificationToken["'][^>]+value=["']([^"']+)/i)?.[1];
  const moduleId = html.match(/"ModuleId"\\s*:\\s*(\\d+)/)?.[1];
  const tabId = html.match(/"TabId"\\s*:\\s*(\\d+)/)?.[1];
  if (!token || !moduleId || !tabId) {{
    throw new Error('GME page did not expose the expected token or module metadata');
  }}

  async function requestPrice(zone, priceType, granularity) {{
    const params = new URLSearchParams({{
      DataInizio: input.dateValue,
      DataFine: input.dateValue,
      Granularita: granularity,
      Mercato: 'MGP',
      Zona: zone,
      Tipologia: priceType,
    }});
    const response = await fetch(`/DesktopModules/GmeEsitiPrezziME/API/item/GetMEPrezzi?${{params}}`, {{
      credentials: 'include',
      headers: {{
        RequestVerificationToken: token,
        ModuleId: moduleId,
        TabId: tabId,
      }},
    }});
    if (!response.ok) {{
      throw new Error(`${{zone}} returned HTTP ${{response.status}}`);
    }}
    return await response.json();
  }}

  async function requestStats(zone, priceType) {{
    const daily = await requestPrice(zone, priceType, 'd');
    const intervals = await requestPrice(zone, priceType, 'qh');
    return {{
      daily,
      intervals,
    }};
  }}

  const pun = await requestStats('PUN', 'PUN');
  const zones = {{}};
  const errors = {{}};
  for (const zone of input.zones) {{
    try {{
      zones[zone] = await requestStats(zone, 'PrezziZonali');
    }} catch (error) {{
      errors[zone] = String(error?.message || error);
    }}
  }}
  return {{ pun, zones, errors }};
}}, {json.dumps(browser_input)});
JSON.stringify(output);
""".strip()


def build_gme_gas_firecrawl_code(target_day: date) -> str:
    browser_input = {
        "year": target_day.year,
        "month": target_day.month,
    }
    return f"""
await page.waitForLoadState('domcontentloaded');
var gasApiResult = await page.evaluate(async (input) => {{
  const config = window.GmeIGIndex;
  const token = document.querySelector('input[name="__RequestVerificationToken"]')?.value;
  if (!config?.ModuleId || !config?.TabId || !token) {{
    throw new Error('IG Index page did not expose the expected token or module metadata');
  }}
  const params = new URLSearchParams({{
    Anno: String(input.year),
    Mese: String(input.month),
    Dettaglio: 'G',
  }});
  const response = await fetch(`/DesktopModules/GmeIGIndex/API/item/GetGasIGI?${{params}}`, {{
    credentials: 'include',
    headers: {{
      RequestVerificationToken: token,
      ModuleId: String(config.ModuleId),
      TabId: String(config.TabId),
    }},
  }});
  if (!response.ok) {{
    throw new Error(`IG Index GME returned HTTP ${{response.status}}`);
  }}
  return {{ entries: await response.json() }};
}}, {json.dumps(browser_input)});
JSON.stringify(gasApiResult);
""".strip()


def build_ceegex_gas_firecrawl_code() -> str:
    return """
await page.waitForLoadState('domcontentloaded');
await page.waitForTimeout(1000);
var ceegexResult = await page.evaluate(() => ({
  rows: Array.from(document.querySelectorAll('table tr')).map((row) =>
    Array.from(row.cells).map((cell) => cell.innerText.trim())
  ).filter((cells) => cells.length >= 9),
}));
JSON.stringify(ceegexResult);
""".strip()


def build_mibgas_gas_firecrawl_code() -> str:
    return """
await page.waitForLoadState('domcontentloaded');
await page.waitForTimeout(1500);
var mibgasResult = await page.evaluate(() => {
  for (const element of document.querySelectorAll('[data-chart]')) {
    try {
      const chart = JSON.parse(element.getAttribute('data-chart'));
      const series = (chart.series || []).find((item) =>
        /MIBGAS-ES Index/i.test(String(item.name || ''))
      );
      if (!series) continue;
      return {
        points: (series.data || []).map((point) => ({
          deliveryDay: point.toollabel || point.name,
          price: point.y,
        })),
      };
    } catch (error) {
      continue;
    }
  }
  throw new Error('MIBGAS page did not expose the PVB Price Index chart');
});
JSON.stringify(mibgasResult);
""".strip()


def build_hupx_firecrawl_code(target_day: date) -> str:
    day = target_day.isoformat()
    return f"""
await page.waitForLoadState('domcontentloaded');
var hupxResult = await page.evaluate(async (day) => {{
  const filter = `DeliveryDay__gte__${{day}},DeliveryDay__lte__${{day}},Region__in__HU`;
  const response = await fetch(`/data/v1/dam_aggregated_trading_data_15min?filter=${{encodeURIComponent(filter)}}`, {{
    credentials: 'include',
  }});
  if (!response.ok) {{
    throw new Error(`HUPX data endpoint returned HTTP ${{response.status}}`);
  }}
  const body = await response.json();
  return {{ entries: Array.isArray(body?.data) ? body.data : [] }};
}}, {json.dumps(day)});
JSON.stringify(hupxResult);
""".strip()


def build_omie_firecrawl_code(target_day: date) -> str:
    browser_input = {
        "year": target_day.year,
        "month": target_day.month,
        "day": target_day.day,
    }
    return f"""
await page.waitForLoadState('domcontentloaded');
var omieResult = await page.evaluate(async (input) => {{
  const year = String(input.year);
  const month = String(input.month).padStart(2, '0');
  const day = String(input.day).padStart(2, '0');
  const path = `/sites/default/files/dados/AGNO_${{year}}/MES_${{month}}/TXT/INT_PBC_EV_H_1_${{day}}_${{month}}_${{year}}_${{day}}_${{month}}_${{year}}.TXT`;
  const response = await fetch(path, {{ credentials: 'include' }});
  if (!response.ok) {{
    throw new Error(`OMIE data file returned HTTP ${{response.status}}`);
  }}
  const bytes = await response.arrayBuffer();
  const text = new TextDecoder('windows-1252').decode(bytes);
  const line = text.split(/\\r?\\n/).find((value) =>
    value.toLowerCase().includes('precio marginal en el sistema espa')
  );
  if (!line) {{
    throw new Error('OMIE data file did not contain the Spanish marginal-price row');
  }}
  const prices = line.split(';').slice(1).map((value) =>
    Number(value.trim().replace(',', '.'))
  ).filter((value) => Number.isFinite(value));
  if (!prices.length) {{
    throw new Error('OMIE Spanish marginal-price row contained no numeric values');
  }}
  return {{
    average: prices.reduce((total, value) => total + value, 0) / prices.length,
    highest: Math.max(...prices),
    lowest: Math.min(...prices),
    periods: prices.length,
  }};
}}, {json.dumps(browser_input)});
JSON.stringify(omieResult);
""".strip()


def fetch_gme_zonal_prices(config: dict[str, Any], target_day: date) -> GmePriceSnapshot | None:
    settings = config.get("gme_zonal_prices") or {}
    if not settings.get("enabled", False):
        return None

    page_url = settings.get(
        "page_url",
        "https://www.mercatoelettrico.org/en-us/Home/Results/Electricity/MGP/Results/ZonalPrices",
    )
    api_key = load_firecrawl_api_key()
    api_base = os.environ.get("FIRECRAWL_API_BASE", settings.get("api_base", "https://api.firecrawl.dev")).rstrip("/")
    timeout_ms = int(settings.get("timeout_ms", 120000))
    interact_timeout = int(settings.get("interact_timeout_seconds", 90))
    max_attempts = max(1, int(settings.get("max_attempts", 2)))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Connection": "close",
        "User-Agent": "new-energy-daily/1.0",
    }
    interaction_code = build_gme_firecrawl_code(target_day)
    last_error: Exception | None = None
    result: dict[str, Any] | None = None

    with httpx.Client(
        headers=headers,
        timeout=httpx.Timeout((timeout_ms / 1000) + 30, connect=30.0),
        limits=httpx.Limits(max_keepalive_connections=0),
    ) as client:
        for attempt in range(max_attempts):
            scrape_id = None
            try:
                try:
                    scrape_response = client.post(
                        f"{api_base}/v2/scrape",
                        json=build_firecrawl_scrape_payload(settings, page_url),
                    )
                except Exception as exc:
                    raise RuntimeError(f"Firecrawl scrape request failed: {exc}") from exc
                scrape_payload = firecrawl_response_json(scrape_response, "scrape")
                scrape_data = scrape_payload.get("data") or {}
                scrape_id = (scrape_data.get("metadata") or {}).get("scrapeId") or scrape_data.get("scrapeId")
                if not scrape_id:
                    raise RuntimeError("Firecrawl scrape did not return a scrapeId")

                try:
                    interact_response = client.post(
                        f"{api_base}/v2/scrape/{scrape_id}/interact",
                        json={
                            "code": interaction_code,
                            "language": "node",
                            "timeout": interact_timeout,
                            "origin": "new-energy-daily-gme",
                        },
                    )
                except Exception as exc:
                    raise RuntimeError(f"Firecrawl interact request failed: {exc}") from exc
                interact_payload = firecrawl_response_json(interact_response, "interact")
                if interact_payload.get("exitCode") not in (None, 0):
                    raise RuntimeError(f"Firecrawl interact exited with code {interact_payload['exitCode']}")
                result = decode_firecrawl_result(interact_payload.get("result") or interact_payload.get("stdout"))
                break
            except Exception as exc:
                last_error = exc
                wait_before_firecrawl_retry(exc, attempt, max_attempts)
            finally:
                if scrape_id:
                    try:
                        client.delete(f"{api_base}/v2/scrape/{scrape_id}/interact")
                    except Exception:
                        pass

    if result is None:
        raise RuntimeError(f"Firecrawl could not retrieve GME prices after {max_attempts} attempt(s): {last_error}")

    pun_payload = result.get("pun") or {}
    pun_average, pun_highest, pun_lowest, pun_periods = parse_gme_price_stats(
        pun_payload.get("daily"),
        pun_payload.get("intervals"),
        target_day,
    )
    pun_price = GmeZonalPrice("PUN", "意大利", pun_average, pun_highest, pun_lowest, pun_periods)
    prices = []
    missing = []
    zone_payloads = result.get("zones") or {}
    for code, name in GME_ZONES:
        try:
            zone_payload = zone_payloads.get(code) or {}
            average, highest, lowest, periods = parse_gme_price_stats(
                zone_payload.get("daily"),
                zone_payload.get("intervals"),
                target_day,
            )
            prices.append(GmeZonalPrice(code, name, average, highest, lowest, periods))
        except Exception:
            missing.append(code)

    if not prices:
        raise RuntimeError("Firecrawl returned no physical GME zonal prices")
    return GmePriceSnapshot(
        target_day=target_day,
        pun_price=pun_price,
        zones=prices,
        source_url=page_url,
        missing_zones=missing,
    )


def fetch_gme_gas_price(config: dict[str, Any], target_day: date) -> GasPriceSnapshot | None:
    settings = config.get("gme_gas_price") or {}
    if not settings.get("enabled", False):
        return None

    page_url = settings.get(
        "page_url",
        "https://www.mercatoelettrico.org/en-us/Home/Publications/Indexes-GME/IGIndexGmeResults",
    )
    api_key = load_firecrawl_api_key()
    api_base = os.environ.get("FIRECRAWL_API_BASE", settings.get("api_base", "https://api.firecrawl.dev")).rstrip("/")
    timeout_ms = int(settings.get("timeout_ms", 120000))
    interact_timeout = int(settings.get("interact_timeout_seconds", 60))
    max_attempts = max(1, int(settings.get("max_attempts", 2)))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Connection": "close",
        "User-Agent": "new-energy-daily/1.0",
    }
    interaction_code = build_gme_gas_firecrawl_code(target_day)
    expected_date = int(target_day.strftime("%Y%m%d"))
    last_error: Exception | None = None

    with httpx.Client(
        headers=headers,
        timeout=httpx.Timeout((timeout_ms / 1000) + 30, connect=30.0),
        limits=httpx.Limits(max_keepalive_connections=0),
    ) as client:
        for attempt in range(max_attempts):
            scrape_id = None
            try:
                scrape_response = client.post(
                    f"{api_base}/v2/scrape",
                    json=build_firecrawl_scrape_payload(settings, page_url),
                )
                scrape_payload = firecrawl_response_json(scrape_response, "IG Index scrape")
                scrape_data = scrape_payload.get("data") or {}
                scrape_id = (scrape_data.get("metadata") or {}).get("scrapeId") or scrape_data.get("scrapeId")
                if not scrape_id:
                    raise RuntimeError("Firecrawl IG Index scrape did not return a scrapeId")

                interact_response = client.post(
                    f"{api_base}/v2/scrape/{scrape_id}/interact",
                    json={
                        "code": interaction_code,
                        "language": "node",
                        "timeout": interact_timeout,
                        "origin": "new-energy-daily-gas",
                    },
                )
                interact_payload = firecrawl_response_json(interact_response, "IG Index interact")
                if interact_payload.get("exitCode") not in (None, 0):
                    raise RuntimeError(f"Firecrawl IG Index interact exited with code {interact_payload['exitCode']}")
                result = decode_firecrawl_result(interact_payload.get("result") or interact_payload.get("stdout"))
                entries = result.get("entries") if isinstance(result.get("entries"), list) else []
                entry = next((item for item in entries if int(item.get("data", 0)) == expected_date), None)
                if not entry or entry.get("igi") is None:
                    raise ValueError(f"IG Index GME returned no price for {target_day.isoformat()}")
                return GasPriceSnapshot(
                    target_day=target_day,
                    effective_day=target_day,
                    region="意大利",
                    price_eur_mwh=float(entry["igi"]),
                    source_name="Gestore dei Mercati Energetici",
                    source_url=page_url,
                )
            except Exception as exc:
                last_error = exc
                wait_before_firecrawl_retry(exc, attempt, max_attempts)
            finally:
                if scrape_id:
                    try:
                        client.delete(f"{api_base}/v2/scrape/{scrape_id}/interact")
                    except Exception:
                        pass

    raise RuntimeError(f"Firecrawl could not retrieve IG Index GME after {max_attempts} attempt(s): {last_error}")


def parse_market_number(value: Any) -> float | None:
    normalized = str(value or "").strip().replace(" ", "")
    if normalized in {"", "-", "N/A", "n/a", "null", "None"}:
        return None
    if "," in normalized and "." not in normalized:
        normalized = normalized.replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None


def choose_target_or_latest(
    entries: list[tuple[date, float, str]], target_day: date
) -> tuple[date, float, str, str | None]:
    exact = next((entry for entry in entries if entry[0] == target_day), None)
    if exact:
        return exact[0], exact[1], exact[2], None
    eligible = [entry for entry in entries if entry[0] <= target_day]
    selected = max(eligible, key=lambda entry: entry[0], default=None)
    if selected is None:
        raise ValueError(f"No usable market value was published for or before {target_day.isoformat()}")
    note = (
        f"目标日 {target_day.isoformat()} 未发布，使用最近可用 delivery day "
        f"{selected[0].isoformat()}"
    )
    return selected[0], selected[1], selected[2], note


def fetch_ceegex_gas_price(config: dict[str, Any], target_day: date) -> GasPriceSnapshot | None:
    settings = config.get("ceegex_gas_price") or {}
    if not settings.get("enabled", False):
        return None

    page_url = settings.get("page_url", "https://ceegex.hu/en/market-data/daily-data")
    result = run_firecrawl_interaction(
        settings,
        page_url,
        build_ceegex_gas_firecrawl_code(),
        "new-energy-daily-ceegex-gas",
        "CEEGEX gas prices",
    )
    rows = result.get("rows") if isinstance(result.get("rows"), list) else []
    entries: list[tuple[date, float, str]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 9 or str(row[1]).strip().upper() != "DA":
            continue
        try:
            delivery_day = datetime.strptime(str(row[2])[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        ceerep = parse_market_number(row[7])
        vwap = parse_market_number(row[6])
        if ceerep is not None:
            entries.append((delivery_day, ceerep, "CEEREP"))
        elif vwap is not None:
            entries.append((delivery_day, vwap, "Volume Weighted Average Price"))

    effective_day, price, metric, note = choose_target_or_latest(entries, target_day)
    metric_note = None if metric == "CEEREP" else "CEEREP 缺失，使用 Volume Weighted Average Price"
    combined_note = "；".join(part for part in (note, metric_note) if part) or None
    return GasPriceSnapshot(
        target_day=target_day,
        effective_day=effective_day,
        region="匈牙利",
        price_eur_mwh=price,
        source_name="CEEGEX",
        source_url=settings.get("source_url", page_url),
        note=combined_note,
    )


def fetch_mibgas_gas_price(config: dict[str, Any], target_day: date) -> GasPriceSnapshot | None:
    settings = config.get("mibgas_gas_price") or {}
    if not settings.get("enabled", False):
        return None

    page_url = settings.get(
        "page_url",
        "https://www.mibgas.es/en/market-results/gas-daily-price-index-and-volumes",
    )
    result = run_firecrawl_interaction(
        settings,
        page_url,
        build_mibgas_gas_firecrawl_code(),
        "new-energy-daily-mibgas-gas",
        "MIBGAS PVB gas prices",
    )
    points = result.get("points") if isinstance(result.get("points"), list) else []
    entries: list[tuple[date, float, str]] = []
    for point in points:
        price = parse_market_number(point.get("price")) if isinstance(point, dict) else None
        try:
            delivery_day = datetime.strptime(str(point.get("deliveryDay")), "%d/%m/%y").date()
        except (AttributeError, TypeError, ValueError):
            continue
        if price is not None:
            entries.append((delivery_day, price, "PVB Price Index"))

    effective_day, price, _, note = choose_target_or_latest(entries, target_day)
    return GasPriceSnapshot(
        target_day=target_day,
        effective_day=effective_day,
        region="西班牙",
        price_eur_mwh=price,
        source_name="MIBGAS PVB",
        source_url=settings.get("source_url", page_url),
        note=note,
    )


def parse_eex_ndi(text: str, target_day: date) -> GasPriceSnapshot:
    rows = csv.DictReader(text.lstrip("\ufeff").splitlines(), delimiter=";")
    for row in rows:
        if str(row.get("Hub", "")).strip().upper() != "TTF":
            continue
        try:
            delivery_day = datetime.strptime(str(row.get("Delivery Day", "")), "%Y-%m-%d").date()
        except ValueError:
            continue
        price = parse_market_number(row.get("Value"))
        unit = str(row.get("Unit", "")).strip()
        if delivery_day == target_day and price is not None and unit == "EUR/MWh":
            return GasPriceSnapshot(
                target_day=target_day,
                effective_day=delivery_day,
                region="欧洲总体",
                price_eur_mwh=price,
                source_name="EEX TTF NDI",
                source_url="https://gasandregistry.eex.com/Gas/NDI/NDI_45_Days.csv",
            )
    raise ValueError(f"EEX TTF NDI returned no value for {target_day.isoformat()}")


def parse_eex_ngp(text: str, target_day: date) -> GasPriceSnapshot:
    rows = csv.DictReader(text.lstrip("\ufeff").splitlines(), delimiter=";")
    entries: list[tuple[date, float, str]] = []
    for row in rows:
        try:
            delivery_day = datetime.strptime(str(row.get("Delivery date", "")), "%Y-%m-%d").date()
        except ValueError:
            continue
        price_value = next(
            (value for key, value in row.items() if str(key).startswith("Index Value")),
            None,
        )
        price = parse_market_number(price_value)
        if price is not None:
            entries.append((delivery_day, price, "TTF NGP"))
    effective_day, price, _, note = choose_target_or_latest(entries, target_day)
    return GasPriceSnapshot(
        target_day=target_day,
        effective_day=effective_day,
        region="欧洲总体",
        price_eur_mwh=price,
        source_name="EEX TTF NGP",
        source_url="https://gasandregistry.eex.com/Gas/NGP/TTF_NGP_60_Days.csv",
        note=note,
    )


def fetch_eex_ttf_gas_price(config: dict[str, Any], target_day: date) -> GasPriceSnapshot | None:
    settings = config.get("eex_ttf_gas_price") or {}
    if not settings.get("enabled", False):
        return None

    ndi_url = settings.get(
        "ndi_url",
        "https://gasandregistry.eex.com/Gas/NDI/NDI_45_Days.csv",
    )
    ngp_url = settings.get(
        "ngp_url",
        "https://gasandregistry.eex.com/Gas/NGP/TTF_NGP_60_Days.csv",
    )
    ndi_error: Exception | None = None
    try:
        snapshot = parse_eex_ndi(
            run_firecrawl_scrape_text(settings, ndi_url, "EEX TTF NDI scrape"),
            target_day,
        )
        snapshot.source_url = settings.get("ndi_source_url", ndi_url)
        return snapshot
    except Exception as exc:
        ndi_error = exc

    try:
        snapshot = parse_eex_ngp(
            run_firecrawl_scrape_text(settings, ngp_url, "EEX TTF NGP scrape"),
            target_day,
        )
        snapshot.source_url = settings.get("ngp_source_url", ngp_url)
        fallback_note = f"EEX TTF NDI 不可用，已回退到 NGP：{ndi_error}"
        snapshot.note = "；".join(part for part in (fallback_note, snapshot.note) if part)
        return snapshot
    except Exception as ngp_error:
        raise RuntimeError(f"EEX TTF NDI failed ({ndi_error}); EEX TTF NGP failed ({ngp_error})") from ngp_error


def fetch_hupx_day_ahead_price(
    config: dict[str, Any], target_day: date
) -> DayAheadMarketSnapshot | None:
    settings = config.get("hupx_day_ahead_prices") or {}
    if not settings.get("enabled", False):
        return None

    page_url = settings.get(
        "page_url",
        "https://labs.hupx.hu/view/DAM_Aggregated_Trading_Data_15min_v1",
    )
    source_url = settings.get("source_url", "https://hupx.hu")
    result = run_firecrawl_interaction(
        settings,
        page_url,
        build_hupx_firecrawl_code(target_day),
        "new-energy-daily-hupx",
        "HUPX day-ahead prices",
    )
    entries = result.get("entries") if isinstance(result.get("entries"), list) else []
    matching_entries = [
        item
        for item in entries
        if str(item.get("Region", "")).upper() == "HU"
        and str(item.get("DeliveryDay", ""))[:10] == target_day.isoformat()
        and str(item.get("Status", "final")).lower() == "final"
        and item.get("Price") is not None
    ]
    prices = [float(item["Price"]) for item in matching_entries]
    if not prices:
        raise ValueError(f"HUPX returned no final Hungarian prices for {target_day.isoformat()}")

    published_average = next(
        (
            float(item["BaseloadPrice"])
            for item in matching_entries
            if item.get("BaseloadPrice") is not None
        ),
        None,
    )
    return DayAheadMarketSnapshot(
        target_day=target_day,
        country="匈牙利",
        market_code="HUPX",
        average_price_eur_mwh=published_average if published_average is not None else sum(prices) / len(prices),
        highest_price_eur_mwh=max(prices),
        lowest_price_eur_mwh=min(prices),
        periods=len(prices),
        source_url=source_url,
    )


def fetch_omie_day_ahead_price(
    config: dict[str, Any], target_day: date
) -> DayAheadMarketSnapshot | None:
    settings = config.get("omie_day_ahead_prices") or {}
    if not settings.get("enabled", False):
        return None

    page_url = settings.get(
        "page_url",
        "https://www.omie.es/en/market-results/daily/daily-market/day-ahead-price",
    )
    source_url = settings.get("source_url", page_url)
    result = run_firecrawl_interaction(
        settings,
        page_url,
        build_omie_firecrawl_code(target_day),
        "new-energy-daily-omie",
        "OMIE day-ahead prices",
    )
    required_fields = ("average", "highest", "lowest", "periods")
    if any(result.get(field) is None for field in required_fields):
        raise ValueError(f"OMIE returned incomplete Spanish prices for {target_day.isoformat()}")
    return DayAheadMarketSnapshot(
        target_day=target_day,
        country="西班牙",
        market_code="OMIE",
        average_price_eur_mwh=float(result["average"]),
        highest_price_eur_mwh=float(result["highest"]),
        lowest_price_eur_mwh=float(result["lowest"]),
        periods=int(result["periods"]),
        source_url=source_url,
    )


def render_gme_price_module(snapshot: GmePriceSnapshot | None, error: str | None = None) -> str:
    lines = ["## 日前电价", "", "### 意大利", ""]
    if snapshot is None:
        lines.extend([f"> 当日电价暂不可用：{error or '未启用数据源'}", ""])
        return "\n".join(lines)

    lines.extend(
        [
            "| 区域 | 日均价格 | 最高价格 | 最低价格 |",
            "|---|---:|---:|---:|",
        ]
    )
    rows = ([snapshot.pun_price] if snapshot.pun_price else []) + snapshot.zones
    lines.extend(
        f"| {item.name}（{item.code}） | {item.average_price_eur_mwh:.2f} EUR/MWh | "
        f"{item.highest_price_eur_mwh:.2f} EUR/MWh | {item.lowest_price_eur_mwh:.2f} EUR/MWh |"
        for item in rows
    )
    lines.extend(
        [
            "",
            f"- 数据源：[Gestore dei Mercati Energetici]({snapshot.source_url})",
        ]
    )
    return "\n".join(lines)


def render_country_day_ahead_module(
    country: str,
    market_code: str,
    source_name: str,
    source_url: str,
    snapshot: DayAheadMarketSnapshot | None,
    error: str | None = None,
) -> str:
    lines = [f"### {country}", ""]
    if snapshot is None:
        lines.extend([f"> 当日电价暂不可用：{error or '未启用数据源'}", ""])
        return "\n".join(lines)

    lines.extend(
        [
            "| 区域 | 日均价格 | 最高价格 | 最低价格 |",
            "|---|---:|---:|---:|",
            f"| {country}（{market_code}） | {snapshot.average_price_eur_mwh:.2f} EUR/MWh | "
            f"{snapshot.highest_price_eur_mwh:.2f} EUR/MWh | "
            f"{snapshot.lowest_price_eur_mwh:.2f} EUR/MWh |",
            "",
            f"- 数据源：[{source_name}]({snapshot.source_url or source_url})",
        ]
    )
    return "\n".join(lines)


def render_gas_price_module(
    rows: list[tuple[str, GasPriceSnapshot | None, str | None, str, str]],
) -> str:
    lines = ["## 天然气价", "", "| 区域 | 天然气价格 |", "|---|---:|"]
    for region, snapshot, _, _, _ in rows:
        value = f"{snapshot.price_eur_mwh:.2f} EUR/MWh" if snapshot else "N/A"
        lines.append(f"| {region} | {value} |")

    sources = []
    for _, snapshot, _, default_source_name, default_source_url in rows:
        source_name = snapshot.source_name if snapshot else default_source_name
        source_url = snapshot.source_url if snapshot else default_source_url
        sources.append(f"[{source_name}]({source_url})")
    lines.extend(["", "- 数据源：<br>"])
    lines.extend(
        f"  {source}{'，<br>' if index < len(sources) - 1 else ''}"
        for index, source in enumerate(sources)
    )
    return "\n".join(lines)


def insert_report_module(report: str, module: str) -> str:
    lines = report.splitlines()
    insert_at = len(lines)
    for index, line in enumerate(lines):
        if line.strip() in {"## 候选概况", "## 抓取异常"}:
            insert_at = index
            break
    updated = [*lines[:insert_at], "", module.strip(), "", *lines[insert_at:]]
    return "\n".join(updated).strip() + "\n"


def render_report_html(report: str) -> str:
    body = render_markdown(report, extensions=["tables", "sane_lists"])
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body {{ margin:0; background:#eef1ee; color:#17221d; font-family:Arial,'Microsoft YaHei',sans-serif; }}
    .shell {{ width:100%; padding:24px 10px; box-sizing:border-box; }}
    .report {{ max-width:680px; margin:0 auto; background:#fff; border:1px solid #d7ded9; padding:28px 32px; box-sizing:border-box; }}
    h1 {{ margin:0 -32px 24px; padding:24px 32px; background:#155d45; color:#fff; font-size:28px; line-height:38px; }}
    h2 {{ margin:28px 0 12px; color:#155d45; font-size:19px; line-height:29px; }}
    h3 {{ margin:24px 0 8px; padding-top:18px; border-top:1px solid #e3e8e5; font-size:18px; line-height:28px; }}
    p, li {{ font-size:14px; line-height:25px; color:#43534b; }}
    blockquote {{ margin:10px 0 14px; padding:10px 14px; border-left:3px solid #2f7b5d; background:#f3f7f4; }}
    blockquote p {{ margin:0; }}
    table {{ width:100%; border-collapse:collapse; margin:12px 0 14px; font-size:13px; }}
    th, td {{ padding:9px 10px; border:1px solid #d9e1dc; text-align:left; }}
    th {{ background:#f1f5f2; color:#28483b; }}
    th:nth-child(2), th:nth-child(3), th:nth-child(4), td:nth-child(2), td:nth-child(3), td:nth-child(4) {{ text-align:right; }}
    a {{ color:#176b4d; }}
    @media (max-width:600px) {{ .shell {{ padding:0; }} .report {{ border:0; padding:22px 18px; }} h1 {{ margin:-22px -18px 22px; padding:22px 18px; font-size:24px; }} }}
  </style>
</head>
<body><div class="shell"><main class="report">{body}</main></div></body>
</html>
"""


def collect_candidates(
    config: dict[str, Any],
    target_day: date,
    tz: ZoneInfo,
    cutoff: time | None = None,
) -> tuple[list[Candidate], list[str]]:
    include_keywords = config.get("include_keywords") or []
    exclude_keywords = config.get("exclude_keywords") or []
    max_items = int(config.get("max_items_per_source", 20))
    fetch_article_text = bool(config.get("fetch_article_text", True))
    candidates: list[Candidate] = []
    errors: list[str] = []
    seen_urls: set[str] = set()
    exa_key_pool: ExaKeyPool | None = None
    cutoff = cutoff or parse_collection_cutoff(
        os.environ.get("COLLECTION_CUTOFF_TIME")
        or str(config.get("collection_cutoff_time") or "12:30")
    )
    window_start, window_end = collection_window(target_day, tz, cutoff)

    headers = {"User-Agent": "new-energy-daily/0.1 (+daily briefing bot)"}
    with httpx.Client(headers=headers, follow_redirects=True, timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        for source in config.get("sources", []):
            if not source.get("enabled", True):
                continue
            try:
                if source.get("type") == "rss":
                    items = fetch_rss(client, source, tz, window_start, window_end, max_items)
                elif source.get("type") == "webpage":
                    items = fetch_webpage(client, source, tz, window_start, window_end, max_items)
                elif source.get("type") == "exa":
                    if exa_key_pool is None:
                        exa_key_pool = ExaKeyPool.from_environment()
                    items = fetch_exa(
                        source,
                        tz,
                        target_day,
                        window_start,
                        window_end,
                        max_items,
                        exa_key_pool,
                    )
                else:
                    errors.append(f"{source.get('name', 'unknown')}: unsupported source type")
                    continue
                for item in items:
                    if fetch_article_text and not item.text:
                        item = enrich_article_text(client, item, source.get("article_selector"))
                    if item.url in seen_urls or not keyword_allowed(item, include_keywords, exclude_keywords):
                        continue
                    seen_urls.add(item.url)
                    candidates.append(item)
            except Exception as exc:
                errors.append(f"{source.get('name', 'unknown')}: {type(exc).__name__}: {exc}")
    return candidates, errors


def openai_client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["AI_API_KEY"],
        base_url=os.environ.get("AI_BASE_URL", "https://api.openai.com/v1"),
        http_client=httpx.Client(timeout=httpx.Timeout(180.0, connect=30.0)),
    )


def chat_completion(client: OpenAI, model: str, messages: list[dict[str, str]], temperature: float, json_object: bool = False) -> str:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if json_object:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception:
        if not json_object:
            raise
        kwargs.pop("response_format", None)
        response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content or ""


def score_candidates(candidates: list[Candidate], model: str) -> list[ScoredItem]:
    if not candidates:
        return []
    client = openai_client()
    compact_items = []
    for idx, candidate in enumerate(candidates, 1):
        compact_items.append(
            {
                "id": idx,
                "title": candidate.title,
                "source": candidate.source,
                "published_at": candidate.published_at,
                "url": candidate.url,
                "summary": candidate.summary,
                "text_excerpt": (candidate.text or "")[:1200],
                "tags": candidate.tags or [],
            }
        )

    prompt = {
        "task": "Evaluate new-energy industry news value and select daily briefing candidates.",
        "rubric": [
            "Policy and regulatory impact",
            "Industry or market impact with concrete data",
            "Major company, financing, M&A, overseas expansion, or project action",
            "Technology value in batteries, storage, solar, wind, hydrogen, charging, grid, VPP, or power markets",
            "Freshness, novelty, and source authority",
            "Penalize ads, soft articles, duplicates, and vague content",
        ],
        "output_schema": {
            "items": [
                {
                    "id": "integer",
                    "score": "integer 0-100",
                    "reason": "Chinese one sentence",
                    "topic": "Chinese short tag",
                    "selected": "boolean",
                }
            ]
        },
        "items": compact_items,
    }
    content = chat_completion(
        client,
        model,
        [
            {"role": "system", "content": "你是新能源产业新闻编辑。只根据输入材料评分，必须输出有效JSON。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
        temperature=0.2,
        json_object=True,
    )
    data = json.loads(content)
    by_id = {idx: candidate for idx, candidate in enumerate(candidates, 1)}
    scored = []
    for item in data.get("items", []):
        candidate = by_id.get(int(item.get("id", 0)))
        if not candidate:
            continue
        scored.append(
            ScoredItem(
                candidate=candidate,
                score=max(0, min(100, int(item.get("score", 0)))),
                reason=str(item.get("reason", "")).strip(),
                topic=str(item.get("topic", "其他")).strip() or "其他",
                selected=bool(item.get("selected", False)),
            )
        )
    known_urls = {item.candidate.url for item in scored}
    for candidate in candidates:
        if candidate.url not in known_urls:
            scored.append(ScoredItem(candidate=candidate, score=0, reason="AI未返回评分", topic="其他", selected=False))
    return sorted(scored, key=lambda item: item.score, reverse=True)


def select_daily_items(
    scored: list[ScoredItem],
    minimum_score: int = 60,
    max_items: int = 15,
) -> list[ScoredItem]:
    return [item for item in scored if item.selected and item.score >= minimum_score][:max_items]


def generate_report(
    scored: list[ScoredItem],
    errors: list[str],
    target_day: date,
    model: str,
    total_candidates: int,
    minimum_score: int = 60,
) -> str:
    selected = select_daily_items(scored, minimum_score=minimum_score)
    if not selected:
        return empty_report(target_day, total_candidates, errors)

    client = openai_client()
    payload = {
        "date": target_day.isoformat(),
        "constraints": [
            "Return valid JSON only",
            "Write one highlights paragraph within 200 Chinese characters",
            "Write a factual Chinese title, a concise 2-4 sentence Chinese summary, and a one-sentence Chinese value judgment for every input item",
            "Keep every item rank unchanged",
            "Do not invent facts",
        ],
        "output_schema": {
            "highlights": "Chinese paragraph within 200 characters",
            "items": [
                {
                    "rank": "integer matching the input rank",
                    "title_zh": "factual Chinese title",
                    "value_judgment_zh": "one Chinese sentence explaining why the item matters",
                    "summary_zh": "2-4 factual Chinese sentences",
                }
            ],
        },
        "items": [
            {
                "rank": idx,
                "score": item.score,
                "title": item.candidate.title,
                "source": item.candidate.source,
                "published_at": item.candidate.published_at,
                "topic": item.topic,
                "reason": item.reason,
                "summary": item.candidate.summary,
                "text_excerpt": (item.candidate.text or "")[:1800],
                "url": item.candidate.url,
            }
            for idx, item in enumerate(selected, 1)
        ],
    }
    content = chat_completion(
        client,
        model,
        [
            {
                "role": "system",
                "content": (
                    "你是新能源产业日报编辑。只根据输入材料撰写事实准确、克制、专业的中文内容。"
                    "必须为每个输入 rank 返回对应项目，不得省略、合并或重排。只输出有效JSON。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0.3,
        json_object=True,
    )
    data = json.loads(content)
    generated_items: dict[int, dict[str, Any]] = {}
    for generated in data.get("items", []):
        try:
            rank = int(generated.get("rank", 0))
        except (TypeError, ValueError):
            continue
        if 1 <= rank <= len(selected) and rank not in generated_items:
            generated_items[rank] = generated

    highlights = clean_text(str(data.get("highlights", "")))
    if not highlights:
        highlights = "；".join(item.reason for item in selected[:4] if item.reason)
    compact_highlights = re.sub(r"\s+", "", highlights)
    if len(compact_highlights) > 200:
        highlights = compact_highlights[:200]

    lines = [
        f"# 新能源日报 - {target_day.isoformat()}",
        "",
        "## 今日看点",
        "",
        highlights,
        "",
        "## 今日精选",
    ]
    for rank, item in enumerate(selected, 1):
        generated = generated_items.get(rank, {})
        title = clean_text(str(generated.get("title_zh", ""))) or item.candidate.title
        summary = clean_text(str(generated.get("summary_zh", "")))
        if not summary:
            summary = clean_text(item.candidate.summary or item.candidate.text or item.candidate.title)[:500]
        source = clean_text(item.candidate.source)
        published_at = format_report_datetime(item.candidate.published_at)
        topic = clean_text(item.topic) or "其他"
        reason = clean_text(str(generated.get("value_judgment_zh", ""))) or clean_text(item.reason)
        if not reason:
            reason = "具备当日新能源行业信息价值。"
        original_title = escape_markdown_link_text(item.candidate.title)
        lines.extend(
            [
                "",
                f"### {rank}. {title}",
                "",
                f"- 来源：{source}",
                f"- 时间：{published_at}",
                f"- 领域：{topic}",
                f"- 价值判断：{reason}",
                f"- 摘要：{summary}",
                f"- 原文：[{original_title}]({item.candidate.url})",
            ]
        )

    sources = "、".join(sorted({clean_text(item.candidate.source) for item in scored})) or "无"
    lines.extend(
        [
            "",
            "## 候选概况",
            "",
            f"- 今日抓取：{total_candidates} 条",
            f"- 去重后：{len(scored)} 条",
            f"- 入选：{len(selected)} 条",
            f"- 主要来源：{sources}",
        ]
    )
    if errors:
        lines.extend(["", "## 抓取异常", ""])
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines).strip() + "\n"


def format_report_datetime(value: str | None) -> str:
    if not value:
        return "时间未知"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M")


def escape_markdown_link_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def enforce_highlights_limit(report: str, model: str) -> str:
    match = re.search(r"(## 今日看点\s*\n+)(.*?)(\n## |\Z)", report, flags=re.S)
    if not match:
        return report
    highlights = match.group(2).strip()
    compact_len = len(re.sub(r"\s+", "", highlights))
    if compact_len <= 200:
        return report

    client = openai_client()
    rewritten = chat_completion(
        client,
        model,
        [
            {"role": "system", "content": "将新能源日报的今日看点压缩到200个中文字符以内，只输出改写后的段落。"},
            {"role": "user", "content": highlights},
        ],
        temperature=0.2,
    ).strip()
    if len(re.sub(r"\s+", "", rewritten)) > 200:
        rewritten = re.sub(r"\s+", "", rewritten)[:200]
    return report[: match.start(2)] + rewritten + report[match.end(2) :]


def empty_report(target_day: date, total_candidates: int, errors: list[str]) -> str:
    lines = [
        f"# 新能源日报 - {target_day.isoformat()}",
        "",
        "## 今日看点",
        "",
        "今日未抓取到足够可靠的新能源新闻，建议检查信源配置或稍后重试。",
        "",
        "## 候选概况",
        "",
        f"- 今日抓取：{total_candidates} 条",
        "- 入选：0 条",
    ]
    if errors:
        lines.extend(["", "## 抓取异常", ""])
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines) + "\n"


def parse_confirmation_token(output: str) -> str | None:
    try:
        data = json.loads(output)
        token = data.get("data", {}).get("confirmation_token") or data.get("confirmation_token")
        if token:
            return str(token)
    except Exception:
        pass
    match = re.search(r"confirmation_token[\"']?\s*[:=]\s*[\"']([^\"'\s,}]+)", output)
    return match.group(1) if match else None


def redact_confirmation_tokens(output: str) -> str:
    return re.sub(
        r"(confirmation_token[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}]+",
        r"\1[REDACTED]",
        output,
        flags=re.IGNORECASE,
    )


def run_agent_mail_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    timeout_seconds = int(os.environ.get("AGENT_MAIL_TIMEOUT_SECONDS", "120"))
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        check=False,
        cwd=PROJECT_ROOT,
        timeout=timeout_seconds,
    )


def load_send_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def save_send_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_file, path)


def send_with_agent_mail(
    subject: str,
    body_file: Path,
    dry_run: bool,
    report_day: date,
    force_send: bool = False,
) -> None:
    if dry_run:
        return
    if is_weekend(report_day):
        print(f"weekend delivery skipped for {report_day.isoformat()}")
        return
    recipients = [item.strip() for item in os.environ.get("AGENT_MAIL_RECIPIENTS", "").split(",") if item.strip()]
    if not recipients:
        return
    report_key = report_day.isoformat()
    state_file = resolve_runtime_path(
        os.environ.get("REPORT_SEND_STATE_FILE", ""),
        DEFAULT_SEND_STATE_FILE,
    )
    send_state = load_send_state(state_file)
    if report_key in send_state and not force_send:
        print(f"email already sent for {report_key}; use --force-send to resend")
        return

    cli = os.environ.get("AGENT_MAIL_CLI", "agently-cli")
    command = [cli, "message", "+send"]
    for recipient in recipients:
        command.extend(["--to", recipient])
    try:
        relative_body_file = body_file.resolve().relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise ValueError("Agent Mail body file must be inside the skill directory") from exc
    command.extend(["--subject", subject, "--body-file", relative_body_file.as_posix()])

    first = run_agent_mail_command(command)
    first_output = "\n".join(part for part in [first.stdout, first.stderr] if part)
    if first.returncode != 0:
        raise RuntimeError(
            f"Agent Mail send failed before confirmation: {redact_confirmation_tokens(first_output).strip()}"
        )

    token = parse_confirmation_token(first.stdout) or parse_confirmation_token(first_output)
    if not token:
        raise RuntimeError(
            f"Agent Mail did not return confirmation_token: {redact_confirmation_tokens(first_output).strip()}"
        )

    confirmed = run_agent_mail_command(command + ["--confirmation-token", token])
    confirmed_output = "\n".join(part for part in [confirmed.stdout, confirmed.stderr] if part)
    if confirmed.returncode != 0:
        raise RuntimeError(
            f"Agent Mail send confirmation failed: {redact_confirmation_tokens(confirmed_output).strip()}"
        )

    send_state[report_key] = {
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "subject": subject,
        "body_sha256": hashlib.sha256(body_file.read_bytes()).hexdigest(),
    }
    save_send_state(state_file, send_state)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate and send a daily new-energy report with Agent Mail.")
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES_FILE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--date", help="Collection-window end date in YYYY-MM-DD.")
    parser.add_argument("--cutoff-time", help="Local collection cutoff in HH:MM. Defaults to 12:30.")
    parser.add_argument("--dry-run", action="store_true", help="Generate report but do not send email.")
    parser.add_argument("--force-send", action="store_true", help="Send again even if this date was already sent.")
    return parser.parse_args()


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = parse_args()
    args.sources = resolve_runtime_path(args.sources, DEFAULT_SOURCES_FILE)
    args.output = resolve_runtime_path(args.output, DEFAULT_OUTPUT_DIR)
    config = load_sources(args.sources)
    tz = ZoneInfo(os.environ.get("REPORT_TIMEZONE") or config.get("timezone") or "Europe/Rome")
    cutoff = parse_collection_cutoff(
        args.cutoff_time
        or os.environ.get("COLLECTION_CUTOFF_TIME")
        or str(config.get("collection_cutoff_time") or "12:30")
    )
    if args.date:
        target_day = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        now = datetime.now(tz)
        target_day = now.date() if now.time() >= cutoff else now.date() - timedelta(days=1)
    model = os.environ.get("AI_MODEL", "gpt-4o-mini")

    market_jobs: dict[str, Any] = {}
    market_fetchers = {
        "gme_zonal_prices": fetch_gme_zonal_prices,
        "gme_gas_price": fetch_gme_gas_price,
        "hupx_day_ahead_prices": fetch_hupx_day_ahead_price,
        "omie_day_ahead_prices": fetch_omie_day_ahead_price,
        "eex_ttf_gas_price": fetch_eex_ttf_gas_price,
        "ceegex_gas_price": fetch_ceegex_gas_price,
        "mibgas_gas_price": fetch_mibgas_gas_price,
    }
    for key, fetcher in market_fetchers.items():
        if (config.get(key) or {}).get("enabled", False):
            market_jobs[key] = fetcher

    market_results: dict[str, Any] = {}
    market_errors: dict[str, str] = {}
    configured_workers = int(
        os.environ.get("MARKET_MAX_WORKERS")
        or config.get("market_max_workers")
        or 2
    )
    max_workers = max(1, min(configured_workers, len(market_jobs) or 1))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_key = {
            executor.submit(run_market_fetch, key, fetcher, config, target_day): key
            for key, fetcher in market_jobs.items()
        }
        for future in concurrent.futures.as_completed(future_to_key):
            key = future_to_key[future]
            try:
                market_results[key] = future.result()
            except Exception as exc:
                market_errors[key] = f"{type(exc).__name__}: {exc}"

    price_snapshot = market_results.get("gme_zonal_prices")
    price_error = market_errors.get("gme_zonal_prices")
    gas_snapshot = market_results.get("gme_gas_price")
    gas_error = market_errors.get("gme_gas_price")
    hupx_snapshot = market_results.get("hupx_day_ahead_prices")
    hupx_error = market_errors.get("hupx_day_ahead_prices")
    omie_snapshot = market_results.get("omie_day_ahead_prices")
    omie_error = market_errors.get("omie_day_ahead_prices")
    eex_gas_snapshot = market_results.get("eex_ttf_gas_price")
    eex_gas_error = market_errors.get("eex_ttf_gas_price")
    ceegex_gas_snapshot = market_results.get("ceegex_gas_price")
    ceegex_gas_error = market_errors.get("ceegex_gas_price")
    mibgas_gas_snapshot = market_results.get("mibgas_gas_price")
    mibgas_gas_error = market_errors.get("mibgas_gas_price")

    window_start, window_end = collection_window(target_day, tz, cutoff)
    print(f"collection window: {window_start.isoformat()} -> {window_end.isoformat()}")
    candidates, errors = collect_candidates(config, target_day, tz, cutoff)
    if price_error:
        errors.append(f"GME zonal prices: {price_error}")
    if gas_error:
        errors.append(f"IG Index GME: {gas_error}")
    if hupx_error:
        errors.append(f"HUPX day-ahead prices: {hupx_error}")
    if omie_error:
        errors.append(f"OMIE day-ahead prices: {omie_error}")
    if eex_gas_error:
        errors.append(f"EEX TTF gas price: {eex_gas_error}")
    if ceegex_gas_error:
        errors.append(f"CEEGEX gas price: {ceegex_gas_error}")
    if mibgas_gas_error:
        errors.append(f"MIBGAS PVB gas price: {mibgas_gas_error}")

    gas_log_items = [
        ("EEX TTF", eex_gas_snapshot, eex_gas_error),
        ("IG Index GME", gas_snapshot, gas_error),
        ("CEEGEX", ceegex_gas_snapshot, ceegex_gas_error),
        ("MIBGAS PVB", mibgas_gas_snapshot, mibgas_gas_error),
    ]
    for label, snapshot, error in gas_log_items:
        if error:
            print(f"market data warning: {label}: {error}", file=sys.stderr)
        elif snapshot and snapshot.note:
            print(f"market data note: {label}: {snapshot.note}", file=sys.stderr)
    scored = score_candidates(candidates, model) if candidates else []
    minimum_news_score = int(config.get("minimum_news_score", 60))
    report = generate_report(
        scored,
        errors,
        target_day,
        model,
        len(candidates),
        minimum_score=minimum_news_score,
    )
    market_modules = []
    day_ahead_modules = []
    if (config.get("gme_zonal_prices") or {}).get("enabled", False):
        day_ahead_modules.append(render_gme_price_module(price_snapshot, price_error))
    if (config.get("hupx_day_ahead_prices") or {}).get("enabled", False):
        if not day_ahead_modules:
            day_ahead_modules.append("## 日前电价")
        settings = config.get("hupx_day_ahead_prices") or {}
        day_ahead_modules.append(
            render_country_day_ahead_module(
                "匈牙利",
                "HUPX",
                "HUPX Hungarian Power Exchange",
                settings.get("source_url", "https://hupx.hu"),
                hupx_snapshot,
                hupx_error,
            )
        )
    if (config.get("omie_day_ahead_prices") or {}).get("enabled", False):
        if not day_ahead_modules:
            day_ahead_modules.append("## 日前电价")
        settings = config.get("omie_day_ahead_prices") or {}
        day_ahead_modules.append(
            render_country_day_ahead_module(
                "西班牙",
                "OMIE",
                "OMIE",
                settings.get(
                    "source_url",
                    "https://www.omie.es/en/market-results/daily/daily-market/day-ahead-price",
                ),
                omie_snapshot,
                omie_error,
            )
        )
    if day_ahead_modules:
        market_modules.append("\n\n".join(day_ahead_modules))
    gas_settings = [
        config.get("eex_ttf_gas_price") or {},
        config.get("gme_gas_price") or {},
        config.get("ceegex_gas_price") or {},
        config.get("mibgas_gas_price") or {},
    ]
    if any(settings.get("enabled", False) for settings in gas_settings):
        eex_settings, gme_gas_settings, ceegex_settings, mibgas_settings = gas_settings
        market_modules.append(
            render_gas_price_module(
                [
                    (
                        "欧洲总体",
                        eex_gas_snapshot,
                        eex_gas_error,
                        "EEX TTF NDI",
                        eex_settings.get(
                            "ndi_source_url",
                            "https://gasandregistry.eex.com/Gas/NDI/NDI_45_Days.csv",
                        ),
                    ),
                    (
                        "意大利",
                        gas_snapshot,
                        gas_error,
                        "Gestore dei Mercati Energetici",
                        gme_gas_settings.get(
                            "page_url",
                            "https://www.mercatoelettrico.org/en-us/Home/Publications/Indexes-GME/IGIndexGmeResults",
                        ),
                    ),
                    (
                        "匈牙利",
                        ceegex_gas_snapshot,
                        ceegex_gas_error,
                        "CEEGEX",
                        ceegex_settings.get(
                            "source_url",
                            "https://ceegex.hu/en/market-data/daily-data",
                        ),
                    ),
                    (
                        "西班牙",
                        mibgas_gas_snapshot,
                        mibgas_gas_error,
                        "MIBGAS PVB",
                        mibgas_settings.get(
                            "source_url",
                            "https://www.mibgas.es/en/market-results/gas-daily-price-index-and-volumes",
                        ),
                    ),
                ]
            )
        )
    if market_modules:
        report = insert_report_module(report, "\n\n".join(market_modules))

    args.output.mkdir(parents=True, exist_ok=True)
    output_file = args.output / f"{target_day.isoformat()}.md"
    output_file.write_text(report, encoding="utf-8")
    html_file = args.output / f"{target_day.isoformat()}.html"
    html_file.write_text(render_report_html(report), encoding="utf-8")

    prefix = os.environ.get("REPORT_SUBJECT_PREFIX", "新能源日报")
    send_with_agent_mail(
        f"{prefix} - {target_day.isoformat()}",
        html_file,
        args.dry_run,
        report_day=target_day,
        force_send=args.force_send,
    )
    print(f"report written: {output_file}")
    print(f"email written: {html_file}")
    print(f"candidates: {len(candidates)}, scored: {len(scored)}, errors: {len(errors)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
