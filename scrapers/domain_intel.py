"""Domain intelligence — WHOIS age + crt.sh subdomain enumeration.

Both are free, no auth required.
- WHOIS via whois.domaintools.com HTML scrape (no API key)
- crt.sh JSON API for SSL certificate transparency logs → subdomains

Never raises — returns empty dict on failure.
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlparse

import httpx

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def _normalise_domain(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.netloc.replace("www.", "") or url.replace("www.", "")


async def _whois_age(domain: str, client: httpx.AsyncClient) -> dict:
    """Get domain creation date via free RDAP (Registration Data Access Protocol) — no auth."""
    try:
        # RDAP is the modern replacement for WHOIS — IANA-operated, JSON, free
        r = await client.get(
            f"https://rdap.org/domain/{domain}",
            headers=_HEADERS, timeout=10, follow_redirects=True,
        )
        if r.status_code == 200:
            data = r.json()
            for event in (data.get("events") or []):
                if event.get("eventAction") in ("registration", "created"):
                    raw = event.get("eventDate", "")
                    year_m = re.search(r"(\d{4})", raw)
                    if year_m:
                        year = int(year_m.group(1))
                        from datetime import date
                        age_years = round(date.today().year - year + (date.today().month / 12), 1)
                        return {"created_year": year, "age_years": age_years, "created_raw": raw[:10]}

        # Fallback: whois.iana.org for TLD info, then parse
        r2 = await client.get(
            f"https://www.whois.com/whois/{domain}",
            headers=_HEADERS, timeout=10, follow_redirects=True,
        )
        if r2.status_code == 200:
            m = re.search(
                r"(?:creation date|created on|registered on|registration date)[:\s]+(\d{4}-\d{2}-\d{2})",
                r2.text, re.I,
            )
            if m:
                raw = m.group(1)
                year = int(raw[:4])
                from datetime import date
                age_years = round(date.today().year - year + (date.today().month / 12), 1)
                return {"created_year": year, "age_years": age_years, "created_raw": raw}
        return {}
    except Exception:
        return {}


async def _crtsh_subdomains(domain: str, client: httpx.AsyncClient) -> dict:
    """Query crt.sh certificate transparency log for subdomains."""
    try:
        r = await client.get(
            "https://crt.sh/",
            params={"q": f"%.{domain}", "output": "json"},
            headers=_HEADERS, timeout=12,
        )
        if r.status_code != 200:
            return {}
        entries = r.json()
        subdomains: set[str] = set()
        for e in entries:
            name = (e.get("name_value") or "").lower()
            for sub in name.split("\n"):
                sub = sub.strip().lstrip("*.")
                if sub and sub.endswith(domain) and sub != domain:
                    prefix = sub[: -len(domain)].rstrip(".")
                    if prefix and "." not in prefix:  # only one level deep
                        subdomains.add(prefix)

        # Categorise known subdomain types
        _INFRA = {"blog", "help", "support", "docs", "careers", "jobs", "status"}
        _APP   = {"app", "my", "dashboard", "admin", "portal", "account", "store"}
        _TECH  = {"api", "cdn", "static", "assets", "media", "img", "images"}
        _STAGE = {"staging", "stage", "dev", "beta", "test", "sandbox", "preview"}

        found_infra  = [s for s in subdomains if s in _INFRA]
        found_app    = [s for s in subdomains if s in _APP]
        found_tech   = [s for s in subdomains if s in _TECH]
        found_stage  = [s for s in subdomains if s in _STAGE]
        found_other  = [s for s in subdomains if s not in _INFRA | _APP | _TECH | _STAGE]

        return {
            "total": len(subdomains),
            "infra":   found_infra,    # blog, help, docs
            "app":     found_app,      # dashboard, my.brand.com
            "tech":    found_tech,     # api, cdn
            "staging": found_stage,    # dev, staging — signals active dev
            "other":   found_other[:10],
        }
    except Exception:
        return {}


async def get_domain_intel(website_url: str) -> dict:
    """Return domain age + subdomain signals. Never raises.

    Returns:
        {
          "domain": "rarerabbit.in",
          "created_year": 2019,
          "age_years": 5.4,
          "subdomains": {
            "total": 8,
            "infra": ["blog"],
            "app": ["my", "app"],
            "staging": ["dev"],
            ...
          },
          "maturity_signal": "established" | "growing" | "new",
        }
    """
    domain = _normalise_domain(website_url)
    if not domain:
        return {"error": "invalid URL"}

    async with httpx.AsyncClient() as client:
        whois_data, crt_data = await asyncio.gather(
            _whois_age(domain, client),
            _crtsh_subdomains(domain, client),
            return_exceptions=True,
        )

    if isinstance(whois_data, Exception):
        whois_data = {}
    if isinstance(crt_data, Exception):
        crt_data = {}

    age = whois_data.get("age_years") or 0
    maturity = "established" if age >= 5 else "growing" if age >= 2 else "new"

    return {
        "domain":          domain,
        "created_year":    whois_data.get("created_year"),
        "age_years":       whois_data.get("age_years"),
        "subdomains":      crt_data,
        "maturity_signal": maturity,
    }
