"""Meta Ad Library — Graph API primary (structured JSON), browser scraper fallback."""
from __future__ import annotations

import json
import os
import re
import asyncio
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

import httpx
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from scrapers.result import DataResult

try:
    from scrapling.fetchers import StealthyFetcher as _StealthyFetcher
    _SCRAPLING_AVAILABLE = True
except ImportError:
    _SCRAPLING_AVAILABLE = False

if TYPE_CHECKING:
    from scrapers.search import SearchAgent
    from llm.client import GroqClient

# ── Graph API config ──────────────────────────────────────────────────────────
_GRAPH_BASE    = "https://graph.facebook.com/v20.0"
_AD_ARCHIVE    = f"{_GRAPH_BASE}/ads_archive"
_GRAPH_TOKEN   = os.getenv("META_AD_LIBRARY_TOKEN", "")

_AD_FIELDS = ",".join([
    "id",
    "page_name",
    "ad_creative_bodies",
    "ad_creative_link_captions",
    "ad_creative_link_titles",
    "ad_snapshot_url",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "impressions",
    "spend",
    "publisher_platforms",
])

META_ADS_BASE = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&country=ALL&q={query}&search_type=keyword_unordered"
)
# Instagram-only variant (publisher_platforms filter)
META_ADS_INSTAGRAM = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&country=ALL&q={query}"
    "&search_type=keyword_unordered&publisher_platforms=instagram"
)
META_ADS_MANUAL = "https://www.facebook.com/ads/library/?q={query}"

# Words that indicate a headline is from the wrong advertiser / UI chrome
# (browser names, generic tech terms, navigation labels)
_JUNK_HEADLINE_PATTERNS = re.compile(
    r"\b(browser|api|engine|sdk|runtime|framework|android|chrome|webkit|"
    r"firefox|safari|node\.?js|python|javascript|typescript|react|vue|angular|"
    r"kubernetes|docker|linux|windows|macos|server|database|cloud|aws|azure|"
    r"best selling book|paperback|hardcover|kindle|audiobook|"
    # Generic corporate/business buzzwords — not ad copy
    r"business model|market trend|market share|value proposition|"
    r"supply chain|go.to.market|synergy|paradigm|scalab|blockchain|"
    r"machine learning|artificial intelligence|deep learning|neural network|"
    r"agile|scrum|devops|microservice|saas|paas|iaas|fintech|edtech|"
    r"b2b|b2c|roi|kpi|okr|crm|erp)\b",
    re.IGNORECASE,
)

def _is_mostly_latin(text: str) -> bool:
    """Return True if text is mostly Latin/ASCII — filters Facebook UI chrome rendered
    in the OS locale (Kannada, Devanagari, etc.) that leaks into headline selectors."""
    if not text:
        return True
    non_latin = sum(1 for c in text if ord(c) > 591)  # above Latin Extended-B
    return (non_latin / len(text)) < 0.35


def _filter_headlines(headlines: list[str]) -> list[str]:
    """Apply junk + ad-signal filters to a raw headline list."""
    # Drop non-Latin UI chrome (Facebook rendered in local OS language)
    headlines = [h for h in headlines if _is_mostly_latin(h)]
    cleaned = [h for h in headlines if not _JUNK_HEADLINE_PATTERNS.search(h)]
    _AD_SIGNALS = re.compile(
        r"\b(shop|buy|get|sale|off|free|new|style|wear|look|fit|collection|"
        r"offer|deal|limited|exclusive|premium|quality|best|top|save|now|"
        r"today|launch|brand|fashion|cloth|shirt|dress|pant|jacket|shoe|"
        r"beauty|skin|care|glow|makeup|lipstick|serum|cream|hair|nykaa|"
        r"order|delivery|discount|% off|flat|upto|starting|\d+%|\d+\s*off)\b",
        re.IGNORECASE,
    )
    signal_filtered = [h for h in cleaned if _AD_SIGNALS.search(h)]
    return (signal_filtered or cleaned)[:8]


_AD_CARD_SEL = '[data-testid="ad-archive-render-ad-card"]'
_HEADLINE_SELS = [
    '[data-testid="ad-archive-render-ad-card"] h3',
    '[class*="x1heor9g"]',
    '[class*="_8n_5"]',
    '[class*="AdCard"] h3',
]


