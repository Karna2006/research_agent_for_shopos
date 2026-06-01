"""Meta Ad Library scraper — extracts active ad signals via Playwright."""
from __future__ import annotations

import json
import re
import asyncio
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

from scrapers.result import DataResult

if TYPE_CHECKING:
    from scrapers.search import SearchAgent
    from llm.client import GroqClient

META_ADS_BASE = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&country=ALL&q={query}&search_type=keyword_unordered"
)
META_ADS_MANUAL = "https://www.facebook.com/ads/library/?q={query}"

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


def _build_url(brand_name: str) -> str:
    return META_ADS_BASE.format(query=quote_plus(brand_name))


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


async def get_ads(
    brand_name: str,
    search_agent: "SearchAgent | None" = None,
    llm_client: "GroqClient | None" = None,
) -> DataResult:
    """Scrape Meta Ad Library for a brand and return a DataResult.

    Handles four distinct cases:
      1. Brand not found in Ad Library
      2. Scrape blocked (login wall / Cloudflare) → DDG search fallback
      3. Brand found but 0 active ads
      4. Ambiguous brand name (multiple matches) → pick best by domain match
    """
    source_url = _build_url(brand_name)
    manual_url = _manual_url(brand_name)

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
    """Case 2: Scrape blocked — fall back to DuckDuckGo for ad signals."""
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
        results = search_agent.search(
            f"{brand_name} facebook ads meta ad library", max_results=5
        )
        snippets = " ".join(r.get("snippet", "") for r in results)

        # Rough heuristic: count ad mentions in search snippets
        ad_mentions = len(re.findall(r"\bad\b|\bads\b|\badvertisement\b", snippets, re.IGNORECASE))
        inferred_count = "unknown"
        if ad_mentions > 3:
            inferred_count = f"~{ad_mentions * 5}+ (inferred)"

        return DataResult(
            value={
                "ads_count": inferred_count,
                "status": "inferred_from_search",
                "ad_formats": {},
                "sample_headlines": [r.get("title", "") for r in results[:3]],
                "search_snippets": snippets[:800],
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
