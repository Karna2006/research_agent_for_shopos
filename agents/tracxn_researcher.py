"""Funding & investor research — no API key required.

Uses DuckDuckGo search + Inc42/Entrackr/Crunchbase article scraping to pull
funding rounds, investors, stage, and competitors for D2C Indian brands.

Falls back gracefully to empty dict on any failure.

Usage:
    from agents.tracxn_researcher import fetch_tracxn_profile
    data = await fetch_tracxn_profile("rarerabbit.in")
    # → {"funding_total": "$12M", "investors": [...], "competitors": [...], ...}
"""
from __future__ import annotations

import asyncio
import re
from typing import Any


# ── helpers ───────────────────────────────────────────────────────────────────

def _brand_from_domain(domain: str) -> str:
    """'rarerabbit.in' → 'Rare Rabbit'."""
    slug = domain.lower().split(".")[0]
    return slug.replace("-", " ").title()


def _parse_amount(text: str) -> int | None:
    """Extract a USD/INR funding amount from a text snippet → USD cents (int)."""
    text = text.replace(",", "")
    # ₹ crore
    m = re.search(r"[₹Rs\.]+\s*([\d.]+)\s*[Cc]rore", text)
    if m:
        return int(float(m.group(1)) * 1_20_000)  # ~1 Cr INR ≈ $120k
    # $ million
    m = re.search(r"\$\s*([\d.]+)\s*[Mm]illion", text)
    if m:
        return int(float(m.group(1)) * 1_000_000)
    # $ X M
    m = re.search(r"\$\s*([\d.]+)\s*M\b", text)
    if m:
        return int(float(m.group(1)) * 1_000_000)
    # plain ₹ N crore
    m = re.search(r"([\d.]+)\s*[Cc]rore", text)
    if m:
        return int(float(m.group(1)) * 120_000)
    return None


def _parse_round_type(text: str) -> str:
    for label in ["Series D", "Series C", "Series B", "Series A",
                  "Pre-Series A", "Seed", "Pre-Seed", "Angel", "Bootstrap",
                  "Bootstrapped", "IPO", "Debt"]:
        if label.lower() in text.lower():
            return label
    return ""