async def normalize_headlines_to_english(
    headlines: list[str],
    llm_client: "GroqClient",
) -> list[str]:
    """Translate non-ASCII (regional script) ad headlines to English via LLM.

    Skips the LLM call entirely when all headlines are already ASCII.
    """
    if not headlines:
        return []
    has_non_ascii = any(any(ord(c) > 127 for c in h) for h in headlines)
    if not has_non_ascii:
        return headlines

    prompt = (
        "Translate these ad headlines to English. "
        "Keep them short and natural. If already in English, keep as-is. "
        "Output a JSON array of strings only, same order, same count.\n"
        f"Headlines: {json.dumps(headlines)}"
    )
    try:
        response = await llm_client.analyze(
            system_prompt="You are a translator. Output only a valid JSON array of strings, nothing else.",
            user_content=prompt,
            max_tokens=500,
        )
        translated = json.loads(response)
        if isinstance(translated, list) and len(translated) == len(headlines):
            return [str(t) for t in translated]
    except Exception:
        pass
    # Fallback: keep ASCII headlines, replace regional ones with placeholder
    return [
        h if all(ord(c) < 128 for c in h) else "[Regional language ad]"
        for h in headlines
    ]


def _build_url(brand_name: str, instagram_only: bool = False) -> str:
    base = META_ADS_INSTAGRAM if instagram_only else META_ADS_BASE
    return base.format(query=quote_plus(brand_name))


def _manual_url(brand_name: str) -> str:
    return META_ADS_MANUAL.format(query=quote_plus(brand_name))


def _count_formats(ad_texts: list[str]) -> dict:
    counts = {"video": 0, "image": 0, "carousel": 0}
    for t in ad_texts:
        lower = t.lower()
        if "video" in lower:
            counts["video"] += 1
        elif "carousel" in lower or "multiple" in lower:
            counts["carousel"] += 1
        else:
            counts["image"] += 1
    return counts


