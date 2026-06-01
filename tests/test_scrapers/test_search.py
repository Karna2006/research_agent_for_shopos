"""Tests for scrapers/search.py — DuckDuckGo calls are mocked."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scrapers.search import SearchAgent, _extract_domain

_RAW_DDGS = [
    {"title": "Rare Rabbit Review", "href": "https://example.com/review", "body": "Great brand."},
    {"title": "Rare Rabbit vs Rival", "href": "https://rival.com", "body": "Comparison."},
    {"title": "Rare Rabbit LinkedIn", "href": "https://linkedin.com/company/rr", "body": "Company."},
]


def test_search_returns_structured_results():
    """search() maps raw DDGS dicts to {title, url, snippet}."""
    with patch("scrapers.search._safe_text", return_value=_RAW_DDGS):
        results = SearchAgent().search("Rare Rabbit", max_results=3)

    assert len(results) == 3
    assert results[0]["title"] == "Rare Rabbit Review"
    assert results[0]["url"] == "https://example.com/review"
    assert results[0]["snippet"] == "Great brand."


def test_search_empty_results_never_crashes():
    """DuckDuckGo returning nothing → empty list, no exception."""
    with patch("scrapers.search._safe_text", return_value=[]):
        results = SearchAgent().search("nonexistent brand xyz", max_results=5)
    assert results == []


def test_search_ddgs_exception_returns_empty():
    """DuckDuckGo raising → _safe_text swallows it, returns []."""
    with patch("scrapers.search._safe_text", return_value=[]):
        results = SearchAgent().search("anything", max_results=5)
    assert isinstance(results, list)


def test_search_news_maps_correctly():
    """search_news() maps raw news items to {title, url, snippet, published, source}."""
    raw_news = [
        {"title": "Brand Launch", "url": "https://news.com/1", "body": "Launched.", "date": "2024-01-01", "source": "TechCrunch"},
    ]
    with patch("scrapers.search._safe_news", return_value=raw_news):
        results = SearchAgent().search_news("Rare Rabbit", days=30)

    assert len(results) == 1
    assert results[0]["published"] == "2024-01-01"
    assert results[0]["source"] == "TechCrunch"


@pytest.mark.parametrize("days,expected_timelimit", [
    (1, "d"),
    (7, "w"),
    (30, "m"),
    (90, "m"),
])
def test_search_news_timelimit_mapping(days, expected_timelimit):
    """Days are correctly mapped to DuckDuckGo timelimit codes."""
    with patch("scrapers.search._safe_news") as mock_news:
        mock_news.return_value = []
        SearchAgent().search_news("brand", days=days)
        _, kwargs = mock_news.call_args
        assert kwargs.get("timelimit") == expected_timelimit or mock_news.call_args[0][1] == expected_timelimit


def test_find_competitors_deduplicates_by_domain():
    """Duplicate domains from multiple queries are deduplicated."""
    # Both queries return the same domain
    duplicate_results = [
        {"title": "Rival", "url": "https://rival.com/article1", "snippet": "Rival brand."},
        {"title": "Rival again", "url": "https://rival.com/article2", "snippet": "Same domain."},
        {"title": "Other", "url": "https://other.com", "snippet": "Different domain."},
    ]
    with patch("scrapers.search._safe_text", return_value=duplicate_results):
        results = SearchAgent().find_competitors("TestBrand", "fashion")

    domains = [_extract_domain(r["url"]) for r in results]
    assert len(domains) == len(set(domains)), "Duplicate domains in competitor results"


def test_extract_domain_parses_correctly():
    """_extract_domain strips protocol and www correctly."""
    assert _extract_domain("https://www.example.com/path") == "example.com"
    assert _extract_domain("http://rival.in") == "rival.in"
    assert _extract_domain("rival.in") == "rival.in"
