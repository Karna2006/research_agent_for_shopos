"""Playwright-based scraper — renders JS pages and returns DataResult."""
from __future__ import annotations

import json
import re
import asyncio
from typing import Any

import httpx
from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PWTimeout

from scrapers.result import DataResult

_CLOUDFLARE_SIGNALS = [
    "cf-ray",
    "cloudflare",
    "Just a moment",
    "Enable JavaScript and cookies",
    "cf_clearance",
    "Checking your browser",
]

_PLATFORM_PATTERNS = {
    "shopify": [
        r"cdn\.shopify\.com",
        r"Shopify\.theme",
        r"shopify-features",
        r'"platform":"shopify"',
        r"myshopify\.com",
    ],
    "woocommerce": [
        r"woocommerce",
        r"wp-content/plugins/woo",
        r'"@type":"Product".*woocommerce',
        r"wc-block",
    ],
}

# Realistic browser headers used in the httpx Cloudflare fallback
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _is_cloudflare_blocked(html: str, headers: dict, status: int = 200) -> bool:
    header_keys = " ".join(str(k) for k in headers.keys()).lower()
    header_vals = " ".join(str(v) for v in headers.values()).lower()
    # Status-based: 403/429 + CF fingerprint in headers
    if status in (403, 429) and ("cf-ray" in header_keys or "cloudflare" in header_vals):
        return True
    # Content-based: CF-Ray header + at least one body signal
    if "cf-ray" in header_keys:
        for signal in _CLOUDFLARE_SIGNALS:
            if signal.lower() in html.lower():
                return True
    # Generic "cloudflare" in body text with a block signal
    if "cloudflare" in html.lower():
        for signal in ("Just a moment", "Enable JavaScript", "Checking your browser"):
            if signal.lower() in html.lower():
                return True
    return False


def _detect_platform_from_html(html: str) -> str:
    for platform, patterns in _PLATFORM_PATTERNS.items():
        if any(re.search(p, html, re.IGNORECASE) for p in patterns):
            return platform
    return "custom"


def _extract_json_ld(html: str) -> list[dict]:
    results = []
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        try:
            results.append(json.loads(match.group(1).strip()))
        except (json.JSONDecodeError, ValueError):
            pass
    return results


async def _safe_text(page: Page, selector: str) -> str:
    try:
        el = await page.query_selector(selector)
        return (await el.inner_text()).strip() if el else ""
    except Exception:
        return ""


async def _safe_attr(page: Page, selector: str, attr: str) -> str:
    try:
        el = await page.query_selector(selector)
        return (await el.get_attribute(attr) or "").strip() if el else ""
    except Exception:
        return ""


async def _safe_all_text(page: Page, selector: str, limit: int = 20) -> list[str]:
    try:
        els = await page.query_selector_all(selector)
        texts = []
        for el in els[:limit]:
            t = (await el.inner_text()).strip()
            if t:
                texts.append(t)
        return texts
    except Exception:
        return []


async def _safe_all_attr(page: Page, selector: str, attr: str, limit: int = 20) -> list[str]:
    try:
        els = await page.query_selector_all(selector)
        values = []
        for el in els[:limit]:
            v = (await el.get_attribute(attr) or "").strip()
            if v:
                values.append(v)
        return values
    except Exception:
        return []


