"""App Store + Google Play scraper — no auth required.

Searches for brand app by name, returns rating, review count, recent reviews.
Uses iTunes Search API (Apple) and google-play-scraper library.
Never raises — returns empty dict on failure.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import quote

import httpx


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


async def _search_app_store(brand_name: str, client: httpx.AsyncClient) -> dict:
    """iTunes Search API — completely free, no key required."""
    try:
        r = await client.get(
            "https://itunes.apple.com/search",
            params={"term": brand_name, "country": "in", "entity": "software", "limit": "5"},
            timeout=10,
        )
        if r.status_code != 200:
            return {}
        results = r.json().get("results", [])
        brand_slug = _slug(brand_name)
        # Pick best match: prefer app whose name contains brand slug
        best = None
        for app in results:
            app_name_slug = _slug(app.get("trackName", ""))
            if brand_slug in app_name_slug or app_name_slug in brand_slug:
                best = app
                break
        if not best and results:
            best = results[0]
        if not best:
            return {}
        return {
            "app_id":       best.get("trackId"),
            "name":         best.get("trackName", ""),
            "rating":       round(float(best.get("averageUserRating", 0) or 0), 1),
            "rating_count": int(best.get("userRatingCount", 0) or 0),
            "version":      best.get("version", ""),
            "updated_at":   (best.get("currentVersionReleaseDate") or "")[:10],
            "genre":        best.get("primaryGenreName", ""),
            "store_url":    best.get("trackViewUrl", ""),
            "description":  (best.get("description") or "")[:300],
        }
    except Exception:
        return {}


async def _search_play_store(brand_name: str) -> dict:
    """google-play-scraper — free, no auth."""
    try:
        from google_play_scraper import search as gps_search, app as gps_app
        results = await asyncio.to_thread(gps_search, brand_name, lang="en", country="in", n_hits=5)
        if not results:
            return {}
        brand_slug = _slug(brand_name)
        best = None
        for r in results:
            app_slug = _slug(r.get("title", ""))
            if brand_slug in app_slug or app_slug in brand_slug:
                best = r
                break
        if not best:
            best = results[0]
        # Fetch full details for reviews
        details = await asyncio.to_thread(gps_app, best["appId"], lang="en", country="in")
        return {
            "app_id":       details.get("appId", ""),
            "name":         details.get("title", ""),
            "rating":       round(float(details.get("score") or 0), 1),
            "rating_count": int(details.get("ratings") or 0),
            "installs":     details.get("installs", ""),
            "version":      details.get("version", ""),
            "updated_at":   str(details.get("updated", ""))[:10],
            "genre":        details.get("genre", ""),
            "store_url":    f"https://play.google.com/store/apps/details?id={best['appId']}",
            "description":  (details.get("description") or "")[:300],
        }
    except Exception:
        return {}


async def _get_app_store_reviews(app_id: int | str, limit: int = 5) -> list[dict]:
    """Fetch recent App Store reviews via RSS feed — no auth."""
    if not app_id:
        return []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://itunes.apple.com/in/rss/customerreviews/id={app_id}/sortBy=mostRecent/json",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if r.status_code != 200:
                return []
            feed = r.json().get("feed", {})
            entries = feed.get("entry", [])
            if not entries:
                return []
            reviews = []
            for e in entries[1:limit+1]:  # entry[0] is app meta
                reviews.append({
                    "title":   e.get("title", {}).get("label", ""),
                    "body":    e.get("content", {}).get("label", "")[:200],
                    "rating":  int(e.get("im:rating", {}).get("label", 0) or 0),
                    "author":  e.get("author", {}).get("name", {}).get("label", ""),
                })
            return reviews
    except Exception:
        return []


async def _get_play_store_reviews(app_id: str, limit: int = 5) -> list[dict]:
    """Fetch recent Play Store reviews — no auth."""
    if not app_id:
        return []
    try:
        from google_play_scraper import reviews as gps_reviews, Sort
        result, _ = await asyncio.to_thread(
            gps_reviews, app_id, lang="en", country="in",
            sort=Sort.NEWEST, count=limit,
        )
        return [
            {
                "title":  r.get("userName", ""),
                "body":   (r.get("content") or "")[:200],
                "rating": int(r.get("score") or 0),
                "author": r.get("userName", ""),
            }
            for r in (result or [])
        ]
    except Exception:
        return []


async def get_app_data(brand_name: str) -> dict:
    """Fetch App Store + Play Store data for a brand. Never raises.

    Returns:
        {
          "app_store": {...} | {},
          "play_store": {...} | {},
          "app_store_reviews": [...],
          "play_store_reviews": [...],
          "has_app": bool,
          "avg_rating": float | None,
          "total_ratings": int,
        }
    """
    async with httpx.AsyncClient() as client:
        ios_data, android_data = await asyncio.gather(
            _search_app_store(brand_name, client),
            _search_play_store(brand_name),
            return_exceptions=True,
        )

    if isinstance(ios_data, Exception):
        ios_data = {}
    if isinstance(android_data, Exception):
        android_data = {}

    # Fetch reviews in parallel if we have app IDs
    ios_reviews, android_reviews = await asyncio.gather(
        _get_app_store_reviews(ios_data.get("app_id")),
        _get_play_store_reviews(android_data.get("app_id", "")),
        return_exceptions=True,
    )
    if isinstance(ios_reviews, Exception):
        ios_reviews = []
    if isinstance(android_reviews, Exception):
        android_reviews = []

    has_app = bool(ios_data or android_data)

    ratings = [
        r for r in [
            ios_data.get("rating") if ios_data else None,
            android_data.get("rating") if android_data else None,
        ] if r
    ]
    avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None

    total_ratings = (
        (ios_data.get("rating_count") or 0) +
        (android_data.get("rating_count") or 0)
    )

    return {
        "app_store":          ios_data,
        "play_store":         android_data,
        "app_store_reviews":  ios_reviews,
        "play_store_reviews": android_reviews,
        "has_app":            has_app,
        "avg_rating":         avg_rating,
        "total_ratings":      total_ratings,
    }
