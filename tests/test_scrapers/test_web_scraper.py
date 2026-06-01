"""Tests for scrapers/web_scraper.py helper functions — marked slow (Playwright)."""
from __future__ import annotations

import pytest

from scrapers.web_scraper import _detect_platform_from_html, _extract_json_ld, _is_cloudflare_blocked


# ── Pure-function tests (no Playwright, not slow) ──────────────────────────────

def test_detect_shopify_from_cdn_url():
    html = '<script src="https://cdn.shopify.com/s/files/theme.js"></script>'
    assert _detect_platform_from_html(html) == "shopify"


def test_detect_woocommerce():
    html = '<link rel="stylesheet" href="/wp-content/plugins/woocommerce/assets/css/style.css">'
    assert _detect_platform_from_html(html) == "woocommerce"


def test_detect_custom_for_unknown():
    html = "<html><body>Hello world</body></html>"
    assert _detect_platform_from_html(html) == "custom"


def test_extract_json_ld_single():
    html = '''
    <script type="application/ld+json">
    {"@type": "Organization", "name": "Test Brand"}
    </script>
    '''
    result = _extract_json_ld(html)
    assert len(result) == 1
    assert result[0]["@type"] == "Organization"


def test_extract_json_ld_multiple():
    html = '''
    <script type="application/ld+json">{"@type": "WebSite"}</script>
    <script type="application/ld+json">{"@type": "Product", "name": "Shirt"}</script>
    '''
    result = _extract_json_ld(html)
    assert len(result) == 2


def test_extract_json_ld_malformed_skipped():
    html = '''
    <script type="application/ld+json">NOT VALID JSON {{{</script>
    <script type="application/ld+json">{"@type": "Organization"}</script>
    '''
    result = _extract_json_ld(html)
    assert len(result) == 1
    assert result[0]["@type"] == "Organization"


def test_extract_json_ld_empty_html():
    assert _extract_json_ld("") == []
    assert _extract_json_ld("<html><body>No schema</body></html>") == []


def test_cloudflare_blocked_detection():
    cf_html = "Just a moment... Enable JavaScript and cookies to continue"
    cf_headers = {"cf-ray": "abc123", "server": "cloudflare"}
    assert _is_cloudflare_blocked(cf_html, cf_headers) is True


def test_cloudflare_not_blocked_normal_site():
    normal_html = "<html><body>Welcome to our store!</body></html>"
    normal_headers = {"server": "nginx", "content-type": "text/html"}
    assert _is_cloudflare_blocked(normal_html, normal_headers) is False


def test_cloudflare_needs_both_header_and_body():
    """cf-ray header alone is not enough — body must also have CF signals."""
    html_no_signal = "<html><body>Normal page</body></html>"
    cf_header = {"cf-ray": "abc123"}
    assert _is_cloudflare_blocked(html_no_signal, cf_header) is False


# ── Integration tests — require Playwright (CI skips these) ───────────────────

@pytest.mark.slow
@pytest.mark.asyncio
async def test_scrape_page_real_url():
    """Smoke test against a real URL — only runs locally."""
    from scrapers.web_scraper import WebScraper
    scraper = WebScraper()
    result = await scraper.scrape_page("https://example.com")
    assert result.value is not None, f"scrape_page returned no value: {result.error}"
    assert "title" in result.value
    assert "body_text" in result.value


@pytest.mark.slow
@pytest.mark.asyncio
async def test_detect_platform_real_url():
    """Detect platform for a known Shopify store — only runs locally."""
    from scrapers.web_scraper import WebScraper
    scraper = WebScraper()
    platform = await scraper.detect_platform("https://rarerabbit.in")
    assert platform in ("shopify", "custom")