async def _httpx_fallback(url: str) -> dict[str, Any] | None:
    """Lightweight httpx scrape used when Playwright is blocked by Cloudflare.

    Strips tags to extract body text; no JS execution.
    Returns a page-like dict or None if also blocked/failed.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url, headers=_BROWSER_HEADERS)
            if r.status_code >= 500:
                return None
            html = r.text
            title_m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
            meta_m = re.search(
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']',
                html, re.IGNORECASE,
            )
            body_text = re.sub(r'<[^>]+>', ' ', html)
            body_text = re.sub(r'\s{3,}', '\n\n', body_text).strip()
            links = re.findall(r'href=["\']([^"\'#][^"\']*)["\']', html)[:50]
            return {
                "url": url,
                "blocked": False,
                "title": (title_m.group(1).strip() if title_m else ""),
                "meta_description": (meta_m.group(1) if meta_m else ""),
                "body_text": body_text[:8000],
                "headings": [],
                "images": [],
                "links": links,
                "schema_json_ld": _extract_json_ld(html),
                "page_html": html[:50000],
            }
    except Exception:
        return None


class WebScraper:
    def __init__(self, headless: bool = True, timeout_ms: int = 30_000):
        self._headless = headless
        self._timeout = timeout_ms

    async def _get_page(
        self, browser: Browser, url: str
    ) -> tuple[Page, dict, str, int]:
        """Navigate to URL and return (page, response_headers, html, status_code)."""
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        headers: dict[str, str] = {}
        status: int = 200
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=self._timeout
            )
            if response:
                headers = dict(response.headers)
                status = response.status
            # Wait for JS-heavy SPAs to settle
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            # Extra wait if body text is still sparse after load
            try:
                body_check = await page.inner_text("body")
                if len(body_check.strip()) < 200:
                    await page.wait_for_timeout(3000)
            except Exception:
                pass
        except PWTimeout:
            pass
        html = await page.content()
        return page, headers, html, status

    async def scrape_page(self, url: str) -> DataResult:
        """General page scrape → DataResult wrapping title, meta, headings, body, schema."""
        source = "homepage_scrape"
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=self._headless)
                try:
                    page, headers, html, status = await self._get_page(browser, url)

                    # Site is returning server errors
                    if status >= 500:
                        return DataResult(
                            value=None,
                            source=source,
                            source_url=url,
                            confidence="unavailable",
                            error="Site appears to be down or unreachable",
                            manual_check_url=url,
                        )

                    if _is_cloudflare_blocked(html, headers, status):
                        fallback_data = await _httpx_fallback(url)
                        if fallback_data:
                            return DataResult(
                                value=fallback_data,
                                source=source,
                                source_url=url,
                                confidence="inferred",
                                fallback_used=True,
                                fallback_method="httpx_static",
                            )
                        return DataResult(
                            value=None,
                            source=source,
                            source_url=url,
                            confidence="unavailable",
                            error="Site protected by Cloudflare — scrape blocked",
                            fallback_used=True,
                            fallback_method="search_only",
                            manual_check_url=url,
                        )

                    title = await _safe_text(page, "title")
                    meta_description = await _safe_attr(page, 'meta[name="description"]', "content")

                    headings: list[str] = []
                    for tag in ("h1", "h2", "h3"):
                        headings += await _safe_all_text(page, tag, limit=10)

                    images = await _safe_all_attr(page, "img[src]", "src", limit=30)
                    links = await _safe_all_attr(page, "a[href]", "href", limit=50)

                    body_text = ""
                    try:
                        body_text = await page.inner_text("body")
                        body_text = re.sub(r"\s{3,}", "\n\n", body_text).strip()
                    except Exception:
                        pass

                    return DataResult(
                        value={
                            "url": url,
                            "blocked": False,
                            "title": title,
                            "meta_description": meta_description,
                            "body_text": body_text[:8000],
                            "headings": headings,
                            "images": images,
                            "links": links,
                            "schema_json_ld": _extract_json_ld(html),
                            "page_html": html[:50000],
                        },
                        source=source,
                        source_url=url,
                        confidence="verified",
                    )
                finally:
                    try:
                        await browser.close()
                    except Exception:
                        pass

        except (OSError, ConnectionError) as exc:
            return DataResult(
                value=None,
                source=source,
                source_url=url,
                confidence="unavailable",
                error=f"Site appears to be down or unreachable: {exc}",
                manual_check_url=url,
            )
        except Exception as exc:
            return DataResult(
                value=None,
                source=source,
                source_url=url,
                confidence="unavailable",
                error=str(exc),
            )

    async def scrape_pdp(self, url: str) -> DataResult:
        """Product detail page scrape → DataResult wrapping name, price, description, reviews."""
        source = "pdp_scrape"
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=self._headless)
                try:
                    page, headers, html, status = await self._get_page(browser, url)

                    if status >= 500:
                        return DataResult(
                            value=None,
                            source=source,
                            source_url=url,
                            confidence="unavailable",
                            error="Product page unreachable",
                        )

                    if _is_cloudflare_blocked(html, headers, status):
                        return DataResult(
                            value=None,
                            source=source,
                            source_url=url,
                            confidence="unavailable",
                            error="Product page blocked by Cloudflare",
                            fallback_used=True,
                            fallback_method="search_only",
                        )

                    # Try JSON-LD Product schema first
                    schemas = _extract_json_ld(html)
                    product_schema: dict = {}
                    for s in schemas:
                        if isinstance(s, dict) and s.get("@type") == "Product":
                            product_schema = s
                            break
                        if isinstance(s, list):
                            for item in s:
                                if isinstance(item, dict) and item.get("@type") == "Product":
                                    product_schema = item
                                    break

                    product_name = (
                        product_schema.get("name")
                        or await _safe_text(page, "h1")
                        or await _safe_text(page, '[class*="product-title"]')
                        or await _safe_text(page, '[class*="product_title"]')
                    )

                    price_raw = ""
                    if "offers" in product_schema:
                        offers = product_schema["offers"]
                        if isinstance(offers, dict):
                            price_raw = str(offers.get("price", ""))
                        elif isinstance(offers, list) and offers:
                            price_raw = str(offers[0].get("price", ""))
                    if not price_raw:
                        for sel in (
                            '[class*="price"]:not([class*="compare"])',
                            ".price", "#price", '[data-price]', '.product-price',
                        ):
                            price_raw = await _safe_text(page, sel)
                            if price_raw:
                                break

                    description = (
                        product_schema.get("description")
                        or await _safe_text(page, '[class*="product-description"]')
                        or await _safe_text(page, '[class*="description"]')
                        or await _safe_text(page, "#description")
                    )

                    images = await _safe_all_attr(page, '[class*="product"] img[src]', "src", limit=10)
                    if not images:
                        images = await _safe_all_attr(page, "img[src]", "src", limit=10)

                    rating_raw = product_schema.get("aggregateRating", {})
                    rating = str(rating_raw.get("ratingValue", "")) if isinstance(rating_raw, dict) else ""
                    reviews_count_raw = str(rating_raw.get("reviewCount", "")) if isinstance(rating_raw, dict) else ""
                    if not rating:
                        rating = await _safe_text(page, '[class*="rating"]')
                    if not reviews_count_raw:
                        reviews_count_raw = await _safe_text(page, '[class*="review-count"]')

                    in_stock = True
                    availability = ""
                    if "offers" in product_schema:
                        offers = product_schema["offers"]
                        if isinstance(offers, dict):
                            availability = offers.get("availability", "")
                        elif isinstance(offers, list) and offers:
                            availability = offers[0].get("availability", "")
                    if "OutOfStock" in availability:
                        in_stock = False

                    cta_text = ""
                    for sel in (
                        'button[name="add"]', 'button[class*="add-to-cart"]',
                        'button[class*="addToCart"]', 'button[class*="buy"]',
                        'input[name="add"]', 'button[type="submit"]',
                    ):
                        cta_text = await _safe_text(page, sel)
                        if cta_text:
                            break

                    return DataResult(
                        value={
                            "url": url,
                            "blocked": False,
                            "product_name": product_name.strip() if product_name else "",
                            "price": price_raw.strip(),
                            "description": (description or "")[:2000].strip(),
                            "images": images,
                            "reviews_count": reviews_count_raw.strip(),
                            "rating": rating.strip(),
                            "in_stock": in_stock,
                            "cta_text": cta_text.strip(),
                        },
                        source=source,
                        source_url=url,
                        confidence="verified",
                    )
                finally:
                    try:
                        await browser.close()
                    except Exception:
                        pass

        except Exception as exc:
            return DataResult(
                value=None,
                source=source,
                source_url=url,
                confidence="unavailable",
                error=str(exc),
            )

    async def detect_platform(self, url: str) -> str:
        """Return 'shopify' | 'woocommerce' | 'custom' by inspecting headers + HTML.

        Fast path: check /products.json and response headers via httpx before launching
        Playwright — saves ~3s on Shopify stores (the majority of our users).
        """
        from urllib.parse import urlparse as _up
        base = f"{_up(url).scheme}://{_up(url).netloc}"

        # ── Fast httpx pre-check (no Playwright needed for Shopify) ──────────
        try:
            async with httpx.AsyncClient(
                timeout=5.0, follow_redirects=True, headers=_BROWSER_HEADERS
            ) as client:
                # /products.json is Shopify-exclusive — returns JSON on any store
                r = await client.get(f"{base}/products.json?limit=1")
                if r.status_code == 200:
                    try:
                        data = r.json()
                        if "products" in data:
                            return "shopify"
                    except Exception:
                        pass
                # Also check response headers for Shopify fingerprint
                for h_key, h_val in r.headers.items():
                    if "shopify" in h_key.lower() or "shopify" in str(h_val).lower():
                        return "shopify"
        except Exception:
            pass

        # ── Full Playwright detection for WooCommerce / custom ────────────────
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=self._headless)
                try:
                    page, headers, html, _status = await self._get_page(browser, url)
                    for h_key, h_val in headers.items():
                        if "shopify" in h_key.lower() or "shopify" in str(h_val).lower():
                            return "shopify"
                    return _detect_platform_from_html(html)
                finally:
                    try:
                        await browser.close()
                    except Exception:
                        pass
        except Exception:
            return "custom"