async def _parse_meta_ads_soup(
    soup: object,
    brand_name: str,
    source_url: str,
    manual_url: str,
    llm_client: "GroqClient | None",
) -> "DataResult | None":
    """Parse a BeautifulSoup page from Scrapling into a Meta Ads DataResult.

    Returns None when a login wall is detected (signal to fall back to Playwright).
    """
    html = str(soup)

    # Login wall → cannot parse, fall back
    if (
        "royal_login_form" in html
        or "Log in to Facebook" in html
        or "You must log in" in html
    ):
        return None

    # No results
    no_results = (
        'data-testid="no-results"' in html
        or "No results found" in html
        or "no results" in html.lower()
    )
    if no_results:
        return DataResult(
            value={
                "ads_count": 0,
                "status": "not_found",
                "ad_formats": {"video": 0, "image": 0, "carousel": 0},
                "sample_headlines": [],
                "oldest_ad_date": None,
                "newest_ad_date": None,
                "display_message": (
                    f"No Meta ads found for '{brand_name}'. This could mean: "
                    "(a) brand doesn't run Meta ads, (b) ads are paused, or "
                    "(c) the brand name didn't match. Verify manually."
                ),
            },
            source="meta_ad_library",
            source_url=source_url,
            confidence="verified",
            error="Brand not found in Meta Ad Library by this name",
            manual_check_url=manual_url,
        )

    page_text = soup.get_text(separator=" ")  # type: ignore[attr-defined]

    # Ad count
    ads_count: int | str = "unknown"
    for pat in (
        r"(\d[\d,]+)\s+results?\s+found",
        r"(\d[\d,]+)\s+ads?\s+found",
        r"Showing\s+results?\s+for.*?(\d[\d,]+)",
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            ads_count = int(m.group(1).replace(",", ""))
            break

    # Headlines — try ad-card specific selectors first, then broad fallback
    headlines: list[str] = []

    # Priority 1: text inside recognised ad-card containers
    for sel in (
        '[data-testid="ad-archive-render-ad-card"]',
        '[class*="AdCard"]',
        '[class*="ad-card"]',
    ):
        for card in soup.select(sel)[:10]:  # type: ignore[attr-defined]
            for tag in card.find_all(["h3", "span", "p"]):
                text = tag.get_text(strip=True)
                if 8 < len(text) < 120 and text not in headlines:
                    headlines.append(text)
        if headlines:
            break

    # Priority 2: h3 elements anywhere — but filter UI chrome
    if not headlines:
        for el in soup.find_all("h3")[:30]:  # type: ignore[attr-defined]
            text = el.get_text(strip=True)
            if 8 < len(text) < 120 and text not in headlines:
                headlines.append(text)

    # Priority 3: span[dir="auto"] — Facebook uses this for user-generated text
    if not headlines:
        for el in soup.find_all("span", attrs={"dir": "auto"})[:40]:  # type: ignore[attr-defined]
            text = el.get_text(strip=True)
            if 8 < len(text) < 120 and text not in headlines:
                headlines.append(text)

    headlines = _filter_headlines(headlines)
    sample_headlines = headlines[:5]
    if llm_client is not None:
        sample_headlines = await normalize_headlines_to_english(sample_headlines, llm_client)

    ad_texts = re.findall(
        r".{0,50}(video|carousel|image|photo).{0,50}", page_text, re.IGNORECASE
    )
    ad_formats = _count_formats(ad_texts)

    date_pattern = (
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
        r"[a-z]*\s+\d{1,2},?\s+\d{4}\b"
    )
    dates_found = re.findall(date_pattern, page_text)

    if isinstance(ads_count, int) and ads_count == 0:
        return DataResult(
            value={
                "ads_count": 0,
                "status": "found_no_active",
                "historical_ads_present": bool(dates_found),
                "ad_formats": {"video": 0, "image": 0, "carousel": 0},
                "sample_headlines": [],
                "oldest_ad_date": dates_found[-1] if dates_found else None,
                "newest_ad_date": dates_found[0] if dates_found else None,
                "display_message": (
                    "Brand found in Meta Ad Library but has 0 active ads. "
                    "This is either a pause or they don't run paid social."
                ),
            },
            source="meta_ad_library",
            source_url=source_url,
            confidence="verified",
            manual_check_url=manual_url,
        )

    return DataResult(
        value={
            "ads_count": ads_count,
            "status": "found_active",
            "ad_formats": ad_formats,
            "sample_headlines": sample_headlines,
            "oldest_ad_date": dates_found[-1] if dates_found else None,
            "newest_ad_date": dates_found[0] if dates_found else None,
            "instagram_ads_url": _build_url(brand_name, instagram_only=True),
        },
        source="meta_ad_library",
        source_url=source_url,
        confidence="verified",
        manual_check_url=manual_url,
    )


async def _fetch_via_graph_api(brand_name: str, countries: list[str] | None = None) -> DataResult | None:
    """Primary path: Meta Graph API Ad Archive — returns clean JSON, no scraping needed.

    Requires META_AD_LIBRARY_TOKEN in env. Get one free at:
    developers.facebook.com → Tools → Graph API Explorer → Generate Token (no special permissions).

    Returns None if token missing or API call fails, so caller can fall back to scraping.
    """
    if not _GRAPH_TOKEN:
        return None

    params = {
        "access_token":       _GRAPH_TOKEN,
        "search_terms":       brand_name,
        "ad_reached_countries": json.dumps(countries or ["IN", "US", "GB"]),
        "ad_active_status":   "ACTIVE",
        "fields":             _AD_FIELDS,
        "limit":              10,
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(_AD_ARCHIVE, params=params)

        # Rate limit check — x-app-usage: {"call_count": N, ...} where N is % of hourly quota
        usage = {}
        try:
            usage = json.loads(r.headers.get("x-app-usage", "{}"))
        except Exception:
            pass
        call_pct = usage.get("call_count", 0)
        if call_pct >= 80:
            print(f"  [meta_ads] WARNING: API usage at {call_pct}% of hourly quota — slowing down", flush=True)
            await asyncio.sleep(10)
        elif call_pct >= 95:
            print(f"  [meta_ads] CRITICAL: API usage at {call_pct}% — skipping to avoid 429", flush=True)
            return DataResult(
                value=None, source="meta_graph_api",
                confidence="unavailable",
                error=f"Rate limit near-exhausted ({call_pct}% of hourly quota used)",
            )

        if r.status_code == 400:
            err = r.json().get("error", {})
            # Token expired or invalid
            if err.get("code") in (190, 102):
                return DataResult(
                    value=None, source="meta_graph_api",
                    confidence="unavailable",
                    error=f"Token invalid/expired: {err.get('message','')[:100]}",
                )
            # Identity not confirmed for Ad Library API
            if err.get("error_subcode") == 2332002:
                return DataResult(
                    value=None, source="meta_graph_api",
                    confidence="unavailable",
                    error="Ad Library API: account identity not confirmed. Go to facebook.com/ads/library/api/ then regenerate token.",
                )
            return None

        if r.status_code == 429:
            return DataResult(
                value=None, source="meta_graph_api",
                confidence="unavailable",
                error="Meta Graph API rate limited (429) — wait ~1 hour",
            )

        if r.status_code != 200:
            return None

        data = r.json().get("data", [])

    except Exception as exc:
        return DataResult(
            value=None, source="meta_graph_api",
            confidence="unavailable", error=str(exc),
        )

    if not data:
        return DataResult(
            value={
                "ads_count": 0,
                "status": "not_found",
                "ad_formats": {"video": 0, "image": 0, "carousel": 0},
                "sample_headlines": [],
                "ad_creatives": [],
                "oldest_ad_date": None,
                "newest_ad_date": None,
                "display_message": f"No active Meta ads found for '{brand_name}' via Graph API.",
            },
            source="meta_graph_api",
            confidence="verified",
            error="No ads found",
        )

    # ── Extract structured ad data ────────────────────────────────────────────
    ad_creatives: list[dict] = []
    all_bodies:   list[str]  = []
    all_titles:   list[str]  = []
    formats = {"video": 0, "image": 0, "carousel": 0}
    dates:   list[str] = []

    for ad in data:
        bodies   = ad.get("ad_creative_bodies", []) or []
        captions = ad.get("ad_creative_link_captions", []) or []
        titles   = ad.get("ad_creative_link_titles", []) or []
        platforms = ad.get("publisher_platforms", []) or []

        all_bodies.extend(b for b in bodies if b and _is_mostly_latin(b))
        all_titles.extend(t for t in titles if t and _is_mostly_latin(t))

        # Guess format from snapshot URL or platform
        snap = ad.get("ad_snapshot_url", "")
        if "video" in snap.lower():
            formats["video"] += 1
        elif "carousel" in snap.lower():
            formats["carousel"] += 1
        else:
            formats["image"] += 1

        start = ad.get("ad_delivery_start_time", "")
        if start:
            dates.append(start[:10])

        ad_creatives.append({
            "id":           ad.get("id"),
            "page_name":    ad.get("page_name", ""),
            "bodies":       bodies[:3],
            "titles":       titles[:2],
            "captions":     captions[:2],
            "platforms":    platforms,
            "snapshot_url": snap,
            "start_date":   start[:10] if start else None,
        })

    # Deduplicate headlines
    seen: set[str] = set()
    sample_headlines: list[str] = []
    for h in [*all_bodies, *all_titles]:
        h = h.strip()
        if h and h not in seen and 6 < len(h) < 250:
            seen.add(h)
            sample_headlines.append(h)

    dates_sorted = sorted(set(dates), reverse=True)

    return DataResult(
        value={
            "ads_count":       len(data),
            "status":          "found_active",
            "ad_formats":      formats,
            "sample_headlines": sample_headlines[:8],
            "ad_creatives":    ad_creatives,
            "oldest_ad_date":  dates_sorted[-1] if dates_sorted else None,
            "newest_ad_date":  dates_sorted[0]  if dates_sorted else None,
        },
        source="meta_graph_api",
        confidence="verified",
    )


async def get_ads(
    brand_name: str,
    search_agent: "SearchAgent | None" = None,
    llm_client: "GroqClient | None" = None,
    instagram_only: bool = False,
) -> DataResult:
    """Fetch Meta Ad Library data for a brand.

    Attempt order:
      0. Meta Graph API (ads_archive) — structured JSON, no scraping, requires META_AD_LIBRARY_TOKEN
      1. Scrapling StealthyFetcher (solve_cloudflare=True) — browser fallback
      2. Original Playwright — full browser control
      3. DuckDuckGo inference — last resort
    """
    source_url = _build_url(brand_name, instagram_only=instagram_only)
    manual_url = _manual_url(brand_name)

    # ── Attempt 0: Graph API (clean JSON, no browser needed) ────────────────
    api_result = await _fetch_via_graph_api(brand_name)
    if api_result is not None and api_result.value is not None:
        return api_result
    # Token expired/invalid or no value → fall through to browser scraping

    # ── Attempt 1: Scrapling StealthyFetcher ────────────────────────────────
    if _SCRAPLING_AVAILABLE:
        try:
            page = await _StealthyFetcher.fetch(
                source_url,
                headless=True,
                solve_cloudflare=True,
                network_idle=True,
                timeout=90_000,
                wait=1_500,
            )
            if page is not None:
                result = await _parse_meta_ads_soup(
                    page.soup, brand_name, source_url, manual_url, llm_client
                )
                # Only accept if we got actionable data (definitive not_found, or actual headlines/count)
                if result is not None:
                    v = result.value or {}
                    has_data = (
                        v.get("status") in ("not_found", "found_no_active")
                        or (v.get("sample_headlines") and len(v["sample_headlines"]) > 0)
                        or (isinstance(v.get("ads_count"), int))
                    )
                    if has_data:
                        return result
        except Exception:
            pass

    # ── Attempt 2: Original Playwright scraper ───────────────────────────────
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 900},
            )
            page = await context.new_page()

            try:
                await page.goto(source_url, wait_until="domcontentloaded", timeout=30_000)
            except PWTimeout:
                await browser.close()
                return await _ddg_fallback(brand_name, search_agent, source_url, manual_url,
                                           error="Ad Library page load timed out")

            try:
                await page.wait_for_selector(
                    ','.join([
                        _AD_CARD_SEL,
                        '[data-testid="no-results"]',
                        'form[data-testid="royal_login_form"]',
                    ]),
                    timeout=15_000,
                )
            except PWTimeout:
                pass

            html = await page.content()

            # ── Login wall / block ───────────────────────────────────────────
            login_wall = (
                "royal_login_form" in html
                or "Log in to Facebook" in html
                or "You must log in" in html
            )
            if login_wall:
                await browser.close()
                return await _ddg_fallback(brand_name, search_agent, source_url, manual_url,
                                           error="Meta Ad Library scrape blocked — login required")

            # ── No-results page — Case 1: Brand not found ────────────────────
            no_results = (
                'data-testid="no-results"' in html
                or "No results found" in html
                or "no results" in html.lower()
            )
            if no_results:
                await browser.close()
                return DataResult(
                    value={
                        "ads_count": 0,
                        "status": "not_found",
                        "ad_formats": {"video": 0, "image": 0, "carousel": 0},
                        "sample_headlines": [],
                        "oldest_ad_date": None,
                        "newest_ad_date": None,
                        "display_message": (
                            f"No Meta ads found for '{brand_name}'. This could mean: "
                            "(a) brand doesn't run Meta ads, (b) ads are paused, or "
                            "(c) the brand name didn't match. Verify manually."
                        ),
                    },
                    source="meta_ad_library",
                    source_url=source_url,
                    confidence="verified",
                    error=f"Brand not found in Meta Ad Library by this name",
                    manual_check_url=manual_url,
                )

            # ── Ad count from page text ──────────────────────────────────────
            ads_count: int | str = "unknown"
            count_patterns = [
                r"(\d[\d,]+)\s+results?\s+found",
                r"(\d[\d,]+)\s+ads?\s+found",
                r"Showing\s+results?\s+for.*?(\d[\d,]+)",
            ]
            for pat in count_patterns:
                m = re.search(pat, html, re.IGNORECASE)
                if m:
                    ads_count = int(m.group(1).replace(",", ""))
                    break

            # ── Scroll to load more cards ────────────────────────────────────
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, 1200)")
                await asyncio.sleep(1)

            # ── Headlines ────────────────────────────────────────────────────
            headlines: list[str] = []
            for sel in _HEADLINE_SELS:
                try:
                    els = await page.query_selector_all(sel)
                    for el in els[:10]:
                        text = (await el.inner_text()).strip()
                        if text and text not in headlines:
                            headlines.append(text)
                    if headlines:
                        break
                except Exception:
                    continue

            if not headlines:
                try:
                    bold_els = await page.query_selector_all("strong, b")
                    for el in bold_els[:20]:
                        text = (await el.inner_text()).strip()
                        if 5 < len(text) < 120 and text not in headlines:
                            headlines.append(text)
                except Exception:
                    pass

            headlines = _filter_headlines(headlines)
            sample_headlines = headlines[:5]
            if llm_client is not None:
                sample_headlines = await normalize_headlines_to_english(sample_headlines, llm_client)

            page_text = await page.inner_text("body")
            ad_texts = re.findall(
                r".{0,50}(video|carousel|image|photo).{0,50}", page_text, re.IGNORECASE
            )
            ad_formats = _count_formats(ad_texts)

            date_pattern = (
                r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                r"[a-z]*\s+\d{1,2},?\s+\d{4}\b"
            )
            dates_found = re.findall(date_pattern, page_text)
            oldest_ad_date = dates_found[-1] if dates_found else None
            newest_ad_date = dates_found[0] if dates_found else None

            await browser.close()

            # ── Case 3: Brand found but 0 active ads ─────────────────────────
            if isinstance(ads_count, int) and ads_count == 0:
                historical = bool(dates_found)
                return DataResult(
                    value={
                        "ads_count": 0,
                        "status": "found_no_active",
                        "historical_ads_present": historical,
                        "ad_formats": {"video": 0, "image": 0, "carousel": 0},
                        "sample_headlines": [],
                        "oldest_ad_date": oldest_ad_date,
                        "newest_ad_date": newest_ad_date,
                        "display_message": (
                            f"Brand found in Meta Ad Library but has 0 active ads. "
                            "This is either a pause or they don't run paid social."
                        ),
                    },
                    source="meta_ad_library",
                    source_url=source_url,
                    confidence="verified",
                    manual_check_url=manual_url,
                )

            return DataResult(
                value={
                    "ads_count": ads_count,
                    "status": "found_active",
                    "ad_formats": ad_formats,
                    "sample_headlines": sample_headlines,
                    "oldest_ad_date": oldest_ad_date,
                    "newest_ad_date": newest_ad_date,
                },
                source="meta_ad_library",
                source_url=source_url,
                confidence="verified",
                manual_check_url=manual_url,
            )

    except Exception as exc:
        return await _ddg_fallback(brand_name, search_agent, source_url, manual_url,
                                   error=f"Ad Library scrape failed: {exc}")


