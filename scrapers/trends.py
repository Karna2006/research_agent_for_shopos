"""Google Trends scraper via pytrends — free, no API key required.

Provides relative search interest for a brand in India (0-100 scale),
trend direction (rising/falling/stable), and seasonal patterns.

Requires: pip install pytrends

Usage:
    from scrapers.trends import get_brand_trends
    data = await get_brand_trends("Rare Rabbit", geo="IN")
    # → {"relative_interest": 72, "trend_direction": "rising", ...}
"""
from __future__ import annotations

import asyncio

_AVAILABLE = False
try:
    from pytrends.request import TrendReq
    _AVAILABLE = True
except ImportError:
    pass


async def get_brand_trends(brand_name: str, geo: str = "IN") -> dict:
    """Return Google Trends relative interest for a brand in the specified geo.

    Uses 12-month window, India by default.
    Never raises — returns an error dict if pytrends unavailable or request fails.
    """
    if not _AVAILABLE:
        return {"error": "pytrends not installed — run: pip install pytrends"}

    try:
        result = await asyncio.to_thread(_fetch_trends_sync, brand_name, geo)
        return result
    except Exception as exc:
        return {"error": str(exc), "brand": brand_name}


def _fetch_trends_sync(brand_name: str, geo: str) -> dict:
    """Synchronous trends fetch — run via asyncio.to_thread to avoid blocking."""
    pt = TrendReq(hl="en-IN", tz=330, timeout=(10, 25), retries=2, backoff_factor=0.5)
    pt.build_payload([brand_name], timeframe="today 12-m", geo=geo)

    df = pt.interest_over_time()
    if df is None or df.empty:
        return {
            "brand":             brand_name,
            "relative_interest": 0,
            "trend_direction":   "unknown",
            "note":              "No data returned from Google Trends",
        }

    series = df[brand_name].dropna()
    if series.empty:
        return {"brand": brand_name, "relative_interest": 0, "trend_direction": "unknown"}

    avg_interest      = int(series.mean())
    recent_avg        = int(series.iloc[-4:].mean())  # last ~month
    older_avg         = int(series.iloc[:4].mean())   # first ~month

    delta = recent_avg - older_avg
    if delta > 8:
        direction = "rising"
    elif delta < -8:
        direction = "falling"
    else:
        direction = "stable"

    peak_week  = str(series.idxmax().date()) if hasattr(series.idxmax(), "date") else str(series.idxmax())
    peak_value = int(series.max())

    # Related queries (rising breakout terms)
    related: list[str] = []
    try:
        rq = pt.related_queries()
        rising = rq.get(brand_name, {}).get("rising")
        if rising is not None and not rising.empty:
            related = rising["query"].head(5).tolist()
    except Exception:
        pass

    return {
        "brand":             brand_name,
        "geo":               geo,
        "relative_interest": avg_interest,   # 0-100 relative scale
        "recent_interest":   recent_avg,
        "trend_direction":   direction,      # "rising" | "falling" | "stable"
        "peak_week":         peak_week,
        "peak_value":        peak_value,
        "related_queries":   related,
        "signal":            (
            "breakout brand" if avg_interest >= 70
            else "growing awareness" if avg_interest >= 35
            else "low search volume"
        ),
    }


async def compare_brand_trends(brands: list[str], geo: str = "IN") -> dict:
    """Compare relative search interest across up to 5 brands simultaneously.

    Returns a dict mapping brand → relative interest (normalized to the highest).
    """
    if not _AVAILABLE:
        return {"error": "pytrends not installed"}
    if len(brands) > 5:
        brands = brands[:5]

    try:
        return await asyncio.to_thread(_compare_sync, brands, geo)
    except Exception as exc:
        return {"error": str(exc)}


def _compare_sync(brands: list[str], geo: str) -> dict:
    pt = TrendReq(hl="en-IN", tz=330, timeout=(10, 25), retries=2, backoff_factor=0.5)
    pt.build_payload(brands, timeframe="today 12-m", geo=geo)
    df = pt.interest_over_time()
    if df is None or df.empty:
        return {b: 0 for b in brands}
    return {b: int(df[b].mean()) for b in brands if b in df.columns}
