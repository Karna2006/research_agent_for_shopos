"""Trustpilot public review scraper — no auth required.

Hits Trustpilot's public business search API and review pages.
Rate-limit friendly: single request per brand, 2s timeout.
Never raises — returns empty dict on failure.
"""
from __future__ import annotations

import asyncio
import re

import httpx

_SEARCH_URL = "https://www.trustpilot.com/search?query={query}"
_BIZ_URL    = "https://www.trustpilot.com/review/{domain}"
_API_URL    = "https://www.trustpilot.com/api/categoriespages.jsonld/review/{domain}"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}


def _extract_domain(url: str) -> str:
    """'https://www.rarerabbit.in/path' → 'rarerabbit.in'"""
    m = re.search(r"(?:https?://)?(?:www\.)?([^/\s]+)", url)
    return m.group(1) if m else url


async def _fetch_trustpilot_page(domain: str, client: httpx.AsyncClient) -> dict:
    """Scrape Trustpilot review page for a domain."""
    url = _BIZ_URL.format(domain=domain)
    try:
        r = await client.get(url, headers=_HEADERS, timeout=12, follow_redirects=True)
        if r.status_code == 404:
            return {"not_found": True}
        if r.status_code != 200:
            return {}
        html = r.text

        # Rating score
        rating_m = re.search(r'"ratingValue"\s*:\s*"?([\d.]+)"?', html)
        rating = float(rating_m.group(1)) if rating_m else None

        # Review count
        count_m = re.search(r'"reviewCount"\s*:\s*(\d+)', html)
        count = int(count_m.group(1)) if count_m else None

        # TrustScore label
        ts_m = re.search(r'TrustScore[^<]*<[^>]+>[^<]*<[^>]+>([\w\s]+)</span>', html, re.I)
        trust_label = ts_m.group(1).strip() if ts_m else ""

        # Recent review snippets from JSON-LD
        reviews = []
        review_blocks = re.findall(
            r'"@type"\s*:\s*"Review".*?"reviewBody"\s*:\s*"([^"]{10,300})".*?"ratingValue"\s*:\s*"?(\d)"?',
            html, re.S
        )
        for body, stars in review_blocks[:5]:
            reviews.append({
                "body":   body.replace("\\n", " ").replace('\\"', '"')[:250],
                "rating": int(stars),
            })

        # Star distribution
        dist: dict[str, int] = {}
        for stars, pct in re.findall(r'(\d)\s*star[s]?[^%\d]*(\d+)\s*%', html, re.I):
            dist[f"{stars}_star"] = int(pct)

        return {
            "rating":       round(rating, 1) if rating else None,
            "review_count": count,
            "trust_label":  trust_label,
            "star_dist":    dist,
            "reviews":      reviews,
            "url":          url,
        }
    except Exception:
        return {}


async def get_trustpilot_data(website_url: str, brand_name: str) -> dict:
    """Main entry — try brand domain first, fall back to brand name search. Never raises.

    Returns:
        {
          "found": bool,
          "rating": float | None,
          "review_count": int | None,
          "trust_label": str,
          "star_dist": {"5_star": 70, "4_star": 15, ...},
          "reviews": [{"body": "...", "rating": 4}, ...],
          "url": str,
        }
    """
    domain = _extract_domain(website_url)

    async with httpx.AsyncClient() as client:
        data = await _fetch_trustpilot_page(domain, client)

        # If domain not found, try without www / with www variant
        if not data or data.get("not_found"):
            alt = f"www.{domain}" if not domain.startswith("www.") else domain.replace("www.", "")
            data = await _fetch_trustpilot_page(alt, client)

    found = bool(data and not data.get("not_found") and data.get("rating") is not None)
    return {
        "found":        found,
        "rating":       data.get("rating") if found else None,
        "review_count": data.get("review_count") if found else None,
        "trust_label":  data.get("trust_label", "") if found else "",
        "star_dist":    data.get("star_dist", {}) if found else {},
        "reviews":      data.get("reviews", []) if found else [],
        "url":          data.get("url", "") if found else "",
    }
