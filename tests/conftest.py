"""Shared pytest fixtures — all tests import from here."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ── Point to in-memory SQLite before any app import touches the DB ─────────────
os.environ.setdefault("DATABASE_URL", "")           # force SQLite path
os.environ.setdefault("GROQ_API_KEY", "test-key")   # silence GroqClient.__init__
os.environ.setdefault("GEMINI_API_KEY", "test-key")  # kept for compatibility

# ── Paths ──────────────────────────────────────────────────────────────────────
_DEMO_DIR = Path(__file__).parent.parent / "demo"


# ── Demo data ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def sample_audit_data() -> dict:
    """Full audit dict loaded from demo/rare_rabbit_audit.json."""
    return json.loads((_DEMO_DIR / "rare_rabbit_audit.json").read_text())


@pytest.fixture(scope="session")
def sample_virality_data() -> dict:
    """Virality scores dict loaded from demo/virality_scores.json."""
    return json.loads((_DEMO_DIR / "virality_scores.json").read_text())


# ── LLM fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def mock_groq_json() -> dict:
    """A minimal brand-basics response that passes JSON parsing."""
    return {
        "brand_name": "Test Brand",
        "founding_year": 2020,
        "founders": ["Alice", "Bob"],
        "brand_story": "Test story.",
        "positioning": "Premium casual.",
        "hero_products": ["Shirt"],
        "price_range": "₹999–₹3,999",
        "revenue_estimate": "₹50 Cr",
        "funding_stage": "bootstrapped",
        "key_strengths": ["Quality fabric"],
        "key_weaknesses": ["Limited SKUs"],
        "omnichannel_presence": "online-only",
        "social_platforms": ["Instagram"],
    }


@pytest.fixture
def mock_llm(mock_groq_json):
    """AsyncMock LLM client whose analyze_structured returns mock_groq_json."""
    client = AsyncMock()
    client.analyze_structured = AsyncMock(return_value=mock_groq_json)
    client.analyze = AsyncMock(return_value=json.dumps(mock_groq_json))
    return client


# ── Scraper fixtures ─────────────────────────────────────────────────────────
# Agents now receive DataResult objects from scrapers.  The fixtures below wrap
# the raw page-data dicts in DataResult so agents can call .value and .ok.

from scrapers.result import DataResult  # noqa: E402  (after env setup)

_HOMEPAGE_DATA = {
    "url": "https://testbrand.in",
    "title": "Test Brand — Premium Casual Wear",
    "meta_description": "Shop premium clothing.",
    "headings": ["New Arrivals", "Best Sellers"],
    "body_text": "Welcome to Test Brand. We make premium casual clothing.",
    "links": ["https://testbrand.in/collections"],
    "images": ["https://testbrand.in/img/hero.jpg"],
    "schema_json_ld": [{"@type": "Organization", "name": "Test Brand"}],
    "blocked": False,
    "page_html": "",
}

_PDP_DATA = {
    "url": "https://testbrand.in/products/classic-shirt",
    "product_name": "Classic Fit Shirt",
    "price": "₹1,499",
    "description": "Premium cotton shirt. Best seller. Sold out last season.",
    "rating": "4.3",
    "reviews_count": "842",
    "in_stock": True,
    "images": [
        "https://testbrand.in/img/shirt-1.jpg",
        "https://testbrand.in/img/shirt-2.jpg",
        "https://testbrand.in/img/shirt-3.jpg",
        "https://testbrand.in/img/shirt-4.jpg",
    ],
    "cta_text": "Add to Cart",
    "blocked": False,
}

# DataResult wrappers for the fixtures
_HOMEPAGE_FIXTURE = DataResult(
    value=_HOMEPAGE_DATA,
    source="homepage_scrape",
    source_url="https://testbrand.in",
    confidence="verified",
)

_HOMEPAGE_BLOCKED_FIXTURE = DataResult(
    value=None,
    source="homepage_scrape",
    source_url="https://testbrand.in",
    confidence="unavailable",
    error="Site protected by Cloudflare — scrape blocked",
    fallback_used=True,
    fallback_method="search_only",
)

_PDP_FIXTURE = DataResult(
    value=_PDP_DATA,
    source="pdp_scrape",
    source_url="https://testbrand.in/products/classic-shirt",
    confidence="verified",
)


@pytest.fixture
def mock_scraper():
    """Mock WebScraper that returns DataResult fixtures without Playwright."""
    scraper = AsyncMock()
    scraper.scrape_page = AsyncMock(return_value=_HOMEPAGE_FIXTURE)
    scraper.scrape_pdp = AsyncMock(return_value=_PDP_FIXTURE)
    scraper.detect_platform = AsyncMock(return_value="shopify")
    return scraper


@pytest.fixture
def mock_scraper_blocked():
    """Scraper that simulates a Cloudflare block (unavailable DataResult)."""
    scraper = AsyncMock()
    scraper.scrape_page = AsyncMock(return_value=_HOMEPAGE_BLOCKED_FIXTURE)
    scraper.scrape_pdp = AsyncMock(return_value=DataResult(
        value=None,
        source="pdp_scrape",
        confidence="unavailable",
        error="Product page blocked by Cloudflare",
    ))
    scraper.detect_platform = AsyncMock(return_value="custom")
    return scraper


# ── Search fixture ─────────────────────────────────────────────────────────────

_SEARCH_RESULTS = [
    {"title": "Test Brand Review", "url": "https://example.com/1", "snippet": "Great brand founded 2020."},
    {"title": "Test Brand vs Rival", "url": "https://example.com/2", "snippet": "Comparison article."},
    {"title": "Test Brand LinkedIn", "url": "https://linkedin.com/company/test", "snippet": "Company page."},
]


@pytest.fixture
def mock_search():
    """Mock SearchAgent that returns fixture results without hitting DuckDuckGo."""
    search = MagicMock()
    search.search = MagicMock(return_value=_SEARCH_RESULTS)
    search.search_news = MagicMock(return_value=_SEARCH_RESULTS[:2])
    search.find_competitors = MagicMock(return_value=_SEARCH_RESULTS)
    return search
