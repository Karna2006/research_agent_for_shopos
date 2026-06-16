"""Web scraper — Scrapling primary, Playwright fallback, returns DataResult."""
from __future__ import annotations

import json
import re
import asyncio
from typing import Any

import httpx
from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PWTimeout

from scrapers.result import DataResult

try:
    from scrapling.fetchers import StealthyFetcher, DynamicFetcher
    _SCRAPLING_AVAILABLE = True
except ImportError:
    _SCRAPLING_AVAILABLE = False

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


def _parse_html_to_dict(url: str, html: str) -> dict[str, Any]:
    """Extract text, title, links from raw HTML — shared by all fallback paths."""
    title_m = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    meta_m  = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']',
        html, re.IGNORECASE,
    )
    clean = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    clean = re.sub(r'<script[^>]*>.*?</script>', ' ', clean, flags=re.DOTALL | re.IGNORECASE)
    body_text = re.sub(r'<[^>]+>', ' ', clean)
    body_text = re.sub(r'\s{3,}', '\n\n', body_text).strip()
    links = re.findall(r'href=["\']([^"\'#][^"\']*)["\']', html)[:50]
    # Extract h1/h2/h3
    headings = re.findall(r'<h[1-3][^>]*>\s*(.*?)\s*</h[1-3]>', html, re.IGNORECASE | re.DOTALL)
    headings = [re.sub(r'<[^>]+>', '', h).strip() for h in headings if h.strip()][:10]
    return {
        "url":              url,
        "blocked":          False,
        "title":            (title_m.group(1).strip() if title_m else ""),
        "meta_description": (meta_m.group(1) if meta_m else ""),
        "body_text":        body_text[:8000],
        "headings":         headings,
        "images":           [],
        "links":            links,
        "schema_json_ld":   _extract_json_ld(html),
        "page_html":        html[:50000],
    }


async def _cffi_fallback(url: str) -> dict[str, Any] | None:
    """curl-cffi fallback — impersonates Chrome TLS fingerprint, bypasses Akamai/Cloudflare WAF.

    Used for sites like Nykaa that block both Playwright (HTTP/2 error) and plain httpx (403).
    curl_cffi is already in requirements.txt (curl-cffi>=0.7.0).
    """
    try:
        from curl_cffi.requests import AsyncSession
        async with AsyncSession(impersonate="chrome124") as session:
            r = await session.get(url, timeout=20, allow_redirects=True)
            if r.status_code >= 500:
                return None
            html = r.text
            return _parse_html_to_dict(url, html)
    except Exception:
        return None


