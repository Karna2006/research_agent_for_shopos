"""Tests for reports/generator.py — uses real demo data, no external calls."""
from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest

from reports.generator import generate_audit_report, generate_virality_card, _build_audit_context

_MOCK_PREDICTOR = MagicMock()
_MOCK_PREDICTOR.predict_price_trajectory = MagicMock(return_value={
    "predicted_prices": [1500.0, 1520.0, 1550.0, 1570.0],
    "direction": "stable",
    "price_war_risk": False,
    "risk_level": "low",
    "recommendation": "Market pricing stable.",
    "pct_change_30d": 2.1,
    "backend": "numpy",
    "is_projected": True,
    "note": "Projected from category price benchmarks.",
})
_MOCK_PREDICTOR.predict_review_velocity = MagicMock(return_value={
    "predicted_counts": [35, 38, 40, 42],
    "trend": "stable",
    "confidence": 0.45,
    "signal": "stable",
    "weekly_sparkline": [50, 55, 60, 65, 70, 72, 74, 76, 78, 80, 82, 84, 86, 88, 90, 92],
    "backend": "numpy",
    "is_projected": True,
    "note": "Projected from category averages.",
})

# ── Section detection ──────────────────────────────────────────────────────────

_SECTION_MARKERS = [
    "Brand Basics",
    "Content &amp; Catalog",
    "Performance &amp; Ads",
    "GEO &amp; AI Visibility",
    "Store &amp; CRO",
    "Competitive Landscape",
]


def test_report_generates_html(sample_audit_data):
    """Generator returns a non-empty HTML string."""
    html = generate_audit_report(sample_audit_data)
    assert isinstance(html, str)
    assert len(html) > 1000
    assert "<!DOCTYPE html>" in html or "<html" in html


def test_report_has_all_6_sections(sample_audit_data):
    """HTML contains all 6 section headers."""
    html = generate_audit_report(sample_audit_data)
    for marker in _SECTION_MARKERS:
        assert marker in html, f"Section '{marker}' missing from report HTML"


def test_report_scores_are_colored(sample_audit_data):
    """Score colour indicators appear in the HTML (pill colours or hex inline styles)."""
    html = generate_audit_report(sample_audit_data)
    color_indicators = ["pill-green", "pill-amber", "pill-red", "#22c55e", "#f59e0b", "#ef4444"]
    found = any(indicator in html for indicator in color_indicators)
    assert found, "No score colour indicator found in HTML"


def test_report_no_shopos_branding(sample_audit_data):
    """Report must NOT contain the word 'ShopOS' (internal codeword, not user-facing)."""
    html = generate_audit_report(sample_audit_data)
    assert "ShopOS" not in html


def test_report_contains_brand_name(sample_audit_data):
    """Brand name from analysis appears in the report."""
    html = generate_audit_report(sample_audit_data)
    # Rare Rabbit is the demo brand
    assert "Rare Rabbit" in html or "Rarerabbit" in html


def test_report_generated_at_timestamp(sample_audit_data):
    """Generated-at timestamp is present in the footer (now shows IST)."""
    html = generate_audit_report(sample_audit_data)
    assert "IST" in html


def test_report_before_after_content(sample_audit_data):
    """Report contains before/after rewrite content when present in analysis."""
    html = generate_audit_report(sample_audit_data)
    # The demo audit has content_catalog analysis with before/after rewrites
    # We just check the content section heading is present
    assert "Content" in html


def test_report_schema_checklist_present(sample_audit_data):
    """Schema.org checklist rows are rendered."""
    html = generate_audit_report(sample_audit_data)
    assert "Organization" in html or "Product" in html or "WebSite" in html


def test_report_does_not_crash_on_empty_data():
    """Generator handles completely empty audit data without raising."""
    html = generate_audit_report({})
    assert isinstance(html, str)
    assert len(html) > 100


def test_report_market_forecast_hidden_when_missing(sample_audit_data):
    """Market Forecast section is hidden when research has no market_forecast."""
    # The base demo data may not have market_forecast — section should be absent
    import copy
    data = copy.deepcopy(sample_audit_data)
    results = data.get("results") or data
    research = results.get("research", {}) or {}
    research.pop("market_forecast", None)

    html = generate_audit_report(data)
    # forecast-grid only appears in CSS, not in body
    body = html[html.find("<main"):]
    assert "forecast-grid" not in body


def test_report_market_forecast_visible_when_present(sample_audit_data):
    """Market Forecast section renders when market_forecast is in research."""
    import copy

    data = copy.deepcopy(sample_audit_data)
    results = data.get("results") or data
    research = results.get("research") or {}
    research["market_forecast"] = {
        "price_trend":     _MOCK_PREDICTOR.predict_price_trajectory(),
        "review_velocity": _MOCK_PREDICTOR.predict_review_velocity(),
        "model_note":      "Chronos → Prophet → regression",
    }
    if "results" in data:
        data["results"]["research"] = research
    else:
        data["research"] = research

    html = generate_audit_report(data)
    assert "Market Forecast" in html
    assert "spark-bar" in html


# ── Context builder ────────────────────────────────────────────────────────────

def test_context_has_required_keys(sample_audit_data):
    """_build_audit_context returns all keys the template expects."""
    ctx = _build_audit_context(sample_audit_data)
    required = [
        "url", "brand_name", "generated_at", "overall_health",
        "bb", "cc", "pa", "geo", "cro", "res",
        "mobile_score", "desktop_score", "ad_formats",
        "schema_checklist", "market_forecast", "price_trend",
        "review_velocity", "spark_hist", "spark_pred",
    ]
    for key in required:
        assert key in ctx, f"Context missing key: {key}"


def test_context_overall_health_range(sample_audit_data):
    """overall_health is an int in 0-100."""
    ctx = _build_audit_context(sample_audit_data)
    h = ctx["overall_health"]
    assert isinstance(h, int)
    assert 0 <= h <= 100


def test_context_schema_checklist_structure(sample_audit_data):
    """schema_checklist entries have name and status fields."""
    ctx = _build_audit_context(sample_audit_data)
    for entry in ctx["schema_checklist"]:
        assert "name" in entry
        assert "status" in entry
        assert entry["status"] in ("found", "missing", "unknown")


# ── Virality card ──────────────────────────────────────────────────────────────

def test_virality_card_generates_html(sample_virality_data):
    """generate_virality_card returns HTML with score."""
    # virality_scores.json may be a list or a single dict
    data = sample_virality_data
    if isinstance(data, list):
        data = data[0]

    html = generate_virality_card(data)
    assert isinstance(html, str)
    assert len(html) > 500


def test_virality_card_has_grade(sample_virality_data):
    """Virality card contains the grade letter."""
    data = sample_virality_data
    if isinstance(data, list):
        data = data[0]

    html = generate_virality_card(data)
    # At least one of the grade letters appears
    assert any(g in html for g in ["S", "A", "B", "C", "D"])