async def _ddg_fallback(
    brand_name: str,
    search_agent: "SearchAgent | None",
    source_url: str,
    manual_url: str,
    error: str,
) -> DataResult:
    """Multi-query DDG fallback — 3 parallel searches when Meta scraping is blocked.

    Queries:
      1. Meta/Facebook ad library mentions
      2. Campaign press coverage (ad copy often quoted in articles)
      3. YouTube official ads (DDG returns video titles = real creative names)
    """
    if search_agent is None:
        return DataResult(
            value={"ads_count": "unknown", "status": "blocked", "ad_formats": {}, "sample_headlines": []},
            source="meta_ad_library",
            source_url=source_url,
            confidence="unavailable",
            error=error,
            fallback_used=True,
            fallback_method="search_inference",
            manual_check_url=manual_url,
        )

    try:
        queries = [
            f"{brand_name} facebook meta ads campaign active",
            f"{brand_name} advertisement campaign creative 2024 2025",
            f"{brand_name} official ad site:youtube.com",
        ]

        results_list = await asyncio.gather(
            *[asyncio.to_thread(search_agent.search, q, max_results=4) for q in queries],
            return_exceptions=True,
        )

        all_results: list[dict] = []
        for r in results_list:
            if not isinstance(r, Exception):
                all_results.extend(r)

        # Deduplicate by URL
        seen_urls: set[str] = set()
        deduped: list[dict] = []
        for r in all_results:
            url = r.get("url", r.get("href", ""))
            if url not in seen_urls:
                seen_urls.add(url)
                deduped.append(r)

        snippets = " ".join(r.get("snippet", "") for r in deduped)
        ad_mentions = len(re.findall(r"\bad\b|\bads\b|\badvertisement\b|\bcampaign\b", snippets, re.IGNORECASE))
        inferred_count = f"~{ad_mentions * 3}+ (inferred)" if ad_mentions > 2 else "unknown"

        sample_headlines = [
            r.get("title", "")
            for r in deduped[:6]
            if r.get("title") and len(r.get("title", "")) > 10
        ][:5]

        return DataResult(
            value={
                "ads_count": inferred_count,
                "status": "inferred_from_search",
                "ad_formats": {},
                "sample_headlines": sample_headlines,
                "search_snippets": snippets[:1200],
            },
            source="meta_ad_library",
            source_url=source_url,
            confidence="inferred",
            error=f"{error} — data inferred from search results",
            fallback_used=True,
            fallback_method="search_inference",
            manual_check_url=manual_url,
        )
    except Exception as search_exc:
        return DataResult(
            value={"ads_count": "unknown", "status": "blocked", "ad_formats": {}, "sample_headlines": []},
            source="meta_ad_library",
            source_url=source_url,
            confidence="unavailable",
            error=f"{error}; DDG fallback also failed: {search_exc}",
            fallback_used=True,
            fallback_method="search_inference",
            manual_check_url=manual_url,
        )
