"""Google PageSpeed Insights API — rate-limited, cached, always returns DataResult.

Free-tier limits (with or without API key):
  25,000 queries/day per key (or per IP without key).

Rate-limiting strategy:
  One PSI call in-flight at a time (_PSI_LOCK).
  Minimum 4s gap between any two calls (_MIN_GAP_S).
  → Cap: 900 calls/hour = 21,600/day — well under quota.
  → Two calls per audit (mobile + desktop) = ~10,800 audits/day free.

Retry strategy on 429:
  Backoff: 5s → 15s → 45s (3 attempts max).

Cache:
  Results cached 10 min per (url, strategy) pair.
  Multiple test-runs on the same URL hit zero quota.
"""
from __future__ import annotations

import asyncio
import os
import time as _time
from urllib.parse import quote

import httpx

from scrapers.result import DataResult

PAGESPEED_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# ── Global rate limiter ───────────────────────────────────────────────────────
_PSI_LOCK   = asyncio.Lock()   # one call in-flight at a time, across all audits
_LAST_AT    = 0.0              # monotonic timestamp of last call dispatch
_MIN_GAP_S  = 4.0              # seconds between consecutive calls
_TIMEOUT_S  = 45               # PSI often takes 20-30s on slow sites
_MAX_RETRY  = 3                # attempts before giving up
_BACKOFF_S  = [5, 15, 45]      # wait before each retry after 429/error

# ── Result cache (per URL×strategy, 10-minute TTL) ───────────────────────────
_CACHE: dict[str, tuple[dict, float]] = {}
_CACHE_TTL  = 600              # seconds


def _cache_key(url: str, strategy: str) -> str:
    return f"{strategy}::{url}"


def _cache_get(url: str, strategy: str) -> dict | None:
    entry = _CACHE.get(_cache_key(url, strategy))
    if entry and _time.monotonic() < entry[1]:
        return entry[0]
    return None


def _cache_set(url: str, strategy: str, data: dict) -> None:
    _CACHE[_cache_key(url, strategy)] = (data, _time.monotonic() + _CACHE_TTL)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _psi_manual_url(url: str) -> str:
    return f"https://pagespeed.web.dev/report?url={quote(url, safe='')}"


def _score_to_label(score: int | None) -> str:
    if score is None:
        return "unknown"
    if score >= 90:
        return "good"
    if score >= 50:
        return "needs-improvement"
    return "poor"


def _extract_audit_value(audits: dict, key: str) -> str:
    return audits.get(key, {}).get("displayValue", "")


def _top_recommendations(audits: dict, limit: int = 5) -> list[dict]:
    candidates = []
    for audit_id, audit in audits.items():
        score = audit.get("score")
        if score is None or score >= 0.9:
            continue
        title = audit.get("title", "")
        if not title:
            continue
        candidates.append({
            "id":    audit_id,
            "title": title,
            "description": audit.get("description", "")[:200],
            "score": score,
        })
    candidates.sort(key=lambda x: x["score"])
    return [{"title": c["title"], "description": c["description"]} for c in candidates[:limit]]


# ── Core fetch — serialized, rate-limited, cached ────────────────────────────

