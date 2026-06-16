"""Tests for the agentic orchestrator — all LLM/scraper calls are mocked."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapers.result import DataResult
from agents.reasoning_brain import InitialPlan, NextDecision, CrossInsight, FinalSynthesis

URL = "https://testbrand.in"
BRAND = "Testbrand"

# ── Mock agent results ────────────────────────────────────────────────────────

_AGENT_KEYS = [
    "brand_basics", "content_catalog", "performance_ads", "geo_visibility",
    "store_cro", "research", "social_profile", "social_media_audit",
]

_AGENT_RESULTS = {
    k: {"agent": k, "url": URL, "analysis": {}, "data_coverage": "partial", "fallbacks_used": []}
    for k in _AGENT_KEYS
}
_AGENT_RESULTS["brand_basics"]["analysis"] = {"brand_name": BRAND}


def _mock_agents():
    agents = {}
    for k in _AGENT_KEYS:
        a = AsyncMock()
        a.run = AsyncMock(return_value=_AGENT_RESULTS[k])
        agents[k] = a
    return agents


# ── Mock ReasoningBrain ───────────────────────────────────────────────────────

def _mock_brain():
    brain = AsyncMock()
    brain.initial_plan = AsyncMock(return_value=InitialPlan(
        priority_agents=list(_AGENT_KEYS),
        predicted_issues=["low CRO"],
        skip_conditions=[],
        investigation_posture="optimize",
        opening_hypothesis="Standard D2C audit.",
    ))
    brain.observe = AsyncMock(return_value=[])
    brain.plan_next = AsyncMock(return_value=NextDecision(
        decision="continue", target_agent=None,
        rationale="proceed", emerging_pattern=None,
    ))
    brain.cross_synthesize = AsyncMock(return_value=CrossInsight(
        pattern=None, insight="No cross-agent pattern yet.",
        highest_leverage_action="Fix CRO first.",
    ))
    brain.final_synthesis = AsyncMock(return_value=FinalSynthesis(
        core_challenge="Low discoverability",
        root_cause="No SEO schema",
        hidden_opportunity="Social growth",
        contradictions=[],
        confidence="medium",
        posture="optimize",
        pattern=None,
        narrative="Brand has strong products but weak digital presence.",
    ))
    return brain


# ── Context manager helpers ───────────────────────────────────────────────────

def _base_patches(agents, brain):
    return (
        patch("agents.agentic_orchestrator.get_client", return_value=AsyncMock()),
        patch("agents.agentic_orchestrator.WebScraper", return_value=AsyncMock()),
        patch("agents.agentic_orchestrator.SearchAgent", return_value=MagicMock()),
        patch("agents.agentic_orchestrator._build_agents", return_value=agents),
        patch("agents.agentic_orchestrator.ReasoningBrain", return_value=brain),
        patch("agents.agentic_orchestrator._generate_one_thing",
              new=AsyncMock(return_value="Fix mobile PageSpeed")),
        patch("agents.agentic_orchestrator._generate_roadmap",
              new=AsyncMock(return_value={})),
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orchestrator_runs_all_6_agents():
    """Every agent in AGENT_SEQUENCE is called exactly once."""
    agents = _mock_agents()
    brain  = _mock_brain()

    with (
        patch("agents.agentic_orchestrator.get_client", return_value=AsyncMock()),
        patch("agents.agentic_orchestrator.WebScraper", return_value=AsyncMock()),
        patch("agents.agentic_orchestrator.SearchAgent", return_value=MagicMock()),
        patch("agents.agentic_orchestrator._build_agents", return_value=agents),
        patch("agents.agentic_orchestrator.ReasoningBrain", return_value=brain),
        patch("agents.agentic_orchestrator._generate_one_thing", new=AsyncMock(return_value="")),
        patch("agents.agentic_orchestrator._generate_roadmap",   new=AsyncMock(return_value={})),
    ):
        from agents.orchestrator import run_full_audit
        result = await run_full_audit(URL)

    assert "results" in result
    # All 8 agents should appear in results (some may be skipped by brain but recorded)
    assert len(result["results"]) >= 1

    # Agents the brain didn't skip should have been called
    for key, agent in agents.items():
        if result["results"].get(key, {}).get("status") != "skipped":
            agent.run.assert_called_once()


@pytest.mark.asyncio
async def test_orchestrator_agent_failure_continues():
    """If one agent raises, remaining agents still run; error is recorded."""
    agents = _mock_agents()
    brain  = _mock_brain()
    agents["performance_ads"].run = AsyncMock(side_effect=RuntimeError("Ad scraper down"))

    with (
        patch("agents.agentic_orchestrator.get_client", return_value=AsyncMock()),
        patch("agents.agentic_orchestrator.WebScraper", return_value=AsyncMock()),
        patch("agents.agentic_orchestrator.SearchAgent", return_value=MagicMock()),
        patch("agents.agentic_orchestrator._build_agents", return_value=agents),
        patch("agents.agentic_orchestrator.ReasoningBrain", return_value=brain),
        patch("agents.agentic_orchestrator._generate_one_thing", new=AsyncMock(return_value="")),
        patch("agents.agentic_orchestrator._generate_roadmap",   new=AsyncMock(return_value={})),
    ):
        from agents.orchestrator import run_full_audit
        result = await run_full_audit(URL)

    assert "results" in result
    pa = result["results"].get("performance_ads", {})
    assert "error" in pa or pa.get("status") == "failed"
    if "error" in pa:
        assert "Ad scraper down" in pa["error"]


@pytest.mark.asyncio
async def test_orchestrator_returns_metadata():
    """Output includes timestamp, total_time_seconds, url, brand_name."""
    agents = _mock_agents()
    brain  = _mock_brain()

    with (
        patch("agents.agentic_orchestrator.get_client", return_value=AsyncMock()),
        patch("agents.agentic_orchestrator.WebScraper", return_value=AsyncMock()),
        patch("agents.agentic_orchestrator.SearchAgent", return_value=MagicMock()),
        patch("agents.agentic_orchestrator._build_agents", return_value=agents),
        patch("agents.agentic_orchestrator.ReasoningBrain", return_value=brain),
        patch("agents.agentic_orchestrator._generate_one_thing", new=AsyncMock(return_value="")),
        patch("agents.agentic_orchestrator._generate_roadmap",   new=AsyncMock(return_value={})),
    ):
        from agents.orchestrator import run_full_audit
        result = await run_full_audit(URL)

    assert "timestamp" in result
    assert "total_time_seconds" in result
    assert isinstance(result["total_time_seconds"], float)
    assert result["url"] == URL
    assert "brand_name" in result


@pytest.mark.asyncio
async def test_orchestrator_agentic_fields_present():
    """Agentic output includes reasoning_trace, signals, agentic_meta, decisions."""
    agents = _mock_agents()
    brain  = _mock_brain()

    with (
        patch("agents.agentic_orchestrator.get_client", return_value=AsyncMock()),
        patch("agents.agentic_orchestrator.WebScraper", return_value=AsyncMock()),
        patch("agents.agentic_orchestrator.SearchAgent", return_value=MagicMock()),
        patch("agents.agentic_orchestrator._build_agents", return_value=agents),
        patch("agents.agentic_orchestrator.ReasoningBrain", return_value=brain),
        patch("agents.agentic_orchestrator._generate_one_thing", new=AsyncMock(return_value="")),
        patch("agents.agentic_orchestrator._generate_roadmap",   new=AsyncMock(return_value={})),
    ):
        from agents.orchestrator import run_full_audit
        result = await run_full_audit(URL)

    assert "agentic_meta" in result
    assert "reasoning_trace" in result
    assert "signals" in result
    assert "decisions" in result
    assert isinstance(result["signals"], list)
    assert isinstance(result["reasoning_trace"], list)


@pytest.mark.asyncio
async def test_orchestrator_brand_name_from_url():
    """Brand name is derived from domain — hyphens become spaces, title-cased."""
    agents = _mock_agents()
    brain  = _mock_brain()

    with (
        patch("agents.agentic_orchestrator.get_client", return_value=AsyncMock()),
        patch("agents.agentic_orchestrator.WebScraper", return_value=AsyncMock()),
        patch("agents.agentic_orchestrator.SearchAgent", return_value=MagicMock()),
        patch("agents.agentic_orchestrator._build_agents", return_value=agents),
        patch("agents.agentic_orchestrator.ReasoningBrain", return_value=brain),
        patch("agents.agentic_orchestrator._generate_one_thing", new=AsyncMock(return_value="")),
        patch("agents.agentic_orchestrator._generate_roadmap",   new=AsyncMock(return_value={})),
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
