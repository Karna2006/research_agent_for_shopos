"""Agent 1: Brand snapshot — founding story, positioning, hero products, pricing tier."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx

from llm.prompts import Prompts
from scrapers.result import DataResult

if TYPE_CHECKING:
    from llm.client import GroqClient
    from scrapers.web_scraper import WebScraper
    from scrapers.search import SearchAgent as SearchAgentT


def _fmt_search(results: list[dict], max_chars: int = 1200) -> str:
    lines = [f"- {r.get('title', '')}: {r.get('snippet', '')}" for r in results]
    return "\n".join(lines)[:max_chars]


def _data_coverage(sources: list[DataResult]) -> str:
    if all(dr.confidence == "unavailable" for dr in sources):
        return "unavailable"
    if any(dr.confidence == "unavailable" for dr in sources if dr.source == "homepage_scrape"):
        return "search_only"
    if any(dr.fallback_used for dr in sources):
        return "partial"
    return "full"


class BrandBasicsAgent:
    def __init__(
        self,
        llm_client: "GroqClient",
        scraper: "WebScraper",
        search_agent: "SearchAgentT",
    ) -> None:
        self.llm = llm_client
        self.scraper = scraper
        self.search = search_agent

    async def run(
        self,
        url: str,
        brand_name: str,
        prefetched: dict | None = None,
    ) -> dict:
        out: dict = {"agent": "brand_basics", "url": url}
        sources: list[DataResult] = []

        try:
            # 1. Scrape homepage (reuse prefetched if available)
            _pre = prefetched or {}
            if isinstance(_pre.get("homepage"), DataResult):
                homepage_result = _pre["homepage"]
            else:
                homepage_result = await self.scraper.scrape_page(url)
            sources.append(homepage_result)
            homepage = homepage_result.value or {}
            blocked = homepage_result.confidence == "unavailable"

            # 2. DuckDuckGo searches (always run — supplements blocked scrapes)
            history = self.search.search(
                f"{brand_name} founded history founders revenue funding", max_results=5
            )
            linkedin = self.search.search(
                f"{brand_name} linkedin company about", max_results=3
            )
            sources.append(DataResult(
                value=history,
                source="duckduckgo_search",
                confidence="inferred" if history else "unavailable",
            ))

            # 3. Platform detection
            platform = await self.scraper.detect_platform(url)

            # 4. Confirm Shopify via /products.json
            if platform != "shopify":
                try:
                    async with httpx.AsyncClient(timeout=6) as client:
                        r = await client.get(
                            url.rstrip("/") + "/products.json?limit=1",
                            follow_redirects=True,
                        )
                        if r.status_code == 200 and "products" in r.text:
                            platform = "shopify"
                except Exception:
                    pass

            # 5. Confidence scoring
            signals = sum([
                not blocked,
                len(history) >= 3,
                bool(homepage.get("schema_json_ld")),
                len(linkedin) >= 1,
            ])
            source_confidence = "high" if signals >= 3 else ("medium" if signals >= 2 else "low")

            # 6. Build user content — handle value=None gracefully
            site_note = ""
            if homepage_result.error:
                site_note = f"\nNOTE: {homepage_result.error} — analysis based on search data only."

            user_content = f"""BRAND: {brand_name}
URL: {url}
PLATFORM: {platform}
HOMEPAGE ACCESSIBLE: {not blocked}{site_note}

TITLE: {homepage.get('title', 'N/A')}
META DESCRIPTION: {homepage.get('meta_description', 'N/A')}
HEADINGS: {' | '.join(homepage.get('headings', [])[:12])}

HOMEPAGE BODY TEXT (truncated):
{homepage.get('body_text', '')[:3500]}

SCHEMA.ORG DATA:
{json.dumps(homepage.get('schema_json_ld', [])[:2], indent=2)[:600]}

SEARCH — history / founders / revenue:
{_fmt_search(history)}

SEARCH — LinkedIn:
{_fmt_search(linkedin)}"""

            # 7. LLM call
            analysis = await self.llm.analyze_structured(
                system_prompt=Prompts.BRAND_BASICS,
                user_content=user_content,
                max_tokens=1200,
            )

            coverage = _data_coverage(sources)
            fallbacks = [dr.fallback_method for dr in sources if dr.fallback_used and dr.fallback_method]

            out["source_confidence"] = source_confidence
            out["platform"] = platform
            out["analysis"] = analysis
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["status"] = "partial" if blocked else "complete"
            out["data_coverage"] = coverage
            out["fallbacks_used"] = fallbacks

        except Exception as exc:
            out["error"] = str(exc)
            out["status"] = "failed"
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["data_coverage"] = "unavailable"
            out["fallbacks_used"] = []

        return out
