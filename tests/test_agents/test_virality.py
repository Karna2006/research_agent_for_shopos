"""Tests for agents/virality.py — all external calls mocked."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.virality import ViralityPredictor, _grade, _weighted_score

_MOCK_TRAJECTORY = {
    "predicted_7day_reach": 5000,
    "viral_probability": 0.72,
    "peak_day": 4,
    "trajectory": "linear",
    "action": "boost_now",
    "day_by_day": [800, 900, 1100, 1200, 1000, 900, 800],
    "backend": "numpy",
    "note": "Projected from engagement proxy signals.",
}

_MOCK_PREDICTOR = MagicMock()
_MOCK_PREDICTOR.predict_virality_trajectory = MagicMock(return_value=_MOCK_TRAJECTORY)

URL = "https://testbrand.in/products/classic-shirt"
PRODUCT = "Classic Fit Shirt"
DESC = "Premium cotton shirt. Best seller. Award-winning design. Must-have item."


@pytest.fixture(autouse=True)
def _mock_trend_predictor():
    """Patch get_predictor so Chronos never loads during virality tests."""
    with patch("agents.trend_predictor.get_predictor", return_value=_MOCK_PREDICTOR):
        yield


def _make_agent(llm, scraper, search):
    return ViralityPredictor(llm_client=llm, scraper=scraper, search_agent=search)


_VIRALITY_RESPONSE = {
    "overall_virality_score": 72,
    "grade": "A (Strong Potential)",
    "dimensions": {
        "emotional_trigger":      {"score": 7, "reasoning": "Aspiration", "signals": ["premium"]},
        "visual_stopping_power":  {"score": 7, "reasoning": "Clean look", "signals": []},
        "transformation_clarity": {"score": 6, "reasoning": "Before/after", "signals": []},
        "social_currency":        {"score": 7, "reasoning": "Identity signal", "signals": []},
        "trend_alignment":        {"score": 6, "reasoning": "Trending", "signals": []},
        "share_trigger":          {"score": 6, "reasoning": "Gift potential", "signals": []},
        "hook_strength":          {"score": 7, "reasoning": "Strong hook", "signals": []},
    },
    "viral_content_angles": ["Angle 1", "Angle 2", "Angle 3"],
    "ideal_creator_profile": "Fashion micro-influencer",
    "best_platforms": ["TikTok", "Instagram Reels"],
    "killer_hook": "This shirt will make you look 10x more put-together",
    "risk_factors": ["Competition"],
    "comparable_viral_products": ["Bombay Shirt Co"],
}


@pytest.mark.asyncio
async def test_virality_url_input(mock_scraper, mock_search):
    """URL provided → product scraped, score returned."""
    llm = AsyncMock()
    llm.analyze_structured = AsyncMock(return_value=_VIRALITY_RESPONSE)

    agent = _make_agent(llm, mock_scraper, mock_search)
    result = await agent.predict(url=URL)

    assert result["agent"] == "virality"
    assert result["score"] is not None
    assert "analysis" in result
    assert result["product_data_used"]["scraped"] is True


@pytest.mark.asyncio
async def test_virality_text_only(mock_search):
    """No URL, just name + description → score returned without scraping."""
    llm = AsyncMock()
    llm.analyze_structured = AsyncMock(return_value=_VIRALITY_RESPONSE)
    scraper = AsyncMock()  # should never be called

    agent = _make_agent(llm, scraper, mock_search)
    result = await agent.predict(product_name=PRODUCT, description=DESC)

    assert result["agent"] == "virality"
    assert result["score"] is not None
    assert result["product_data_used"]["name"] == PRODUCT
    scraper.scrape_pdp.assert_not_called()


@pytest.mark.asyncio
async def test_virality_score_range(mock_scraper, mock_search):
    """Score is always an integer in 0-100."""
    for raw_score in [0, 35, 50, 72, 85, 100]:
        resp = {**_VIRALITY_RESPONSE, "overall_virality_score": raw_score}
        llm = AsyncMock()
        llm.analyze_structured = AsyncMock(return_value=resp)

        agent = _make_agent(llm, mock_scraper, mock_search)
        result = await agent.predict(product_name=PRODUCT, description=DESC)

        assert result["score"] is not None
        assert 0 <= result["score"] <= 100, f"Score {result['score']} out of range"


@pytest.mark.asyncio
@pytest.mark.parametrize("score,expected_grade_prefix", [
    (85, "S"),
    (70, "A"),
    (55, "B"),
    (40, "C"),
    (10, "D"),
])
async def test_virality_grade_mapping(mock_scraper, mock_search, score, expected_grade_prefix):
    """Grade function maps scores to correct letter grades."""
    assert _grade(score).startswith(expected_grade_prefix)


@pytest.mark.asyncio
async def test_virality_all_7_dimensions_present(mock_scraper, mock_search):
    """Output analysis contains all 7 dimension scores."""
    llm = AsyncMock()
    llm.analyze_structured = AsyncMock(return_value=_VIRALITY_RESPONSE)

    agent = _make_agent(llm, mock_scraper, mock_search)
    result = await agent.predict(product_name=PRODUCT, description=DESC)

    dims = result["analysis"].get("dimensions", {})
    expected = {
        "emotional_trigger", "visual_stopping_power", "transformation_clarity",
        "social_currency", "trend_alignment", "share_trigger", "hook_strength",
    }
    assert expected == set(dims.keys())


@pytest.mark.asyncio
async def test_virality_killer_hook_nonempty(mock_scraper, mock_search):
    """killer_hook is always a non-empty string."""
    llm = AsyncMock()
    llm.analyze_structured = AsyncMock(return_value=_VIRALITY_RESPONSE)

    agent = _make_agent(llm, mock_scraper, mock_search)
    result = await agent.predict(product_name=PRODUCT, description=DESC)

    hook = result["analysis"].get("killer_hook", "")
    assert isinstance(hook, str) and hook.strip(), "killer_hook must be non-empty"


@pytest.mark.asyncio
async def test_virality_empty_description(mock_scraper, mock_search):
    """Empty description handled gracefully — no crash."""
    llm = AsyncMock()
    llm.analyze_structured = AsyncMock(return_value=_VIRALITY_RESPONSE)

    agent = _make_agent(llm, mock_scraper, mock_search)
    result = await agent.predict(product_name=PRODUCT, description="")

    assert "error" not in result
    assert result["score"] is not None


@pytest.mark.asyncio
async def test_virality_score_override_when_drifted(mock_scraper, mock_search):
    """LLM score > 10 off from weighted recalculation triggers override."""
    # Weighted sum of the _VIRALITY_RESPONSE dimensions:
    # (7*.22 + 7*.18 + 6*.14 + 7*.14 + 6*.12 + 6*.10 + 7*.10) * 10 = 65
    resp = {**_VIRALITY_RESPONSE, "overall_virality_score": 99}  # far from ~65
    llm = AsyncMock()
    llm.analyze_structured = AsyncMock(return_value=resp)

    agent = _make_agent(llm, mock_scraper, mock_search)
    result = await agent.predict(product_name=PRODUCT, description=DESC)

    # Either overridden or within ±10 of weighted score — never 99
    assert result["score"] != 99 or result["analysis"].get("_score_overridden")


def test_weighted_score_calculation():
    """_weighted_score produces correct weighted average from dimension dict."""
    dims = {
        "emotional_trigger":      {"score": 10},
        "visual_stopping_power":  {"score": 10},
        "transformation_clarity": {"score": 10},
        "social_currency":        {"score": 10},
        "trend_alignment":        {"score": 10},
        "share_trigger":          {"score": 10},
        "hook_strength":          {"score": 10},
    }
    assert _weighted_score(dims) == 100


def test_grade_boundaries():
    """Grade boundaries match the spec exactly."""
    assert _grade(85).startswith("S")
    assert _grade(84).startswith("A")
    assert _grade(70).startswith("A")
    assert _grade(69).startswith("B")
    assert _grade(55).startswith("B")
    assert _grade(54).startswith("C")
    assert _grade(40).startswith("C")
    assert _grade(39).startswith("D")
    assert _grade(0).startswith("D")
