"""Reddit brand intelligence via public JSON API — no credentials needed.

Uses reddit.com/search.json with curl_cffi Chrome impersonation to bypass
TLS fingerprint detection. Falls back to DDG site:reddit.com on 403/429.
Never raises — returns empty dict on failure.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

# curl_cffi handles Reddit's TLS fingerprint checks
try:
    from curl_cffi import requests as _cffi_req
    _CFFI_OK = True
except ImportError:
    import httpx as _cffi_req  # type: ignore
    _CFFI_OK = False

_BASE = "https://www.reddit.com"
_SEARCH_URL = f"{_BASE}/search.json"
_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.reddit.com/",
}
_TIMEOUT = 14.0


# ── Core fetch ────────────────────────────────────────────────────────────────

def _cffi_get(url: str, params: dict | None = None) -> "Response":
    """GET via curl_cffi (Chrome impersonation) or httpx fallback."""
    kwargs = dict(headers=_HEADERS, timeout=_TIMEOUT, params=params or {})
    if _CFFI_OK:
        kwargs["impersonate"] = "chrome124"
        return _cffi_req.get(url, **kwargs)
    else:
        import httpx
        return httpx.get(url, follow_redirects=True, **kwargs)


def _fetch_reddit_sync(brand_name: str, limit: int = 25) -> dict:
    """Search public Reddit JSON API — no auth, Chrome TLS fingerprint."""
    params = {
        "q":           f'"{brand_name}"',
        "sort":        "relevance",
        "t":           "year",
        "limit":       limit,
        "type":        "link",
        "restrict_sr": "false",
    }
    try:
        resp = _cffi_get(_SEARCH_URL, params)
        if resp.status_code in (403, 429):
            print(f"  [reddit] {resp.status_code} blocked, will DDG fallback", flush=True)
            return {"_blocked": True}
        resp.raise_for_status()
        # Guard against HTML response (bot redirect)
        ct = resp.headers.get("content-type", "")
        if "html" in ct:
            print("  [reddit] got HTML instead of JSON, will DDG fallback", flush=True)
            return {"_blocked": True}
        data = resp.json()
    except Exception as e:
        print(f"  [reddit] fetch failed: {e}", flush=True)
        return {}

    children = (data.get("data") or {}).get("children") or []
    posts: list[dict] = []
    subreddits_seen: set[str] = set()

    for child in children:
        p = child.get("data") or {}
        subreddit  = p.get("subreddit", "")
        title      = p.get("title", "")
        body       = (p.get("selftext") or "")[:400]
        score      = p.get("score", 0)
        created    = p.get("created_utc", 0)
        permalink  = p.get("permalink", "")

        subreddits_seen.add(subreddit)
        top_comments = _fetch_post_comments(permalink) if permalink else []

        created_str = (
            datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%d")
            if created else ""
        )

        posts.append({
            "title":        title,
            "body":         body,
            "url":          f"{_BASE}{permalink}" if permalink else "",
            "subreddit":    subreddit,
            "score":        score,
            "num_comments": p.get("num_comments", 0),
            "created":      created_str,
            "sentiment_hint": _quick_sentiment(title + " " + body),
            "top_comments": top_comments,
        })

    posts.sort(key=lambda p: p["score"], reverse=True)

    sentiments = [p["sentiment_hint"] for p in posts]
    pos = sentiments.count("positive")
    neg = sentiments.count("negative")
    neu = sentiments.count("neutral")
    total = len(sentiments) or 1
    overall = "positive" if pos / total > 0.5 else ("negative" if neg / total > 0.4 else "neutral")

    top_subs = sorted(
        subreddits_seen,
        key=lambda s: sum(1 for p in posts if p["subreddit"] == s),
        reverse=True,
    )[:5]

    return {
        "posts":       posts[:10],
        "total_found": len(posts),
        "subreddits":  top_subs,
        "sentiment": {
            "overall":  overall,
            "positive": pos,
            "negative": neg,
            "neutral":  neu,
        },
        "source": "reddit_json",
    }


def _fetch_post_comments(permalink: str, limit: int = 3) -> list[dict]:
    """Fetch top comments via post .json — best-effort."""
    try:
        url = f"{_BASE}{permalink}.json?limit=5&sort=top"
        resp = _cffi_get(url)
        if resp.status_code != 200:
            return []
        ct = resp.headers.get("content-type", "")
        if "html" in ct:
            return []
        data = resp.json()
        if not isinstance(data, list) or len(data) < 2:
            return []
        children = (data[1].get("data") or {}).get("children") or []
        result = []
        for c in children:
            cd = c.get("data") or {}
            body = (cd.get("body") or "").strip()
            if body and body not in ("[deleted]", "[removed]") and len(body) > 10:
                result.append({
                    "body":   body[:300],
                    "score":  cd.get("score", 0),
                    "author": cd.get("author", "[deleted]"),
                })
            if len(result) >= limit:
                break
        return result
    except Exception:
        return []


def _quick_sentiment(text: str) -> str:
    text = text.lower()
    pos_words = {"love", "great", "amazing", "best", "good", "excellent", "recommend",
                 "happy", "satisfied", "worth", "quality", "impressed", "fan"}
    neg_words = {"bad", "worst", "terrible", "avoid", "scam", "fake", "broken",
                 "disappointed", "waste", "never", "poor", "horrible", "fraud", "return"}
    pos = sum(1 for w in pos_words if w in text)
    neg = sum(1 for w in neg_words if w in text)
    if pos > neg:   return "positive"
    if neg > pos:   return "negative"
    return "neutral"


# ── DDG fallback ──────────────────────────────────────────────────────────────

def _ddg_reddit_fallback_sync(brand_name: str, search_agent) -> dict:
    try:
        results = search_agent.search(
            f"{brand_name} reviews reddit honest opinion", max_results=8
        )
        posts = [
            {
                "title":          r.get("title", ""),
                "body":           r.get("snippet", "")[:300],
                "url":            r.get("url", ""),
                "subreddit":      _extract_subreddit(r.get("url", "")),
                "score":          0,
                "num_comments":   0,
                "created":        "",
                "sentiment_hint": _quick_sentiment(r.get("title", "") + r.get("snippet", "")),
                "top_comments":   [],
            }
            for r in results
            if "reddit.com" in r.get("url", "")
        ]
        sentiments = [p["sentiment_hint"] for p in posts]
        pos = sentiments.count("positive")
        neg = sentiments.count("negative")
        total = len(sentiments) or 1
        overall = "positive" if pos / total > 0.5 else ("negative" if neg / total > 0.4 else "neutral")
        return {
            "posts":       posts,
            "total_found": len(posts),
            "subreddits":  list({p["subreddit"] for p in posts if p["subreddit"]}),
            "sentiment":   {
                "overall":  overall,
                "positive": pos,
                "negative": neg,
                "neutral":  len(sentiments) - pos - neg,
            },
            "source":      "ddg_fallback",
        }
    except Exception:
        return {"posts": [], "total_found": 0, "subreddits": [], "sentiment": {}, "source": "failed"}


def _extract_subreddit(url: str) -> str:
    m = re.search(r"reddit\.com/r/([^/]+)", url)
    return m.group(1) if m else ""


# ── Public entry point ─────────────────────────────────────────────────────────

async def get_reddit_data(brand_name: str, search_agent=None) -> dict:
    """Fetch Reddit brand intelligence. Never raises.

    Tries public Reddit JSON API via Chrome TLS impersonation (curl_cffi).
    Falls back to DDG site:reddit.com search on 403/429/html-redirect.

    Returns:
        {
          "posts": [{title, body, url, subreddit, score, num_comments, created,
                     sentiment_hint, top_comments}, ...],
          "total_found": int,
          "subreddits": [...],
          "sentiment": {"overall": str, "positive": n, "negative": n, "neutral": n},
          "source": "reddit_json" | "ddg_fallback" | "failed",
        }
    """
    try:
        result = await asyncio.to_thread(_fetch_reddit_sync, brand_name)
        if result and not result.get("_blocked"):
            return result
    except Exception as e:
        print(f"  [reddit] JSON fetch error: {e}", flush=True)

    if search_agent:
        return await asyncio.to_thread(_ddg_reddit_fallback_sync, brand_name, search_agent)

    return {"posts": [], "total_found": 0, "subreddits": [], "sentiment": {}, "source": "failed"}