async def _httpx_fallback(url: str) -> dict[str, Any] | None:
    """Plain httpx fallback (HTTP/1.1) — used for Cloudflare-blocked sites that allow static fetch.

    Returns a page-like dict or None if also blocked/failed.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url, headers=_BROWSER_HEADERS)
            if r.status_code >= 500:
                return None
            if r.status_code == 403:
                # 403 from WAF → try curl-cffi with real TLS fingerprint
                return await _cffi_fallback(url)
            return _parse_html_to_dict(url, r.text)
    except Exception:
        return None


_SCRAPLING_TIMEOUT = 90_000   # 90 s — quality over latency
_SCRAPLING_WAIT    = 1_500    # extra ms after network-idle before returning

class WebScraper:
    def __init__(self, headless: bool = True, timeout_ms: int = 60_000):
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

    def _parse_page(self, soup: Any, url: str, source: str) -> DataResult:
        """Parse a Scrapling/BeautifulSoup page object into a DataResult."""
        html = str(soup)

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        meta_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.IGNORECASE)})
        meta_description = (meta_tag.get("content") or "").strip() if meta_tag else ""

        headings: list[str] = []
        for tag in ("h1", "h2", "h3"):
            for el in soup.find_all(tag)[:10]:
                text = el.get_text(strip=True)
                if text:
                    headings.append(text)

        images = [
            img.get("src", "").strip()
            for img in soup.find_all("img", src=True)
            if img.get("src", "").strip()
        ][:30]

        links = [
            a.get("href", "").strip()
            for a in soup.find_all("a", href=True)
            if a.get("href", "").strip()
        ][:50]

        # Remove <style>/<script> nodes before text extraction — Shopify sites inject
        # large CSS blocks directly in <body>, which pollute body_text otherwise.
        try:
            for el in soup.find_all(["style", "script"]):
                el.decompose()
        except Exception:
            pass
        body_text = soup.get_text(separator=" ")
        body_text = re.sub(r"\s{3,}", "\n\n", body_text).strip()

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

    async def scrape_page(self, url: str) -> DataResult:
        """General page scrape → DataResult.

        Attempt order:
          1. Scrapling StealthyFetcher  (stealth + Cloudflare-bypass, 90s)
          2. Scrapling DynamicFetcher   (full JS rendering, 90s)
          3. Playwright fallback        (full control, 60s)
        """
        if _SCRAPLING_AVAILABLE:
            # Attempt 1: StealthyFetcher — stealth UA + Cloudflare solve + network idle
            try:
                page = await StealthyFetcher.fetch(
                    url,
                    headless=True,
                    network_idle=True,
                    timeout=_SCRAPLING_TIMEOUT,
                    wait=_SCRAPLING_WAIT,
                    solve_cloudflare=True,
                    google_search=True,
                )
                if page and page.status == 200:
                    return self._parse_page(page.soup, url, "scrapling_stealth")
            except Exception:
                pass

            # Attempt 2: DynamicFetcher — full JS render, waits for network idle
            try:
                page = await DynamicFetcher.fetch(
                    url,
                    headless=True,
                    network_idle=True,
                    timeout=_SCRAPLING_TIMEOUT,
                    wait=_SCRAPLING_WAIT,
                )
                if page:
                    return self._parse_page(page.soup, url, "scrapling_dynamic")
            except Exception:
                pass

        # Attempt 3: Playwright fallback
        return await self._playwright_scrape(url)

    async def _playwright_scrape(self, url: str) -> DataResult:
        """Original Playwright-based scrape — used when Scrapling is unavailable or fails."""
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
            err_str = str(exc)
            # HTTP/2 protocol errors from aggressive WAFs (Nykaa, Myntra, Ajio, Purplle)
            # — retry with curl-cffi which mimics real Chrome TLS fingerprint
            if "HTTP2_PROTOCOL_ERROR" in err_str or "ERR_HTTP2" in err_str:
                cffi_data = await _cffi_fallback(url)
                if cffi_data:
                    return DataResult(
                        value=cffi_data,
                        source=source,
                        source_url=url,
                        confidence="inferred",
                        fallback_used=True,
                        fallback_method="cffi_chrome124",
                    )
            return DataResult(
                value=None,
                source=source,
                source_url=url,
                confidence="unavailable",
                error=err_str,
            )

    def _parse_pdp_from_soup(self, soup: Any, url: str) -> DataResult:
        """Parse a Scrapling soup object into a PDP DataResult.

        Extracts product name, price, description, images, reviews from JSON-LD
        and common CSS selectors — no Playwright page object needed.
        """
        html = str(soup)
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
            or (soup.find("h1") or {}).get_text(strip=True) if hasattr(soup, "find") else ""
        )

        price_raw = ""
        offers = product_schema.get("offers")
        if isinstance(offers, dict):
            price_raw = str(offers.get("price", ""))
        elif isinstance(offers, list) and offers:
            price_raw = str(offers[0].get("price", ""))
        if not price_raw and hasattr(soup, "select"):
            for sel in ('[class*="price"]', ".price", "#price", ".product-price"):
                els = soup.select(sel)
                if els:
                    price_raw = els[0].get_text(strip=True)
                    break

        description = product_schema.get("description", "")
        if not description and hasattr(soup, "select"):
            for sel in ('[class*="description"]', "#description", '[class*="product-desc"]'):
                els = soup.select(sel)
                if els:
                    description = els[0].get_text(separator=" ", strip=True)
                    break

        images: list[str] = []
        if hasattr(soup, "find_all"):
            for img in soup.find_all("img", src=True)[:15]:
                src = img.get("src", "").strip()
                if src and "product" in src.lower():
                    images.append(src)
            if not images:
                images = [img.get("src", "").strip() for img in soup.find_all("img", src=True)[:10] if img.get("src")]

        rating_raw = product_schema.get("aggregateRating", {})
        rating = str(rating_raw.get("ratingValue", "")) if isinstance(rating_raw, dict) else ""
        reviews_count = str(rating_raw.get("reviewCount", "")) if isinstance(rating_raw, dict) else ""

        availability = ""
        in_stock = True
        if isinstance(offers, dict):
            availability = offers.get("availability", "")
        elif isinstance(offers, list) and offers:
            availability = offers[0].get("availability", "")
        if "OutOfStock" in availability:
            in_stock = False

        return DataResult(
            value={
                "url": url,
                "blocked": False,
                "product_name": (product_name or "").strip(),
                "price": price_raw.strip(),
                "description": (description or "")[:2000].strip(),
                "images": images,
                "reviews_count": reviews_count.strip(),
                "rating": rating.strip(),
                "in_stock": in_stock,
                "cta_text": "",
            },
            source="pdp_scrape",
            source_url=url,
            confidence="verified",
        )

    async def scrape_pdp(self, url: str) -> DataResult:
        """Product detail page scrape → DataResult wrapping name, price, description, reviews.

        Attempt order:
          1. Scrapling StealthyFetcher  (stealth + Cloudflare, 90s)
          2. Scrapling DynamicFetcher   (full JS, 90s)
          3. Playwright                 (fallback, 60s)
        """
        source = "pdp_scrape"

        # ── Attempt 1: Scrapling StealthyFetcher ────────────────────────────
        if _SCRAPLING_AVAILABLE:
            try:
                page = await StealthyFetcher.fetch(
                    url,
                    headless=True,
                    network_idle=True,
                    timeout=_SCRAPLING_TIMEOUT,
                    wait=_SCRAPLING_WAIT,
                    solve_cloudflare=True,
                    google_search=True,
                )
                if page and page.status == 200:
                    result = self._parse_pdp_from_soup(page.soup, url)
                    if result.value and result.value.get("product_name"):
                        result.source = "pdp_scrape_stealth"
                        return result
            except Exception:
                pass

            # ── Attempt 2: Scrapling DynamicFetcher ─────────────────────────
            try:
                page = await DynamicFetcher.fetch(
                    url,
                    headless=True,
                    network_idle=True,
                    timeout=_SCRAPLING_TIMEOUT,
                    wait=_SCRAPLING_WAIT,
                )
                if page:
                    result = self._parse_pdp_from_soup(page.soup, url)
                    if result.value and result.value.get("product_name"):
                        result.source = "pdp_scrape_dynamic"
                        return result
            except Exception:
                pass

        # ── Attempt 3: Playwright ────────────────────────────────────────────
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

        return DataResult(
            value=None,
            source=source,
            source_url=url,
            confidence="unavailable",
            error="All scrape attempts failed",
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
