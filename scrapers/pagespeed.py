"""Google PageSpeed Insights API — returns DataResult."""
from __future__ import annotations

import asyncio
import os
from urllib.parse import quote

import httpx

from scrapers.result import DataResult

PAGESPEED_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
_TIMEOUT = 10           # seconds — tight timeout; PSI is penalised gracefully
_MAX_RETRIES = 2
_RETRY_DELAY = 5


def _psi_manual_url(url: str) -> str:
    return f"https://pagespeed.web.dev/report?url={quote(url, safe='')}"


def _score_to_label(score: float | None) -> str:
    if score is None:
        return "unknown"
    pct = round(score * 100)
    if pct >= 90:
        return "good"
    if pct >= 50:
        return "needs-improvement"
    return "poor"


def _extract_audit_value(audits: dict, key: str) -> str:
    return audits.get(key, {}).get("displayValue", "")


def _top_recommendations(audits: dict, limit: int = 5) -> list[dict]:
    """Return the highest-impact failed audits as recommendations."""
    candidates = []
    for audit_id, audit in audits.items():
        score = audit.get("score")
        if score is None or score >= 0.9:
            continue
        title = audit.get("title", "")
        description = audit.get("description", "")
        if not title:
            continue
        candidates.append({
            "id": audit_id,
            "title": title,
            "description": description[:200],
            "score": score,
        })
    candidates.sort(key=lambda x: x["score"])
    return [
        {"title": c["title"], "description": c["description"]}
        for c in candidates[:limit]
    ]


async def _fetch_strategy(client: httpx.AsyncClient, url: str, strategy: str) -> dict:
    params: dict = {"url": url, "strategy": strategy}
    api_key = os.environ.get("PAGESPEED_API_KEY")
    if api_key:
        params["key"] = api_key

    last_error = ""
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = await client.get(PAGESPEED_URL, params=params, timeout=_TIMEOUT)
            if resp.status_code == 429:
                last_error = "429 Too Many Requests"
                await asyncio.sleep(_RETRY_DELAY * (attempt + 1))
                continue
            resp.raise_for_status()
            data = resp.json()
            # PSI can return 200 with an error object inside
            if "error" in data:
                return {"_error": str(data["error"].get("message", "PSI returned an error"))}
            return data
        except httpx.TimeoutException:
            return {"_timeout": True}
        except httpx.HTTPStatusError as exc:
            last_error = str(exc)
            if exc.response.status_code == 429:
                await asyncio.sleep(_RETRY_DELAY * (attempt + 1))
                continue
            return {"_error": last_error}
        except Exception as exc:
            return {"_error": str(exc)}
    return {"_error": last_error}


async def get_scores(url: str) -> DataResult:
    """Fetch mobile + desktop PageSpeed scores and return a DataResult.

    On timeout or PSI error, returns a DataResult with confidence='unavailable'
    and a manual_check_url so the user can run the check themselves.
    """
    manual_url = _psi_manual_url(url)

    async with httpx.AsyncClient() as client:
        mobile_data = await _fetch_strategy(client, url, "mobile")
        await asyncio.sleep(2)
        desktop_data = await _fetch_strategy(client, url, "desktop")

    # ── Timeout case ─────────────────────────────────────────────────────────
    if mobile_data.get("_timeout") and desktop_data.get("_timeout"):
        return DataResult(
            value={
                "mobile_score": "N/A",
                "desktop_score": "N/A",
                "mobile_label": "unknown",
                "desktop_label": "unknown",
                "lcp": "", "cls": "", "fid": "", "ttfb": "",
                "recommendations": [],
                "error": "PageSpeed API timed out",
            },
            source="pagespeed_insights",
            source_url=url,
            confidence="unavailable",
            error="PageSpeed API timed out",
            manual_check_url=manual_url,
        )

    def _parse(data: dict) -> tuple[int | None, dict, dict]:
        if "_error" in data or "_timeout" in data:
            return None, {}, {}
        lhr = data.get("lighthouseResult", {})
        categories = lhr.get("categories", {})
        audits = lhr.get("audits", {})
        raw_score = categories.get("performance", {}).get("score")
        score = round(raw_score * 100) if raw_score is not None else None
        return score, audits, categories

    mobile_score, mobile_audits, _ = _parse(mobile_data)
    desktop_score, desktop_audits, _ = _parse(desktop_data)

    audits = mobile_audits or desktop_audits

    has_error = "_error" in mobile_data or "_error" in desktop_data
    error_msg = mobile_data.get("_error") or desktop_data.get("_error") or None

    # ── PSI can't access the URL ──────────────────────────────────────────────
    if mobile_score is None and desktop_score is None and has_error:
        return DataResult(
            value={
                "mobile_score": None,
                "desktop_score": None,
                "mobile_label": "unknown",
                "desktop_label": "unknown",
                "lcp": "", "cls": "", "fid": "", "ttfb": "",
                "recommendations": [],
                "error": error_msg,
            },
            source="pagespeed_insights",
            source_url=url,
            confidence="unavailable",
            error=error_msg or "PageSpeed Insights could not access the URL",
            manual_check_url=manual_url,
        )

    result_value = {
        "mobile_score": mobile_score,
        "desktop_score": desktop_score,
        "mobile_label": _score_to_label(mobile_score / 100 if mobile_score is not None else None),
        "desktop_label": _score_to_label(desktop_score / 100 if desktop_score is not None else None),
        "lcp": _extract_audit_value(audits, "largest-contentful-paint"),
        "cls": _extract_audit_value(audits, "cumulative-layout-shift"),
        "fid": _extract_audit_value(audits, "total-blocking-time"),
        "ttfb": _extract_audit_value(audits, "server-response-time"),
        "recommendations": _top_recommendations(audits, limit=5),
        "error": error_msg,
    }

    return DataResult(
        value=result_value,
        source="pagespeed_insights",
        source_url=url,
        confidence="verified" if not error_msg else "inferred",
        error=error_msg,
        manual_check_url=manual_url,
    )
