"""Agent 4: GEO (Generative Engine Optimization) — schema audit + AI citation likelihood."""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from llm.prompts import Prompts
from scrapers.result import DataResult

if TYPE_CHECKING:
    from llm.client import GroqClient
    from scrapers.web_scraper import WebScraper
    from scrapers.search import SearchAgent as SearchAgentT

_CATEGORY_KEYWORDS = [
    "clothing", "fashion", "apparel", "menswear", "womenswear",
    "shoes", "footwear", "bags", "accessories", "jewellery", "jewelry",
    "skincare", "beauty", "cosmetics", "grooming",
    "electronics", "gadgets", "furniture", "home decor",
    "fitness", "sports", "activewear", "food", "nutrition",
]


def _infer_category(homepage: dict) -> str:
    text = (
        " ".join(homepage.get("headings", []))
        + " "
        + homepage.get("body_text", "")[:2000]
    ).lower()
    for cat in _CATEGORY_KEYWORDS:
        if cat in text:
            return cat
    return "ecommerce"


def _brand_mentioned(results: list[dict], brand_name: str) -> int:
    name_lower = brand_name.lower()
    return sum(
        1
        for r in results
        if name_lower in r.get("title", "").lower()
        or name_lower in r.get("snippet", "").lower()
    )


def _fmt_search(label: str, results: list[dict], brand_name: str, max_chars: int = 800) -> str:
    mentioned = _brand_mentioned(results, brand_name)
    lines = [f"[Brand mentioned in {mentioned}/{len(results)} results]"]
    for r in results:
        lines.append(f"- {r.get('title', '')}: {r.get('snippet', '')[:120]}")
    return f"{label}:\n" + "\n".join(lines)[:max_chars]


class GEOVisibilityAgent:
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
        out: dict = {"agent": "geo_visibility", "url": url}
        sources: list[DataResult] = []

        try:
            # 1. Scrape homepage for schema.org data (reuse prefetched if available)
            _pre = prefetched or {}
            if isinstance(_pre.get("homepage"), DataResult):
                homepage_result = _pre["homepage"]
            else:
                homepage_result = await self.scraper.scrape_page(url)
            sources.append(homepage_result)
            homepage = homepage_result.value or {}

            schemas = homepage.get("schema_json_ld", [])
            schema_types = [s.get("@type", "") for s in schemas if isinstance(s, dict)]
            schema_types = [t for t in schema_types if t]

            # 2. Category inference
            category = _infer_category(homepage)

            # 3. Wikipedia presence
            wiki_results = self.search.search(
                f"site:wikipedia.org {brand_name}", max_results=3
            )
            on_wikipedia = any("wikipedia.org" in r.get("url", "") for r in wiki_results)
            sources.append(DataResult(
                value=wiki_results,
                source="duckduckgo_search",
                confidence="inferred",
            ))

            # 4. Five AI-simulation queries
            ai_queries = [
                f"best {category} brands in India",
                f"top {category} online brands 2025",
                f"premium {category} brands to buy online",
                f"which {category} brand is best quality",
                f"{category} brand comparison India",
            ]
            ai_search_blocks: list[str] = []
            total_brand_mentions = 0
            for q in ai_queries:
                results = self.search.search(q, max_results=5)
                mentions = _brand_mentioned(results, brand_name)
                total_brand_mentions += mentions
                ai_search_blocks.append(_fmt_search(f'Query: "{q}"', results, brand_name))

            sources.append(DataResult(
                value={"queries": ai_queries, "total_mentions": total_brand_mentions},
                source="ai_simulation_search",
                confidence="inferred",
            ))

            ai_visibility_pct = round((total_brand_mentions / (len(ai_queries) * 5)) * 100)

            # 5. Build user content
            site_note = f"\nNOTE: {homepage_result.error}" if homepage_result.error else ""
            user_content = f"""BRAND: {brand_name}
URL: {url}{site_note}
INFERRED CATEGORY: {category}

SCHEMA.ORG AUDIT:
- Schema types found: {schema_types or ['None detected']}
- Raw schema data (first 2): {json.dumps(schemas[:2], indent=2)[:800]}

WIKIPEDIA PRESENCE: {'YES' if on_wikipedia else 'NOT FOUND'}
Wikipedia search results: {[r.get('url') for r in wiki_results]}

AI-SIMULATION SEARCH RESULTS:
Brand appeared in {total_brand_mentions} out of {len(ai_queries) * 5} AI-simulated results ({ai_visibility_pct}% visibility).

{chr(10).join(ai_search_blocks)}"""

            # 6. LLM call
            analysis = await self.llm.analyze_structured(
                system_prompt=Prompts.GEO_VISIBILITY,
                user_content=user_content,
                max_tokens=1400,
            )

            fallbacks = [dr.fallback_method for dr in sources if dr.fallback_used and dr.fallback_method]
            blocked = homepage_result.confidence == "unavailable"

            out["category_inferred"] = category
            out["schema_types_found"] = schema_types
            out["on_wikipedia"] = on_wikipedia
            out["ai_simulation_visibility_pct"] = ai_visibility_pct
            out["analysis"] = analysis
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["status"] = "partial" if blocked else "complete"
            out["data_coverage"] = "search_only" if blocked else "full"
            out["fallbacks_used"] = fallbacks

        except Exception as exc:
            out["error"] = str(exc)
            out["status"] = "failed"
            out["sources_used"] = [dr.to_dict() for dr in sources]
            out["data_coverage"] = "unavailable"
            out["fallbacks_used"] = []

        return out