async def _fetch_strategy(url: str, strategy: str) -> dict:
    """Fetch one PSI strategy (mobile or desktop).

    Guarantees:
    - Never raises — always returns a dict.
    - On 429: exponential backoff, max 3 retries.
    - Global lock ensures no two calls overlap.
    - Min 4s gap enforced inside the lock.
    - Results cached 10 min per URL×strategy.
    """
    global _LAST_AT

    # Cache hit — skip network entirely
    cached = _cache_get(url, strategy)
    if cached:
        print(f"    [pagespeed] {strategy} cache hit for {url[:60]}", flush=True)
        return cached

    params: dict = {"url": url, "strategy": strategy}
    api_key = os.environ.get("PAGESPEED_API_KEY")
    if api_key:
        params["key"] = api_key

    async with _PSI_LOCK:
        # Enforce minimum gap since last call (shared globally)
        now = _time.monotonic()
        wait = _LAST_AT + _MIN_GAP_S - now
        if wait > 0:
            print(f"    [pagespeed] rate-limit gap {wait:.1f}s before {strategy}…", flush=True)
            await asyncio.sleep(wait)

        _LAST_AT = _time.monotonic()

        for attempt in range(_MAX_RETRY):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        PAGESPEED_URL, params=params, timeout=_TIMEOUT_S
                    )

                if resp.status_code == 429:
                    backoff = _BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)]
                    print(
                        f"    [pagespeed] 429 rate-limited ({strategy}), "
                        f"backing off {backoff}s…",
                        flush=True,
                    )
                    await asyncio.sleep(backoff)
                    continue

                if resp.status_code != 200:
                    err = f"HTTP {resp.status_code}"
                    if attempt < _MAX_RETRY - 1:
                        await asyncio.sleep(_BACKOFF_S[attempt])
                        continue
                    return {"_error": err}

                data = resp.json()

                # PSI returns 200 with an error object when it can't access the URL
                if "error" in data:
                    msg = data["error"].get("message", "PSI error")
                    # Don't retry on "URL not accessible" — it won't change
                    if "not accessible" in msg.lower() or "invalid" in msg.lower():
                        return {"_error": msg}
                    if attempt < _MAX_RETRY - 1:
                        await asyncio.sleep(_BACKOFF_S[attempt])
                        continue
                    return {"_error": msg}

                _cache_set(url, strategy, data)
                return data

            except httpx.TimeoutException:
                print(
                    f"    [pagespeed] timeout on {strategy} (attempt {attempt + 1}/{_MAX_RETRY})",
                    flush=True,
                )
                if attempt < _MAX_RETRY - 1:
                    await asyncio.sleep(_BACKOFF_S[attempt])
                    continue
                return {"_timeout": True}

            except Exception as exc:
                if attempt < _MAX_RETRY - 1:
                    await asyncio.sleep(_BACKOFF_S[attempt])
                    continue
                return {"_error": str(exc)}

        return {"_error": "max retries exceeded"}


# ── Public API ────────────────────────────────────────────────────────────────

async def get_scores(url: str) -> DataResult:
    """Fetch mobile + desktop PageSpeed scores — always returns a DataResult.

    Never raises. On failure, confidence='unavailable' with manual_check_url.
    Mobile and desktop are fetched sequentially — the global lock + 4s gap
    prevents hammering the free-tier quota.
    """
    manual_url = _psi_manual_url(url)

    print(f"    [pagespeed] fetching mobile…", flush=True)
    mobile_data = await _fetch_strategy(url, "mobile")
    print(f"    [pagespeed] fetching desktop…", flush=True)
    desktop_data = await _fetch_strategy(url, "desktop")

    def _parse(data: dict) -> tuple[int | None, dict]:
        if "_error" in data or "_timeout" in data:
            return None, {}
        lhr        = data.get("lighthouseResult", {})
        audits     = lhr.get("audits", {})
        raw_score  = lhr.get("categories", {}).get("performance", {}).get("score")
        score      = round(raw_score * 100) if raw_score is not None else None
        return score, audits

    mobile_score,  mobile_audits  = _parse(mobile_data)
    desktop_score, desktop_audits = _parse(desktop_data)

    audits    = mobile_audits or desktop_audits
    error_msg = (
        mobile_data.get("_error") or desktop_data.get("_error")
        or ("timeout" if mobile_data.get("_timeout") or desktop_data.get("_timeout") else None)
    )
    is_timeout = mobile_data.get("_timeout") and desktop_data.get("_timeout")
    both_failed = mobile_score is None and desktop_score is None

    # Determine confidence
    if both_failed:
        confidence = "unavailable"
    elif mobile_score is None or desktop_score is None:
        confidence = "inferred"   # partial — one strategy succeeded
    else:
        confidence = "verified"

    result_value = {
        "mobile_score":   mobile_score,
        "desktop_score":  desktop_score,
        "mobile_label":   _score_to_label(mobile_score),
        "desktop_label":  _score_to_label(desktop_score),
        "lcp":  _extract_audit_value(audits, "largest-contentful-paint"),
        "cls":  _extract_audit_value(audits, "cumulative-layout-shift"),
        "fid":  _extract_audit_value(audits, "total-blocking-time"),
        "ttfb": _extract_audit_value(audits, "server-response-time"),
        "recommendations": _top_recommendations(audits, limit=5),
        "manual_check_url": manual_url,
        "error": "timeout" if is_timeout else error_msg,
    }

    if mobile_score is not None or desktop_score is not None:
        print(
            f"    [pagespeed] mobile={mobile_score} desktop={desktop_score} "
            f"lcp={result_value['lcp']} cls={result_value['cls']}",
            flush=True,
        )

    return DataResult(
        value=result_value,
        source="pagespeed_insights",
        source_url=url,
        confidence=confidence,
        error=error_msg,
        manual_check_url=manual_url,
    )
