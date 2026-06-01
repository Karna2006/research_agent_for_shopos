"""Tests for agents/orchestrator.py — agents are mocked, no LLM/scraper calls."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapers.result import DataResult

URL = "https://testbrand.in"
BRAND = "Test Brand"

_AGENT_RESULTS = {
    "brand_basics":    {"agent": "brand_basics",    "url": URL, "analysis": {"brand_name": BRAND}},
    "content_catalog": {"agent": "content_catalog", "url": URL, "analysis": {}},
    "performance_ads": {"agent": "performance_ads", "url": URL, "analysis": {}},
    "geo_visibility":  {"agent": "geo_visibility",  "url": URL, "analysis": {}},
    "store_cro":       {"agent": "store_cro",       "url": URL, "analysis": {}},
    "research":        {"agent": "research",        "url": URL, "analysis": {}},
}

_FAKE_PREFETCHED = {
    "homepage": DataResult(value={"title": "Test"}, confidence="full", source="homepage_scrape"),
    "pagespeed": DataResult(value={}, confidence="full", source="pagespeed"),
    "meta_ads":  DataResult(value={}, confidence="full", source="meta_ads"),
}


def _mock_agent_for(key: str):
    agent = AsyncMock()
    agent.run = AsyncMock(return_value=_AGENT_RESULTS[key])
    return agent


def _all_mock_agents():
    return {k: _mock_agent_for(k) for k in _AGENT_RESULTS}


def _phase1_patch():
    return patch(
        "agents.orchestrator._run_phase1",
        new=AsyncMock(return_value=_FAKE_PREFETCHED),
    )


@pytest.mark.asyncio
async def test_orchestrator_runs_all_6_agents():
    """Every agent in AGENT_SEQUENCE is called exactly once."""
    agents = _all_mock_agents()

    with (
        patch("agents.orchestrator.get_client", return_value=AsyncMock()),
        patch("agents.orchestrator.WebScraper", return_value=AsyncMock()),
        patch("agents.orchestrator.SearchAgent", return_value=MagicMock()),
        patch("agents.orchestrator._build_agents", return_value=agents),
        _phase1_patch(),
    ):
        from agents.orchestrator import run_full_audit
        result = await run_full_audit(URL)

    for key, agent in agents.items():
        agent.run.assert_called_once()
        args = agent.run.call_args.args
        assert args[0] == URL
        assert args[1] == "Testbrand"  # _brand_name_from_url normalises

    assert "results" in result
    assert len(result["results"]) == 6


@pytest.mark.asyncio
async def test_orchestrator_agent_failure_continues():
    """If one agent raises, remaining agents still run; error is recorded."""
    agents = _all_mock_agents()
    # Make agent 3 (performance_ads) raise
    agents["performance_ads"].run = AsyncMock(side_effect=RuntimeError("Ad scraper down"))

    with (
        patch("agents.orchestrator.get_client", return_value=AsyncMock()),
        patch("agents.orchestrator.WebScraper", return_value=AsyncMock()),
        patch("agents.orchestrator.SearchAgent", return_value=MagicMock()),
        patch("agents.orchestrator._build_agents", return_value=agents),
        _phase1_patch(),
    ):
        from agents.orchestrator import run_full_audit
        result = await run_full_audit(URL)

    # All 6 agents attempted
    assert len(result["results"]) == 6
    # performance_ads captured the error
    assert "error" in result["results"]["performance_ads"]
    assert "Ad scraper down" in result["results"]["performance_ads"]["error"]
    # Agents after performance_ads still ran (parallel — all run regardless)
    agents["geo_visibility"].run.assert_called_once()
    agents["store_cro"].run.assert_called_once()
    agents["research"].run.assert_called_once()


@pytest.mark.asyncio
async def test_orchestrator_returns_metadata():
    """Output includes timestamp, total_time_seconds, and agent_status array."""
    agents = _all_mock_agents()

    with (
        patch("agents.orchestrator.get_client", return_value=AsyncMock()),
        patch("agents.orchestrator.WebScraper", return_value=AsyncMock()),
        patch("agents.orchestrator.SearchAgent", return_value=MagicMock()),
        patch("agents.orchestrator._build_agents", return_value=agents),
        _phase1_patch(),
    ):
        from agents.orchestrator import run_full_audit
        result = await run_full_audit(URL)

    assert "timestamp" in result
    assert "total_time_seconds" in result
    assert isinstance(result["total_time_seconds"], float)
    assert "agent_status" in result
    assert isinstance(result["agent_status"], list)
    assert len(result["agent_status"]) == 6


@pytest.mark.asyncio
async def test_orchestrator_agent_status_fields():
    """Each agent_status entry has agent, label, status, elapsed_s keys."""
    agents = _all_mock_agents()

    with (
        patch("agents.orchestrator.get_client", return_value=AsyncMock()),
        patch("agents.orchestrator.WebScraper", return_value=AsyncMock()),
        patch("agents.orchestrator.SearchAgent", return_value=MagicMock()),
        patch("agents.orchestrator._build_agents", return_value=agents),
        _phase1_patch(),
    ):
        from agents.orchestrator import run_full_audit
        result = await run_full_audit(URL)

    for entry in result["agent_status"]:
        assert "agent" in entry
        assert "label" in entry
        assert "status" in entry
        assert "elapsed_s" in entry
        assert entry["status"] in ("done", "error")


@pytest.mark.asyncio
async def test_orchestrator_brand_name_from_url():
    """Brand name is derived from domain — hyphens become spaces, title-cased."""
    agents = _all_mock_agents()

    with (
        patch("agents.orchestrator.get_client", return_value=AsyncMock()),
        patch("agents.orchestrator.WebScraper", return_value=AsyncMock()),
        patch("agents.orchestrator.SearchAgent", return_value=MagicMock()),
        patch("agents.orchestrator._build_agents", return_value=agents),
        _phase1_patch(),
    ):
        from agents.orchestrator import run_full_audit
        result = await run_full_audit("https://my-brand-store.in")

    assert result["brand_name"] == "My Brand Store"


def test_brand_name_from_url_direct():
    """Unit-test _brand_name_from_url in isolation."""
    from agents.orchestrator import _brand_name_from_url

    assert _brand_name_from_url("https://rarerabbit.in") == "Rarerabbit"
    assert _brand_name_from_url("https://www.my-brand.com") == "My Brand"
    assert _brand_name_from_url("https://snitch.co.in") == "Snitch"