def _extract_investors(text: str) -> list[str]:
    """Pull investor names from a snippet using common patterns."""
    investors: list[str] = []
    # "led by X", "backed by X and Y"
    patterns = [
        r"led by ([A-Z][A-Za-z\s&,]+?)(?:\s*,|\s+and\s+|\s*\.|\s*;)",
        r"backed by ([A-Z][A-Za-z\s&,]+?)(?:\s*,|\s+and\s+|\s*\.|\s*;)",
        r"investors?\s+(?:include[sd]?\s+)?([A-Z][A-Za-z\s&,]+?)(?:\s*,|\s*\.|\s*;|\s+participated)",
        r"participation (?:from|of) ([A-Z][A-Za-z\s&,]+?)(?:\s*,|\s*\.|\s*;)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            chunk = m.group(1)
            # split on " and ", ", "
            parts = re.split(r",\s*|\s+and\s+", chunk)
            for p in parts:
                p = p.strip().strip(".")
                if 2 < len(p) < 60 and p not in investors:
                    investors.append(p)
    return investors[:8]


def _ddg_search(query: str, max_results: int = 6) -> list[dict]:
    try:
        from ddgs import DDGS
        return DDGS().text(query, max_results=max_results) or []
    except Exception:
        return []


def _ddg_news(query: str) -> list[dict]:
    try:
        from ddgs import DDGS
        return DDGS().news(query, timelimit="y", max_results=8) or []
    except Exception:
        return []


async def _fetch_page(url: str) -> str:
    """Fetch article page using Scrapling Fetcher (stealth headers, no browser).

    Crunchbase is Cloudflare-protected and cannot be scraped headlessly — skip it.
    For Inc42, Entrackr, YourStory etc. Scrapling's Fetcher handles anti-bot headers.
    Returns '' on failure.
    """
    if "crunchbase.com" in url:
        return ""  # Cloudflare Turnstile requires human interaction — not feasible headlessly
    try:
        from scrapling import Fetcher
        loop = asyncio.get_event_loop()
        def _fetch():
            page = Fetcher.get(url, stealthy_headers=True, follow_redirects=True)
            return page.html_content if page.status == 200 else ""
        return (await loop.run_in_executor(None, _fetch))[:40_000]
    except Exception:
        pass
    return ""


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _parse_crunchbase_html(html: str, brand: str) -> dict[str, Any]:
    """Extract structured data from Crunchbase page HTML.

    Crunchbase embeds a JSON blob in a <script id="ng-state"> tag.
    Falls back to regex on raw HTML if that's absent.
    """
    import json as _json

    # Try structured JSON blob first
    m = re.search(r'<script[^>]+id=["\']ng-state["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        m = re.search(r'window\.__CB_BOOTSTRAP__\s*=\s*(\{.*?\});', html, re.DOTALL)
    if m:
        try:
            blob = _json.loads(m.group(1))
            # Drill into known Crunchbase JSON shape
            entity = (
                blob.get("HttpState", {})
                    .get(f"GET/icrl/autocomplete_id_recs?name={brand.lower()}", {})
                or blob
            )
            # Pull funding rounds
            rounds = entity.get("fundingRounds", []) or []
            total_usd = sum(r.get("raised_amount_usd", 0) or 0 for r in rounds)
            latest = rounds[-1] if rounds else {}
            investors = []
            for r in rounds:
                for inv in (r.get("investor_names") or []):
                    if inv and inv not in investors:
                        investors.append(inv)
            stage = latest.get("investment_type", "") or entity.get("funding_stage", "")
            result: dict[str, Any] = {}
            if total_usd:
                result["funding_total_usd"] = total_usd
                result["funding_display"] = f"${total_usd/1e6:.1f}M" if total_usd >= 1e6 else f"${total_usd:,}"
            if stage:
                result["stage"] = stage.replace("_", " ").title()
            if investors:
                result["investors"] = investors[:8]
            if latest:
                result["latest_round"] = {
                    "type": latest.get("investment_type", ""),
                    "amount": latest.get("raised_amount_usd"),
                    "date": latest.get("announced_on", ""),
                }
            founded = entity.get("founded_on", {})
            if isinstance(founded, dict):
                result["founded"] = founded.get("value", "")[:4]
            elif isinstance(founded, str):
                result["founded"] = founded[:4]
            result["employee_count"] = entity.get("num_employees_enum", "")
            loc = entity.get("location_identifiers", [])
            if loc:
                result["hq_city"] = loc[0].get("value", "") if isinstance(loc[0], dict) else str(loc[0])
            if result:
                return result
        except Exception:
            pass

    # Fallback: regex on raw HTML text
    return _extract_funding_from_text(_strip_html(html), brand)


def _extract_funding_from_text(text: str, brand: str) -> dict[str, Any]:
    """Parse funding info from article/snippet text."""
    result: dict[str, Any] = {}
    amount = _parse_amount(text)
    if amount:
        result["funding_total_usd"] = amount
        result["funding_display"] = (
            f"${amount/1e6:.1f}M" if amount >= 1_000_000 else f"${amount:,}"
        )
    rt = _parse_round_type(text)
    if rt:
        result["stage"] = rt
    investors = _extract_investors(text)
    if investors:
        result["investors"] = investors
    return result


# ── main entry point ───────────────────────────────────────────────────────────

async def fetch_tracxn_profile(website_url: str) -> dict:
    """Fetch company funding profile via DDG + article scraping.

    Returns a structured dict with funding, investors, stage, competitors.
    Returns {} on failure — never raises.
    """
    try:
        domain = website_url.lower().strip().removeprefix("https://").removeprefix("http://").split("/")[0]
        brand = _brand_from_domain(domain)

        # ── Step 1: DDG search across Indian startup media ────────────────────
        queries = [
            f'"{brand}" funding round investors site:inc42.com OR site:entrackr.com OR site:yourstory.com',
            f'"{brand}" raises crore million Series',
            f'site:crunchbase.com/organization "{brand}"',
        ]

        all_snippets: list[str] = []
        article_urls: list[str] = []

        loop = asyncio.get_event_loop()
        raw_results: list[list[dict]] = await asyncio.gather(
            *[loop.run_in_executor(None, _ddg_search, q, 5) for q in queries],
            loop.run_in_executor(None, _ddg_news, f'"{brand}" funding'),
        )

        for batch in raw_results:
            for r in batch:
                snippet = r.get("body", r.get("excerpt", ""))
                url = r.get("href", r.get("url", ""))
                if snippet:
                    all_snippets.append(snippet)
                if url and any(d in url for d in [
                    "inc42.com", "entrackr.com", "yourstory.com",
                    "livemint.com", "economictimes.com", "businessinsider.in",
                    "crunchbase.com",
                ]):
                    article_urls.append(url)

        # ── Step 2: Scrape top article pages for deeper investor text ─────────
        article_texts: list[str] = []
        if article_urls:
            pages = await asyncio.gather(*[_fetch_page(u) for u in article_urls[:3]])
            for html in pages:
                if html:
                    article_texts.append(_strip_html(html)[:8_000])

        all_text = " ".join(all_snippets + article_texts)

        if not all_text.strip():
            return {"note": "no public funding data found", "source": "crunchbase_scrape"}

        # ── Step 3: Parse funding data ────────────────────────────────────────
        profile: dict[str, Any] = {
            "company_name": brand,
            "source": "crunchbase_scrape",
        }
        profile.update(_extract_funding_from_text(all_text, brand))

        # Founded year
        m = re.search(r"founded\s+in\s+(\d{4})|established\s+in\s+(\d{4})", all_text, re.IGNORECASE)
        if m:
            profile["founded"] = int(m.group(1) or m.group(2))

        # Employee range
        m = re.search(r"(\d[\d,]+)\s+employees?", all_text, re.IGNORECASE)
        if m:
            profile["employee_count"] = m.group(1).replace(",", "")

        # HQ
        for city in ["Mumbai", "Bangalore", "Bengaluru", "Delhi", "Gurgaon",
                     "Gurugram", "Hyderabad", "Chennai", "Pune", "Noida"]:
            if city.lower() in all_text.lower():
                profile["hq_city"] = city
                break

        # ── Step 4: Competitor search ─────────────────────────────────────────
        comp_results = await loop.run_in_executor(
            None, _ddg_search, f"{brand} competitors alternatives D2C India fashion", 5
        )
        # Blocklist: UI noise, generic words, the brand itself
        _COMP_BLOCKLIST = {
            brand.lower(), "india", "the", "and", "for", "see", "all",
            "competitors", "alternatives", "retail", "online", "view",
            "more", "top", "best", "brands", "similar", "list",
        }
        competitors = []
        seen_comp = set()
        for r in comp_results:
            snippet = r.get("body", "")
            # Only pull multi-word proper nouns (less likely to be UI text)
            names = re.findall(r"\b([A-Z][a-z]{2,}(?:\s[A-Z][a-z]{2,})+)\b", snippet)
            for n in names:
                nl = n.lower()
                if nl not in _COMP_BLOCKLIST and nl not in seen_comp and 4 < len(n) < 40:
                    competitors.append({"name": n, "website": "", "stage": ""})
                    seen_comp.add(nl)
                if len(competitors) >= 5:
                    break
            if len(competitors) >= 5:
                break
        if competitors:
            profile["competitors"] = competitors

        # Ensure required fields exist
        profile.setdefault("stage", "")
        profile.setdefault("investors", [])
        profile.setdefault("funding_total_usd", 0)
        profile.setdefault("funding_display", "undisclosed")
        profile.setdefault("latest_round", {})
        profile["tracxn_url"] = ""
        profile["license"] = "public web data"

        return profile

    except Exception as exc:
        print(f"[crunchbase_scrape] unexpected error — {exc}", flush=True)
        return {}
