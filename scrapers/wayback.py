"""Wayback Machine CDX API — free, no auth required.

Used to establish brand longevity signals:
  - When was the domain first archived?
  - How frequently does it appear (crawl frequency ≈ relevance proxy)?
  - Was the site down / redirecting at any point?

Free CDX API docs: https://github.com/internetarchive/wayback-cdx-server

Usage:
    from scrapers.wayback import get_brand_longevity
    data = await get_brand_longevity("rarerabbit.in")
    # → {"first_seen": "2018-03", "years_online": 6, "crawl_frequency": "high", ...}
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx

_CDX_URL = "https://web.archive.org/cdx/search/cdx"
_TIMEOUT  = 12


async def get_brand_longevity(website_url: str) -> dict:
    """Return domain age and crawl signals from the Wayback Machine CDX API.

    Never raises — returns an error dict on failure.
    """
    try:
        domain = urlparse(website_url).netloc.replace("www.", "").strip("/")
        if not domain:
            return {"error": "invalid URL"}

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            # Single request: timestamps + status codes, no filter (filter+collapse combo
            # returns 400 on some domains). We filter status=200 client-side.
            r = await client.get(
                _CDX_URL,
                params={
                    "url":    domain,
                    "output": "json",
                    "fl":     "timestamp,statuscode",
                    "limit":  "500",
                },
            )
            if r.status_code != 200 or not r.text.strip():
                return {"error": f"CDX returned {r.status_code}"}

            rows = r.json()
            # First row is the header ["timestamp", "statuscode"]
            if len(rows) < 2:
                return {"first_seen": None, "years_online": 0, "crawl_frequency": "unknown"}

            data_rows = [row for row in rows[1:] if len(row) >= 2 and row[1] == "200"]
            if not data_rows:
                # Fall back to all rows if no 200s
                data_rows = rows[1:]

            total_snapshots = len(data_rows)

            # Earliest timestamp (YYYYMMDDHHMMSS)
            timestamps = [row[0] for row in data_rows if len(row[0]) >= 4]
            if not timestamps:
                return {"first_seen": None, "years_online": 0, "crawl_frequency": "unknown"}
            first_ts = min(timestamps)
            first_year  = int(first_ts[:4])
            first_month = int(first_ts[4:6]) if len(first_ts) >= 6 else 1

        from datetime import date
        today = date.today()
        years_online = (today.year - first_year) + ((today.month - first_month) / 12)

        if total_snapshots > 200:
            frequency = "very high"
        elif total_snapshots > 80:
            frequency = "high"
        elif total_snapshots > 30:
            frequency = "medium"
        elif total_snapshots > 5:
            frequency = "low"
        else:
            frequency = "rare"

        return {
            "first_seen":       f"{first_year}-{first_month:02d}",
            "years_online":     round(years_online, 1),
            "total_snapshots":  total_snapshots,
            "crawl_frequency":  frequency,
            "longevity_signal": (
                "established" if years_online >= 5
                else "growing" if years_online >= 2
                else "new"
            ),
        }

    except Exception as exc:
        return {"error": str(exc)}
