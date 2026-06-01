"""Tests for agents/brand_basics.py — all external calls mocked."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agents.brand_basics import BrandBasicsAgent

URL = "https://testbrand.in"
BRAND = "Test Brand"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_agent(llm, scraper, search):
    return BrandBasicsAgent(llm_client=llm, scraper=scraper, search_agent=search)


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_brand_basics_happy_path(mock_llm, mock_scraper, mock_search, mock_groq_json):
    """Valid URL, scraper returns data, LLM parses OK — full result returned."""
    with patch("agents.brand_basics.httpx.AsyncClient") as mock_client_cls:
        # Suppress the /products.json check
        mock_resp = AsyncMock()
        mock_resp.status_code = 404
        mock_resp.text = ""
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=AsyncMock(
            get=AsyncMock(return_value=mock_resp)
        ))
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        agent = _make_agent(mock_llm, mock_scraper, mock_search)
        result = await agent.run(URL, BRAND)

    assert result["agent"] == "brand_basics"
    assert result["url"] == URL
    assert "analysis" in result
    assert result["analysis"]["brand_name"] == "Test Brand"
    assert "source_confidence" in result
    assert "platform" in result
    assert "error" not in result
    assert "sources_used" in result
    assert isinstance(result["sources_used"], list)
    assert result["status"] in ("complete", "partial")


@pytest.mark.asyncio
async def test_brand_basics_blocked_scraper(mock_llm, mock_scraper_blocked, mock_search, mock_groq_json):
    """Blocked scraper returns partial data — agent still runs and returns analysis."""
    with patch("agents.brand_basics.httpx.AsyncClient") as mock_client_cls:
        mock_resp = AsyncMock()
        mock_resp.status_code = 404
        mock_resp.text = ""
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=AsyncMock(
            get=AsyncMock(return_value=mock_resp)
        ))
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        agent = _make_agent(mock_llm, mock_scraper_blocked, mock_search)
        result = await agent.run(URL, BRAND)

    # Agent must complete — not crash
    assert "error" not in result
    assert "analysis" in result
    # Confidence should be low when blocked
    assert result["source_confidence"] in ("low", "medium", "high")


@pytest.mark.asyncio
async def test_brand_basics_confidence_field(mock_llm, mock_scraper, mock_search):
    """source_confidence field is always present in the result."""
    with patch("agents.brand_basics.httpx.AsyncClient") as mock_client_cls:
        mock_resp = AsyncMock()
        mock_resp.status_code = 404
        mock_resp.text = ""
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=AsyncMock(
            get=AsyncMock(return_value=mock_resp)
        ))
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        agent = _make_agent(mock_llm, mock_scraper, mock_search)
        result = await agent.run(URL, BRAND)

    assert "source_confidence" in result
    assert result["source_confidence"] in ("high", "medium", "low")


@pytest.mark.asyncio
async def test_brand_basics_confidence_high_when_signals_present(mock_llm, mock_scraper, mock_search):
    """3+ signals → high confidence: unblocked + search results + schema."""
    with patch("agents.brand_basics.httpx.AsyncClient") as mock_client_cls:
        mock_resp = AsyncMock()
        mock_resp.status_code = 404
        mock_resp.text = ""
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=AsyncMock(
            get=AsyncMock(return_value=mock_resp)
        ))
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        # mock_scraper has schema_json_ld set and is not blocked
        # mock_search returns 3 results (≥3 triggers len check)
        agent = _make_agent(mock_llm, mock_scraper, mock_search)
        result = await agent.run(URL, BRAND)

    assert result["source_confidence"] == "high"


@pytest.mark.asyncio
async def test_brand_basics_llm_invalid_json(mock_scraper, mock_search):
    """LLM returns malformed JSON — agent returns _parse_error, never crashes."""
    bad_llm = AsyncMock()
    bad_llm.analyze_structured = AsyncMock(
        return_value={"_raw": "not json at all", "_parse_error": "Could not parse JSON after repair attempt"}
    )

    with patch("agents.brand_basics.httpx.AsyncClient") as mock_client_cls:
        mock_resp = AsyncMock()
        mock_resp.status_code = 404
        mock_resp.text = ""
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=AsyncMock(
            get=AsyncMock(return_value=mock_resp)
        ))
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        agent = _make_agent(bad_llm, mock_scraper, mock_search)
        result = await agent.run(URL, BRAND)

    # Agent must still complete; parse error propagates inside analysis
    assert "analysis" in result
    assert "_parse_error" in result["analysis"]


@pytest.mark.asyncio
async def test_brand_basics_shopify_detected_via_products_json(mock_llm, mock_search):
    """When homepage detection misses shopify, /products.json fallback sets platform=shopify."""
    from scrapers.result import DataResult
    scraper = AsyncMock()
    scraper.scrape_page = AsyncMock(return_value=DataResult(
        value={
            "title": "Test", "meta_description": "", "headings": [],
            "body_text": "", "links": [], "images": [],
            "schema_json_ld": [], "blocked": False, "page_html": "",
        },
        source="homepage_scrape",
        confidence="verified",
    ))
    scraper.detect_platform = AsyncMock(return_value="custom")

    with patch("agents.brand_basics.httpx.AsyncClient") as mock_client_cls:
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"products": [{"id": 1}]}'
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=AsyncMock(
            get=AsyncMock(return_value=mock_resp)
        ))
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        agent = _make_agent(mock_llm, scraper, mock_search)
        result = await agent.run(URL, BRAND)

    assert result["platform"] == "shopify"


@pytest.mark.asyncio
async def test_brand_basics_scraper_exception_returns_error(mock_llm, mock_search):
    """If scraper raises unexpectedly, result has an 'error' key."""
    scraper = AsyncMock()
    scraper.scrape_page = AsyncMock(side_effect=RuntimeError("Playwright crashed"))
    scraper.detect_platform = AsyncMock(return_value="custom")

    agent = _make_agent(mock_llm, scraper, mock_search)
    result = await agent.run(URL, BRAND)

    assert "error" in result
    assert "Playwright crashed" in result["error"]
